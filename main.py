import uuid
import httpx
from fastapi import FastAPI, Request, WebSocket, HTTPException
from fastapi.responses import Response
import re

# from twilio.rest import Client
# from twilio.twiml.voice_response import Connect, VoiceResponse

from config import (
    PUBLIC_BASE_URL,
    # TWILIO_ACCOUNT_SID,
    # TWILIO_AUTH_TOKEN,
    # TWILIO_PHONE_NUMBER,
    TELNYX_API_KEY,
    TELNYX_PHONE_NUMBER,
    TELNYX_CONNECTION_ID,
    SYSTEM_PROMPT_TEMPLATE,
    build_call_config,
    prepend_previous_chat_context,
)
from llm import generate_opening_greeting, generate_questions_to_ask
from media_stream import run_media_stream

app = FastAPI()

# twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# ─── Call config store ────────────────────────────────────────────────────────
pending_call_configs: dict[str, dict] = {}
call_configs_by_sid: dict[str, dict] = {}

_E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")


def _normalize_to_e164(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return ""

    # Allow "00" international prefix.
    if s.startswith("00"):
        s = "+" + s[2:]

    # Strip formatting characters/spaces, keep digits and a leading "+".
    if s.startswith("+"):
        s = "+" + re.sub(r"\D", "", s[1:])
    else:
        s = re.sub(r"\D", "", s)
        if s:
            s = "+" + s

    return s


@app.get("/health")
async def health():
    return {"ok": True}


# @app.post("/call/outbound")
# async def make_outbound_call(request: Request):
#     body = await request.json()
#     print(f"[OUTBOUND] Body: {body}", flush=True)
#     to_number = body.get("to")
#     if not to_number:
#         raise HTTPException(status_code=400, detail="Missing 'to' number")

#     cfg_body = {k: v for k, v in body.items() if k != "to"}
#     cfg = build_call_config(cfg_body)

#     use_dynamic = cfg_body.get("dynamic_greeting", True)
#     if use_dynamic and not cfg_body.get("opening_greeting"):
#         cfg["opening_greeting"] = await generate_opening_greeting(cfg, cfg["llm_provider"])
#         print(f"[GREETING] {cfg['opening_greeting']}", flush=True)

#     cfg_token = str(uuid.uuid4())
#     pending_call_configs[cfg_token] = cfg

#     call = twilio_client.calls.create(
#         to=to_number,
#         from_=TWILIO_PHONE_NUMBER,
#         url=f"{PUBLIC_BASE_URL}/voice/incoming?cfg={cfg_token}",
#         method="POST",
#     )
#     print(f"[{call.sid}] Outbound → {to_number} | LLM={cfg['llm_provider']}/{cfg['llm_model']}", flush=True)
#     return {
#         "call_sid": call.sid,
#         "status": call.status,
#         "opening_greeting": cfg["opening_greeting"],
#     }

@app.post("/call/outbound")
async def make_outbound_call(request: Request):
    body = await request.json()
    raw_to = body.get("to")
    if not raw_to:
        raise HTTPException(status_code=400, detail="Missing 'to' number")

    to_number = _normalize_to_e164(str(raw_to))
    if not _E164_RE.match(to_number):
        raise HTTPException(
            status_code=400,
            detail=f"'to' must be in +E164 format, e.g. +918102244713 (got {raw_to!r})",
        )

    cfg_body = {k: v for k, v in body.items() if k != "to"}
    cfg = build_call_config(cfg_body)

    use_dynamic = cfg_body.get("dynamic_greeting", True)
    if use_dynamic and not cfg_body.get("opening_greeting"):
        cfg["opening_greeting"] = await generate_opening_greeting(cfg, cfg["llm_provider"])

    q_raw = cfg_body.get("questions_to_ask") or cfg_body.get("QUESTIONS_TO_ASK") or cfg_body.get("questions")
    if (not q_raw or (isinstance(q_raw, str) and not q_raw.strip()) or (isinstance(q_raw, list) and not q_raw)) and not cfg_body.get("system_prompt"):
        cfg["questions_to_ask"] = await generate_questions_to_ask(cfg, cfg["llm_provider"])
        ctx = {
            "LANGUAGE": cfg["language"],
            "NAME": cfg["name"],
            "COMPANY": cfg["company"],
            "PRODUCT": cfg["product"],
            "PERKS_OF_PRODUCT": cfg["perks_of_product"],
            "INFO_ABOUT_LEAD": cfg["info_about_lead"],
            "Agent_Name": cfg["agent_name"],
            "AGENT_ROLE": cfg["agent_role"],
            "QUESTIONS_TO_ASK": cfg["questions_to_ask"],
        }
        print(f"Request Body: {ctx}")
        cfg["system_prompt"] = SYSTEM_PROMPT_TEMPLATE.format(**ctx)
        cfg["system_prompt"] = prepend_previous_chat_context(
            cfg["system_prompt"],
            cfg.get("previous_chat_context"),
        )

    cfg["_ids"] = {
        "companyId": body.get("companyId"),
        "leadId": body.get("leadId"),
        "campaignId": body.get("campaignId"),
    }
    cfg["_phone"] = to_number

    cfg_token = str(uuid.uuid4())
    pending_call_configs[cfg_token] = cfg

    ws_base = PUBLIC_BASE_URL.replace("https://", "wss://").replace("http://", "ws://")

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.telnyx.com/v2/calls",
            headers={
                "Authorization": f"Bearer {TELNYX_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "connection_id": TELNYX_CONNECTION_ID,
                "to": to_number,
                "from": TELNYX_PHONE_NUMBER,
                "stream_url": f"{ws_base}/media-stream/{cfg_token}",
                "stream_track": "inbound_track",
                 "stream_codec": "PCMU"
            },
        )
        resp_body = resp.text
        print(f"[TELNYX] {resp.status_code}: {resp_body}", flush=True)
        print(f"to_number: {to_number}")

   
        resp.raise_for_status()
        data = resp.json()["data"]

    call_control_id = data["call_control_id"]

    call_configs_by_sid[cfg_token] = cfg              

    # call_configs_by_sid[call_control_id] = cfg
    # pending_call_configs.pop(cfg_token, None)

    return {
        "call_control_id": call_control_id,
        "status": "initiated",
        "opening_greeting": cfg["opening_greeting"],
    }


