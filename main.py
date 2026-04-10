import os
import json
import base64
import asyncio
import websockets
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import Response
from twilio.twiml.voice_response import VoiceResponse, Connect
from twilio.rest import Client
from groq import AsyncGroq
from dotenv import load_dotenv

load_dotenv()

TWILIO_ACCOUNT_SID  = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN   = os.environ["TWILIO_AUTH_TOKEN"]
TWILIO_PHONE_NUMBER = os.environ["TWILIO_PHONE_NUMBER"]
PUBLIC_BASE_URL     = os.environ["PUBLIC_BASE_URL"].rstrip("/")
DEEPGRAM_API_KEY    = os.environ["DEEPGRAM_API_KEY"]
GROQ_API_KEY        = os.environ["GROQ_API_KEY"]

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

# ─── Your agent's personality + context injected here ───────────────────────
SYSTEM_PROMPT = """You are Aryan, a friendly and professional AI assistant 
making a phone call on behalf of your user. 

Keep responses:
- Short and conversational (1-3 sentences max)
- Natural sounding — this is a phone call, not an essay
- Focused on the goal of the call

If asked something you don't know, politely say you'll follow up via message.
Never say you are an AI unless directly asked."""
# ─────────────────────────────────────────────────────────────────────────────


async def ask_groq(conversation_history: list) -> str:
    """Send conversation history to Groq, get back agent's reply."""
    try:
        response = await groq_client.chat.completions.create(
            model       = "llama-3.3-70b-versatile",
            messages    = [{"role": "system", "content": SYSTEM_PROMPT}] + conversation_history,
            temperature = 0.7,
            max_tokens  = 150,   # keep responses short for voice
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"[Groq] Error: {e}", flush=True)
        return "Sorry, give me just a moment."


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

    # Conversation history — grows as the call progresses
    conversation_history = []

    # Buffer to collect FINAL transcript chunks until speech_final fires
    transcript_buffer = []

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
                            msg      = json.loads(raw_msg)
                            msg_type = msg.get("type")

                            if msg_type != "Results":
                                continue

                            alt        = msg["channel"]["alternatives"][0]
                            transcript = alt.get("transcript", "").strip()
                            if not transcript:
                                continue

                            is_final     = msg.get("is_final", False)
                            speech_final = msg.get("speech_final", False)

                            if is_final:
                                label = "FINAL" if not speech_final else "FINAL ✅"
                                print(f"[{call_sid}] [{label}] {transcript}", flush=True)
                                # Accumulate final chunks into buffer
                                transcript_buffer.append(transcript)

                            if speech_final and transcript_buffer:
                                # Full human turn is complete — flush buffer
                                full_turn = " ".join(transcript_buffer)
                                transcript_buffer.clear()

                                print(f"[{call_sid}] 🎤 Human said: {full_turn}", flush=True)

                                # Add to conversation history
                                conversation_history.append({
                                    "role": "user",
                                    "content": full_turn
                                })

                                # Ask Groq
                                print(f"[{call_sid}] ⏳ Asking Groq...", flush=True)
                                agent_reply = await ask_groq(conversation_history)

                                # Add agent reply to history
                                conversation_history.append({
                                    "role": "assistant",
                                    "content": agent_reply
                                })

                                print(f"[{call_sid}] 🤖 Agent: {agent_reply}", flush=True)
                                # TODO next step: send agent_reply → ElevenLabs TTS → audio back to Twilio

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