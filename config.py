"""Environment, API constants, STT routing, and call configuration."""

import os
from dotenv import load_dotenv

load_dotenv()

# ─── Twilio ───────────────────────────────────────────────────────────────────
# TWILIO_ACCOUNT_SID = os.environ["TWILIO_ACCOUNT_SID"]
# TWILIO_AUTH_TOKEN = os.environ["TWILIO_AUTH_TOKEN"]
# TWILIO_PHONE_NUMBER = os.environ["TWILIO_PHONE_NUMBER"]

TELNYX_API_KEY        = os.environ["TELNYX_API_KEY"].strip()
TELNYX_PHONE_NUMBER   = os.environ["TELNYX_PHONE_NUMBER"].strip()
TELNYX_CONNECTION_ID  = os.environ["TELNYX_CONNECTION_ID"].strip()  # your Telnyx app UUID

PUBLIC_BASE_URL = os.environ["PUBLIC_BASE_URL"].rstrip("/")
SARVAM_API_KEY = os.environ.get("SARVAM_API_KEY", "")

# ─── STT ──────────────────────────────────────────────────────────────────────
DEEPGRAM_API_KEY = os.environ["DEEPGRAM_API_KEY"]

# ─── TTS ──────────────────────────────────────────────────────────────────────
ELEVENLABS_API_KEY = os.environ["ELEVENLABS_API_KEY"]
ELEVENLABS_VOICE_ID = os.environ["ELEVENLABS_VOICE_ID"]

# ─── LLM keys (only set the ones you use) ────────────────────────────────────
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# ─── Default LLM provider + models ───────────────────────────────────────────
DEFAULT_LLM_PROVIDER = "claude"  # groq | openai | claude | gemini | sarvam

DEFAULT_LLM_MODELS = {
    "groq": "llama-3.3-70b-versatile",
    "openai": "gpt-5.4-nano",
    "claude": "claude-haiku-4-5-20251001",
    "gemini": "gemini-3-flash-preview",
    "sarvam": "sarvam-105b",
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
    "kn": "kn-IN",  # Kannada
    "ta": "ta-IN",  # Tamil
    "te": "te-IN",  # Telugu
    "ml": "ml-IN",  # Malayalam
    "mr": "mr-IN",  # Marathi
    "gu": "gu-IN",  # Gujarati
    "bn": "bn-IN",  # Bengali
    "pa": "pa-IN",  # Punjabi
    "od": "od-IN",  # Odia
    "en": "en-IN",  # English (fallback if forced via stt_provider)
    "hi": "hi-IN",  # Hindi  (fallback if forced via stt_provider)
}


def to_sarvam_lang(dg_lang: str) -> str:
    """Convert a Deepgram-style language code (e.g. 'kn') to Sarvam's BCP-47 code (e.g. 'kn-IN')."""
    base = dg_lang.split("-")[0].lower()
    return _DG_TO_SARVAM_LANG.get(base, f"{base}-IN")


# ─── ElevenLabs ───────────────────────────────────────────────────────────────
# Telnyx bidirectional playback expects base64-encoded MP3 media payloads.
ELEVENLABS_STREAM_PATH = "/stream?output_format=mp3_44100_128"
ELEVENLABS_MODEL = "eleven_flash_v2_5"


def elevenlabs_stream_url(voice_id: str) -> str:
    return f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}{ELEVENLABS_STREAM_PATH}"


def deepgram_ws_url(lang: str) -> str:
    return f"{DEEPGRAM_URL_BASE}&language={lang}"


# ─── Agent defaults ───────────────────────────────────────────────────────────
LANGUAGE = "English"
NAME = "Samaaira"
COMPANY = "Immortell Company"
PRODUCT = "GEO optimization services"
PERKS_OF_PRODUCT = "10% off on the first month"
INFO_ABOUT_LEAD = (
    "The Lead is a poor guy with a low income and is looking for a cheap way to grow his business."
)
MIN_WORDS_TO_RESPOND = 3

QUESTIONS_TO_ASK = """

1. How are you doing

"""

AGENT_NAME = "Annie"
AGENT_ROLE = "warm and persuasive sales representative"


