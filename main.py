import json
import re
import time
import uuid
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from arq import create_pool
from arq.connections import RedisSettings
from fastapi import FastAPI, HTTPException, Request, WebSocket

from arq_jobs import cfg_key, set_call_status, start_key, status_key
from config import (
    CAMPAIGN_QUEUE,
    CFG_TTL_SEC,
    ON_DEMAND_DEADLINE_SEC,
    ONCALL_QUEUE,
    REDIS_URL,
    SYSTEM_PROMPT_TEMPLATE,
    build_call_config,
    prepend_previous_chat_context,
)
from llm import generate_opening_greeting, generate_questions_to_ask
from media_stream import run_media_stream
from sms import send_telnyx_sms

arq_pool = None
redis_client: aioredis.Redis | None = None

_E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global arq_pool, redis_client
    redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)
    arq_pool = await create_pool(RedisSettings.from_dsn(REDIS_URL))
    yield
    await arq_pool.close()
    await redis_client.aclose()
    arq_pool = None
    redis_client = None


app = FastAPI(lifespan=lifespan)


def _normalize_to_e164(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return ""

    if s.startswith("00"):
        s = "+" + s[2:]

    if s.startswith("+"):
        s = "+" + re.sub(r"\D", "", s[1:])
    else:
        s = re.sub(r"\D", "", s)
        if s:
            s = "+" + s

    return s


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/call/status/{cfg_token}")
async def call_status(cfg_token: str):
    if redis_client is None:
        raise HTTPException(status_code=503, detail="Service not ready")
    status = await redis_client.get(status_key(cfg_token))
    if status is None:
        raise HTTPException(status_code=404, detail="Call not found")
    return {"cfg_token": cfg_token, "status": status}


@app.post("/sms/send")
async def sms_send(request: Request):
    """Test or external trigger: send SMS via Telnyx. JSON: to (+E164), message."""
    body = await request.json()
    raw_to = body.get("to")
    raw_message = body.get("message")
    if raw_to is None or raw_message is None:
        raise HTTPException(status_code=400, detail="Missing 'to' or 'message'")
    to = _normalize_to_e164(str(raw_to))
    if not to or not _E164_RE.match(to):
        raise HTTPException(
            status_code=400,
            detail=f"'to' must be in +E164 format, e.g. +918102244713 (got {raw_to!r})",
        )
    message = str(raw_message).strip()
    if not message:
        raise HTTPException(status_code=400, detail="'message' must be non-empty")

    msg_id = await send_telnyx_sms(to, message)
    if msg_id is None:
        raise HTTPException(
            status_code=502,
            detail="Failed to send SMS; check server logs for Telnyx error",
        )
    return {"id": msg_id}


@app.post("/call/outbound")
async def make_outbound_call(request: Request):
    if arq_pool is None or redis_client is None:
        raise HTTPException(status_code=503, detail="Service not ready")

    body = await request.json()
    raw_to = body.get("to")
    if not raw_to:
        raise HTTPException(status_code=400, detail="Missing 'to' number")

    to_number = _normalize_to_e164(str(raw_to))
    if not _E164_RE.match(to_number):
        raise HTTPException(
            status_code=400,
            detail=f"'to' must be in +E164 format, e.g. +918102244713 (got {raw_to!r})",
        )

    call_type = body.get("call_type", "campaign")
    if call_type not in ("on_demand", "campaign"):
        raise HTTPException(
            status_code=400,
            detail="call_type must be 'on_demand' or 'campaign'",
        )

    cfg_body = {k: v for k, v in body.items() if k != "to"}
    cfg = build_call_config(cfg_body)

    use_dynamic = cfg_body.get("dynamic_greeting", True)
    if use_dynamic and not cfg_body.get("opening_greeting"):
        cfg["opening_greeting"] = await generate_opening_greeting(cfg, cfg["llm_provider"])

    q_raw = (
        cfg_body.get("questions_to_ask")
        or cfg_body.get("QUESTIONS_TO_ASK")
        or cfg_body.get("questions")
    )
    if (
        (
            not q_raw
            or (isinstance(q_raw, str) and not q_raw.strip())
            or (isinstance(q_raw, list) and not q_raw)
        )
        and not cfg_body.get("system_prompt")
    ):
        cfg["questions_to_ask"] = await generate_questions_to_ask(cfg, cfg["llm_provider"])
        ctx = {
            "LANGUAGE": cfg["language"],
            "NAME": cfg["name"],
            "COMPANY": cfg["company"],
            "PRODUCT": cfg["product"],
            "PERKS_OF_PRODUCT": cfg["perks_of_product"],
            "INFO_ABOUT_LEAD": cfg["info_about_lead"],
            "Agent_Name": cfg["agent_name"],
            "AGENT_ROLE": cfg["agent_role"],
            "QUESTIONS_TO_ASK": cfg["questions_to_ask"],
        }
        print(f"Request Body: {ctx}")
        cfg["system_prompt"] = SYSTEM_PROMPT_TEMPLATE.format(**ctx)
        cfg["system_prompt"] = prepend_previous_chat_context(
            cfg["system_prompt"],
            cfg.get("previous_chat_context"),
        )

    cfg["_ids"] = {
        "companyId": body.get("companyId"),
        "leadId": body.get("leadId"),
        "campaignId": body.get("campaignId"),
    }
    cfg["_phone"] = to_number
    cfg["call_type"] = call_type
    cfg["_enqueue_time"] = time.time()

    cfg_token = str(uuid.uuid4())
    await redis_client.set(cfg_key(cfg_token), json.dumps(cfg), ex=CFG_TTL_SEC)
    await set_call_status(redis_client, cfg_token, "queued")

    queue = ONCALL_QUEUE if call_type == "on_demand" else CAMPAIGN_QUEUE
    job = await arq_pool.enqueue_job("run_call_job", cfg_token, _queue_name=queue)

    if call_type == "campaign":
        return {
            "status": "queued",
            "cfg_token": cfg_token,
            "job_id": job.job_id,
        }

    result = await redis_client.blpop(start_key(cfg_token), timeout=ON_DEMAND_DEADLINE_SEC)
    if result is None:
        try:
            await job.abort()
        except Exception as e:
            print(f"[OUTBOUND {cfg_token}] job.abort failed: {e}", flush=True)
        await set_call_status(redis_client, cfg_token, "expired")
        raise HTTPException(
            status_code=503,
            detail="Agent busy, try again shortly",
        )

    _key, payload_raw = result
    payload = json.loads(payload_raw)
    if payload.get("error"):
        await set_call_status(redis_client, cfg_token, payload["error"])
        raise HTTPException(
            status_code=503,
            detail="Agent busy, try again shortly",
        )

    return {
        "call_control_id": payload["call_control_id"],
        "status": "initiated",
        "opening_greeting": payload.get("opening_greeting", cfg["opening_greeting"]),
        "cfg_token": cfg_token,
        "job_id": job.job_id,
    }


@app.websocket("/media-stream/{cfg_token}")
async def media_stream(websocket: WebSocket, cfg_token: str):
    if redis_client is None:
        await websocket.close(code=1011)
        return

    raw_cfg = await redis_client.get(cfg_key(cfg_token))
    if raw_cfg:
        call_cfg = json.loads(raw_cfg)
    else:
        call_cfg = build_call_config(None)

    await run_media_stream(
        websocket,
        cfg_token,
        call_cfg,
        redis_client=redis_client,
        cfg_token=cfg_token,
    )


@app.post("/webhook")
async def telnyx_webhook(request: Request):
    return {"ok": True}
