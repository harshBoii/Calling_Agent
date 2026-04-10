import os
import json
import base64
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import Response, PlainTextResponse
from twilio.twiml.voice_response import VoiceResponse, Connect
from twilio.request_validator import RequestValidator
from dotenv import load_dotenv

load_dotenv()

TWILIO_ACCOUNT_SID  = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN   = os.environ["TWILIO_AUTH_TOKEN"]
TWILIO_PHONE_NUMBER = os.environ["TWILIO_PHONE_NUMBER"]
PUBLIC_BASE_URL     = os.environ["PUBLIC_BASE_URL"].rstrip("/")

app = FastAPI()
validator = RequestValidator(TWILIO_AUTH_TOKEN)


async def validate_twilio_signature(request: Request):
    """Reject any POST not signed by Twilio."""
    url = str(request.url)
    form = await request.form()
    params = dict(form)
    signature = request.headers.get("X-Twilio-Signature", "")

    if not validator.validate(url, params, signature):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")

    return params


@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/voice/incoming")
async def incoming_call(request: Request):
    params = await validate_twilio_signature(request)

    call_sid = params.get("CallSid", "unknown")
    caller   = params.get("From", "unknown")
    print(f"[{call_sid}] Incoming call from {caller}")

    ws_base    = PUBLIC_BASE_URL.replace("https://", "wss://").replace("http://", "ws://")
    stream_url = f"{ws_base}/media-stream/{call_sid}"

    response = VoiceResponse()
    response.say("Please wait while we connect you.", voice="alice")

    connect = Connect()
    connect.stream(url=stream_url, name="voice-agent-stream", track="inbound_track")
    response.append(connect)

    return Response(content=str(response), media_type="application/xml")


@app.websocket("/media-stream/{call_sid}")
async def media_stream(websocket: WebSocket, call_sid: str):
    await websocket.accept()
    stream_sid = None
    print(f"[{call_sid}] WebSocket connected")

    try:
        while True:
            raw = await websocket.receive_text()
            data  = json.loads(raw)
            event = data.get("event")

            if event == "connected":
                print(f"[{call_sid}] Twilio connected")

            elif event == "start":
                stream_sid = data["start"]["streamSid"]
                print(f"[{call_sid}] Stream started → streamSid={stream_sid}")
                print(f"[{call_sid}] Media format: {data['start'].get('mediaFormat')}")
                # Audio format is always: mulaw, 8000 Hz, 1 channel

            elif event == "media":
                audio_bytes = base64.b64decode(data["media"]["payload"])
                # TODO next step: pipe audio_bytes → Silero VAD → Deepgram
                print(f"[{call_sid}] audio chunk: {len(audio_bytes)} bytes (mulaw 8kHz)")

            elif event == "mark":
                print(f"[{call_sid}] Mark received: {data['mark']['name']}")

            elif event == "stop":
                print(f"[{call_sid}] Stream stopped")
                break

    except WebSocketDisconnect:
        print(f"[{call_sid}] WebSocket disconnected")
    except Exception as e:
        print(f"[{call_sid}] Error: {e}")
        await websocket.close()