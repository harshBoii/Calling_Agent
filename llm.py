"""LLM clients, greeting generation, and provider routing."""

import asyncio

import anthropic
import google.generativeai as genai
from groq import AsyncGroq
from openai import AsyncOpenAI
from sarvamai import SarvamAI

from config import (
    ANTHROPIC_API_KEY,
    GEMINI_API_KEY,
    GROQ_API_KEY,
    OPENAI_API_KEY,
    SARVAM_API_KEY,
)

groq_client = AsyncGroq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
claude_client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None
sarvam_client = SarvamAI(api_subscription_key=SARVAM_API_KEY) if SARVAM_API_KEY else None
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)


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

    if groq_client:
        resp = await groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.9,
            max_tokens=120,
        )
        return resp.choices[0].message.content.strip()

    if openai_client:
        resp = await openai_client.chat.completions.create(
            model="gpt-5.4-nano",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.9,
            max_tokens=120,
        )
        return resp.choices[0].message.content.strip()

    if sarvam_client:
        resp = await asyncio.to_thread(
            sarvam_client.chat.completions,
            model="sarvam-30b",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.9,
            top_p=1,
            max_tokens=120,
        )
        return resp.choices[0].message.content.strip()

    if GEMINI_API_KEY:
        gemini_model = genai.GenerativeModel("gemini-3-flash-preview")
        resp = await asyncio.to_thread(gemini_model.generate_content, prompt)
        return resp.text.strip()

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
            chat = gemini_model.start_chat(history=gemini_history[:-1] if gemini_history else [])
            last_msg = gemini_history[-1]["parts"][0] if gemini_history else ""
            response = await asyncio.to_thread(chat.send_message, last_msg)
            return response.text.strip()

        if provider == "sarvam":
            if not sarvam_client:
                raise ValueError("SARVAM_API_KEY not set")
            messages = [{"role": "system", "content": system_prompt}] + conversation_history
            response = await asyncio.to_thread(
                sarvam_client.chat.completions,
                model=model,
                messages=messages,
                temperature=0.7,
                top_p=1,
                max_tokens=150,
            )
            return response.choices[0].message.content.strip()

        raise ValueError(
            f"Unknown LLM provider: '{provider}'. Use groq | openai | claude | gemini | sarvam"
        )

    except Exception as e:
        print(f"[LLM/{provider}] Error: {e}", flush=True)
        return "Sorry, give me just a moment."
