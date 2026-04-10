import os
import json
import base64
import asyncio
import audioop
import uuid
import websockets
import httpx
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import Response
from twilio.twiml.voice_response import VoiceResponse, Connect
from twilio.rest import Client
from groq import AsyncGroq
import anthropic
import google.generativeai as genai
from openai import AsyncOpenAI
from dotenv import load_dotenv

load_dotenv()

# ─── Twilio ───────────────────────────────────────────────────────────────────
TWILIO_ACCOUNT_SID  = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN   = os.environ["TWILIO_AUTH_TOKEN"]
TWILIO_PHONE_NUMBER = os.environ["TWILIO_PHONE_NUMBER"]
PUBLIC_BASE_URL     = os.environ["PUBLIC_BASE_URL"].rstrip("/")

# ─── STT ──────────────────────────────────────────────────────────────────────
DEEPGRAM_API_KEY    = os.environ["DEEPGRAM_API_KEY"]

# ─── TTS ──────────────────────────────────────────────────────────────────────
ELEVENLABS_API_KEY  = os.environ["ELEVENLABS_API_KEY"]
ELEVENLABS_VOICE_ID = os.environ["ELEVENLABS_VOICE_ID"]

# ─── LLM keys (only set the ones you use) ────────────────────────────────────
GROQ_API_KEY        = os.environ.get("GROQ_API_KEY", "")
OPENAI_API_KEY      = os.environ.get("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
GEMINI_API_KEY      = os.environ.get("GEMINI_API_KEY", "")

# ─── LLM clients ─────────────────────────────────────────────────────────────
groq_client   = AsyncGroq(api_key=GROQ_API_KEY)         if GROQ_API_KEY      else None
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)     if OPENAI_API_KEY    else None
claude_client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

app           = FastAPI()
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# ─── Default LLM provider + models ───────────────────────────────────────────
DEFAULT_LLM_PROVIDER = "groq"   # groq | openai | claude | gemini

DEFAULT_LLM_MODELS = {
    "groq":   "llama-3.3-70b-versatile",
    "openai": "gpt-5.4-nano",
    "claude": "claude-haiku-4-5-20251001",  
    "gemini": "gemini-3-flash-preview",
}

# ─── Deepgram ─────────────────────────────────────────────────────────────────
DEEPGRAM_URL_BASE = (
    "wss://api.deepgram.com/v1/listen"
    "?model=nova-3"
    "&encoding=mulaw"
    "&sample_rate=8000"
    "&channels=1"
    "&punctuate=true"
    "&interim_results=true"
    "&endpointing=300"
    "&utterance_end_ms=1000"
    "&language="
)

# ─── ElevenLabs ───────────────────────────────────────────────────────────────
ELEVENLABS_STREAM_PATH = "/stream?output_format=pcm_8000"
ELEVENLABS_MODEL       = "eleven_flash_v2_5"

def elevenlabs_stream_url(voice_id: str) -> str:
    return f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}{ELEVENLABS_STREAM_PATH}"

def deepgram_ws_url(lang: str) -> str:
    return DEEPGRAM_URL_BASE + lang

# ─── Agent defaults ───────────────────────────────────────────────────────────
LANGUAGE          = "English"
NAME              = "Samaaira"
COMPANY           = "Immortell Company"
PRODUCT           = "GEO optimization services"
PERKS_OF_PRODUCT  = "10% off on the first month"
INFO_ABOUT_LEAD   = "The Lead is a poor guy with a low income and is looking for a cheap way to grow his business."
MIN_WORDS_TO_RESPOND = 3

SYSTEM_PROMPT_TEMPLATE = """You are Samaaira, a warm and persuasive sales representative calling on behalf of {COMPANY}.

## Your Goal
Sell {PRODUCT} to {NAME}. The offer includes {PERKS_OF_PRODUCT}. Close the call with either a confirmed interest or a scheduled follow-up.

## What You Know About This Lead
{INFO_ABOUT_LEAD}
Use this intel subtly — don't reference it directly. Let it shape HOW you pitch, not WHAT you say.

## Voice Call Rules (Critical)
- Speak in {LANGUAGE} only
- Max 1-2 sentences per response — this is a phone call, not an email
- NEVER use bullet points, markdown, asterisks, or lists — they sound terrible when spoken
- NEVER say "As an AI" or "I'm a language model" — you are Samaaira, a human sales rep
- NEVER use filler phrases like "Certainly!", "Absolutely!", "Great question!" — they sound robotic
- Avoid repeating the caller's name in every response — use it sparingly (max once per minute)

## Conversation Flow
1. Warm intro → establish rapport quickly
2. Identify pain point → ask one open question tied to their situation
3. Bridge → connect their pain to your product naturally
4. Offer → present {PRODUCT} + {PERKS_OF_PRODUCT} as the solution
5. Handle objections → acknowledge, then redirect
6. Close → ask for a simple yes: "Want me to get that set up for you?"

## Handling Common Objections
- "Not interested" → "I totally get that. Most people I speak to said the same thing before they saw [specific benefit]. Can I take just 30 seconds?"
- "Too expensive" → lean into {PERKS_OF_PRODUCT} and ROI
- "Call me later" → try to lock in a specific time before hanging up
- "How did you get my number" → "You were referred to us as someone who could benefit from this."

## Tone
Confident but never pushy. Friendly but professional. Sound like a real person having a real conversation — with natural pauses and occasional light humor if the vibe allows."""

