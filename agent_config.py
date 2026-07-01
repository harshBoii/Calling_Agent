"""Agent config merge, prompt building, and live conversation session."""

from __future__ import annotations

import re
from typing import Any

import datetime as dt

from schemas.outbound_call import AgentConfig, ConversationStage, OutboundCallRequest

from campaign_vertical import (
    AGENT_MISSION_BY_VERTICAL,
    AGENT_OMIT_BOOKING,
    AGENT_OMIT_DISCOVERY_QUESTIONS,
    build_call_context_lines,
    normalize_campaign_vertical,
)

# ─── Keyword heuristics for stage routing ─────────────────────────────────────

_OBJECTION_KEYWORDS: dict[str, list[str]] = {
    "not_interested": [
        "not interested",
        "no thanks",
        "don't want",
        "not looking",
        "pass",
    ],
    "no_budget": ["no budget", "can't afford", "too expensive", "no money", "costly"],
    "already_have_vendor": [
        "already have",
        "current vendor",
        "using another",
        "happy with",
    ],
    "send_email": ["send email", "email me", "mail me", "send info"],
    "busy_now": ["busy", "bad time", "call later", "in a meeting", "not now"],
}

_SKIP_KEYWORDS: dict[str, list[str]] = {
    "objection_handling": [
        "not interested",
        "too expensive",
        "no budget",
        "already have",
        "don't need",
    ],
    "slot_suggestion": [
        "pricing",
        "price",
        "cost",
        "demo",
        "meeting",
        "schedule",
        "learn more",
        "tell me more",
        "how much",
    ],
    "close": ["goodbye", "bye", "thank you", "thanks", "talk later", "gotta go"],
}

_OPT_OUT_KEYWORDS = [
    "remove me",
    "stop calling",
    "do not call",
    "don't call",
    "unsubscribe",
    "take me off",
    "never call",
]

_ANGER_KEYWORDS = [
    "angry",
    "annoyed",
    "frustrated",
    "stop bothering",
    "harassment",
    "idiot",
    "shut up",
    "leave me alone",
]

_HUMAN_REQUEST_KEYWORDS = [
    "real person",
    "human agent",
    "speak to someone",
    "talk to a person",
    "transfer me",
    "representative",
    "actual person",
]

_EXIT_ACK_KEYWORDS = [
    "yes",
    "sure",
    "okay",
    "ok",
    "go ahead",
    "sounds good",
    "tell me",
    "interested",
    "haan",
    "ha",
    "theek",
    "thik",
]

_SLOT_ORDINALS = [
    ("first", 0),
    ("1st", 0),
    ("pehla", 0),
    ("pehle", 0),
    ("second", 1),
    ("2nd", 1),
    ("dusra", 1),
    ("dusre", 1),
    ("doosra", 1),
]

HANGUP_MARKER = "<<HANGUP>>"

CALL_END_INSTRUCTIONS = """## Ending the call
When you are confident the conversation should end — meeting confirmed, clear mutual goodbye, or the lead has no interest after you have already tried handling objections — give a brief polite closing line and append <<HANGUP>> at the very end of your message (after your spoken words).
Only use <<HANGUP>> when you are ready to end the call. Do not use it mid-conversation."""

_GOODBYE_PHRASES = [
    "goodbye",
    "good bye",
    "bye",
    "take care",
    "have a great day",
    "thank you for your time",
    "talk soon",
    "speak soon",
]


def _format_meet_scheduled(slot: dict) -> str:
    """Human-readable meeting time for webhook, e.g. Tuesday June 24 at 2:00 PM."""
    label = (slot.get("label") or "").strip()
    if label:
        cleaned = re.sub(r"\s+(UTC|IST|GMT.*)$", "", label, flags=re.IGNORECASE)
        cleaned = cleaned.replace(",", "")
        return cleaned.strip()
    start_at = slot.get("startAt") or slot.get("id")
    if not start_at:
        return ""
    try:
        ts = start_at.replace("Z", "+00:00")
        dt_val = dt.datetime.fromisoformat(ts)
        return dt_val.strftime("%A %B %d at %I:%M %p").lstrip("0").replace(" 0", " ")
    except ValueError:
        return str(start_at)


