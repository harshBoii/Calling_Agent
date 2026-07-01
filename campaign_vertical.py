"""Campaign vertical normalization and live-agent prompt fragments."""

from __future__ import annotations

from typing import Literal

CampaignType = Literal[
    "Sales",
    "Collection",
    "Appointment reminders",
    "Feedback / NPS",
    "Renewal / win-back",
]

VERTICALS = ("SALES", "COLLECTIONS", "REMINDER", "SURVEY", "RENEWAL")

_CAMPAIGN_TYPE_TO_VERTICAL: dict[str, str] = {
    "sales": "SALES",
    "collection": "COLLECTIONS",
    "collections": "COLLECTIONS",
    "appointment reminders": "REMINDER",
    "appointment_reminders": "REMINDER",
    "reminder": "REMINDER",
    "feedback / nps": "SURVEY",
    "feedback/nps": "SURVEY",
    "feedback nps": "SURVEY",
    "survey": "SURVEY",
    "nps": "SURVEY",
    "renewal / win-back": "RENEWAL",
    "renewal/win-back": "RENEWAL",
    "renewal win-back": "RENEWAL",
    "renewal": "RENEWAL",
    "win-back": "RENEWAL",
}


def normalize_campaign_vertical(raw: str | None) -> str:
    """Map API campaign_type to internal vertical key. Defaults to SALES."""
    if not raw or not str(raw).strip():
        return "SALES"
    key = str(raw).strip().lower().replace("_", " ")
    return _CAMPAIGN_TYPE_TO_VERTICAL.get(key, "SALES")


AGENT_MISSION_BY_VERTICAL: dict[str, str] = {
    "SALES": (
        "Discovery call: learn the lead's situation, present the offer from context, "
        "handle objections briefly, and aim to book a meeting or agreed follow-up."
    ),
    "COLLECTIONS": (
        "Collections call: confirm identity by name; state the outstanding matter "
        "(e.g. Am I speaking with [name]?); reference account context professionally; "
        "professionally; work toward payment via link or a clear promise-to-pay. "
        "Be empathetic and compliant. Do not upsell or pitch unrelated products."
    ),
    "REMINDER": (
        "Appointment reminder call: confirm the upcoming appointment, or help reschedule "
        "or cancel if needed. Do not pitch products or run a sales discovery."
    ),
    "SURVEY": (
        "Short feedback / NPS call: politely ask for a 0-10 rating and one brief follow-up "
        "question about their experience. No sales pitch. Respect if they decline to answer."
    ),
    "RENEWAL": (
        "Renewal / win-back call: discuss renewing their plan, present the renewal offer "
        "from context, handle discount or downgrade questions, and confirm renew or decline."
    ),
}

# Primary close outcome per vertical (what success looks like)
VERTICAL_CLOSE_GOAL: dict[str, str] = {
    "SALES": "MEET BOOKING",
    "COLLECTIONS": "payment link",
    "REMINDER": "booked_meet_link",
    "SURVEY": "A Doc Of our Survey Conversation",
    "RENEWAL": "payment link",
}

COLLECTIONS_HANGUP_ATTEMPT_LIMIT = 3
COLLECTIONS_HANGUP_HOLD_LINE = (
    "I will need to cut this call soon, but we must clarify a few things first."
)

# Only SALES may offer calendar meeting slots
VERTICALS_WITH_MEETING_SLOTS: frozenset[str] = frozenset({"SALES"})

# Sections to skip in build_base_system_prompt (see agent_config.py)
AGENT_OMIT_BOOKING: frozenset[str] = frozenset(
    {"COLLECTIONS", "REMINDER", "SURVEY", "RENEWAL"}
)
AGENT_OMIT_DISCOVERY_QUESTIONS: frozenset[str] = frozenset({"SURVEY"})
AGENT_COLLECTIONS_CONTEXT: frozenset[str] = frozenset({"COLLECTIONS"})


def get_vertical_close_goal(vertical: str | None) -> str:
    """Return the primary close goal label for a campaign vertical."""
    key = normalize_campaign_vertical(vertical)
    return VERTICAL_CLOSE_GOAL.get(key, VERTICAL_CLOSE_GOAL["SALES"])


def vertical_allows_meeting_slots(vertical: str | None) -> bool:
    """Only Sales campaigns may offer calendar meeting slots."""
    return normalize_campaign_vertical(vertical) in VERTICALS_WITH_MEETING_SLOTS


def vertical_uses_sales_objection_hangup(vertical: str | None) -> bool:
    """Only Sales auto-hangs up after max objection attempts."""
    return normalize_campaign_vertical(vertical) == "SALES"