OPENING_GREETING_TEMPLATE = (
    "Hi, is this {NAME}? "
    "Hey! This is Samaaira calling from {COMPANY}. "
    "I'll keep this quick — I'm reaching out because we're helping businesses like yours with {PRODUCT}, "
    "and right now we have {PERKS_OF_PRODUCT} for new clients. "
    "Is this a good time to talk for two minutes?"
)
# ─── Call config store ────────────────────────────────────────────────────────
pending_call_configs:  dict[str, dict] = {}
call_configs_by_sid:   dict[str, dict] = {}


# ─── LLM ROUTER ───────────────────────────────────────────────────────────────

async def ask_llm(
    conversation_history: list,
    system_prompt: str,
    provider: str,
    model: str,
) -> str:
    """
    Unified LLM caller. Routes to the right provider.
    provider: "groq" | "openai" | "claude" | "gemini"
    """
    try:
        # ── Groq ──────────────────────────────────────────────────────────────
        if provider == "groq":
            if not groq_client:
                raise ValueError("GROQ_API_KEY not set")
            response = await groq_client.chat.completions.create(
                model       = model,
                messages    = [{"role": "system", "content": system_prompt}] + conversation_history,
                temperature = 0.7,
                max_tokens  = 150,
            )
            return response.choices[0].message.content.strip()

        # ── OpenAI / ChatGPT ──────────────────────────────────────────────────
        elif provider == "openai":
            if not openai_client:
                raise ValueError("OPENAI_API_KEY not set")
            response = await openai_client.chat.completions.create(
                model       = model,
                messages    = [{"role": "system", "content": system_prompt}] + conversation_history,
                temperature = 0.7,
                max_tokens  = 150,
            )
            return response.choices[0].message.content.strip()

        # ── Claude ────────────────────────────────────────────────────────────
        elif provider == "claude":
            if not claude_client:
                raise ValueError("ANTHROPIC_API_KEY not set")
            # Claude takes system prompt separately, not in messages array
            response = await claude_client.messages.create(
                model      = model,
                system     = system_prompt,
                messages   = conversation_history,   # already in {role, content} format
                max_tokens = 150,
            )
            return response.content[0].text.strip()

        # ── Gemini ────────────────────────────────────────────────────────────
        elif provider == "gemini":
            if not GEMINI_API_KEY:
                raise ValueError("GEMINI_API_KEY not set")
            gemini_model = genai.GenerativeModel(
                model_name   = model,
                system_instruction = system_prompt,
            )
            # Convert OpenAI-style history to Gemini format
            gemini_history = []
            for msg in conversation_history:
                role = "user" if msg["role"] == "user" else "model"
                gemini_history.append({"role": role, "parts": [msg["content"]]})
            chat = gemini_model.start_chat(history=gemini_history[:-1] if gemini_history else [])
            last_msg = gemini_history[-1]["parts"][0] if gemini_history else ""
            response = await asyncio.to_thread(chat.send_message, last_msg)
            return response.text.strip()

        else:
            raise ValueError(f"Unknown LLM provider: '{provider}'. Use groq | openai | claude | gemini")

    except Exception as e:
        print(f"[LLM/{provider}] Error: {e}", flush=True)
        return "Sorry, give me just a moment."


# ─── Config builder ───────────────────────────────────────────────────────────

def _format_vars(*, language, name, company, product, perks_of_product, info_about_lead) -> dict:
    return {
        "LANGUAGE": language, "NAME": name, "COMPANY": company,
        "PRODUCT": product, "PERKS_OF_PRODUCT": perks_of_product,
        "INFO_ABOUT_LEAD": info_about_lead,
    }