SYSTEM_PROMPT_TEMPLATE = """You are {Agent_Name} , a {AGENT_ROLE} representative calling on behalf of {COMPANY} RESPOND WITH text written only in {LANGUAGE} AND IN HUMANE WAY , IT'S OK TO BE A LITTLE MESSY LIKE HUMANS ARE.

## Your Goal
Sell {PRODUCT} to {NAME}. The offer includes {PERKS_OF_PRODUCT}. Close the call with either a confirmed interest or a scheduled follow-up.

## What You Know About This Lead
{INFO_ABOUT_LEAD}
Use this intel subtly — don't reference it directly. Let it shape HOW you pitch, not WHAT you say.

## Voice Call Rules (Critical)
- Speak in {LANGUAGE} only , keep the conversation natural ,engaging and concise.
- Max 1-2 sentences per response — this is a phone call, not an email

## Conversation Flow
1. start by asking permission to ask questions tied to their situation → Bridge → connect their pain to your product naturallyOffer → present {PRODUCT} + {PERKS_OF_PRODUCT} as the solution
You have to ask these questions to the lead:{QUESTIONS_TO_ASK}

## Tone
Sound like a real person having a real conversation — with natural pauses and occasional light humor if the vibe allows."""

OPENING_GREETING_TEMPLATE = (
    "Hi, {NAME} , This is {Agent_Name} calling from {COMPANY}. "
    "I'll keep this quick — I'm reaching out to tell you an offer on {PRODUCT}, "
    "we have {PERKS_OF_PRODUCT} for you , Is this a good time to talk for two minutes?"
)


def _format_vars(*, language, name, company, product, perks_of_product, info_about_lead) -> dict:
    return {
        "LANGUAGE": language,
        "NAME": name,
        "COMPANY": company,
        "PRODUCT": product,
        "PERKS_OF_PRODUCT": perks_of_product,
        "INFO_ABOUT_LEAD": info_about_lead,
    }


def build_call_config(body: dict | None) -> dict:
    print(f"[BUILD_CALL_CONFIG] Body: {body}", flush=True)
    b = body or {}
    # Accept both snake_case (backend) and camelCase (frontend UI) inputs.
    language = b.get("language") or b.get("languageMode") or LANGUAGE
    dg_language = b.get("deepgram_language") or b.get("deepgramLanguage") or "en"
    el_model = b.get("elevenlabs_model") or b.get("voiceMode") or ELEVENLABS_MODEL
    name = b.get("name", NAME)
    company = b.get("company", COMPANY)
    product = b.get("product", PRODUCT)
    perks = b.get("perks_of_product", PERKS_OF_PRODUCT)
    lead_info = b.get("info_about_lead", INFO_ABOUT_LEAD)
    voice_id = b.get("voiceId") or ELEVENLABS_VOICE_ID
    use_sarvam_tts = b.get("use_sarvam_tts", False)
    sarvam_speaker = b.get("sarvam_speaker", "rohan")
    agent_name = b.get("agent_name") or b.get("AGENT_NAME") or AGENT_NAME
    agent_role = b.get("agent_role") or b.get("AGENT_ROLE") or AGENT_ROLE

    q_raw = b.get("questions_to_ask") or b.get("QUESTIONS_TO_ASK") or b.get("questions")
    if isinstance(q_raw, list):
        questions_to_ask = "\n".join(
            f"{i+1}. {str(q).strip()}" for i, q in enumerate(q_raw) if str(q).strip()
        )
    elif isinstance(q_raw, str) and q_raw.strip():
        questions_to_ask = q_raw.strip()
    else:
        questions_to_ask = QUESTIONS_TO_ASK.strip()

    provider = b.get("llm_provider", DEFAULT_LLM_PROVIDER).lower()
    model = b.get(
        "llm_model",
        DEFAULT_LLM_MODELS.get(provider, DEFAULT_LLM_MODELS[DEFAULT_LLM_PROVIDER]),
    )

    # ── STT provider: "auto" resolves based on language ──────────────────────
    stt_raw = b.get("stt_provider", "auto").lower()  # "auto" | "deepgram" | "sarvam"
    stt_provider = _auto_select_stt(dg_language) if stt_raw == "auto" else stt_raw

    ctx = _format_vars(
        language=language,
        name=name,
        company=company,
        product=product,
        perks_of_product=perks,
        info_about_lead=lead_info,
    )
    ctx.update(
        {
            "Agent_Name": agent_name,
            "AGENT_ROLE": agent_role,
            "QUESTIONS_TO_ASK": questions_to_ask,
        }
    )

    system_prompt = b.get("system_prompt") or SYSTEM_PROMPT_TEMPLATE.format(**ctx)
    opening_greeting = b.get("opening_greeting") or OPENING_GREETING_TEMPLATE.format(**ctx)
    res = {
        "language": language,
        "deepgram_language": dg_language,
        "stt_provider": stt_provider,
        "elevenlabs_model": el_model,
        "voice_id": voice_id,
        "name": name,
        "company": company,
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
        "use_sarvam_tts": use_sarvam_tts,
        "sarvam_speaker": sarvam_speaker,
    }
    print(f"Config: {res}")

    return res