def normalize_meet_slots(raw: list | None) -> list[dict]:
    if not raw:
        return []
    out: list[dict] = []
    for item in raw:
        if isinstance(item, dict):
            out.append(item)
        elif hasattr(item, "model_dump"):
            out.append(item.model_dump())
    return out


_SLOT_LABEL_RE = re.compile(r"^(\w{3} \w{3} \d{1,2})\s+(.+)$")


def _parse_slot_day_time(slot: dict) -> tuple[str, str] | None:
    label = (slot.get("label") or "").strip()
    label = re.sub(r"\s+(UTC|IST|GMT.*)$", "", label, flags=re.IGNORECASE)
    label = label.replace(",", "").strip()
    match = _SLOT_LABEL_RE.match(label)
    if match:
        return match.group(1), match.group(2).strip()
    start_at = slot.get("startAt") or slot.get("id")
    if not start_at:
        return None
    try:
        ts = start_at.replace("Z", "+00:00")
        dt_val = dt.datetime.fromisoformat(ts)
        day = dt_val.strftime("%a %b %d")
        time_str = dt_val.strftime("%I:%M %p").lstrip("0").replace(" 0", " ")
        return day, time_str
    except ValueError:
        return None


def format_slots_compact(slots: list[dict]) -> str:
    """Human-readable grouped slot list for LLM prompts."""
    if not slots:
        return ""
    by_day: dict[str, list[str]] = {}
    day_order: list[str] = []
    for slot in slots:
        parsed = _parse_slot_day_time(slot)
        if not parsed:
            continue
        day, time_str = parsed
        if day not in by_day:
            by_day[day] = []
            day_order.append(day)
        if time_str not in by_day[day]:
            by_day[day].append(time_str)
    if not day_order:
        return ""
    lines = ["Available slots:"]
    for day in day_order:
        lines.append(f"- {day}: {', '.join(by_day[day])}")
    return "\n".join(lines)


def parse_agent_reply(reply: str) -> tuple[str, bool]:
    """Strip hangup marker from spoken text. Returns (spoken, should_hangup)."""
    if HANGUP_MARKER in reply:
        spoken = reply.replace(HANGUP_MARKER, "").strip()
        return spoken, True
    return reply.strip(), False


def append_call_end_instructions(prompt: str) -> str:
    if CALL_END_INSTRUCTIONS.strip() in prompt:
        return prompt
    return f"{prompt.rstrip()}\n\n{CALL_END_INSTRUCTIONS}"


def evaluate_hangup_confidence(session: "ConversationSession", spoken: str, marker_hangup: bool) -> tuple[bool, str | None]:
    """Decide if the agent should hang up after this reply."""
    if marker_hangup:
        return True, "agent_confident_end"

    low = spoken.lower()
    if session.current_stage_key == "close":
        if marker_hangup or any(p in low for p in _GOODBYE_PHRASES):
            return True, "conversation_complete"

    if session.booking_confirmed and session.current_stage_key in ("confirmation", "close"):
        if any(p in low for p in _GOODBYE_PHRASES):
            return True, "booking_confirmed"

    if session.objection_attempts >= session._max_objection_attempts():
        return True, "no_interest"

    if session.no_interest_streak >= 2 and any(
        p in low for p in _GOODBYE_PHRASES + ["understand", "no problem", "thanks"]
    ):
        return True, "no_interest"

    return False, None


def _resolve_role_framing(role: str | None, company: str) -> str:
    if not role:
        return ""
    return role.replace("{{companyName}}", company or "")