# Collections: ignore soft-timing objections — stay on payment clarification
COLLECTIONS_SKIP_OBJECTION_KEYS: frozenset[str] = frozenset(
    {"busy_now", "send_email"}
)


def collections_payment_guardrail_lines(cfg: dict) -> list[str]:
    """Strict collections payment rules for the live agent system prompt."""
    ac = cfg.get("agent_config") or {}
    kg = ac.get("knowledgeGrounding") or {}
    whitelist = kg.get("claimWhitelist") or []
    lines = [
        "NEVER negotiate, discount, waive, or reduce the outstanding balance unless "
        "call context or approved claims explicitly authorize a specific amount or settlement.",
        "The ONLY concession you may offer without explicit authorization: suggest a "
        "partial payment for now to help avoid further escalation from the company. "
        "Whenever you offer or accept a partial payment, you MUST explicitly state that "
        "the remaining balance is still due and full payment will be required later. "
        "Do not offer multiple concessions or payment plans on your own.",
        "If the contact hesitates, refuses, or mentions defaulting: calmly inform that "
        "continued non-payment may lead to legal action and can negatively affect their "
        "CIBIL score. Stay professional — inform, do not threaten or harass.",
        "Prefer full payment via the payment link. If they cannot pay in full, secure a "
        "partial promise-to-pay for now with a specific amount and date — and clearly "
        "confirm the rest of the outstanding balance must be paid later.",
    ]
    if whitelist:
        lines.append(
            f"Approved claims you may use: {', '.join(whitelist)}"
        )
    auth_notes = (cfg.get("perks_of_product") or cfg.get("info_about_lead") or "").strip()
    if auth_notes:
        lines.append(f"Authorized payment/settlement context: {auth_notes}")
    return lines


def build_collections_payment_guardrails_section(cfg: dict) -> str:
    """Markdown section for collections payment guardrails."""
    lines = collections_payment_guardrail_lines(cfg)
    body = "\n".join(f"- {line}" for line in lines)
    return f"## Collections payment rules (strict)\n{body}\n"


def build_call_end_instructions(vertical: str | None) -> str:
    """Vertical-specific call ending rules for the agent system prompt."""
    key = normalize_campaign_vertical(vertical)
    goal = get_vertical_close_goal(key)

    if key == "COLLECTIONS":
        limit = COLLECTIONS_HANGUP_ATTEMPT_LIMIT
        return f"""## Ending the call
Primary close goal: {goal} — confirm or send payment link. Never offer meeting slots.
Before ending, clarify outstanding balance, promise-to-pay (amount + date), or the next step.
You may attempt to close the call at most {limit} times. On attempts 1–{limit - 1}, be strict: say you will cut the call soon but must clarify a few things first — do NOT append {HANGUP_MARKER}.
Only on attempt {limit}, after blockers are addressed, give a brief polite closing line and append {HANGUP_MARKER} at the very end.
Never use {HANGUP_MARKER} while payment details are still unclear."""

    if key == "SALES":
        return f"""## Ending the call
Primary close goal: {goal}.
When meeting is confirmed, clear mutual goodbye, or no interest after objection handling — give a brief polite closing line and append {HANGUP_MARKER} at the very end (after your spoken words).
Only use {HANGUP_MARKER} when you are ready to end the call. Do not use it mid-conversation."""

    return f"""## Ending the call
Primary close goal: {goal}.
Do NOT offer calendar meeting slots or schedule sales demos.
When the call objective is met or the contact clearly declines — give a brief polite closing line and append {HANGUP_MARKER} at the very end.
Only use {HANGUP_MARKER} when you are ready to end the call."""


# Marker used in build_call_end_instructions (imported by agent_config at runtime)
HANGUP_MARKER = "<<HANGUP>>"


def build_close_goal_section(vertical: str | None) -> str:
    """Prompt section describing what a successful close looks like."""
    key = normalize_campaign_vertical(vertical)
    goal = get_vertical_close_goal(key)
    lines = [f"Success = {goal}"]
    if not vertical_allows_meeting_slots(key):
        lines.append("Do NOT offer meeting slots or calendar booking times.")
    return "\n".join(f"- {line}" for line in lines)


def build_call_context_lines(cfg: dict, vertical: str) -> list[str]:
    """Call-context bullets for the agent system prompt."""
    name = cfg.get("name") or ""
    perks = cfg.get("perks_of_product") or ""
    info = cfg.get("info_about_lead") or ""
    if vertical in AGENT_COLLECTIONS_CONTEXT:
        lines = [f"Contact name: {name}"]
        if info:
            lines.append(f"Account context: {info}")
        if perks:
            lines.append(f"Balance / payment note: {perks}")
        return lines
    lines = [f"Lead name: {name}"]
    if perks:
        lines.append(f"Offer: {perks}")
    if info:
        lines.append(f"Lead intel (use subtly): {info}")
    return lines


