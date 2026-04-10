import os
import json
import base64
import asyncio
import audioop
import websockets
import httpx
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import Response
from twilio.twiml.voice_response import VoiceResponse, Connect
from twilio.rest import Client
from groq import AsyncGroq
from dotenv import load_dotenv

load_dotenv()

TWILIO_ACCOUNT_SID   = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN    = os.environ["TWILIO_AUTH_TOKEN"]
TWILIO_PHONE_NUMBER  = os.environ["TWILIO_PHONE_NUMBER"]
PUBLIC_BASE_URL      = os.environ["PUBLIC_BASE_URL"].rstrip("/")
DEEPGRAM_API_KEY     = os.environ["DEEPGRAM_API_KEY"]
GROQ_API_KEY         = os.environ["GROQ_API_KEY"]
ELEVENLABS_API_KEY   = os.environ["ELEVENLABS_API_KEY"]
ELEVENLABS_VOICE_ID  = os.environ["ELEVENLABS_VOICE_ID"]

app           = FastAPI()
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
groq_client   = AsyncGroq(api_key=GROQ_API_KEY)

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

ELEVENLABS_URL = (
    f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"
    "/stream"
    "?output_format=pcm_8000"   # 8kHz PCM → we convert to mulaw for Twilio
)

SYSTEM_PROMPT = """You are Aryan, a friendly and professional AI assistant 
making a phone call on behalf of your user.

Keep responses:
- Short and conversational (1-3 sentences max)
- Natural sounding — this is a phone call, not an essay
- Focused on the goal of the call

If asked something you don't know, politely say you'll follow up via message.
Never say you are an AI unless directly asked."""


async def ask_groq(conversation_history: list) -> str:
    try:
        response = await groq_client.chat.completions.create(
            model       = "llama-3.3-70b-versatile",
            messages    = [{"role": "system", "content": SYSTEM_PROMPT}] + conversation_history,
            temperature = 0.7,
            max_tokens  = 150,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"[Groq] Error: {e}", flush=True)
        return "Sorry, give me just a moment."


async def text_to_mulaw_chunks(text: str):
    """
    Stream PCM audio from ElevenLabs, convert each chunk to mulaw 8kHz,
    yield base64-encoded mulaw chunks ready for Twilio.
    """
    headers = {
        "xi-api-key":   ELEVENLABS_API_KEY,
        "Content-Type": "application/json",
    }
    payload = {
        "text":       text,
        "model_id":   "eleven_turbo_v2_5",   # lowest latency ElevenLabs model
        "voice_settings": {
            "stability":        0.4,
            "similarity_boost": 0.8,
            "style":            0.0,
            "use_speaker_boost": True,
        },
    }

    async with httpx.AsyncClient(timeout=30) as client:
        async with client.stream("POST", ELEVENLABS_URL, headers=headers, json=payload) as response:
            if response.status_code != 200:
                body = await response.aread()
                print(f"[ElevenLabs] Error {response.status_code}: {body}", flush=True)
                return

            async for pcm_chunk in response.aiter_bytes(chunk_size=320):
                if not pcm_chunk:
                    continue
                # PCM 16-bit 8kHz → mulaw 8kHz (Twilio's required format)
                mulaw_chunk = audioop.lin2ulaw(pcm_chunk, 2)
                yield base64.b64encode(mulaw_chunk).decode("utf-8")


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
    connect  = Connect()
    connect.stream(url=stream_url, name="voice-agent-stream", track="inbound_track")
    response.append(connect)
    return Response(content=str(response), media_type="application/xml")