def parse_and_merge(body: dict | None) -> tuple[dict | None, dict]:
    """Validate agent_config and merge top-level fields. Returns (agent_config_dict, body)."""
    b = dict(body or {})
    raw_ac = b.get("agent_config")
    if raw_ac is None:
        return None, b

    if isinstance(raw_ac, AgentConfig):
        ac_model = raw_ac
    else:
        ac_model = AgentConfig.model_validate(raw_ac)

    ac = ac_model.model_dump()
    company = b.get("company") or ""

    if b.get("agent_name"):
        if ac.get("identity") is None:
            ac["identity"] = {}
        ac["identity"]["agentName"] = b["agent_name"]
    elif ac.get("identity", {}).get("agentName"):
        b.setdefault("agent_name", ac["identity"]["agentName"])

    if b.get("agent_role"):
        if ac.get("identity") is None:
            ac["identity"] = {}
        ac["identity"]["roleFraming"] = b["agent_role"]
    elif ac.get("identity", {}).get("roleFraming"):
        b.setdefault(
            "agent_role",
            _resolve_role_framing(ac["identity"]["roleFraming"], company),
        )

    if b.get("voiceId"):
        if ac.get("identity") is None:
            ac["identity"] = {}
        if ac["identity"].get("voice") is None:
            ac["identity"]["voice"] = {}
        ac["identity"]["voice"]["ttsVoiceId"] = b["voiceId"]
    elif ac.get("identity", {}).get("voice", {}).get("ttsVoiceId"):
        b.setdefault("voiceId", ac["identity"]["voice"]["ttsVoiceId"])

    if ac.get("identity", {}).get("roleFraming"):
        ac["identity"]["roleFraming"] = _resolve_role_framing(
            ac["identity"]["roleFraming"], company
        )

    b["agent_config"] = ac
    return ac, b


def _section(title: str, lines: list[str]) -> str:
    body = "\n".join(f"- {line}" for line in lines if line)
    return f"## {title}\n{body}\n" if body else ""


def _build_personalization_hints(cfg: dict, ac: dict) -> list[str]:
    presets = (ac.get("personalization") or {}).get("enabledPresetIds") or []
    hints: list[str] = []
    info = cfg.get("info_about_lead") or ""
    prev = cfg.get("previous_chat_context") or ""

    for preset in presets:
        if preset == "industry" and info:
            hints.append(f"Lead context may imply industry signals: {info}")
        elif preset == "pain_point" and info:
            hints.append(f"Look for pain points in: {info}")
        elif preset == "prior_touch_history" and prev:
            hints.append(f"Prior touch history: {prev}")
    return hints