def vertical_legacy_goal(vertical: str, name: str, perks: str) -> str:
    """Goal paragraph for legacy SYSTEM_PROMPT_TEMPLATE path."""
    if vertical == "SALES":
        return (
            f"Sell to {name}. The offer: {perks}. "
            "Close with confirmed interest or a scheduled follow-up."
        )
    mission = AGENT_MISSION_BY_VERTICAL.get(vertical, AGENT_MISSION_BY_VERTICAL["SALES"])
    return f"Contact: {name}. {mission}"


def vertical_legacy_flow(vertical: str, perks: str) -> str:
    """Conversation flow line for legacy template."""
    if vertical == "SALES":
        return (
            "1. Ask permission to ask questions → bridge to their situation → "
            f"present {perks} as the solution"
        )
    if vertical == "REMINDER":
        return "1. Greet → confirm appointment → offer reschedule or cancel only if needed"
    if vertical == "SURVEY":
        return "1. Greet → ask 0-10 rating → one brief follow-up → thank and close"
    if vertical == "COLLECTIONS":
        return (
            "1. Verify identity → state purpose → request full payment via link → "
            "if unable, offer partial payment for now only (state full balance still due later) → "
            "note legal/CIBIL impact if defaulting → secure promise-to-pay"
        )
    if vertical == "RENEWAL":
        return f"1. Greet → discuss renewal → present offer ({perks}) → confirm decision"
    return AGENT_MISSION_BY_VERTICAL.get(vertical, "")


def greeting_instructions(vertical: str, cfg: dict) -> str:
    """Extra bullet lines for generate_opening_greeting."""
    company = cfg.get("company", "")
    perks = cfg.get("perks_of_product", "")
    if vertical == "REMINDER":
        return (
            "- State you are calling from {company} to remind them about an upcoming appointment\n"
            f"- Reference timing/details subtly from context: {perks or cfg.get('info_about_lead', '')}\n"
            "- Ask if the time still works"
        ).format(company=company)
    if vertical == "SURVEY":
        return (
            f"- Introduce yourself from {company} for a very brief feedback call (under 1 minute)\n"
            "- Mention it is a short survey, not a sales call\n"
            "- Ask if now is okay for 2 quick questions"
        )
    if vertical == "COLLECTIONS":
        lead_name = (cfg.get("name") or "").strip()
        identity_line = (
            f'- End with a direct identity check using their name: "Am I speaking with {lead_name}?"'
            if lead_name
            else "- Ask if you are speaking with the account holder (use their name if known from context)"
        )
        return (
            f"- Introduce yourself professionally from {company}\n"
            "- State this is regarding an account matter (do not threaten)\n"
            f"{identity_line}\n"
            "- Do NOT say \"the right person\" — use the lead's name in the question"
        )
    if vertical == "RENEWAL":
        return (
            f"- Introduce yourself from {company} about their renewal\n"
            f"- Mention the renewal offer briefly: {perks}\n"
            "- Ask if now is a good time"
        )
    return (
        f"- Tease the offer: {perks}\n"
        "- End with a soft permission question (\"Is this a good time?\")"
    )


def questions_generation_task(vertical: str, language: str) -> str:
    """Task description for generate_questions_to_ask."""
    if vertical == "SURVEY":
        return (
            f"Produce 2 short questions for an NPS/feedback call in {language}:\n"
            "- One asking for a 0-10 rating\n"
            "- One brief open follow-up about their experience\n"
            "Each question <= 12 words. No sales questions."
        )
    if vertical == "REMINDER":
        return (
            f"Produce 2 short questions for an appointment reminder call in {language}:\n"
            "- Confirm they will attend\n"
            "- Ask if they need to reschedule\n"
            "Each question <= 12 words."
        )
    if vertical == "COLLECTIONS":
        return (
            f"Produce 2 short questions for a collections call in {language}:\n"
            "- Verify identity or willingness to discuss the account\n"
            "- Ask about ability to pay or preferred callback time\n"
            "Each question <= 12 words. Stay professional and compliant."
        )
    if vertical == "RENEWAL":
        return (
            f"Produce 2 short questions for a renewal call in {language}:\n"
            "- Gauge interest in renewing\n"
            "- Ask about any concerns on price or plan\n"
            "Each question <= 12 words."
        )
    return (
        f"Produce 2 to 4 short, natural discovery questions for a 2-minute outbound "
        f"sales call in {language}. Each question <= 12 words."
    )
