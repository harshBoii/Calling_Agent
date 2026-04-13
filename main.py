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
from sarvamai import AsyncSarvamAI   


load_dotenv()

# ─── Twilio ───────────────────────────────────────────────────────────────────
TWILIO_ACCOUNT_SID  = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN   = os.environ["TWILIO_AUTH_TOKEN"]
TWILIO_PHONE_NUMBER = os.environ["TWILIO_PHONE_NUMBER"]
PUBLIC_BASE_URL     = os.environ["PUBLIC_BASE_URL"].rstrip("/")
SARVAM_API_KEY      = os.environ.get("SARVAM_API_KEY", "")


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
DEFAULT_LLM_PROVIDER = "claude"   # groq | openai | claude | gemini

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
)

# ─── STT Provider Routing ──────────────────────────────────────────────────────

# Languages routed to Deepgram (strong Nova-3 models exist for these)
_DEEPGRAM_LANGS = {"en"}

def _auto_select_stt(deepgram_language: str) -> str:
    """
    Returns 'deepgram' for English and Hindi, 'sarvam' for all other
    Indian regional languages (ta, te, kn, ml, mr, gu, bn, pa, od, etc.)
    """
    lang_base = deepgram_language.split("-")[0].lower()
    return "deepgram" if lang_base in _DEEPGRAM_LANGS else "sarvam"

_DG_TO_SARVAM_LANG: dict[str, str] = {
    "kn": "kn-IN",   # Kannada
    "ta": "ta-IN",   # Tamil
    "te": "te-IN",   # Telugu
    "ml": "ml-IN",   # Malayalam
    "mr": "mr-IN",   # Marathi
    "gu": "gu-IN",   # Gujarati
    "bn": "bn-IN",   # Bengali
    "pa": "pa-IN",   # Punjabi
    "od": "od-IN",   # Odia
    "en": "en-IN",   # English (fallback if forced via stt_provider)
    "hi": "hi-IN",   # Hindi  (fallback if forced via stt_provider)
}

def _to_sarvam_lang(dg_lang: str) -> str:
    """Convert a Deepgram-style language code (e.g. 'kn') to Sarvam's BCP-47 code (e.g. 'kn-IN')."""
    base = dg_lang.split("-")[0].lower()
    return _DG_TO_SARVAM_LANG.get(base, f"{base}-IN")

    
# ─── ElevenLabs ───────────────────────────────────────────────────────────────
ELEVENLABS_STREAM_PATH = "/stream?output_format=pcm_8000"
ELEVENLABS_MODEL       = "eleven_flash_v2_5"

def elevenlabs_stream_url(voice_id: str) -> str:
    return f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}{ELEVENLABS_STREAM_PATH}"

def deepgram_ws_url(lang: str) -> str:
    return f"{DEEPGRAM_URL_BASE}&language={lang}"

# ─── Agent defaults ───────────────────────────────────────────────────────────
LANGUAGE          = "English"
NAME              = "Samaaira"
COMPANY           = "Immortell Company"
PRODUCT           = "GEO optimization services"
PERKS_OF_PRODUCT  = "10% off on the first month"
INFO_ABOUT_LEAD   = "The Lead is a poor guy with a low income and is looking for a cheap way to grow his business."
MIN_WORDS_TO_RESPOND = 3

QUESTIONS_TO_ASK = """
1. What is your name?
2. What is your email?
3. What is your phone number?
"""

AGENT_NAME = "Annie"
AGENT_ROLE = "warm and persuasive sales representative"
SYSTEM_PROMPT_TEMPLATE = """You are {Agent_Name} , a {AGENT_ROLE} representative calling on behalf of {COMPANY}.

## Your Goal
Sell {PRODUCT} to {NAME}. The offer includes {PERKS_OF_PRODUCT}. Close the call with either a confirmed interest or a scheduled follow-up.

## What You Know About This Lead
{INFO_ABOUT_LEAD}
Use this intel subtly — don't reference it directly. Let it shape HOW you pitch, not WHAT you say.

## Voice Call Rules (Critical)
- Speak in {LANGUAGE} only , keep the conversation natural ,engaging and concise.
- Max 1-2 sentences per response — this is a phone call, not an email

## Conversation Flow
1. Warm intro → Identify pain point → ask permission and ask questions tied to their situation → Bridge → connect their pain to your product naturallyOffer → present {PRODUCT} + {PERKS_OF_PRODUCT} as the solution
You have to ask these questions to the lead:{QUESTIONS_TO_ASK}

## Tone
Act like the customer is your boss. humbly but professional. Sound like a real person having a real conversation — with natural pauses and occasional light humor if the vibe allows."""

