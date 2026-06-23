"""Call-completed webhook: transcript analysis + POST to Next.js service."""

import asyncio
import datetime as dt
import hashlib
import hmac
import json
import re
import uuid

import httpx

from config import (
    NEXT_JS_SERVICE_URL,
    DEFAULT_LLM_MODELS,
    DEFAULT_LLM_PROVIDER,
    WEBHOOK_CALL,
    WEBHOOK_EMAIL,
    WEBHOOK_SECRET,
    WEBHOOK_SMS,
)
from llm import ask_llm_for_analysis


_ANALYSIS_SYSTEM_PROMPT = (
    "You are a sales-call analyst. Analyze the transcript of a phone call "
    "between an AI sales agent and a human lead, and produce a structured "
    "JSON report.\n\n"
    "Return STRICT JSON (no prose, no markdown fences) with EXACTLY these keys:\n"
    '  - "summary" (string): 1-3 sentence summary of the call.\n'
    '  - "outcome" (string): one of '
    "INTERESTED, NOT_INTERESTED, CALLBACK, VOICEMAIL, NO_ANSWER, DO_NOT_CALL, UNKNOWN.\n"
    '  - "followUpAgreed" (boolean): true if the lead explicitly agreed to a follow-up call or scheduled callback; otherwise false.\n'
    '  - "followUpAt" (string|null): the scheduled follow-up time as an ISO-8601 UTC timestamp (e.g. "2026-04-23T15:30:00Z") if the time can be determined; otherwise null.\n'
    '  - "sentiment" (string): one of POSITIVE, NEUTRAL, NEGATIVE.\n'
    '  - "objections" (array of strings): short phrases describing objections the lead raised.\n'
    '  - "aiConfidence" (number): 0.0 to 1.0, your confidence in this classification.\n'
    '  - "suggestedNextMove" (string): concrete next action the sales team should take.\n\n'
    "Rules: respond with a single JSON object only. Put all string values on one line; "
    "escape any double quote inside a string as \\\". Do not use markdown. "
    "If uncertain about followUpAt, use null."
)


def _strip_code_fences(text: str) -> str:
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    return s.strip()


def _extract_first_json_object(s: str) -> str | None:
    """Find the first top-level balanced {...} honoring strings and escapes."""
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


def _parse_json_strict(raw: str) -> dict:
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
    required = {
        "summary",
        "outcome",
        "followUpAgreed",
        "followUpAt",
        "sentiment",
        "objections",
        "aiConfidence",
        "suggestedNextMove",
    }
    missing = required - set(obj.keys())
    if missing:
        raise ValueError(f"Analysis JSON missing keys: {missing}")
    if not isinstance(obj["objections"], list):
        raise ValueError("'objections' must be a list")
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
    return obj


def _default_analysis() -> dict:
    return {
        "summary": None,
        "outcome": "UNKNOWN",
        "followUpAgreed": False,
        "followUpAt": None,
        "sentiment": None,
        "objections": [],
        "aiConfidence": None,
        "suggestedNextMove": None,
    }


async def analyze_transcript(turns: list[dict], cfg: dict, *, call_ended_at: dt.datetime | None = None) -> dict:
    """Run an LLM analysis pass over the transcript turns, with 3 retries."""
    if not turns:
        return _default_analysis()

    ended_at_iso = _iso(call_ended_at) if call_ended_at else None
    transcript_text = "\n".join(f"{t['role']}: {t['text']}" for t in turns)
    # Provide a reference timestamp so the model can resolve relative times like
    # "tomorrow at 3" into an absolute ISO-8601 UTC timestamp when possible.
    meta = f"Call ended at (UTC): {ended_at_iso}\n" if ended_at_iso else ""
    user_msg = [{"role": "user", "content": f"{meta}\nTranscript:\n{transcript_text}".strip()}]
    provider = (cfg.get("llm_provider") or DEFAULT_LLM_PROVIDER).lower()
    model = cfg.get("llm_model") or DEFAULT_LLM_MODELS.get(
        provider, DEFAULT_LLM_MODELS[DEFAULT_LLM_PROVIDER]
    )

    last_err: Exception | None = None
    for attempt in range(3):
        raw: str | None = None
        try:
            raw = await ask_llm_for_analysis(
                user_msg, _ANALYSIS_SYSTEM_PROMPT, provider, model
            )
            if not raw or raw.strip().lower().startswith("sorry, give me"):
                raise ValueError(f"LLM returned fallback/empty: {raw!r}")
            return _parse_json_strict(raw)
        except Exception as e:
            last_err = e
            print(f"[WEBHOOK] analyze attempt {attempt + 1}/3 failed: {e}", flush=True)
            if raw and raw.strip():
                print(
                    f"[WEBHOOK] raw model output (up to 2000 chars): {raw[:2000]!r}",
                    flush=True,
                )
            await asyncio.sleep(0.3 * (2 ** attempt))

    print(f"[WEBHOOK] analyze gave up after 3 tries; last error: {last_err}", flush=True)
    return _default_analysis()


