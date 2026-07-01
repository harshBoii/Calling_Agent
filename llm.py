"""LLM clients, greeting generation, and provider routing."""

import asyncio
import re

import anthropic
import google.generativeai as genai
from groq import AsyncGroq
from openai import AsyncOpenAI
from sarvamai import SarvamAI 

from config import (
    ANTHROPIC_API_KEY,
    DEFAULT_LLM_MODELS,
    DEFAULT_LLM_PROVIDER,
    GEMINI_API_KEY,
    GROQ_API_KEY,
    OPENAI_API_KEY,
    QUESTIONS_TO_ASK,
    SARVAM_API_KEY,
)
from agent_config import get_discovery_stage_goal, get_greeting_stage_goal
from campaign_vertical import greeting_instructions, normalize_campaign_vertical, questions_generation_task

groq_client = AsyncGroq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
claude_client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

# SarvamAI SDK — keep for TTS/STT/translation endpoints
sarvam_client = SarvamAI(api_subscription_key=SARVAM_API_KEY) if SARVAM_API_KEY else None


if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

LLM_FALLBACK_REPLY = "Sorry, give me just a moment."
CLAUDE_CALL_CONNECTED_CUE = "(The outbound call has connected.)"
CLAUDE_SILENCE_USER_CUE = (
    "(The user has not spoken yet — check if they are still on the line.)"
)


def _claude_temperature_deprecated(model: str) -> bool:
    """Newer Claude models reject explicit temperature (use model default)."""
    m = (model or "").lower()
    return "sonnet-5" in m or m.startswith("claude-opus-4-")


async def _claude_messages_create(**kwargs):
    """Create Claude message; omit temperature when the model rejects it."""
    if _claude_temperature_deprecated(kwargs.get("model", "")):
        kwargs = {k: v for k, v in kwargs.items() if k != "temperature"}
    return await claude_client.messages.create(**kwargs)