OPENING_GREETING_TEMPLATE = (
    "Hi, {NAME} , This is {Agent_Name} calling from {COMPANY}. "
    "I'll keep this quick — I'm reaching out to tell you an offer on {PRODUCT}, "
    "we have {PERKS_OF_PRODUCT} for you , Is this a good time to talk for two minutes?"
)
# ─── Call config store ────────────────────────────────────────────────────────
pending_call_configs:  dict[str, dict] = {}
call_configs_by_sid:   dict[str, dict] = {}


async def generate_opening_greeting(cfg: dict) -> str:
    """
    Generates a natural, dynamic opening line using a fast LLM.
    Runs once at call-creation time, before Twilio connects.
    """
    prompt = f"""You are making an outbound sales call on behalf of {cfg['company']}.

Generate a warm, natural opening line for a phone call. It should:
- Introduce yourself as {cfg['agent_name']} from {cfg['company']}
- Mention you're calling about {cfg['product']}
- Tease the offer: {cfg['perks_of_product']}
- End with a soft permission question ("Is this a good time?")
- Sound like a real human — not scripted or robotic
- Be MAX 2-3 sentences total
- Speak in {cfg['language']}

Lead context (use subtly to personalize tone, don't state it directly):
{cfg['info_about_lead']}

Output ONLY the spoken greeting text. No quotes, no labels, no explanation."""

    # Always use the fastest available model for greetings — cost and speed matter here
    if groq_client:
        resp = await groq_client.chat.completions.create(
            model       = "llama-3.3-70b-versatile",   # fastest Groq model
            messages    = [{"role": "user", "content": prompt}],
            temperature = 0.9,    # higher = more natural variation per call
            max_tokens  = 120,
        )
        return resp.choices[0].message.content.strip()

    elif openai_client:
        resp = await openai_client.chat.completions.create(
            model       = "gpt-5.4-nano",
            messages    = [{"role": "user", "content": prompt}],
            temperature = 0.9,
            max_tokens  = 120,
        )
        return resp.choices[0].message.content.strip()

    elif GEMINI_API_KEY:
        gemini_model = genai.GenerativeModel("gemini-3-flash-preview")
        resp = await asyncio.to_thread(gemini_model.generate_content, prompt)
        return resp.text.strip()

    else:
        # Fallback to template if no LLM key available
        return cfg["opening_greeting"]

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
    b           = body or {}
    # Accept both snake_case (backend) and camelCase (frontend UI) inputs.
    language    = b.get("language") or b.get("languageMode") or LANGUAGE
    dg_language = b.get("deepgram_language") or b.get("deepgramLanguage") or "en"
    el_model    = b.get("elevenlabs_model") or b.get("voiceMode") or ELEVENLABS_MODEL
    name        = b.get("name", NAME)
    company     = b.get("company", COMPANY)
    product     = b.get("product", PRODUCT)
    perks       = b.get("perks_of_product", PERKS_OF_PRODUCT)
    lead_info   = b.get("info_about_lead", INFO_ABOUT_LEAD)
    voice_id    = b.get("voiceId") or ELEVENLABS_VOICE_ID
    agent_name  = b.get("agent_name") or b.get("AGENT_NAME") or AGENT_NAME
    agent_role  = b.get("agent_role") or b.get("AGENT_ROLE") or AGENT_ROLE

    q_raw = b.get("questions_to_ask") or b.get("QUESTIONS_TO_ASK") or b.get("questions")
    if isinstance(q_raw, list):
        questions_to_ask = "\n".join(f"{i+1}. {str(q).strip()}" for i, q in enumerate(q_raw) if str(q).strip())
    elif isinstance(q_raw, str) and q_raw.strip():
        questions_to_ask = q_raw.strip()
    else:
        questions_to_ask = QUESTIONS_TO_ASK.strip()

    provider    = b.get("llm_provider", DEFAULT_LLM_PROVIDER).lower()
    model       = b.get("llm_model", DEFAULT_LLM_MODELS.get(provider, DEFAULT_LLM_MODELS[DEFAULT_LLM_PROVIDER]))

    # ── STT provider: "auto" resolves based on language ──────────────────────
    stt_raw      = b.get("stt_provider", "auto").lower()   # "auto" | "deepgram" | "sarvam"
    stt_provider = _auto_select_stt(dg_language) if stt_raw == "auto" else stt_raw

    ctx = _format_vars(language=language, name=name, company=company,
                       product=product, perks_of_product=perks, info_about_lead=lead_info)
    ctx.update({
        "Agent_Name": agent_name,
        "AGENT_ROLE": agent_role,
        "QUESTIONS_TO_ASK": questions_to_ask,
    })

    system_prompt    = b.get("system_prompt") or SYSTEM_PROMPT_TEMPLATE.format(**ctx)
    opening_greeting = b.get("opening_greeting") or OPENING_GREETING_TEMPLATE.format(**ctx)
    res={        
        "language": language,
        "deepgram_language": dg_language,
        "stt_provider": stt_provider,          # ← NEW
        "elevenlabs_model": el_model,
        "voice_id": voice_id,
        "name": name, "company": company, 
        "product": product,
        "perks_of_product": perks, 
        "info_about_lead": lead_info,
        "system_prompt": system_prompt, 
        "opening_greeting": opening_greeting,
        "agent_name": agent_name, 
        "agent_role": agent_role,
        "questions_to_ask": questions_to_ask,
        "llm_provider": provider, 
        "llm_model": model,
    }
    print(f"Config: {res}")

    return res

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
    print(f"[OUTBOUND] Body: {body}", flush=True)
    to_number = body.get("to")
    if not to_number:
        raise HTTPException(status_code=400, detail="Missing 'to' number")

    cfg_body = {k: v for k, v in body.items() if k != "to"}
    cfg      = build_call_config(cfg_body)

    # ── Generate dynamic greeting (runs before Twilio dials) ─────────────────
    # If caller provided an opening greeting, respect it (don't overwrite).
    use_dynamic = cfg_body.get("dynamic_greeting", True)   # opt-out via API if needed
    if use_dynamic and not cfg_body.get("opening_greeting"):
        cfg["opening_greeting"] = await generate_opening_greeting(cfg)
        print(f"[GREETING] {cfg['opening_greeting']}", flush=True)

    cfg_token = str(uuid.uuid4())
    pending_call_configs[cfg_token] = cfg

    call = twilio_client.calls.create(
        to=to_number, from_=TWILIO_PHONE_NUMBER,
        url=f"{PUBLIC_BASE_URL}/voice/incoming?cfg={cfg_token}", method="POST"
    )
    print(f"[{call.sid}] Outbound → {to_number} | LLM={cfg['llm_provider']}/{cfg['llm_model']}", flush=True)
    return {
        "call_sid": call.sid,
        "status": call.status,
        "opening_greeting": cfg["opening_greeting"]   # see what was generated
    }

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
    stt_provider     = call_cfg["stt_provider"]   # ← read here, once

    print(f"[{call_sid}] STT: {stt_provider} | LLM: {llm_provider}/{llm_model}", flush=True)

    audio_queue          = asyncio.Queue()
    conversation_history = []
    transcript_buffer    = []
    stream_sid           = None
    agent_speaking       = False

    # ──────────────────────────────────────────────────────────────────────────
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

    # ──────────────────────────────────────────────────────────────────────────
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

    # ──────────────────────────────────────────────────────────────────────────
    # ← sibling to receive_from_twilio, NOT nested inside it
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

    # ──────────────────────────────────────────────────────────────────────────
    # ← sibling to stream_to_deepgram, NOT nested inside it
    async def stream_to_sarvam():
        nonlocal agent_speaking

        if not SARVAM_API_KEY:
            print(f"[{call_sid}] ❌ SARVAM_API_KEY not set", flush=True)
            return

        sarvam_lang       = _to_sarvam_lang(call_cfg["deepgram_language"])
        sarvam_client     = AsyncSarvamAI(api_subscription_key=SARVAM_API_KEY)
        PCM_BUFFER_TARGET = 3200   # 200ms @ 8kHz PCM16

        print(f"[{call_sid}] Connecting to Sarvam STT (lang={sarvam_lang})…", flush=True)

        try:
            async with sarvam_client.speech_to_text_streaming.connect(
                model                = "saaras:v3",
                mode                 = "transcribe",
                language_code        = sarvam_lang,
                sample_rate          = 8000,
                input_audio_codec    = "pcm_s16le",
                high_vad_sensitivity = True,
                vad_signals          = True,
            ) as ws:
                print(f"[{call_sid}] Sarvam STT connected ✅", flush=True)

                async def send_audio():
                    pcm_buffer = bytearray()
                    while True:
                        chunk = await audio_queue.get()
                        if chunk is None:
                            if pcm_buffer:
                                audio_b64 = base64.b64encode(bytes(pcm_buffer)).decode()
                                await ws.transcribe(
                                    audio=audio_b64,
                                    encoding="audio/wav",   # ✅ fixed
                                    sample_rate=8000,
                                )
                            break
                        pcm_chunk = audioop.ulaw2lin(chunk, 2)
                        pcm_buffer.extend(pcm_chunk)
                        if len(pcm_buffer) >= PCM_BUFFER_TARGET:
                            audio_b64 = base64.b64encode(bytes(pcm_buffer)).decode()
                            await ws.transcribe(
                                audio=audio_b64,
                                encoding="audio/wav",       # ✅ fixed
                                sample_rate=8000,
                            )
                            pcm_buffer.clear()

                async def receive_transcripts():
                    nonlocal agent_speaking
                    async for message in ws:
                        try:
                            msg_type = getattr(message, "type", None) or ""

                            # ── Barge-in: human started speaking while agent is talking ──────
                            if msg_type == "speech_start" and agent_speaking:
                                agent_speaking = False
                                print(f"[{call_sid}] ⚡ Human interrupted agent (Sarvam)", flush=True)
                                try:
                                    await websocket.send_text(json.dumps({
                                        "event": "clear", "streamSid": stream_sid
                                    }))
                                except Exception:
                                    pass

                            # ── Final transcript arrives in "data" events ────────────────────
                            elif msg_type == "data":
                                data_obj   = getattr(message, "data", None)
                                transcript = (getattr(data_obj, "transcript", None) or "").strip()

                                if not transcript:
                                    continue

                                print(f"[{call_sid}] [SARVAM FINAL ✅] {transcript}", flush=True)

                                if len(transcript.split()) < MIN_WORDS_TO_RESPOND:
                                    print(f"[{call_sid}] ⏭ Skipping short turn: '{transcript}'", flush=True)
                                    continue

                                # Each "data" event is already a complete utterance — fire LLM directly
                                print(f"[{call_sid}] 🎤 Human: {transcript}", flush=True)
                                conversation_history.append({"role": "user", "content": transcript})
                                agent_reply = await ask_llm(
                                    conversation_history, system_prompt,
                                    llm_provider, llm_model,
                                )
                                conversation_history.append({"role": "assistant", "content": agent_reply})
                                print(f"[{call_sid}] 🤖 [{llm_provider}] Agent: {agent_reply}", flush=True)
                                await send_audio_to_twilio(agent_reply)

                        except Exception as e:
                            print(f"[{call_sid}] Sarvam transcript error: {e}", flush=True)
                await asyncio.gather(send_audio(), receive_transcripts())

        except Exception as e:
            print(f"[{call_sid}] Sarvam STT error: {type(e).__name__}: {e}", flush=True)

    # ── Route to the correct STT pipeline ─────────────────────────────────────
    # ← at the bottom of media_stream, after ALL 4 functions are defined
    stt_task = stream_to_deepgram if stt_provider == "deepgram" else stream_to_sarvam
    await asyncio.gather(receive_from_twilio(), stt_task())
    print(f"[{call_sid}] Pipeline finished", flush=True)