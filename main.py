import json
import re
import time
import uuid
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from arq import create_pool
from arq.connections import RedisSettings
from fastapi import FastAPI, HTTPException, Request, WebSocket

from pydantic import ValidationError

from agent_config import get_compliance_disclosure
from arq_jobs import cfg_key, dial_telnyx_call, set_call_status, start_key, status_key
from config import (
    CAMPAIGN_QUEUE,
    CFG_TTL_SEC,
    DEFAULT_FROM_EMAIL,
    MESSAGES_CAMPAIGN_QUEUE,
    MESSAGES_ONCALL_QUEUE,
    MSG_CFG_TTL_SEC,
    ON_DEMAND_DEADLINE_SEC,
    ONCALL_QUEUE,
    REDIS_URL,
    SYSTEM_PROMPT_TEMPLATE,
    TELNYX_PHONE_NUMBER,
    USE_ARQ_QUEUE,
    build_call_config,
    prepend_previous_chat_context,
)
from llm import generate_opening_greeting, generate_questions_to_ask
from media_stream import run_media_stream
from message_jobs import (
    execute_message_send,
    msg_cfg_key,
    msg_start_key,
    msg_status_key,
    set_message_status,
)
from schemas.outbound_call import OutboundCallRequest
from webhook import send_email_completed_webhook, send_sms_completed_webhook

arq_pool = None
redis_client: aioredis.Redis | None = None

_E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global arq_pool, redis_client
    redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)
    if USE_ARQ_QUEUE:
        arq_pool = await create_pool(RedisSettings.from_dsn(REDIS_URL))
    else:
        arq_pool = None
    yield
    if arq_pool is not None:
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


def _parse_message_type(body: dict) -> str:
    message_type = body.get("message_type", "campaign")
    if message_type not in ("on_demand", "campaign"):
        raise HTTPException(
            status_code=400,
            detail="message_type must be 'on_demand' or 'campaign'",
        )
    return message_type


def _ensure_service_ready() -> None:
    if redis_client is None:
        raise HTTPException(status_code=503, detail="Service not ready")
    if USE_ARQ_QUEUE and arq_pool is None:
        raise HTTPException(status_code=503, detail="Service not ready")


async def _direct_message(payload: dict, message_type: str) -> dict:
    _ensure_service_ready()

    task_token = str(uuid.uuid4())
    payload["message_type"] = message_type
    payload["_enqueue_time"] = time.time()
    payload["_ids"] = {
        "companyId": payload.pop("companyId", None),
        "leadId": payload.pop("leadId", None),
        "campaignId": payload.pop("campaignId", None),
    }

    await redis_client.set(msg_cfg_key(task_token), json.dumps(payload), ex=MSG_CFG_TTL_SEC)
    await set_message_status(redis_client, task_token, "sending")

    channel = payload.get("channel", "sms")
    external_id: str | None = None
    webhook_status = "FAILED"
    error: str | None = None

    try:
        external_id, webhook_status, error = await execute_message_send(payload)
    except Exception as e:
        webhook_status = "FAILED"
        error = str(e)

    if webhook_status == "SENT":
        await set_message_status(redis_client, task_token, "sent")
    else:
        await set_message_status(redis_client, task_token, "failed")

    try:
        if channel == "sms":
            await send_sms_completed_webhook(
                task_token=task_token,
                cfg=payload,
                external_id=external_id,
                status=webhook_status,
                error=error,
            )
        elif channel == "email":
            await send_email_completed_webhook(
                task_token=task_token,
                cfg=payload,
                external_id=external_id,
                status=webhook_status,
                error=error,
            )
    except Exception as e:
        print(f"[MSG {task_token}] webhook error: {type(e).__name__}: {e}", flush=True)

    if webhook_status != "SENT":
        raise HTTPException(status_code=502, detail=error or "send failed")

    return {
        "status": "sent",
        "id": external_id,
        "task_token": task_token,
    }