def build_call_config(body: dict | None) -> dict:
    b              = body or {}
    language       = b.get("language", LANGUAGE)
    dg_language    = b.get("deepgram_language", "en")
    el_model       = b.get("elevenlabs_model", ELEVENLABS_MODEL)
    name           = b.get("name", NAME)
    company        = b.get("company", COMPANY)
    product        = b.get("product", PRODUCT)
    perks          = b.get("perks_of_product", PERKS_OF_PRODUCT)
    lead_info      = b.get("info_about_lead", INFO_ABOUT_LEAD)
    voice_id       = ELEVENLABS_VOICE_ID

    # ── LLM selection ────────────────────────────────────────────────────────
    provider       = b.get("llm_provider", DEFAULT_LLM_PROVIDER).lower()
    model          = b.get("llm_model", DEFAULT_LLM_MODELS.get(provider, DEFAULT_LLM_MODELS[DEFAULT_LLM_PROVIDER]))

    ctx = _format_vars(language=language, name=name, company=company,
                       product=product, perks_of_product=perks, info_about_lead=lead_info)

    system_prompt    = b.get("system_prompt") or SYSTEM_PROMPT_TEMPLATE.format(**ctx)
    opening_greeting = b.get("opening_greeting") or OPENING_GREETING_TEMPLATE.format(**ctx)

    return {
        "language": language, "deepgram_language": dg_language,
        "elevenlabs_model": el_model, "voice_id": voice_id,
        "name": name, "company": company, "product": product,
        "perks_of_product": perks, "info_about_lead": lead_info,
        "system_prompt": system_prompt, "opening_greeting": opening_greeting,
        "llm_provider": provider,
        "llm_model": model,
    }


# ─── TTS ──────────────────────────────────────────────────────────────────────

async def text_to_mulaw_chunks(text: str, model_id: str, voice_id: str):
    headers = {"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"}
    payload = {
        "text": text, "model_id": model_id,
        "voice_settings": {"stability": 0.4, "similarity_boost": 0.8, "style": 0.0, "use_speaker_boost": True},
    }
    url = elevenlabs_stream_url(voice_id)
    async with httpx.AsyncClient(timeout=30) as client:
        async with client.stream("POST", url, headers=headers, json=payload) as response:
            if response.status_code != 200:
                body = await response.aread()
                print(f"[ElevenLabs] Error {response.status_code}: {body}", flush=True)
                return
            async for pcm_chunk in response.aiter_bytes(chunk_size=320):
                if not pcm_chunk:
                    continue
                yield base64.b64encode(audioop.lin2ulaw(pcm_chunk, 2)).decode("utf-8")


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/call/outbound")
async def make_outbound_call(request: Request):
    body      = await request.json()
    to_number = body.get("to")
    if not to_number:
        raise HTTPException(status_code=400, detail="Missing 'to' number")
    cfg_body  = {k: v for k, v in body.items() if k != "to"}
    cfg       = build_call_config(cfg_body)
    cfg_token = str(uuid.uuid4())
    pending_call_configs[cfg_token] = cfg
    call = twilio_client.calls.create(
        to=to_number, from_=TWILIO_PHONE_NUMBER,
        url=f"{PUBLIC_BASE_URL}/voice/incoming?cfg={cfg_token}", method="POST"
    )
    print(f"[{call.sid}] Outbound → {to_number} using LLM={cfg['llm_provider']}/{cfg['llm_model']}", flush=True)
    return {"call_sid": call.sid, "status": call.status}


@app.post("/voice/incoming")
async def incoming_call(request: Request):
    form      = await request.form()
    params    = dict(form)
    call_sid  = params.get("CallSid", "unknown")
    caller    = params.get("From", "unknown")
    cfg_token = request.query_params.get("cfg")
    if cfg_token and cfg_token in pending_call_configs:
        call_configs_by_sid[call_sid] = pending_call_configs.pop(cfg_token)
    print(f"[{call_sid}] Incoming call from {caller}", flush=True)
    ws_base    = PUBLIC_BASE_URL.replace("https://", "wss://").replace("http://", "ws://")
    response   = VoiceResponse()
    connect    = Connect()
    connect.stream(url=f"{ws_base}/media-stream/{call_sid}", name="voice-agent-stream", track="inbound_track")
    response.append(connect)
    return Response(content=str(response), media_type="application/xml")


# ─── WebSocket pipeline ───────────────────────────────────────────────────────

