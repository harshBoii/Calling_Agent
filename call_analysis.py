"""Post-call transcript analysis prompts and parsing by campaign vertical."""

from __future__ import annotations

import json
import re

from campaign_vertical import VERTICALS, normalize_campaign_vertical

SALES_OUTCOME_RULES = """
Classify this call transcript into exactly ONE of the following outcomes.
Use the FIRST rule that matches, in this priority order:

1. WRONG_NUMBER
   - Person confirms they are not the intended lead (different person, wrong
     company, "no one here by that name," reassigned number).
   - Only use if explicitly confirmed — do not infer from silence or confusion.

2. MEET_REQUESTED
   - Prospect agrees to a specific next step with a time/date component:
     demo, meeting, callback at a SPECIFIC time ("call me tomorrow at 3"),
     or agrees to a calendar link / confirms a booking.
   - Must be an explicit commitment, not just "maybe send info."

3. CALLBACK
   - Prospect asks to be called back but WITHOUT a specific time commitment
     ("call me later," "I'm busy right now," "try me next week" without a date).
   - Also use if they ask to speak to someone else at the company later.

4. INTERESTED
   - Prospect engages positively: asks about pricing/features, asks clarifying
     questions about the product, says "sounds interesting," "tell me more,"
     or agrees to receive follow-up info (email/WhatsApp) without a firm
     meeting commitment.
   - Distinguish from CALLBACK: INTERESTED = engaged in THIS call.
     CALLBACK = wants to engage LATER instead of now.

5. NOT_INTERESTED
   - Prospect explicitly declines: "not interested," "we don't need this,"
     "already have a solution," "no thanks," asks not to be called again,
     or expresses hostility about the call — even if phrased aggressively.
   - Do NOT use a separate do-not-call outcome; use NOT_INTERESTED.

6. VOICEMAIL
   - Call reached voicemail/answering machine (with or without message left).
   - Detected via call metadata (Telnyx answering_machine_detection) or
     transcript pattern (automated greeting, beep, no live turn-taking).

7. NO_ANSWER
   - Call rang out / not picked up. No transcript exists, or transcript is
     empty/near-empty with no human turn.

8. UNKNOWN
   - Call connected but transcript is unintelligible, disconnected mid-call
     with no signal either way, wrong language the agent can't parse,
     or genuinely ambiguous (e.g., prospect hung up immediately with no words).
   - Use sparingly — this should be a small % of calls, not a dumping ground
     for effort-avoidance. Only use when no other rule reasonably applies.

IMPORTANT:
- Never set outcome to DO_NOT_CALL — that value is not allowed.
- Base classification ONLY on what was explicitly said or clearly evidenced
  in the transcript/metadata. Do not infer tone or sentiment beyond what's stated.
- If multiple signals conflict (e.g., prospect says "not interested" then later
  asks "actually, what's the price?"), use the LATEST signal in the conversation,
  since that reflects their final position.
- Set the "outcome" field to exactly ONE enum value from the list above.
"""

OUTCOME_RULES_BY_VERTICAL: dict[str, str] = {
    "SALES": SALES_OUTCOME_RULES,
    "COLLECTIONS": """
Classify this call transcript into exactly ONE of the following outcomes.
Use the FIRST rule that matches, in this priority order:

1. WRONG_NUMBER
   - Person confirms they are not the intended account holder.
2. DISPUTED
   - Prospect denies owing the debt, disputes the amount, or cites legal
     representation ("talk to my lawyer").
3. PAID_IN_FULL
   - Confirmed full payment action taken during or resulting from this call.
4. PARTIAL_PAYMENT_MADE
   - Confirmed partial payment action taken during or resulting from this call.
5. PROMISE_TO_PAY
   - Commits to a SPECIFIC amount AND date. Vague "I'll sort it out" does
     not qualify — use CALLBACK_REQUESTED instead.
6. HARDSHIP_CLAIMED
   - Cites inability to pay (job loss, medical, etc.).
7. CALLBACK_REQUESTED
   - Wants to discuss further, no firm commitment yet, or asks to stop
     contact / be called later instead of resolving now.
8. REFUSED_TO_PAY
   - Acknowledges debt, explicitly declines, no hardship/dispute raised.
9. VOICEMAIL
10. NO_ANSWER
11. UNKNOWN

IMPORTANT: Never set outcome to DO_NOT_CALL — that value is not allowed.
Only classify PROMISE_TO_PAY if both amount and date are explicit.
Use latest signal if conflicting. Set "outcome" to exactly ONE enum value.
""",
    "REMINDER": """
Classify this call transcript into exactly ONE of the following outcomes.
Use the FIRST rule that matches, in this priority order:

1. WRONG_NUMBER
2. CONFIRMED — prospect confirms they will attend/keep the appointment.
3. RESCHEDULE_REQUESTED — asks to change date/time.
4. CANCELLED — explicitly cancels or asks not to be contacted again.
5. VOICEMAIL
6. NO_ANSWER
7. UNKNOWN

Never set outcome to DO_NOT_CALL — that value is not allowed.
Use the latest signal if the person changes their mind mid-call.
Set "outcome" to exactly ONE enum value.
""",
    "SURVEY": """
Classify this call transcript into exactly ONE of the following outcomes.
Use the FIRST rule that matches, in this priority order:

1. WRONG_NUMBER
2. RESPONDED_POSITIVE — clearly favorable feedback/rating.
3. RESPONDED_NEUTRAL — mixed or lukewarm feedback.
4. RESPONDED_NEGATIVE — clearly unfavorable feedback/rating.
5. DECLINED_SURVEY — refuses to answer, asks to stop contact, or ends early.
6. VOICEMAIL
7. NO_ANSWER
8. UNKNOWN

Never set outcome to DO_NOT_CALL — that value is not allowed.
Set "outcome" to exactly ONE enum value.
""",
    "RENEWAL": """
Classify this call transcript into exactly ONE of the following outcomes.
Use the FIRST rule that matches, in this priority order:

1. WRONG_NUMBER
2. RENEWED — confirms renewal during the call.
3. DECLINED_RENEWAL — explicitly declines to renew or asks not to be contacted.
4. DOWNGRADE_REQUESTED — wants to renew at a lower tier.
5. NEEDS_DISCOUNT_APPROVAL — will renew but is negotiating price/terms.
6. CALLBACK — wants to discuss later, no decision made.
7. VOICEMAIL
8. NO_ANSWER
9. UNKNOWN

Never set outcome to DO_NOT_CALL — that value is not allowed.
Set "outcome" to exactly ONE enum value.
""",
}

