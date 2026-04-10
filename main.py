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
    print(f"[{call_sid}] Incoming call from {caller}")

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
    print(f"[{call_sid}] Twilio WebSocket connected")

    # Queue bridges the two tasks — Twilio audio → Deepgram
    audio_queue: asyncio.Queue = asyncio.Queue()

    async def receive_from_twilio():
        """Read audio chunks from Twilio and push to queue."""
        try:
            while True:
                raw   = await websocket.receive_text()
                data  = json.loads(raw)
                event = data.get("event")

                if event == "start":
                    sid = data["start"]["streamSid"]
                    print(f"[{call_sid}] Stream started → {sid}")

                elif event == "media":
                    audio_bytes = base64.b64decode(data["media"]["payload"])
                    await audio_queue.put(audio_bytes)

                elif event == "stop":
                    print(f"[{call_sid}] Stream stopped")
                    break

        except WebSocketDisconnect:
            print(f"[{call_sid}] Twilio disconnected")
        except Exception as e:
            print(f"[{call_sid}] Twilio error: {e}")
        finally:
            await audio_queue.put(None)  # sentinel → tells deepgram task to stop

    async def stream_to_deepgram():
        """Pull audio from queue and stream to Deepgram. Print transcripts."""
        headers = {"Authorization": f"Token {DEEPGRAM_API_KEY}"}
        try:
            async with websockets.connect(DEEPGRAM_URL, extra_headers=headers) as dg_ws:
                print(f"[{call_sid}] Deepgram connected")

                async def send_audio():
                    while True:
                        chunk = await audio_queue.get()
                        if chunk is None:
                            break
                        await dg_ws.send(chunk)
                    # Tell Deepgram we're done sending
                    await dg_ws.send(json.dumps({"type": "CloseStream"}))

                async def receive_transcripts():
                    async for message in dg_ws:
                        try:
                            msg  = json.loads(message)
                            # Only care about transcript results
                            if msg.get("type") != "Results":
                                continue

                            alt        = msg["channel"]["alternatives"][0]
                            transcript = alt.get("transcript", "").strip()
                            if not transcript:
                                continue

                            is_final = msg.get("is_final", False)
                            label    = "FINAL" if is_final else "interim"
                            print(f"[{call_sid}] [{label}] {transcript}")

                            # TODO next step: on is_final → send to Groq

                            if msg.get("speech_final"):
                                print(f"[{call_sid}] UtteranceEnd → human turn complete")
                                # TODO next step: trigger LLM here

                        except Exception as e:
                            print(f"[{call_sid}] Transcript parse error: {e}")

                # Run both concurrently inside the Deepgram connection
                await asyncio.gather(send_audio(), receive_transcripts())

        except Exception as e:
            print(f"[{call_sid}] Deepgram error: {e}")

    # Run both tasks concurrently
    await asyncio.gather(
        receive_from_twilio(),
        stream_to_deepgram()
    )
    print(f"[{call_sid}] Call pipeline finished")