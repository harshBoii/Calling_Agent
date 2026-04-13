"""LLM clients, greeting generation, and provider routing."""

import asyncio

import anthropic
import google.generativeai as genai
from groq import AsyncGroq
from openai import AsyncOpenAI
from sarvamai import SarvamAI  # still used for TTS/STT/translation

from config import (
    ANTHROPIC_API_KEY,
    DEFAULT_LLM_MODELS,
    DEFAULT_LLM_PROVIDER,
    GEMINI_API_KEY,
    GROQ_API_KEY,
    OPENAI_API_KEY,
    SARVAM_API_KEY,
)

groq_client = AsyncGroq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
claude_client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

# SarvamAI SDK — keep for TTS/STT/translation endpoints
sarvam_client = SarvamAI(api_subscription_key=SARVAM_API_KEY) if SARVAM_API_KEY else None

# ✅ Sarvam Chat via OpenAI-compatible client (proven by curl test)
# Uses Bearer auth, same /v1/chat/completions endpoint
# sarvam_chat_client = (
#     AsyncOpenAI(
#         base_url="https://api.sarvam.ai/v1",
#         api_key=SARVAM_API_KEY,
#     )
#     if SARVAM_API_KEY
#     else None
# )

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)


async def _sarvam_call(
    *, model: str, messages: list, temperature: float, max_tokens: int
) -> str:
    """
    Sarvam chat via OpenAI-compatible client.
    
    Key notes:
    - sarvam-105b is a reasoning model: it thinks before replying.
    - reasoning_content tokens count toward max_tokens.
    - Use at least 500 tokens so reasoning doesn't exhaust the budget.
    - content field holds the user-facing reply (safe for Twilio TTS).
    """
    if not sarvam_client:
        return ""
    response = await sarvam_client.chat.completions(
        model=model,
        messages=messages,
        temperature=temperature,
        top_p=1,
        max_tokens=max_tokens,
    )
    content = response.choices[0].message.content or ""
    return content.strip()


async def generate_opening_greeting(cfg: dict, provider: str | None = None) -> str:
    """
    Generates a natural, dynamic opening line using a fast LLM.
    Runs once at call-creation time, before Twilio connects.
    """
    p = (provider or cfg.get("llm_provider") or DEFAULT_LLM_PROVIDER).strip().lower()
    llm_model = cfg.get("llm_model") or DEFAULT_LLM_MODELS.get(
        p, DEFAULT_LLM_MODELS[DEFAULT_LLM_PROVIDER]
    )

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

    if p == "groq" and groq_client:
        print("[GREETING] Using Groq", flush=True)
        resp = await groq_client.chat.completions.create(
            model=llm_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.9,
            max_tokens=120,
        )
        return resp.choices[0].message.content.strip()

    if p == "openai" and openai_client:
        print("[GREETING] Using OpenAI", flush=True)
        resp = await openai_client.chat.completions.create(
            model=llm_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.9,
            max_tokens=120,
        )
        return resp.choices[0].message.content.strip()

    if p == "claude" and claude_client:
        print("[GREETING] Using Claude", flush=True)
        resp = await claude_client.messages.create(
            model=llm_model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=120,
            temperature=0.9,
        )
        return resp.content[0].text.strip()

    if p == "gemini" and GEMINI_API_KEY:
        print("[GREETING] Using Gemini", flush=True)
        gemini_model = genai.GenerativeModel(llm_model)
        resp = await asyncio.to_thread(gemini_model.generate_content, prompt)
        return resp.text.strip()

    if p == "sarvam" and sarvam_chat_client:
        print("[GREETING] Using Sarvam", flush=True)
        text = await _sarvam_call(
            model=llm_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.9,
            max_tokens=500,
        )
        if text:
            return text
        print("[GREETING] Sarvam returned empty, using template", flush=True)

    print(f"[GREETING] No LLM path for provider={p!r} (missing client or key?), using template", flush=True)
    return cfg["opening_greeting"]


async def ask_llm(
    conversation_history: list,
    system_prompt: str,
    provider: str,
    model: str,
) -> str:
    """
    Unified LLM caller. Routes to the right provider.
    provider: "groq" | "openai" | "claude" | "gemini" | "sarvam"
    """
    try:
        if provider == "groq":
            if not groq_client:
                raise ValueError("GROQ_API_KEY not set")
            response = await groq_client.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": system_prompt}] + conversation_history,
                temperature=0.7,
                max_tokens=150,
            )
            return response.choices[0].message.content.strip()

        if provider == "openai":
            if not openai_client:
                raise ValueError("OPENAI_API_KEY not set")
            response = await openai_client.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": system_prompt}] + conversation_history,
                temperature=0.7,
                max_tokens=150,
            )
            return response.choices[0].message.content.strip()

        if provider == "claude":
            if not claude_client:
                raise ValueError("ANTHROPIC_API_KEY not set")
            response = await claude_client.messages.create(
                model=model,
                system=system_prompt,
                messages=conversation_history,
                max_tokens=150,
            )
            return response.content[0].text.strip()

        if provider == "gemini":
            if not GEMINI_API_KEY:
                raise ValueError("GEMINI_API_KEY not set")
            gemini_model = genai.GenerativeModel(
                model_name=model,
                system_instruction=system_prompt,
            )
            gemini_history = []
            for msg in conversation_history:
                role = "user" if msg["role"] == "user" else "model"
                gemini_history.append({"role": role, "parts": [msg["content"]]})
            chat = gemini_model.start_chat(
                history=gemini_history[:-1] if gemini_history else []
            )
            last_msg = gemini_history[-1]["parts"][0] if gemini_history else ""
            response = await asyncio.to_thread(chat.send_message, last_msg)
            return response.text.strip()

        if provider == "sarvam":
            if not sarvam_chat_client:
                raise ValueError("SARVAM_API_KEY not set")

            # Sarvam-105b requires conversation to start with a user turn.
            # The opening greeting is pushed as assistant[0] in history — drop it.
            filtered = list(conversation_history)
            while filtered and filtered[0]["role"] == "assistant":
                filtered.pop(0)

            messages = [{"role": "system", "content": system_prompt}] + filtered
            text = await _sarvam_call(
                model=model,
                messages=messages,
                temperature=0.7,
                max_tokens=2000,
            )

            if not text:
                raise ValueError("Sarvam returned empty response")
            return text

        raise ValueError(
            f"Unknown LLM provider: '{provider}'. Use groq | openai | claude | gemini | sarvam"
        )

    except Exception as e:
        print(f"[LLM/{provider}] Error: {e}", flush=True)
        return "Sorry, give me just a moment."