def _iso(ts: dt.datetime | None) -> str | None:
    if ts is None:
        return None
    return ts.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _extract_question_answer_map(turns: list[dict]) -> dict[str, str]:
    """
    Build a best-effort question->answer map from transcript turns.

    - Questions are extracted from agent turns by splitting on '?'.
    - Each question is paired with the next user turn as its answer.
    """
    qa: dict[str, str] = {}
    pending_questions: list[str] = []

    for t in turns or []:
        role = (t.get("role") or "").strip().lower()
        text = (t.get("text") or "").strip()
        if not text:
            continue

        if role == "agent":
            if "?" not in text:
                continue
            parts = text.split("?")
            for part in parts[:-1]:
                q = part.strip()
                if not q:
                    continue
                q = q + "?"
                pending_questions.append(q)

        elif role == "user":
            if not pending_questions:
                continue
            ans = text
            # Pair all pending questions to the same immediate user answer.
            for q in pending_questions:
                key = q
                if key in qa:
                    i = 2
                    while f"{key} ({i})" in qa:
                        i += 1
                    key = f"{key} ({i})"
                qa[key] = ans
            pending_questions.clear()

    return qa


def build_payload(call_record: dict, cfg: dict, analysis: dict) -> dict:
    ids = cfg.get("_ids") or {}
    phone = cfg.get("_phone")
    started_at = call_record.get("started_at")
    ended_at = call_record.get("ended_at") or dt.datetime.now(dt.timezone.utc)
    connected = bool(call_record.get("connected"))
    duration_sec = int(call_record.get("duration_sec") or 0)

    voice_model = (
        cfg.get("sarvam_speaker", "rohan")
        if cfg.get("use_sarvam_tts")
        else cfg.get("elevenlabs_model")
    )
    turns = call_record.get("turns") or []
    conv = cfg.get("_conversation_state") or cfg.get("conversation_metadata") or {}
    meet_scheduled = conv.get("meetScheduled") or conv.get("meet_scheduled")

    return {
        "event": "call.completed",
        "eventId": f"evt_{uuid.uuid4().hex}",
        "occurredAt": _iso(ended_at),
        "companyId": ids.get("companyId"),
        "meet_scheduled": meet_scheduled,
        "call": {
            "externalCallId": call_record.get("call_sid"),
            "leadId": ids.get("leadId"),
            "phone": phone,
            "direction": "OUTBOUND",
            "status": "COMPLETED" if connected else "FAILED",
            "startedAt": _iso(started_at),
            "endedAt": _iso(ended_at),
            "durationSec": duration_sec,
            "connected": connected,
            "outcome": analysis.get("outcome"),
            "followUpAgreed": bool(analysis.get("followUpAgreed")),
            "followUpAt": analysis.get("followUpAt"),
            "meet_scheduled": meet_scheduled,
            "sentiment": analysis.get("sentiment"),
            "costCents": None,
            "recordingUrl": None,
            "campaignId": ids.get("campaignId"),
            "metadata": {
                "provider": "telnyx",
                "language": cfg.get("deepgram_language"),
                "voiceModel": voice_model,
                "llmProvider": cfg.get("llm_provider"),
                **(cfg.get("_conversation_state") or cfg.get("conversation_metadata") or {}),
            },
        },
        "transcript": {
            "summary": analysis.get("summary"),
            "turns": turns,
            "objections": analysis.get("objections") or [],
            "aiConfidence": analysis.get("aiConfidence"),
            "suggestedNextMove": analysis.get("suggestedNextMove"),
            "qa": _extract_question_answer_map(turns),
        },
    }


def _call_webhook_url() -> str | None:
    if WEBHOOK_CALL:
        return WEBHOOK_CALL
    if NEXT_JS_SERVICE_URL:
        return f"{NEXT_JS_SERVICE_URL}/api/calling-agent/webhook"
    return None