@app.websocket("/media-stream/{call_sid}")
async def media_stream(websocket: WebSocket, call_sid: str):
    await websocket.accept()
    print(f"[{call_sid}] Twilio WebSocket connected", flush=True)

    call_cfg         = call_configs_by_sid.pop(call_sid, None) or build_call_config(None)
    system_prompt    = call_cfg["system_prompt"]
    opening_greeting = call_cfg["opening_greeting"]
    el_model         = call_cfg["elevenlabs_model"]
    voice_id         = call_cfg["voice_id"]
    dg_url           = deepgram_ws_url(call_cfg["deepgram_language"])
    llm_provider     = call_cfg["llm_provider"]
    llm_model        = call_cfg["llm_model"]

    print(f"[{call_sid}] LLM: {llm_provider}/{llm_model}", flush=True)

    audio_queue          = asyncio.Queue()
    conversation_history = []
    transcript_buffer    = []
    stream_sid           = None
    agent_speaking       = False

    async def send_audio_to_twilio(text: str):
        nonlocal agent_speaking
        agent_speaking = True
        print(f"[{call_sid}] 🔊 Speaking: {text[:80]}", flush=True)
        chunk_count = 0
        async for mulaw_b64 in text_to_mulaw_chunks(text, el_model, voice_id):
            if not agent_speaking:
                print(f"[{call_sid}] ⚡ Interrupted — stopping TTS", flush=True)
                break
            try:
                await websocket.send_text(json.dumps({
                    "event": "media", "streamSid": stream_sid,
                    "media": {"payload": mulaw_b64}
                }))
                chunk_count += 1
            except Exception as e:
                print(f"[{call_sid}] Send error: {e}", flush=True)
                break
        try:
            await websocket.send_text(json.dumps({
                "event": "mark", "streamSid": stream_sid, "mark": {"name": "agent_done"}
            }))
        except Exception:
            pass
        agent_speaking = False
        print(f"[{call_sid}] ✅ Sent {chunk_count} chunks", flush=True)

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
                    conversation_history.append({"role": "assistant", "content": opening_greeting})
                    asyncio.create_task(send_audio_to_twilio(opening_greeting))
                elif event == "media":
                    await audio_queue.put(base64.b64decode(data["media"]["payload"]))
                elif event == "mark":
                    print(f"[{call_sid}] Mark: {data['mark']['name']}", flush=True)
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
        nonlocal agent_speaking
        headers = {"Authorization": f"Token {DEEPGRAM_API_KEY}"}
        try:
            async with websockets.connect(dg_url, additional_headers=headers,
                                          ping_interval=5, ping_timeout=20) as dg_ws:
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
                    nonlocal agent_speaking
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

                            if agent_speaking:
                                agent_speaking = False
                                print(f"[{call_sid}] ⚡ Human interrupted agent", flush=True)
                                try:
                                    await websocket.send_text(json.dumps({
                                        "event": "clear", "streamSid": stream_sid
                                    }))
                                except Exception:
                                    pass

                            if is_final:
                                label = "FINAL ✅" if speech_final else "FINAL"
                                print(f"[{call_sid}] [{label}] {transcript}", flush=True)
                                transcript_buffer.append(transcript)

                            if speech_final and transcript_buffer:
                                full_turn = " ".join(transcript_buffer)
                                transcript_buffer.clear()
                                if len(full_turn.split()) < MIN_WORDS_TO_RESPOND:
                                    print(f"[{call_sid}] ⏭ Skipping short turn: '{full_turn}'", flush=True)
                                    continue
                                print(f"[{call_sid}] 🎤 Human: {full_turn}", flush=True)
                                conversation_history.append({"role": "user", "content": full_turn})
                                agent_reply = await ask_llm(
                                    conversation_history, system_prompt,
                                    llm_provider, llm_model
                                )
                                conversation_history.append({"role": "assistant", "content": agent_reply})
                                print(f"[{call_sid}] 🤖 [{llm_provider}] Agent: {agent_reply}", flush=True)
                                await send_audio_to_twilio(agent_reply)

                        except Exception as e:
                            print(f"[{call_sid}] Transcript error: {e}", flush=True)

                await asyncio.gather(send_audio(), receive_transcripts())

        except websockets.exceptions.InvalidStatus as e:
            print(f"[{call_sid}] ❌ Deepgram rejected: {e}", flush=True)
        except Exception as e:
            print(f"[{call_sid}] Deepgram error: {type(e).__name__}: {e}", flush=True)

    await asyncio.gather(receive_from_twilio(), stream_to_deepgram())
    print(f"[{call_sid}] Pipeline finished", flush=True)