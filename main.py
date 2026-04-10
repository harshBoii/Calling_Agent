import os
import json
import base64
import asyncio
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import Response
from twilio.twiml.voice_response import VoiceResponse, Connect
from twilio.request_validator import RequestValidator
from twilio.rest import Client
from deepgram import DeepgramClient, LiveTranscriptionEvents, LiveOptions
from dotenv import load_dotenv

load_dotenv()

TWILIO_ACCOUNT_SID  = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN   = os.environ["TWILIO_AUTH_TOKEN"]
TWILIO_PHONE_NUMBER = os.environ["TWILIO_PHONE_NUMBER"]
PUBLIC_BASE_URL     = os.environ["PUBLIC_BASE_URL"].rstrip("/")
DEEPGRAM_API_KEY    = os.environ["DEEPGRAM_API_KEY"]

app = FastAPI()
validator       = RequestValidator(TWILIO_AUTH_TOKEN)
twilio_client   = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
deepgram_client = DeepgramClient(DEEPGRAM_API_KEY)


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

    connect = Connect()
    connect.stream(url=stream_url, name="voice-agent-stream", track="inbound_track")
    response.append(connect)

    return Response(content=str(response), media_type="application/xml")


@app.websocket("/media-stream/{call_sid}")
async def media_stream(websocket: WebSocket, call_sid: str):
    await websocket.accept()
    print(f"[{call_sid}] WebSocket connected")

    dg_connection = deepgram_client.listen.asyncwebsocket.v("1")

    # ✅ No 'self' parameter — this is the correct v3 signature
    async def on_transcript(result, **kwargs):
        try:
            alt        = result.channel.alternatives[0]
            transcript = alt.transcript.strip()

            if not transcript:
                return

            is_final = result.is_final
            label    = "FINAL" if is_final else "interim"
            print(f"[{call_sid}] [{label}] {transcript}")

        except Exception as e:
            print(f"[{call_sid}] Transcript parse error: {e}")

    async def on_utterance_end(utterance_end, **kwargs):
        print(f"[{call_sid}] UtteranceEnd → human finished speaking")

    async def on_error(error, **kwargs):
        print(f"[{call_sid}] Deepgram error: {error}")

    dg_connection.on(LiveTranscriptionEvents.Transcript,   on_transcript)
    dg_connection.on(LiveTranscriptionEvents.UtteranceEnd, on_utterance_end)
    dg_connection.on(LiveTranscriptionEvents.Error,        on_error)

    options = LiveOptions(
        model            = "nova-3",
        encoding         = "mulaw",
        sample_rate      = 8000,
        channels         = 1,
        punctuate        = True,
        interim_results  = True,
        endpointing      = 300,
        utterance_end_ms = "1000",
    )

    connected = await dg_connection.start(options)
    if not connected:
        print(f"[{call_sid}] Failed to connect to Deepgram")
        await websocket.close()
        return

    print(f"[{call_sid}] Deepgram connected")

    try:
        while True:
            raw   = await websocket.receive_text()
            data  = json.loads(raw)
            event = data.get("event")

            if event == "connected":
                print(f"[{call_sid}] Twilio connected")

            elif event == "start":
                stream_sid = data["start"]["streamSid"]
                print(f"[{call_sid}] Stream started → streamSid={stream_sid}")

            elif event == "media":
                audio_bytes = base64.b64decode(data["media"]["payload"])
                await dg_connection.send(audio_bytes)

            elif event == "mark":
                print(f"[{call_sid}] Mark: {data['mark']['name']}")

            elif event == "stop":
                print(f"[{call_sid}] Stream stopped")
                break

    except WebSocketDisconnect:
        print(f"[{call_sid}] WebSocket disconnected")
    except Exception as e:
        # ✅ Suppress the noisy 'tasks cancelled' error from Deepgram SDK internals
        if "tasks cancelled" not in str(e).lower() and "cancel" not in str(e).lower():
            print(f"[{call_sid}] Error: {e}")
    finally:
        try:
            await dg_connection.finish()
        except Exception:
            pass
        print(f"[{call_sid}] Deepgram connection closed")