def build_base_system_prompt(cfg: dict, custom_prompt: str | None = None) -> str:
    """Build system prompt from agent_config + top-level call context."""
    ac = cfg.get("agent_config")
    if not ac:
        return custom_prompt or cfg.get("system_prompt", "")

    identity = ac.get("identity") or {}
    personality = identity.get("personalityProfile") or {}
    kg = ac.get("knowledgeGrounding") or {}
    objections = ac.get("objectionHandling") or {}
    guardrails = ac.get("behavioralGuardrails") or {}
    compliance = ac.get("compliance") or {}
    booking = ac.get("bookingClose") or {}

    agent_name = cfg.get("agent_name") or identity.get("agentName") or "Agent"
    agent_role = cfg.get("agent_role") or _resolve_role_framing(
        identity.get("roleFraming"), cfg.get("company", "")
    )
    vertical = normalize_campaign_vertical(cfg.get("campaign_type"))

    parts: list[str] = []

    if custom_prompt:
        parts.append(custom_prompt.strip())
        parts.append("\n---\nThe following agent rules still apply:\n")

    parts.append(
        f"You are {agent_name}, {agent_role}, calling on behalf of {cfg.get('company')}."
    )
    parts.append(f"Respond in {cfg.get('language', 'English')} only. Sound human and natural.")

    parts.append(
        _section("Campaign objective", [AGENT_MISSION_BY_VERTICAL[vertical]])
    )

    if personality:
        parts.append(
            _section(
                "Personality",
                [
                    f"Tone: {personality.get('tone')}" if personality.get("tone") else "",
                    f"Formality: {personality.get('formality')}" if personality.get("formality") else "",
                    f"Energy: {personality.get('energy')}" if personality.get("energy") else "",
                    f"Humor: {personality.get('humorAllowance')}" if personality.get("humorAllowance") else "",
                ],
            )
        )

    parts.append(
        _section("Call context", build_call_context_lines(cfg, vertical))
    )

    kg_lines = []
    if kg.get("claimWhitelist"):
        kg_lines.append(f"Approved claims only: {', '.join(kg['claimWhitelist'])}")
    if kg.get("claimBlacklist"):
        kg_lines.append(
            f"NEVER claim or discuss: {', '.join(kg['claimBlacklist'])}"
        )
    if kg.get("unknownFactFallbackLine"):
        kg_lines.append(
            f"When you do not know an answer, use exactly: \"{kg['unknownFactFallbackLine']}\""
        )
    parts.append(_section("Knowledge grounding (strict)", kg_lines))

    obj_lines = []
    library = objections.get("objectionLibrary") or {}
    for key, guidance in library.items():
        obj_lines.append(f"{key}: {guidance}")
    if objections.get("maxObjectionAttempts"):
        obj_lines.append(
            f"Max objection handling attempts: {objections['maxObjectionAttempts']}"
        )
    if objections.get("softCloseLine"):
        obj_lines.append(f"Soft close line: \"{objections['softCloseLine']}\"")
    parts.append(_section("Objection handling", obj_lines))

    hint_lines = _build_personalization_hints(cfg, ac)
    if hint_lines:
        parts.append(
            _section(
                "Personalization hints (suggestive — use subtly, not required)",
                hint_lines,
            )
        )

    comp_lines = []
    if compliance.get("aiDisclosureLine"):
        comp_lines.append(
            f"AI disclosure (say once early if not already said): \"{compliance['aiDisclosureLine']}\""
        )
    if compliance.get("maxCallDurationSec"):
        comp_lines.append(
            f"Keep call under {compliance['maxCallDurationSec']} seconds total."
        )
    if compliance.get("optOutPhrase"):
        comp_lines.append(
            f"If lead opts out, say: \"{compliance['optOutPhrase']}\" and end the call."
        )
    parts.append(_section("Compliance (strict)", comp_lines))

    guard_lines = []
    if guardrails.get("maxSentencesPerTurn"):
        guard_lines.append(
            f"Max {guardrails['maxSentencesPerTurn']} sentences per reply."
        )
    for trig in guardrails.get("escalationTriggers") or []:
        t = trig.get("type")
        if t == "anger_detected":
            guard_lines.append(
                "If lead is angry: de-escalate once calmly; if still angry, apologize and end call."
            )
        elif t == "explicit_human_request":
            guard_lines.append(
                "If lead asks for a human: apologize, offer callback, end call politely."
            )
        elif t == "failed_objection_handles":
            thresh = trig.get("threshold") or objections.get("maxObjectionAttempts", 2)
            guard_lines.append(
                f"After {thresh} failed objection handles, use soft close and end call."
            )
    parts.append(_section("Behavioral guardrails (strict)", guard_lines))
    parts.append(CALL_END_INSTRUCTIONS)

    if booking.get("closeTrigger") and vertical not in AGENT_OMIT_BOOKING:
        parts.append(
            _section(
                "Booking trigger",
                [
                    f"When lead shows: {booking['closeTrigger']}, move to slot suggestion.",
                    f"Offer exactly {booking.get('slotsToOffer', 2)} meeting time options.",
                ],
            )
        )

    flow = ac.get("conversationFlow") or {}
    stages = flow.get("stages") or []
    if stages:
        stage_lines = [
            f"{s['key']}: {s['label']} — {s['goal']} (exit: {s['exitCondition']})"
            for s in stages
        ]
        parts.append(_section("Conversation flow stages", stage_lines))
        if flow.get("offScriptBehavior") == "redirect":
            parts.append(
                "If the lead goes off-script, briefly acknowledge then redirect to the current stage goal."
            )

    if cfg.get("questions_to_ask") and vertical not in AGENT_OMIT_DISCOVERY_QUESTIONS:
        parts.append(f"## Discovery questions\n{cfg['questions_to_ask']}")

    return "\n\n".join(p for p in parts if p.strip())


def get_greeting_stage_goal(ac: dict | None) -> str | None:
    if not ac:
        return None
    for stage in (ac.get("conversationFlow") or {}).get("stages") or []:
        if stage.get("key") == "greeting":
            return stage.get("goal")
    return None


