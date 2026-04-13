import uuid

from fastapi import FastAPI, Request, WebSocket, HTTPException
from fastapi.responses import Response
from twilio.rest import Client
from twilio.twiml.voice_response import Connect, VoiceResponse

from config import (
    PUBLIC_BASE_URL,
    TWILIO_ACCOUNT_SID,
    TWILIO_AUTH_TOKEN,
    TWILIO_PHONE_NUMBER,
    build_call_config,
)
from llm import generate_opening_greeting
from media_stream import run_media_stream

app = FastAPI()
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# ─── Call config store ────────────────────────────────────────────────────────
pending_call_configs: dict[str, dict] = {}
call_configs_by_sid: dict[str, dict] = {}


@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/call/outbound")
async def make_outbound_call(request: Request):
    body = await request.json()
    print(f"[OUTBOUND] Body: {body}", flush=True)
    to_number = body.get("to")
    if not to_number:
        raise HTTPException(status_code=400, detail="Missing 'to' number")

    cfg_body = {k: v for k, v in body.items() if k != "to"}
    cfg = build_call_config(cfg_body)

    use_dynamic = cfg_body.get("dynamic_greeting", True)
    if use_dynamic and not cfg_body.get("opening_greeting"):
        cfg["opening_greeting"] = await generate_opening_greeting(cfg, cfg["llm_provider"])
        print(f"[GREETING] {cfg['opening_greeting']}", flush=True)

    cfg_token = str(uuid.uuid4())
    pending_call_configs[cfg_token] = cfg

    call = twilio_client.calls.create(
        to=to_number,
        from_=TWILIO_PHONE_NUMBER,
        url=f"{PUBLIC_BASE_URL}/voice/incoming?cfg={cfg_token}",
        method="POST",
    )
    print(f"[{call.sid}] Outbound → {to_number} | LLM={cfg['llm_provider']}/{cfg['llm_model']}", flush=True)
    return {
        "call_sid": call.sid,
        "status": call.status,
        "opening_greeting": cfg["opening_greeting"],
    }


@app.post("/voice/incoming")
async def incoming_call(request: Request):
    form = await request.form()
    params = dict(form)
    call_sid = params.get("CallSid", "unknown")
    caller = params.get("From", "unknown")
    cfg_token = request.query_params.get("cfg")
    if cfg_token and cfg_token in pending_call_configs:
        call_configs_by_sid[call_sid] = pending_call_configs.pop(cfg_token)
    print(f"[{call_sid}] Incoming call from {caller}", flush=True)
    ws_base = PUBLIC_BASE_URL.replace("https://", "wss://").replace("http://", "ws://")
    response = VoiceResponse()
    connect = Connect()
    connect.stream(
        url=f"{ws_base}/media-stream/{call_sid}",
        name="voice-agent-stream",
        track="inbound_track",
    )
    response.append(connect)
    return Response(content=str(response), media_type="application/xml")


@app.websocket("/media-stream/{call_sid}")
async def media_stream(websocket: WebSocket, call_sid: str):
    call_cfg = call_configs_by_sid.pop(call_sid, None) or build_call_config(None)
    await run_media_stream(websocket, call_sid, call_cfg)