# @app.post("/voice/incoming")
# async def incoming_call(request: Request):
#     form = await request.form()
#     params = dict(form)
#     call_sid = params.get("CallSid", "unknown")
#     caller = params.get("From", "unknown")
#     cfg_token = request.query_params.get("cfg")
#     if cfg_token and cfg_token in pending_call_configs:
#         call_configs_by_sid[call_sid] = pending_call_configs.pop(cfg_token)
#     print(f"[{call_sid}] Incoming call from {caller}", flush=True)
#     ws_base = PUBLIC_BASE_URL.replace("https://", "wss://").replace("http://", "ws://")
#     response = VoiceResponse()
#     connect = Connect()
#     connect.stream(
#         url=f"{ws_base}/media-stream/{call_sid}",
#         name="voice-agent-stream",
#         track="inbound_track",
#     )
#     response.append(connect)
#     return Response(content=str(response), media_type="application/xml")


# @app.websocket("/media-stream/{call_sid}")
# async def media_stream(websocket: WebSocket, call_sid: str):
#     call_cfg = call_configs_by_sid.pop(call_sid, None) or build_call_config(None)
#     await run_media_stream(websocket, call_sid, call_cfg)

@app.websocket("/media-stream/{cfg_token}")
async def media_stream(websocket: WebSocket, cfg_token: str):
    call_cfg = call_configs_by_sid.pop(cfg_token, None) or build_call_config(None)
    await run_media_stream(websocket, cfg_token, call_cfg)

@app.post("/webhook")
async def telnyx_webhook(request: Request):
    # Telnyx call control events — handle later if needed
    return {"ok": True}