async def _enqueue_message(payload: dict, message_type: str) -> dict:
    _ensure_service_ready()

    task_token = str(uuid.uuid4())
    payload["message_type"] = message_type
    payload["_enqueue_time"] = time.time()
    payload["_ids"] = {
        "companyId": payload.pop("companyId", None),
        "leadId": payload.pop("leadId", None),
        "campaignId": payload.pop("campaignId", None),
    }

    await redis_client.set(msg_cfg_key(task_token), json.dumps(payload), ex=MSG_CFG_TTL_SEC)
    await set_message_status(redis_client, task_token, "queued")

    queue = MESSAGES_ONCALL_QUEUE if message_type == "on_demand" else MESSAGES_CAMPAIGN_QUEUE
    job = await arq_pool.enqueue_job("run_message_job", task_token, _queue_name=queue)

    if message_type == "campaign":
        return {
            "status": "queued",
            "task_token": task_token,
            "job_id": job.job_id,
        }

    result = await redis_client.blpop(msg_start_key(task_token), timeout=ON_DEMAND_DEADLINE_SEC)
    if result is None:
        try:
            await job.abort()
        except Exception as e:
            print(f"[MSG {task_token}] job.abort failed: {e}", flush=True)
        await set_message_status(redis_client, task_token, "expired")
        raise HTTPException(
            status_code=503,
            detail="Agent busy, try again shortly",
        )

    _key, payload_raw = result
    start_payload = json.loads(payload_raw)
    if start_payload.get("error"):
        await set_message_status(redis_client, task_token, start_payload["error"])
        raise HTTPException(
            status_code=503,
            detail="Agent busy, try again shortly",
        )

    return {
        "status": "sent",
        "id": start_payload.get("id"),
        "task_token": task_token,
        "job_id": job.job_id,
    }


@app.get("/health")
async def health():
    return {"ok": True, "use_arq_queue": USE_ARQ_QUEUE}


@app.get("/call/status/{cfg_token}")
async def call_status(cfg_token: str):
    if redis_client is None:
        raise HTTPException(status_code=503, detail="Service not ready")
    status = await redis_client.get(status_key(cfg_token))
    if status is None:
        raise HTTPException(status_code=404, detail="Call not found")
    return {"cfg_token": cfg_token, "status": status}


@app.get("/message/status/{task_token}")
async def message_status(task_token: str):
    if redis_client is None:
        raise HTTPException(status_code=503, detail="Service not ready")
    status = await redis_client.get(msg_status_key(task_token))
    if status is None:
        raise HTTPException(status_code=404, detail="Message not found")
    channel = None
    raw_cfg = await redis_client.get(msg_cfg_key(task_token))
    if raw_cfg:
        try:
            channel = json.loads(raw_cfg).get("channel")
        except json.JSONDecodeError:
            pass
    return {"task_token": task_token, "status": status, "channel": channel}


@app.post("/sms/send")
async def sms_send(request: Request):
    """Queue outbound SMS via Telnyx. JSON: to (+E164), message, optional from, message_type."""
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

    message_type = _parse_message_type(body)
    from_number = body.get("from")
    if from_number is not None:
        from_number = _normalize_to_e164(str(from_number))
        if not _E164_RE.match(from_number):
            raise HTTPException(status_code=400, detail="'from' must be valid +E164")

    payload = {
        "channel": "sms",
        "to": to,
        "message": message,
        "from": from_number or TELNYX_PHONE_NUMBER,
        "companyId": body.get("companyId"),
        "leadId": body.get("leadId"),
        "campaignId": body.get("campaignId"),
    }
    if USE_ARQ_QUEUE:
        return await _enqueue_message(payload, message_type)
    return await _direct_message(payload, message_type)