def prepare_claude_messages(
    conversation_history: list,
    *,
    trailing_user_cue: str | None = None,
) -> list[dict]:
    """
    Anthropic requires alternating roles, starting with user, ending with user
    before the model's reply. Merge consecutive same-role turns in place.
    """
    msgs: list[dict] = []
    for item in conversation_history:
        role = item.get("role")
        content = (item.get("content") or "").strip()
        if role not in ("user", "assistant") or not content:
            continue
        if msgs and msgs[-1]["role"] == role:
            msgs[-1]["content"] = f"{msgs[-1]['content']}\n\n{content}"
        else:
            msgs.append({"role": role, "content": content})
    if not msgs:
        msgs.append({"role": "user", "content": CLAUDE_CALL_CONNECTED_CUE})
    elif msgs[0]["role"] != "user":
        msgs.insert(0, {"role": "user", "content": CLAUDE_CALL_CONNECTED_CUE})
    if msgs[-1]["role"] != "user":
        msgs.append({
            "role": "user",
            "content": trailing_user_cue or "Please continue the conversation.",
        })
    return msgs


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
    response = sarvam_client.chat.completions(
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

    stage_goal = get_greeting_stage_goal(cfg.get("agent_config"))
    goal_line = f"\n- Stage goal: {stage_goal}" if stage_goal else ""
    vertical = normalize_campaign_vertical(cfg.get("campaign_type"))
    greeting_extra = greeting_instructions(vertical, cfg)
    call_kind = {
        "SALES": "outbound sales call",
        "COLLECTIONS": "account collections call",
        "REMINDER": "appointment reminder call",
        "SURVEY": "brief feedback/NPS call",
        "RENEWAL": "renewal call",
    }.get(vertical, "outbound call")

    lead_name = (cfg.get("name") or "").strip()
    collections_note = ""
    if vertical == "COLLECTIONS" and lead_name:
        collections_note = (
            f"\n- Collections: you MUST ask \"Am I speaking with {lead_name}?\" "
            f"using that exact name — not \"the right person\"."
        )

    prompt = f"""You are making an {call_kind} on behalf of {cfg['company']}.

Generate a warm, natural opening line for a phone call. Respond with text written only in {cfg['language']}:
- Introduce yourself as {cfg['agent_name']} from {cfg['company']}
{greeting_extra}
- Sound like a real human — not scripted or robotic
- Be MAX 1-3 short sentences total ,keep the greeting short inturn increase the chances of lead to listen and understand the pitch.
- Speak in {cfg['language']}{goal_line}{collections_note}

Lead name: {lead_name or "(not provided)"}
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
        )
        return resp.choices[0].message.content.strip()

    if p == "claude" and claude_client:
        print("[GREETING] Using Claude", flush=True)
        resp = await _claude_messages_create(
            model=llm_model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=4000,
            temperature=0.9,
        )
        return resp.content[0].text.strip()

    if p == "gemini" and GEMINI_API_KEY:
        print("[GREETING] Using Gemini", flush=True)
        gemini_model = genai.GenerativeModel(llm_model)
        resp = await asyncio.to_thread(gemini_model.generate_content, prompt)
        return resp.text.strip()

    if p == "sarvam" and sarvam_client:
        print("[GREETING] Using Sarvam", flush=True)
        text = await _sarvam_call(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.9,
            max_tokens=4000,
        )
        if text:
            return text
        print("[GREETING] Sarvam returned empty, using template", flush=True)

    print(f"[GREETING] No LLM path for provider={p!r} (missing client or key?), using template", flush=True)
    return cfg["opening_greeting"]


def _normalize_questions(text: str) -> list[str]:
    lines = []
    for raw in (text or "").splitlines():
        s = raw.strip()
        if not s:
            continue
        # Strip bullets / numbering.
        s = re.sub(r"^\s*(?:[-*]|\d+[.)])\s*", "", s).strip()
        if not s:
            continue
        # Ensure it looks like a question.
        if not s.endswith("?"):
            s = s + "?"
        lines.append(s)
    # Deduplicate while preserving order.
    seen = set()
    out: list[str] = []
    for q in lines:
        key = q.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(q)
    return out


async def generate_questions_to_ask(cfg: dict, provider: str | None = None) -> str:
    """
    Generate 2-4 short discovery questions for a 2-minute sales call.
    Returns a numbered string (\"1. ...\\n2. ...\").
    """
    p = (provider or cfg.get("llm_provider") or DEFAULT_LLM_PROVIDER).strip().lower()
    llm_model = cfg.get("llm_model") or DEFAULT_LLM_MODELS.get(
        p, DEFAULT_LLM_MODELS[DEFAULT_LLM_PROVIDER]
    )

    discovery_goal = get_discovery_stage_goal(cfg.get("agent_config"))
    ac = cfg.get("agent_config") or {}
    kg = ac.get("knowledgeGrounding") or {}
    blacklist = kg.get("claimBlacklist") or []
    blacklist_note = ""
    if blacklist:
        blacklist_note = f"\n- Do NOT ask about: {', '.join(blacklist)}"

    goal_note = f"\n- Discovery goal: {discovery_goal}" if discovery_goal else ""
    vertical = normalize_campaign_vertical(cfg.get("campaign_type"))
    task = questions_generation_task(vertical, cfg.get("language") or "English")

    prompt = f"""You are drafting questions for a phone call.

Context:
- Language: {cfg.get('language')}
- Company: {cfg.get('company')}
- Offer: {cfg.get('perks_of_product')}
- Lead context (use subtly): {cfg.get('info_about_lead')}{goal_note}{blacklist_note}

Task:
- {task}
- Avoid sounding robotic. Avoid personal/sensitive data questions.

Output format:
Return ONLY the questions, one per line. No numbering, no bullets, no extra text."""

    try:
        if p == "groq" and groq_client:
            resp = await groq_client.chat.completions.create(
                model=llm_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.6,
                max_tokens=160,
            )
            raw = resp.choices[0].message.content.strip()
        elif p == "openai" and openai_client:
            resp = await openai_client.chat.completions.create(
                model=llm_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.6,
            )
            raw = resp.choices[0].message.content.strip()
        elif p == "claude" and claude_client:
            resp = await _claude_messages_create(
                model=llm_model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=500,
                temperature=0.6,
            )
            raw = resp.content[0].text.strip()
        elif p == "gemini" and GEMINI_API_KEY:
            gemini_model = genai.GenerativeModel(llm_model)
            resp = await asyncio.to_thread(gemini_model.generate_content, prompt)
            raw = (resp.text or "").strip()
        else:
            raw = ""

        qs = _normalize_questions(raw)
        if len(qs) < 2:
            raise ValueError(f"Too few questions generated: {qs}")
        qs = qs[:4]
        return "\n".join(f"{i+1}. {q}" for i, q in enumerate(qs))
    except Exception as e:
        print(f"[QUESTIONS] Generation failed ({p}): {e}", flush=True)
        return (cfg.get("questions_to_ask") or QUESTIONS_TO_ASK).strip()


def _log_llm_prompt(
    provider: str,
    model: str,
    system_prompt: str,
    conversation_history: list,
) -> None:
    print(f"[LLM/{provider}] model={model}", flush=True)
    print(f"[LLM/{provider}] system_prompt:\n{system_prompt}", flush=True)
    for i, msg in enumerate(conversation_history):
        role = msg.get("role", "?")
        content = msg.get("content", "")
        print(f"[LLM/{provider}] message[{i}] role={role}:\n{content}", flush=True)


async def ask_llm(
    conversation_history: list,
    system_prompt: str,
    provider: str,
    model: str,
    *,
    trailing_user_cue: str | None = None,
) -> str:
    """
    Unified LLM caller. Routes to the right provider.
    provider: "groq" | "openai" | "claude" | "gemini" | "sarvam"
    """
    provider = (provider or DEFAULT_LLM_PROVIDER).strip().lower()
    model = model or DEFAULT_LLM_MODELS.get(
        provider, DEFAULT_LLM_MODELS[DEFAULT_LLM_PROVIDER]
    )
    _log_llm_prompt(provider, model, system_prompt, conversation_history)
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
            claude_messages = prepare_claude_messages(
                conversation_history, trailing_user_cue=trailing_user_cue
            )
            response = await _claude_messages_create(
                model=model,
                system=system_prompt,
                messages=claude_messages,
                max_tokens=4000,
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
            if not sarvam_client:
                raise ValueError("SARVAM_API_KEY not set")

            messages = [{"role": "system", "content": system_prompt}]
            text = await _sarvam_call(
                model=model,
                messages=messages,
                temperature=0.7,
                max_tokens=4000,
            )

            if not text:
                raise ValueError("Sarvam returned empty response")
            return text

        raise ValueError(
            f"Unknown LLM provider: '{provider}'. Use groq | openai | claude | gemini | sarvam"
        )

    except Exception as e:
        import traceback

        print(f"[LLM/{provider}] Error: {type(e).__name__}: {e}", flush=True)
        print(traceback.format_exc(), flush=True)
        return LLM_FALLBACK_REPLY


# --- Transcript analysis (webhook): needs room for structured JSON, not 150 tokens ---
_ANALYSIS_MAX_TOKENS = 2048
_ANALYSIS_TEMPERATURE = 0.2


async def ask_llm_for_analysis(
    conversation_history: list,
    system_prompt: str,
    provider: str,
    model: str,
) -> str:
    """
    Same routing as ask_llm but with high max_tokens and low temperature for
    strict JSON. Raises on API failures (unlike ask_llm, which never raises).
    """
    p = (provider or "").strip().lower()
    model = model or DEFAULT_LLM_MODELS.get(
        p, DEFAULT_LLM_MODELS[DEFAULT_LLM_PROVIDER]
    )

    if p == "groq":
        if not groq_client:
            raise ValueError("GROQ_API_KEY not set")
        response = await groq_client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system_prompt}] + conversation_history,
            temperature=_ANALYSIS_TEMPERATURE,
            max_tokens=_ANALYSIS_MAX_TOKENS,
        )
        return (response.choices[0].message.content or "").strip()

    if p == "openai":
        if not openai_client:
            raise ValueError("OPENAI_API_KEY not set")
        response = await openai_client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system_prompt}] + conversation_history,
            temperature=_ANALYSIS_TEMPERATURE,
            max_tokens=_ANALYSIS_MAX_TOKENS,
        )
        return (response.choices[0].message.content or "").strip()

    if p == "claude":
        if not claude_client:
            raise ValueError("ANTHROPIC_API_KEY not set")
        response = await _claude_messages_create(
            model=model,
            system=system_prompt,
            messages=conversation_history,
            max_tokens=_ANALYSIS_MAX_TOKENS,
            temperature=_ANALYSIS_TEMPERATURE,
        )
        return response.content[0].text.strip()

    if p == "gemini":
        if not GEMINI_API_KEY:
            raise ValueError("GEMINI_API_KEY not set")
        gemini_model = genai.GenerativeModel(
            model_name=model,
            system_instruction=system_prompt,
        )
        gem_hist = []
        for msg in conversation_history:
            role = "user" if msg["role"] == "user" else "model"
            gem_hist.append({"role": role, "parts": [msg["content"]]})
        chat = gemini_model.start_chat(history=gem_hist[:-1] if gem_hist else [])
        last_msg = gem_hist[-1]["parts"][0] if gem_hist else ""
        gen_cfg = {
            "max_output_tokens": _ANALYSIS_MAX_TOKENS,
            "temperature": _ANALYSIS_TEMPERATURE,
        }
        response = await asyncio.to_thread(
            chat.send_message, last_msg, generation_config=gen_cfg
        )
        return (response.text or "").strip()

    if p == "sarvam":
        if not sarvam_client:
            raise ValueError("SARVAM_API_KEY not set")
        messages = [{"role": "system", "content": system_prompt}] + list(conversation_history)
        text = await _sarvam_call(
            model=model,
            messages=messages,
            temperature=_ANALYSIS_TEMPERATURE,
            max_tokens=_ANALYSIS_MAX_TOKENS,
        )
        if not text:
            raise ValueError("Sarvam returned empty response")
        return text.strip()

    raise ValueError(
        f"Unknown LLM provider: '{provider}'. Use groq | openai | claude | gemini | sarvam"
    )