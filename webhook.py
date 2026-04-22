"""Call-completed webhook: transcript analysis + POST to Next.js service."""

import asyncio
import datetime as dt
import hashlib
import hmac
import json
import re
import uuid

import httpx

from config import NEXT_JS_SERVICE_URL, WEBHOOK_SECRET
from llm import ask_llm


_ANALYSIS_SYSTEM_PROMPT = (
    "You are a sales-call analyst. Analyze the transcript of a phone call "
    "between an AI sales agent and a human lead, and produce a structured "
    "JSON report.\n\n"
    "Return STRICT JSON (no prose, no markdown fences) with EXACTLY these keys:\n"
    '  - "summary" (string): 1-3 sentence summary of the call.\n'
    '  - "outcome" (string): one of '
    "INTERESTED, NOT_INTERESTED, CALLBACK, VOICEMAIL, NO_ANSWER, DO_NOT_CALL, UNKNOWN.\n"
    '  - "sentiment" (string): one of POSITIVE, NEUTRAL, NEGATIVE.\n'
    '  - "objections" (array of strings): short phrases describing objections the lead raised.\n'
    '  - "aiConfidence" (number): 0.0 to 1.0, your confidence in this classification.\n'
    '  - "suggestedNextMove" (string): concrete next action the sales team should take.\n\n'
    "Respond with JSON only. No explanation."
)


def _strip_code_fences(text: str) -> str:
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    return s.strip()


def _parse_json_strict(raw: str) -> dict:
    s = _strip_code_fences(raw)
    # Fallback: pull the first {...} block if the model wrapped it in prose.
    if not s.startswith("{"):
        m = re.search(r"\{.*\}", s, re.DOTALL)
        if not m:
            raise ValueError("No JSON object found in LLM response")
        s = m.group(0)
    obj = json.loads(s)
    required = {"summary", "outcome", "sentiment", "objections", "aiConfidence", "suggestedNextMove"}
    missing = required - set(obj.keys())
    if missing:
        raise ValueError(f"Analysis JSON missing keys: {missing}")
    if not isinstance(obj["objections"], list):
        raise ValueError("'objections' must be a list")
    return obj


def _default_analysis() -> dict:
    return {
        "summary": None,
        "outcome": "UNKNOWN",
        "sentiment": None,
        "objections": [],
        "aiConfidence": None,
        "suggestedNextMove": None,
    }


async def analyze_transcript(turns: list[dict], cfg: dict) -> dict:
    """Run an LLM analysis pass over the transcript turns, with 3 retries."""
    if not turns:
        return _default_analysis()

    transcript_text = "\n".join(f"{t['role']}: {t['text']}" for t in turns)
    user_msg = [{"role": "user", "content": transcript_text}]
    provider = cfg["llm_provider"]
    model = cfg["llm_model"]

    last_err: Exception | None = None
    for attempt in range(3):
        try:
            raw = await ask_llm(user_msg, _ANALYSIS_SYSTEM_PROMPT, provider, model)
            if not raw or raw.strip().lower().startswith("sorry, give me"):
                raise ValueError(f"LLM returned fallback/empty: {raw!r}")
            return _parse_json_strict(raw)
        except Exception as e:
            last_err = e
            print(f"[WEBHOOK] analyze attempt {attempt + 1}/3 failed: {e}", flush=True)
            await asyncio.sleep(0.3 * (2 ** attempt))

    print(f"[WEBHOOK] analyze gave up after 3 tries; last error: {last_err}", flush=True)
    return _default_analysis()


def _iso(ts: dt.datetime | None) -> str | None:
    if ts is None:
        return None
    return ts.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


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

    return {
        "event": "call.completed",
        "eventId": f"evt_{uuid.uuid4().hex}",
        "occurredAt": _iso(ended_at),
        "companyId": ids.get("companyId"),
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
            "sentiment": analysis.get("sentiment"),
            "costCents": None,
            "recordingUrl": None,
            "campaignId": ids.get("campaignId"),
            "metadata": {
                "provider": "telnyx",
                "language": cfg.get("deepgram_language"),
                "voiceModel": voice_model,
                "llmProvider": cfg.get("llm_provider"),
            },
        },
        "transcript": {
            "summary": analysis.get("summary"),
            "turns": call_record.get("turns") or [],
            "objections": analysis.get("objections") or [],
            "aiConfidence": analysis.get("aiConfidence"),
            "suggestedNextMove": analysis.get("suggestedNextMove"),
        },
    }


async def send_call_completed_webhook(call_record: dict, cfg: dict) -> None:
    """Analyze the transcript and POST the call-completed event to the Next.js service."""
    analysis = await analyze_transcript(call_record.get("turns") or [], cfg)
    payload = build_payload(call_record, cfg, analysis)

    url = f"{NEXT_JS_SERVICE_URL}/api/calling-agent/webhook"
    if not WEBHOOK_SECRET:
        raise ValueError("WEBHOOK_SECRET is not set; cannot sign webhook request")

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
                    f"[WEBHOOK] delivered {payload['eventId']} "
                    f"(call={payload['call']['externalCallId']}) → {r.status_code}",
                    flush=True,
                )
                return
            except Exception as e:
                last_err = e
                print(
                    f"[WEBHOOK] POST attempt {attempt + 1}/3 failed: "
                    f"{type(e).__name__}: {e}",
                    flush=True,
                )
                await asyncio.sleep(0.5 * (2 ** attempt))

    print(
        f"[WEBHOOK] gave up delivering {payload['eventId']} after 3 tries; "
        f"last error: {last_err}",
        flush=True,
    )