OUTCOME_VALUES_BY_VERTICAL: dict[str, list[str]] = {
    "SALES": [
        "WRONG_NUMBER", "MEET_REQUESTED", "CALLBACK", "INTERESTED",
        "NOT_INTERESTED", "VOICEMAIL", "NO_ANSWER", "UNKNOWN",
    ],
    "COLLECTIONS": [
        "WRONG_NUMBER", "DISPUTED", "PAID_IN_FULL",
        "PARTIAL_PAYMENT_MADE", "PROMISE_TO_PAY", "HARDSHIP_CLAIMED",
        "CALLBACK_REQUESTED", "REFUSED_TO_PAY", "VOICEMAIL", "NO_ANSWER", "UNKNOWN",
    ],
    "REMINDER": [
        "WRONG_NUMBER", "CONFIRMED", "RESCHEDULE_REQUESTED",
        "CANCELLED", "VOICEMAIL", "NO_ANSWER", "UNKNOWN",
    ],
    "SURVEY": [
        "WRONG_NUMBER", "RESPONDED_POSITIVE", "RESPONDED_NEUTRAL",
        "RESPONDED_NEGATIVE", "DECLINED_SURVEY", "VOICEMAIL", "NO_ANSWER", "UNKNOWN",
    ],
    "RENEWAL": [
        "WRONG_NUMBER", "RENEWED", "DECLINED_RENEWAL",
        "DOWNGRADE_REQUESTED", "NEEDS_DISCOUNT_APPROVAL", "CALLBACK",
        "VOICEMAIL", "NO_ANSWER", "UNKNOWN",
    ],
}

# If the model still returns DO_NOT_CALL, remap to the closest allowed outcome.
_DNC_OUTCOME_FALLBACK: dict[str, str] = {
    "SALES": "NOT_INTERESTED",
    "COLLECTIONS": "CALLBACK_REQUESTED",
    "REMINDER": "CANCELLED",
    "SURVEY": "DECLINED_SURVEY",
    "RENEWAL": "DECLINED_RENEWAL",
}

_ANALYST_LABEL: dict[str, str] = {
    "SALES": "sales-call",
    "COLLECTIONS": "collections-call",
    "REMINDER": "appointment-reminder",
    "SURVEY": "feedback/NPS",
    "RENEWAL": "renewal/win-back",
}

BASE_SCHEMA_KEYS = """  - "summary" (string): 1-3 sentence summary of the call.
  - "outcome" (string): one of {outcome_values}.
  - "followUpAgreed" (boolean): true if a follow-up/callback was explicitly agreed.
  - "followUpAt" (string|null): ISO-8601 UTC timestamp if determinable, else null.
  - "sentiment" (string): one of POSITIVE, NEUTRAL, NEGATIVE.
  - "aiConfidence" (number): 0.0 to 1.0, your confidence in this classification.
  - "suggestedNextMove" (string): concrete next action the team should take.
"""

EXTRA_SCHEMA_KEYS_BY_VERTICAL: dict[str, str] = {
    "SALES": '  - "objections" (array of strings): objections the lead raised.\n',
    "COLLECTIONS": (
        '  - "objections" (array of strings): disputes or pushback raised.\n'
        '  - "promisedAmount" (number|null): amount promised, if PROMISE_TO_PAY.\n'
        '  - "promisedDate" (string|null): ISO-8601 date promised, if PROMISE_TO_PAY.\n'
        '  - "disputeReason" (string|null): reason given, if DISPUTED.\n'
    ),
    "REMINDER": (
        '  - "newRequestedTime" (string|null): ISO-8601 timestamp if RESCHEDULE_REQUESTED.\n'
    ),
    "SURVEY": (
        '  - "npsScore" (number|null): 0-10 if a numeric score was given, else null.\n'
        '  - "themes" (array of strings): short phrases describing feedback themes.\n'
    ),
    "RENEWAL": (
        '  - "requestedDiscount" (string|null): discount/terms requested, if any.\n'
    ),
}

