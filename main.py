import os
import json
import base64
import asyncio
import threading
import queue as thread_queue
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import Response
from twilio.twiml.voice_response import VoiceResponse, Connect
from twilio.rest import Client
from deepgram import DeepgramClient
from deepgram.core.events import EventType
from dotenv import load_dotenv

load_dotenv()

TWILIO_ACCOUNT_SID  = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN   = os.environ["TWILIO_AUTH_TOKEN"]
TWILIO_PHONE_NUMBER = os.environ["TWILIO_PHONE_NUMBER"]
PUBLIC_BASE_URL     = os.environ["PUBLIC_BASE_URL"].rstrip("/")
DEEPGRAM_API_KEY    = os.environ["DEEPGRAM_API_KEY"]

app           = FastAPI()
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
dg_client     = DeepgramClient(api_key=DEEPGRAM_API_KEY)


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

    # Thread-safe queue bridges async Twilio → threaded Deepgram
    audio_queue: thread_queue.Queue = thread_queue.Queue()

    def run_deepgram():
        """Runs entirely in a background thread — matches the user's working pattern."""
        with dg_client.listen.v1.connect(
            model          = "nova-3",
            encoding       = "linear16",
            sample_rate    = 8000,
            channels       = 1,
            punctuate      = True,
            # To get UtteranceEnd, the following must be set:
            interim_results=True,
            utterance_end_ms="1000",
            vad_events=True,
            # Time in milliseconds of silence to wait for before finalizing speech
            endpointing=300
        ) as connection:
            ready = threading.Event()

            def on_open(result):
                print(f"[{call_sid}] Deepgram connected")
                ready.set()

            def on_message(result):
                try:
                    event_type = getattr(result, "type", None)
                    channel    = getattr(result, "channel", None)

                    if channel and hasattr(channel, "alternatives"):
                        transcript = channel.alternatives[0].transcript.strip()
                        if not transcript:
                            return
                        is_final = getattr(result, "is_final", False)
                        label    = "FINAL" if is_final else "interim"
                        print(f"[{call_sid}] [{label}] {transcript}")

                        # next step: on is_final → send to Groq

                    speech_final = getattr(result, "speech_final", False)
                    if speech_final:
                        print(f"[{call_sid}] UtteranceEnd → human turn complete")
                        # next step: trigger Groq LLM here

                except Exception as e:
                    print(f"[{call_sid}] Transcript parse error: {e}")

            def on_error(error):
                print(f"[{call_sid}] Deepgram error: {error}")

            def on_close(result):
                print(f"[{call_sid}] Deepgram connection closed")

            connection.on(EventType.OPEN,    on_open)
            connection.on(EventType.MESSAGE, on_message)
            connection.on(EventType.ERROR,   on_error)
            connection.on(EventType.CLOSE,   on_close)

            def stream_audio():
                """Wait for Deepgram to be ready, then drain queue into send_media."""
                ready.wait()
                print(f"[{call_sid}] Audio streaming to Deepgram started")
                while True:
                    chunk = audio_queue.get()
                    if chunk is None:   # sentinel → call ended
                        break
                    connection.send_media(chunk)

            threading.Thread(target=stream_audio, daemon=True).start()
            connection.start_listening()  # blocks until connection closes

    # Start Deepgram in background thread so it doesn't block FastAPI event loop
    dg_thread = threading.Thread(target=run_deepgram, daemon=True)
    dg_thread.start()

    try:
        while True:
            raw   = await websocket.receive_text()
            data  = json.loads(raw)
            event = data.get("event")

            if event == "connected":
                print(f"[{call_sid}] Twilio connected")

            elif event == "start":
                stream_sid = data["start"]["streamSid"]
                print(f"[{call_sid}] Stream started → {stream_sid}")

            elif event == "media":
                audio_bytes = base64.b64decode(data["media"]["payload"])
                audio_queue.put(audio_bytes)   # hand off to Deepgram thread

            elif event == "stop":
                print(f"[{call_sid}] Stream stopped")
                break

    except WebSocketDisconnect:
        print(f"[{call_sid}] Twilio disconnected")
    except Exception as e:
        print(f"[{call_sid}] Error: {e}")
    finally:
        audio_queue.put(None)   # stop the Deepgram thread cleanly
        print(f"[{call_sid}] Pipeline finished")