@app.websocket("/media-stream/{call_sid}")
async def media_stream(websocket: WebSocket, call_sid: str):
    await websocket.accept()
    print(f"[{call_sid}] Twilio WebSocket connected", flush=True)

    audio_queue          = asyncio.Queue()
    conversation_history = []
    transcript_buffer    = []
    stream_sid           = None

    async def send_audio_to_twilio(text: str):
        """Convert text → ElevenLabs PCM → mulaw → send over Twilio WebSocket."""
        print(f"[{call_sid}] 🔊 Sending to ElevenLabs: {text[:60]}...", flush=True)
        chunk_count = 0
        async for mulaw_b64 in text_to_mulaw_chunks(text):
            try:
                await websocket.send_text(json.dumps({
                    "event":     "media",
                    "streamSid": stream_sid,
                    "media":     {"payload": mulaw_b64}
                }))
                chunk_count += 1
            except Exception as e:
                print(f"[{call_sid}] WebSocket send error: {e}", flush=True)
                break
        # Tell Twilio we're done sending this response
        await websocket.send_text(json.dumps({
            "event":     "mark",
            "streamSid": stream_sid,
            "mark":      {"name": "agent_done"}
        }))
        print(f"[{call_sid}] ✅ Sent {chunk_count} audio chunks to Twilio", flush=True)

    async def receive_from_twilio():
        nonlocal stream_sid
        try:
            while True:
                raw   = await websocket.receive_text()
                data  = json.loads(raw)
                event = data.get("event")

                if event == "connected":
                    print(f"[{call_sid}] Twilio connected", flush=True)
                elif event == "start":
                    stream_sid = data["start"]["streamSid"]
                    print(f"[{call_sid}] Stream started → {stream_sid}", flush=True)
                elif event == "media":
                    chunk = base64.b64decode(data["media"]["payload"])
                    await audio_queue.put(chunk)
                elif event == "mark":
                    print(f"[{call_sid}] Mark received: {data['mark']['name']}", flush=True)
                elif event == "stop":
                    print(f"[{call_sid}] Stream stopped", flush=True)
                    break
        except WebSocketDisconnect:
            print(f"[{call_sid}] Twilio disconnected", flush=True)
        except Exception as e:
            print(f"[{call_sid}] Twilio error: {e}", flush=True)
        finally:
            await audio_queue.put(None)

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
                            try:
                                await dg_ws.send(json.dumps({"type": "CloseStream"}))
                            except Exception:
                                pass
                            break
                        await dg_ws.send(chunk)

                async def receive_transcripts():
                    async for raw_msg in dg_ws:
                        try:
                            msg = json.loads(raw_msg)
                            if msg.get("type") != "Results":
                                continue

                            alt        = msg["channel"]["alternatives"][0]
                            transcript = alt.get("transcript", "").strip()
                            if not transcript:
                                continue

                            is_final     = msg.get("is_final", False)
                            speech_final = msg.get("speech_final", False)

                            if is_final:
                                label = "FINAL ✅" if speech_final else "FINAL"
                                print(f"[{call_sid}] [{label}] {transcript}", flush=True)
                                transcript_buffer.append(transcript)

                            if speech_final and transcript_buffer:
                                full_turn = " ".join(transcript_buffer)
                                transcript_buffer.clear()

                                print(f"[{call_sid}] 🎤 Human: {full_turn}", flush=True)
                                conversation_history.append({"role": "user", "content": full_turn})

                                # Groq → reply text
                                agent_reply = await ask_groq(conversation_history)
                                conversation_history.append({"role": "assistant", "content": agent_reply})
                                print(f"[{call_sid}] 🤖 Agent: {agent_reply}", flush=True)

                                # ElevenLabs → audio → Twilio
                                await send_audio_to_twilio(agent_reply)

                        except Exception as e:
                            print(f"[{call_sid}] Transcript error: {e}", flush=True)

                await asyncio.gather(send_audio(), receive_transcripts())

        except websockets.exceptions.InvalidStatus as e:
            print(f"[{call_sid}] ❌ Deepgram rejected: {e}", flush=True)
        except Exception as e:
            print(f"[{call_sid}] Deepgram error: {type(e).__name__}: {e}", flush=True)

    await asyncio.gather(
        receive_from_twilio(),
        stream_to_deepgram()
    )
    print(f"[{call_sid}] Pipeline finished", flush=True)