async def deliver_webhook(url: str, payload: dict, event_type: str) -> None:
    """POST signed JSON to url with 3 retries. Never raises."""
    if not url:
        print(f"[WEBHOOK] no URL for {event_type}; skipped", flush=True)
        return
    if not WEBHOOK_SECRET:
        print(f"[WEBHOOK] WEBHOOK_SECRET not set; skipped {event_type}", flush=True)
        return

    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    sig_hex = hmac.new(
        WEBHOOK_SECRET.encode("utf-8"),
        raw.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    headers = {
        "Content-Type": "application/json",
        "x-calling-agent-signature": f"sha256={sig_hex}",
        "x-calling-agent-event-id": payload["eventId"],
        "x-calling-agent-event-type": payload["event"],
    }

    async with httpx.AsyncClient(timeout=10) as client:
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                r = await client.post(url, content=raw, headers=headers)
                r.raise_for_status()
                print(
                    f"[WEBHOOK] delivered {payload['eventId']} ({event_type}) → {r.status_code}",
                    flush=True,
                )
                return
            except Exception as e:
                last_err = e
                print(
                    f"[WEBHOOK] POST attempt {attempt + 1}/3 failed ({event_type}): "
                    f"{type(e).__name__}: {e}",
                    flush=True,
                )
                await asyncio.sleep(0.5 * (2 ** attempt))

    print(
        f"[WEBHOOK] gave up delivering {payload['eventId']} ({event_type}); "
        f"last error: {last_err}",
        flush=True,
    )


def build_sms_payload(
    *,
    task_token: str,
    cfg: dict,
    external_id: str | None,
    status: str,
    error: str | None = None,
) -> dict:
    ids = cfg.get("_ids") or {}
    return {
        "event": "sms.completed",
        "eventId": f"evt_{uuid.uuid4().hex}",
        "occurredAt": _iso(dt.datetime.now(dt.timezone.utc)),
        "companyId": ids.get("companyId") or cfg.get("companyId"),
        "leadId": ids.get("leadId") or cfg.get("leadId"),
        "campaignId": ids.get("campaignId") or cfg.get("campaignId"),
        "message": {
            "taskToken": task_token,
            "externalId": external_id,
            "from": cfg.get("from"),
            "to": cfg.get("to"),
            "body": cfg.get("message") or cfg.get("body"),
            "status": status,
            "messageType": cfg.get("message_type", "campaign"),
            "provider": "telnyx",
            "error": error,
        },
    }


def build_email_payload(
    *,
    task_token: str,
    cfg: dict,
    external_id: str | None,
    status: str,
    error: str | None = None,
) -> dict:
    ids = cfg.get("_ids") or {}
    return {
        "event": "email.completed",
        "eventId": f"evt_{uuid.uuid4().hex}",
        "occurredAt": _iso(dt.datetime.now(dt.timezone.utc)),
        "companyId": ids.get("companyId") or cfg.get("companyId"),
        "leadId": ids.get("leadId") or cfg.get("leadId"),
        "campaignId": ids.get("campaignId") or cfg.get("campaignId"),
        "message": {
            "taskToken": task_token,
            "externalId": external_id,
            "from": cfg.get("from"),
            "to": cfg.get("to"),
            "subject": cfg.get("subject"),
            "status": status,
            "messageType": cfg.get("message_type", "campaign"),
            "provider": "resend",
            "error": error,
        },
    }


async def send_sms_completed_webhook(
    *,
    task_token: str,
    cfg: dict,
    external_id: str | None,
    status: str,
    error: str | None = None,
) -> None:
    if not WEBHOOK_SMS:
        print("[WEBHOOK] WEBHOOK_SMS not set; skipped sms.completed", flush=True)
        return
    payload = build_sms_payload(
        task_token=task_token,
        cfg=cfg,
        external_id=external_id,
        status=status,
        error=error,
    )
    await deliver_webhook(WEBHOOK_SMS, payload, "sms.completed")


async def send_email_completed_webhook(
    *,
    task_token: str,
    cfg: dict,
    external_id: str | None,
    status: str,
    error: str | None = None,
) -> None:
    if not WEBHOOK_EMAIL:
        print("[WEBHOOK] WEBHOOK_EMAIL not set; skipped email.completed", flush=True)
        return
    payload = build_email_payload(
        task_token=task_token,
        cfg=cfg,
        external_id=external_id,
        status=status,
        error=error,
    )
    await deliver_webhook(WEBHOOK_EMAIL, payload, "email.completed")


def build_call_status_payload(
    cfg: dict,
    *,
    call_sid: str,
    status: str,
) -> dict:
    ids = cfg.get("_ids") or {}
    return {
        "event": "call.status",
        "eventId": f"evt_{uuid.uuid4().hex}",
        "occurredAt": _iso(dt.datetime.now(dt.timezone.utc)),
        "companyId": ids.get("companyId"),
        "call": {
            "externalCallId": call_sid,
            "leadId": ids.get("leadId"),
            "phone": cfg.get("_phone"),
            "direction": "OUTBOUND",
            "status": status,
            "campaignId": ids.get("campaignId"),
        },
    }


async def send_call_status_webhook(
    *,
    call_sid: str,
    cfg: dict,
    status: str,
) -> None:
    payload = build_call_status_payload(cfg, call_sid=call_sid, status=status)
    url = _call_webhook_url()
    if not url:
        print("[WEBHOOK] no call webhook URL configured; skipped call.status", flush=True)
        return
    try:
        await deliver_webhook(url, payload, "call.status")
    except Exception as e:
        print(f"[WEBHOOK] call.status error: {type(e).__name__}: {e}", flush=True)


async def send_call_completed_webhook(call_record: dict, cfg: dict) -> None:
    """Analyze the transcript and POST the call-completed event."""
    analysis = await analyze_transcript(
        call_record.get("turns") or [],
        cfg,
        call_ended_at=call_record.get("ended_at"),
    )
    payload = build_payload(call_record, cfg, analysis)

    url = _call_webhook_url()
    if not url:
        print("[WEBHOOK] no call webhook URL configured; skipped call.completed", flush=True)
        return
    try:
        await deliver_webhook(url, payload, "call.completed")
    except Exception as e:
        print(f"[WEBHOOK] call.completed error: {type(e).__name__}: {e}", flush=True)