def get_discovery_stage_goal(ac: dict | None) -> str | None:
    if not ac:
        return None
    for stage in (ac.get("conversationFlow") or {}).get("stages") or []:
        if stage.get("key") == "discovery":
            return stage.get("goal")
    return None


def get_compliance_disclosure(ac: dict | None) -> str | None:
    if not ac:
        return None
    return (ac.get("compliance") or {}).get("aiDisclosureLine")


def _text_matches_any(text: str, keywords: list[str]) -> bool:
    low = text.lower()
    return any(k in low for k in keywords)


def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p for p in parts if p.strip()]


class ConversationSession:
    """Per-call stage machine and guardrail state."""

    def __init__(self, call_cfg: dict) -> None:
        self.call_cfg = call_cfg
        ac = call_cfg.get("agent_config") or {}
        flow = ac.get("conversationFlow") or {}
        self.stages: list[dict] = flow.get("stages") or []
        self.off_script_behavior = flow.get("offScriptBehavior", "redirect")
        self.stage_index = 0
        self.stage_turn_count = 0
        self.objection_attempts = 0
        self.ai_disclosure_done = bool(call_cfg.get("_ai_disclosure_done"))
        self.anger_deescalation_used = False
        self.escalation_reason: str | None = None
        self.dnc_requested = False
        self.call_should_end = False
        self.available_slots: list[dict] = normalize_meet_slots(
            call_cfg.get("available_meet_slots")
        )
        self.offered_slots: list[str] = []
        self.offered_slot_records: list[dict] = []
        self.selected_slot_id: str | None = None
        self.meet_scheduled: str | None = None
        self.booking_confirmed = False
        self.end_call_reason: str | None = None
        self.no_interest_streak = 0

    @property
    def current_stage_key(self) -> str:
        if not self.stages:
            return "default"
        return self.stages[min(self.stage_index, len(self.stages) - 1)].get("key", "default")

    def get_current_stage(self) -> dict | None:
        if not self.stages:
            return None
        return self.stages[min(self.stage_index, len(self.stages) - 1)]

    def _slots_to_offer_count(self) -> int:
        ac = self.call_cfg.get("agent_config") or {}
        booking = ac.get("bookingClose") or {}
        return int(booking.get("slotsToOffer") or 2)

    def ensure_offered_slots(self) -> None:
        """Populate offered slots from request payload or keep existing."""
        if self.offered_slots:
            return
        count = self._slots_to_offer_count()
        if self.available_slots:
            picked = self.available_slots[:count]
            self.offered_slot_records = picked
            self.offered_slots = [
                (s.get("label") or _format_meet_scheduled(s)).strip() for s in picked
            ]
            return
        self.offered_slot_records = []

    def set_offered_slots_from_labels(self, labels: list[str]) -> None:
        self.offered_slots = labels
        self.offered_slot_records = []

    def detect_slot_selection(self, user_text: str) -> str | None:
        """Match user reply to an offered slot; sets meet_scheduled when found."""
        if not self.offered_slot_records and not self.offered_slots:
            return None

        low = user_text.lower()

        for ordinal, idx in _SLOT_ORDINALS:
            if ordinal in low and idx < len(self.offered_slot_records):
                slot = self.offered_slot_records[idx]
                self.selected_slot_id = slot.get("id")
                self.meet_scheduled = _format_meet_scheduled(slot)
                self.booking_confirmed = True
                return self.meet_scheduled

        for slot in self.offered_slot_records:
            label = (slot.get("label") or "").lower()
            if label and label in low:
                self.selected_slot_id = slot.get("id")
                self.meet_scheduled = _format_meet_scheduled(slot)
                self.booking_confirmed = True
                return self.meet_scheduled
            # Partial time match from label, e.g. "2:00 pm"
            for token in re.findall(r"\d{1,2}:\d{2}\s*(?:am|pm)?", label):
                if token.strip() in low:
                    self.selected_slot_id = slot.get("id")
                    self.meet_scheduled = _format_meet_scheduled(slot)
                    self.booking_confirmed = True
                    return self.meet_scheduled

        if self.offered_slots and _text_matches_any(user_text, _EXIT_ACK_KEYWORDS):
            # Generic yes on slot stage — associate first slot if only one offered.
            if len(self.offered_slot_records) == 1:
                slot = self.offered_slot_records[0]
                self.selected_slot_id = slot.get("id")
                self.meet_scheduled = _format_meet_scheduled(slot)
                self.booking_confirmed = True
                return self.meet_scheduled

        return None

    def maybe_confirm_booking(self, user_text: str) -> None:
        if self.current_stage_key != "confirmation":
            return
        if self.meet_scheduled and _text_matches_any(user_text, _EXIT_ACK_KEYWORDS):
            self.booking_confirmed = True

    def _max_sentences(self) -> int:
        ac = self.call_cfg.get("agent_config") or {}
        return (ac.get("behavioralGuardrails") or {}).get("maxSentencesPerTurn", 2)

    def _max_objection_attempts(self) -> int:
        ac = self.call_cfg.get("agent_config") or {}
        return (ac.get("objectionHandling") or {}).get("maxObjectionAttempts", 2)

    def _soft_close_line(self) -> str:
        ac = self.call_cfg.get("agent_config") or {}
        return (ac.get("objectionHandling") or {}).get("softCloseLine") or (
            "Totally understand — can I send a short overview instead?"
        )

    def _opt_out_phrase(self) -> str:
        ac = self.call_cfg.get("agent_config") or {}
        return (ac.get("compliance") or {}).get("optOutPhrase") or (
            "Understood — I'll remove you from our calling list."
        )

    def _max_call_duration_sec(self) -> int | None:
        ac = self.call_cfg.get("agent_config") or {}
        return (ac.get("compliance") or {}).get("maxCallDurationSec")

    def build_turn_prompt(self, base_prompt: str) -> str:
        stage = self.get_current_stage()
        if not stage:
            return base_prompt

        remaining = max(0, stage.get("maxTurns", 2) - self.stage_turn_count)
        slot_hint = ""
        if self.current_stage_key == "slot_suggestion":
            count = self._slots_to_offer_count()
            if self.available_slots:
                compact = format_slots_compact(self.available_slots)
                if compact:
                    slot_hint = (
                        f"\n{compact}\n"
                        f"Offer exactly {count} options from the list above."
                    )
            elif self.offered_slots:
                details = []
                for i, slot in enumerate(self.offered_slot_records or [], start=1):
                    lbl = slot.get("label") or self.offered_slots[i - 1]
                    details.append(f"{i}. {lbl}")
                if details:
                    slot_hint = (
                        f"\nOffer these slots (exactly {len(details)}): "
                        + "; ".join(details)
                    )
                else:
                    slot_hint = f"\nOffer these slots: {', '.join(self.offered_slots)}"

        stage_block = f"""## Active stage: {stage.get('label')} ({stage.get('key')})
Goal: {stage.get('goal')}
Exit when: {stage.get('exitCondition')}
Turns used: {self.stage_turn_count}/{stage.get('maxTurns', 2)} (remaining: {remaining})
Off-script behavior: {self.off_script_behavior}{slot_hint}
Focus ONLY on this stage goal for your next reply."""

        return f"{base_prompt}\n\n{stage_block}"

    def detect_objection(self, user_text: str) -> str | None:
        low = user_text.lower()
        for key, keywords in _OBJECTION_KEYWORDS.items():
            if any(k in low for k in keywords):
                return key
        return None

    def check_compliance_on_user_input(self, user_text: str) -> str | None:
        if _text_matches_any(user_text, _OPT_OUT_KEYWORDS):
            self.dnc_requested = True
            self.call_should_end = True
            ac = self.call_cfg.get("agent_config") or {}
            if (ac.get("compliance") or {}).get("optOutSetsDncFlag", True):
                self.call_cfg["_dnc_flag"] = True
            return self._opt_out_phrase()
        return None

    def check_escalation(self, user_text: str) -> str | None:
        if _text_matches_any(user_text, _HUMAN_REQUEST_KEYWORDS):
            self.escalation_reason = "explicit_human_request"
            self.call_should_end = True
            return (
                "I understand — I'll have someone from our team reach out to you directly. "
                "Thank you for your time. Goodbye."
            )

        if _text_matches_any(user_text, _ANGER_KEYWORDS):
            if self.anger_deescalation_used:
                self.escalation_reason = "anger_detected"
                self.call_should_end = True
                return (
                    "I'm really sorry for the frustration. I'll let you go now. "
                    "Thank you, and have a good day."
                )
            self.anger_deescalation_used = True
            return (
                "I'm sorry — I didn't mean to frustrate you. "
                "Would it be okay if I ask just one quick question, or should I call back later?"
            )

        return None

    def _jump_to_stage(self, key: str) -> bool:
        for i, stage in enumerate(self.stages):
            if stage.get("key") == key:
                old = self.current_stage_key
                self.stage_index = i
                self.stage_turn_count = 0
                print(f"[STAGE] {old} → {key} (skip)", flush=True)
                return True
        return False

    def _advance_sequential(self) -> None:
        if self.stage_index < len(self.stages) - 1:
            old = self.current_stage_key
            self.stage_index += 1
            self.stage_turn_count = 0
            print(f"[STAGE] {old} → {self.current_stage_key}", flush=True)

    def advance_stage(self, user_text: str) -> None:
        if not self.stages:
            return

        stage = self.get_current_stage()
        if not stage:
            return

        for target in stage.get("skipToTargets") or []:
            keywords = _SKIP_KEYWORDS.get(target, [])
            if keywords and _text_matches_any(user_text, keywords):
                if self._jump_to_stage(target):
                    return

        booking = (self.call_cfg.get("agent_config") or {}).get("bookingClose") or {}
        close_trigger = (booking.get("closeTrigger") or "").lower()
        if close_trigger and any(
            word in user_text.lower() for word in close_trigger.split() if len(word) > 3
        ):
            if self._jump_to_stage("slot_suggestion"):
                return

        if self.stage_turn_count >= stage.get("maxTurns", 2):
            self._advance_sequential()
            return

        if _text_matches_any(user_text, _EXIT_ACK_KEYWORDS):
            self._advance_sequential()

    def handle_objection(self, user_text: str) -> str | None:
        key = self.detect_objection(user_text)
        if not key:
            return None
        self.objection_attempts += 1
        if key == "not_interested":
            self.no_interest_streak += 1
        else:
            self.no_interest_streak = 0
        library = (
            (self.call_cfg.get("agent_config") or {}).get("objectionHandling") or {}
        ).get("objectionLibrary") or {}
        return library.get(key)

    def enforce_reply(self, reply: str) -> str:
        max_sent = self._max_sentences()
        sentences = _split_sentences(reply)
        if len(sentences) > max_sent:
            reply = " ".join(sentences[:max_sent])

        blacklist = (
            (self.call_cfg.get("agent_config") or {}).get("knowledgeGrounding") or {}
        ).get("claimBlacklist") or []
        low = reply.lower()
        for term in blacklist:
            if term.lower() in low:
                fallback = (
                    (self.call_cfg.get("agent_config") or {})
                    .get("knowledgeGrounding", {})
                    .get("unknownFactFallbackLine")
                )
                if fallback:
                    return fallback
        return reply.strip()

    def sync_to_cfg(self) -> None:
        self.call_cfg["_conversation_state"] = self.to_metadata()

    def to_metadata(self) -> dict[str, Any]:
        ac = self.call_cfg.get("agent_config") or {}
        booking = ac.get("bookingClose") or {}
        return {
            "conversationStage": self.current_stage_key,
            "escalationReason": self.escalation_reason,
            "dncRequested": self.dnc_requested,
            "calendarSourceId": booking.get("calendarSourceId"),
            "offeredSlots": self.offered_slots,
            "selectedSlotId": self.selected_slot_id,
            "meetScheduled": self.meet_scheduled,
            "objectionAttempts": self.objection_attempts,
            "bookingConfirmed": self.booking_confirmed,
            "endCallReason": self.end_call_reason,
        }


def validate_outbound_body(body: dict) -> OutboundCallRequest:
    return OutboundCallRequest.model_validate(body)