@app.post("/email/send")
async def email_send(request: Request):
    """Queue outbound email via Resend."""
    body = await request.json()
    raw_to = body.get("to")
    raw_subject = body.get("subject")
    raw_body = body.get("body")
    if raw_to is None or raw_subject is None or raw_body is None:
        raise HTTPException(status_code=400, detail="Missing 'to', 'subject', or 'body'")

    to = str(raw_to).strip()
    if not to:
        raise HTTPException(status_code=400, detail="'to' must be non-empty")

    subject = str(raw_subject).strip()
    html_body = str(raw_body).strip()
    if not subject or not html_body:
        raise HTTPException(status_code=400, detail="'subject' and 'body' must be non-empty")

    from_addr = (body.get("from") or DEFAULT_FROM_EMAIL or "").strip()
    if not from_addr:
        raise HTTPException(
            status_code=400,
            detail="Missing 'from' (or set DEFAULT_FROM_EMAIL in env)",
        )

    message_type = _parse_message_type(body)
    payload = {
        "channel": "email",
        "to": to,
        "from": from_addr,
        "subject": subject,
        "body": html_body,
        "text": body.get("text"),
        "companyId": body.get("companyId"),
        "leadId": body.get("leadId"),
        "campaignId": body.get("campaignId"),
    }
    if USE_ARQ_QUEUE:
        return await _enqueue_message(payload, message_type)
    return await _direct_message(payload, message_type)


async def _direct_outbound_call(cfg: dict, cfg_token: str, to_number: str) -> dict:
    await redis_client.set(cfg_key(cfg_token), json.dumps(cfg), ex=CFG_TTL_SEC)
    await set_call_status(redis_client, cfg_token, "dialling")
    try:
        call_control_id = await dial_telnyx_call(cfg_token, to_number)
    except Exception as e:
        await set_call_status(redis_client, cfg_token, "dial_failed")
        raise HTTPException(status_code=502, detail=f"Dial failed: {e}")

    await set_call_status(redis_client, cfg_token, "connected")
    return {
        "status": "initiated",
        "call_control_id": call_control_id,
        "opening_greeting": cfg.get("opening_greeting", ""),
        "cfg_token": cfg_token,
    }


@app.post("/call/outbound")
async def make_outbound_call(request: Request):
    _ensure_service_ready()

    body = await request.json()
    try:
        req = OutboundCallRequest.model_validate(body)
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=e.errors()) from e

    to_number = _normalize_to_e164(str(req.to))
    if not _E164_RE.match(to_number):
        raise HTTPException(
            status_code=400,
            detail=f"'to' must be in +E164 format, e.g. +918102244713 (got {req.to!r})",
        )

    call_type = req.call_type
    cfg_body = req.to_cfg_body()
    cfg = build_call_config(cfg_body)

    use_dynamic = req.dynamic_greeting
    if use_dynamic and not req.opening_greeting:
        cfg["opening_greeting"] = await generate_opening_greeting(cfg, cfg["llm_provider"])
        disclosure = get_compliance_disclosure(cfg.get("agent_config"))
        if disclosure:
            cfg["opening_greeting"] = f"{disclosure} {cfg['opening_greeting']}"
            cfg["_ai_disclosure_done"] = True

    q_raw = (
        req.questions_to_ask
        or req.QUESTIONS_TO_ASK
        or req.questions
    )
    if (
        (
            not q_raw
            or (isinstance(q_raw, str) and not q_raw.strip())
            or (isinstance(q_raw, list) and not q_raw)
        )
        and not req.system_prompt
        and not cfg.get("agent_config")
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
    elif (
        (
            not q_raw
            or (isinstance(q_raw, str) and not q_raw.strip())
            or (isinstance(q_raw, list) and not q_raw)
        )
        and cfg.get("agent_config")
    ):
        cfg["questions_to_ask"] = await generate_questions_to_ask(cfg, cfg["llm_provider"])
        from agent_config import build_base_system_prompt

        cfg["system_prompt"] = build_base_system_prompt(
            cfg, custom_prompt=req.system_prompt
        )
        cfg["system_prompt"] = prepend_previous_chat_context(
            cfg["system_prompt"],
            cfg.get("previous_chat_context"),
        )

    cfg["_ids"] = {
        "companyId": req.companyId,
        "leadId": req.leadId,
        "campaignId": req.campaignId,
    }
    cfg["_phone"] = to_number
    cfg["call_type"] = call_type
    cfg["_enqueue_time"] = time.time()

    cfg_token = str(uuid.uuid4())

    if not USE_ARQ_QUEUE:
        return await _direct_outbound_call(cfg, cfg_token, to_number)

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