REQUIRED_BASE_KEYS = frozenset({
    "summary", "outcome", "followUpAgreed", "followUpAt",
    "sentiment", "aiConfidence", "suggestedNextMove",
})

VERTICALS_REQUIRING_OBJECTIONS = frozenset({"SALES", "COLLECTIONS"})


def build_analysis_system_prompt(vertical: str) -> str:
    vertical = normalize_campaign_vertical(vertical)
    outcome_values = ", ".join(OUTCOME_VALUES_BY_VERTICAL[vertical])
    extra_keys = EXTRA_SCHEMA_KEYS_BY_VERTICAL.get(vertical, "")
    outcome_rules = OUTCOME_RULES_BY_VERTICAL[vertical]
    label = _ANALYST_LABEL.get(vertical, "phone")

    return (
        f"You are a {label} analyst. Analyze the transcript of a phone call "
        "between an AI agent and a human contact, and produce a structured "
        "JSON report.\n\n"
        "Return STRICT JSON (no prose, no markdown fences) with EXACTLY these keys:\n"
        f"{BASE_SCHEMA_KEYS.format(outcome_values=outcome_values)}"
        f"{extra_keys}\n"
        "Rules: respond with a single JSON object only. Put all string values on "
        "one line; escape any double quote inside a string as \\\". Do not use "
        "markdown. If uncertain about followUpAt, use null.\n\n"
        f'Outcome classification (apply to the "outcome" field):\n{outcome_rules}'
    )


_ANALYSIS_PROMPTS: dict[str, str] = {
    v: build_analysis_system_prompt(v) for v in VERTICALS
}


def get_analysis_prompt(vertical: str | None) -> str:
    key = normalize_campaign_vertical(vertical)
    return _ANALYSIS_PROMPTS[key]


def validate_outcome(vertical: str | None, outcome: str | None) -> str:
    key = normalize_campaign_vertical(vertical)
    allowed = OUTCOME_VALUES_BY_VERTICAL[key]
    normalized = str(outcome).strip().upper() if outcome else ""
    if normalized == "DO_NOT_CALL":
        fallback = _DNC_OUTCOME_FALLBACK.get(key, "UNKNOWN")
        print(
            f"[ANALYSIS] Remapped DO_NOT_CALL → {fallback} for vertical {key}",
            flush=True,
        )
        return fallback
    if normalized in allowed:
        return normalized
    if outcome:
        print(
            f"[ANALYSIS] Invalid outcome {outcome!r} for vertical {key}; using UNKNOWN",
            flush=True,
        )
    return "UNKNOWN"


def _strip_code_fences(text: str) -> str:
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    return s.strip()


def _extract_first_json_object(s: str) -> str | None:
    start = s.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return s[start : i + 1]
    return None


def parse_analysis_json(raw: str, vertical: str | None) -> dict:
    """Parse and normalize LLM analysis JSON for a campaign vertical."""
    key = normalize_campaign_vertical(vertical)
    s = _strip_code_fences(raw)
    if not s.strip():
        raise ValueError("Empty LLM response")

    candidates: list[str] = []
    for c in (s, _extract_first_json_object(s)):
        if c and c.strip() and c not in candidates:
            candidates.append(c)
    obj = None
    last_json_err: Exception | None = None
    for candidate in candidates:
        try:
            obj = json.loads(candidate)
            break
        except json.JSONDecodeError as e:
            last_json_err = e
    if obj is None:
        raise ValueError(f"No valid JSON in LLM response: {last_json_err}")

    missing = REQUIRED_BASE_KEYS - set(obj.keys())
    if missing:
        raise ValueError(f"Analysis JSON missing keys: {missing}")

    if key in VERTICALS_REQUIRING_OBJECTIONS:
        if "objections" not in obj:
            raise ValueError("Analysis JSON missing key: objections")
        if not isinstance(obj["objections"], list):
            raise ValueError("'objections' must be a list")
    else:
        obj.setdefault("objections", [])

    fup = obj.get("followUpAgreed")
    if isinstance(fup, str):
        low = fup.strip().lower()
        if low == "true":
            obj["followUpAgreed"] = True
        elif low == "false":
            obj["followUpAgreed"] = False
    if not isinstance(obj["followUpAgreed"], bool):
        raise ValueError("'followUpAgreed' must be a boolean")
    if obj["followUpAt"] is not None and not isinstance(obj["followUpAt"], str):
        raise ValueError("'followUpAt' must be a string or null")

    if key == "SURVEY":
        themes = obj.get("themes") or []
        if themes and not obj.get("objections"):
            obj["objections"] = list(themes)

    obj["outcome"] = validate_outcome(key, obj.get("outcome"))
    return obj
