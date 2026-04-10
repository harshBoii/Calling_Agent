import os
import json
import base64
import asyncio
import websockets
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import Response
from twilio.twiml.voice_response import VoiceResponse, Connect
from twilio.rest import Client
from dotenv import load_dotenv

load_dotenv()

TWILIO_ACCOUNT_SID  = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN   = os.environ["TWILIO_AUTH_TOKEN"]
TWILIO_PHONE_NUMBER = os.environ["TWILIO_PHONE_NUMBER"]
PUBLIC_BASE_URL     = os.environ["PUBLIC_BASE_URL"].rstrip("/")
DEEPGRAM_API_KEY    = os.environ["DEEPGRAM_API_KEY"]

app           = FastAPI()
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

DEEPGRAM_URL = (
    "wss://api.deepgram.com/v1/listen"
    "?model=nova-3"
    "&encoding=mulaw"
    "&sample_rate=8000"
    "&channels=1"
    "&punctuate=true"
    "&interim_results=true"
    "&endpointing=300"
    "&utterance_end_ms=1000"
)


@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/call/outbound")
async def make_outbound_call(request: Request):
    body      = await request.json()
    to_number = body.get("to")
    if not to_number:
        raise HTTPException(status_code=400, detail="Missing 'to' number")
    call = twilio_client.calls.create(
        to=to_number,
        from_=TWILIO_PHONE_NUMBER,
        url=f"{PUBLIC_BASE_URL}/voice/incoming",
        method="POST"
    )
    return {"call_sid": call.sid, "status": call.status}


@app.post("/voice/incoming")
async def incoming_call(request: Request):
    form     = await request.form()
    params   = dict(form)
    call_sid = params.get("CallSid", "unknown")
    caller   = params.get("From", "unknown")
    print(f"[{call_sid}] Incoming call from {caller}", flush=True)

    ws_base    = PUBLIC_BASE_URL.replace("https://", "wss://").replace("http://", "ws://")
    stream_url = f"{ws_base}/media-stream/{call_sid}"

    response = VoiceResponse()
    response.say("Please wait while we connect you.", voice="alice")
    connect  = Connect()
    connect.stream(url=stream_url, name="voice-agent-stream", track="inbound_track")
    response.append(connect)
    return Response(content=str(response), media_type="application/xml")


@app.websocket("/media-stream/{call_sid}")
async def media_stream(websocket: WebSocket, call_sid: str):
    await websocket.accept()
    print(f"[{call_sid}] Twilio WebSocket connected", flush=True)

    audio_queue: asyncio.Queue = asyncio.Queue()

    async def receive_from_twilio():
        try:
            while True:
                raw   = await websocket.receive_text()
                data  = json.loads(raw)
                event = data.get("event")

                if event == "connected":
                    print(f"[{call_sid}] Twilio connected", flush=True)

                elif event == "start":
                    sid = data["start"]["streamSid"]
                    print(f"[{call_sid}] Stream started → {sid}", flush=True)

                elif event == "media":
                    chunk = base64.b64decode(data["media"]["payload"])
                    await audio_queue.put(chunk)

                elif event == "stop":
                    print(f"[{call_sid}] Stream stopped", flush=True)
                    break

        except WebSocketDisconnect:
            print(f"[{call_sid}] Twilio disconnected", flush=True)
        except Exception as e:
            print(f"[{call_sid}] Twilio receive error: {e}", flush=True)
        finally:
            await audio_queue.put(None)  # sentinel

    async def stream_to_deepgram():
        headers = {"Authorization": f"Token {DEEPGRAM_API_KEY}"}
        try:
            async with websockets.connect(
                DEEPGRAM_URL,
                additional_headers=headers,
                ping_interval=5,
                ping_timeout=20,
            ) as dg_ws:
                print(f"[{call_sid}] Deepgram connected ✅", flush=True)

                async def send_audio():
                    while True:
                        chunk = await audio_queue.get()
                        if chunk is None:
                            # Signal end of stream to Deepgram
                            await dg_ws.send(json.dumps({"type": "CloseStream"}))
                            print(f"[{call_sid}] Sent CloseStream to Deepgram", flush=True)
                            break
                        await dg_ws.send(chunk)

                async def receive_transcripts():
                    async for raw_msg in dg_ws:
                        try:
                            msg = json.loads(raw_msg)
                            msg_type = msg.get("type")

                            # Log everything Deepgram sends so we can debug
                            if msg_type != "Results":
                                print(f"[{call_sid}] Deepgram event: {msg_type}", flush=True)
                                continue

                            alt        = msg["channel"]["alternatives"][0]
                            transcript = alt.get("transcript", "").strip()
                            if not transcript:
                                continue

                            is_final     = msg.get("is_final", False)
                            speech_final = msg.get("speech_final", False)
                            label        = "FINAL" if is_final else "interim"

                            print(f"[{call_sid}] [{label}] {transcript}", flush=True)

                            if speech_final:
                                print(f"[{call_sid}] 🎤 Human turn complete → Groq next", flush=True)
                                # TODO next step: send transcript to Groq

                        except Exception as e:
                            print(f"[{call_sid}] Transcript parse error: {e}", flush=True)

                await asyncio.gather(send_audio(), receive_transcripts())

        except websockets.exceptions.InvalidStatus as e:
            # This shows the EXACT HTTP error from Deepgram with body
            print(f"[{call_sid}] ❌ Deepgram rejected connection: {e}", flush=True)
        except Exception as e:
            print(f"[{call_sid}] Deepgram error: {type(e).__name__}: {e}", flush=True)

    await asyncio.gather(
        receive_from_twilio(),
        stream_to_deepgram()
    )
    print(f"[{call_sid}] Pipeline finished", flush=True)