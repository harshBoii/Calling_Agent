"""Arq job: send SMS or email and fire completion webhook."""

import json
import time

from config import (
    DEFAULT_FROM_EMAIL,
    ON_DEMAND_DEADLINE_SEC,
    STATUS_TTL_SEC,
    TELNYX_PHONE_NUMBER,
)
from email_service import send_resend_email
from sms import send_telnyx_sms
from webhook import send_email_completed_webhook, send_sms_completed_webhook


def msg_cfg_key(task_token: str) -> str:
    return f"msg:cfg:{task_token}"


def msg_start_key(task_token: str) -> str:
    return f"msg:start:{task_token}"


def msg_status_key(task_token: str) -> str:
    return f"msg:status:{task_token}"


async def set_message_status(redis, task_token: str, status: str) -> None:
    await redis.set(msg_status_key(task_token), status, ex=STATUS_TTL_SEC)


async def cleanup_message_keys(redis, task_token: str) -> None:
    await redis.delete(msg_cfg_key(task_token), msg_start_key(task_token))


def _decode_redis_value(raw) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, bytes):
        return raw.decode("utf-8")
    return str(raw)


async def run_message_job(ctx, task_token: str) -> None:
    redis = ctx["redis"]
    cfg: dict = {}
    channel = "sms"
    external_id: str | None = None
    webhook_status = "FAILED"
    error: str | None = None

    try:
        await set_message_status(redis, task_token, "sending")

        raw_cfg = await redis.get(msg_cfg_key(task_token))
        cfg_text = _decode_redis_value(raw_cfg)
        if not cfg_text:
            print(f"[MSG {task_token}] No cfg found in Redis", flush=True)
            webhook_status = "FAILED"
            error = "missing config"
            await set_message_status(redis, task_token, "failed")
            return

        cfg = json.loads(cfg_text)
        channel = cfg.get("channel", "sms")
        message_type = cfg.get("message_type", "campaign")

        enqueue_time = float(cfg.get("_enqueue_time") or 0)
        if message_type == "on_demand" and time.time() - enqueue_time > ON_DEMAND_DEADLINE_SEC:
            webhook_status = "EXPIRED"
            error = "expired"
            await set_message_status(redis, task_token, "expired")
            await redis.rpush(msg_start_key(task_token), json.dumps({"error": "expired"}))
            return

        if channel == "sms":
            to_number = cfg.get("to")
            body = cfg.get("message") or cfg.get("body")
            from_number = cfg.get("from") or TELNYX_PHONE_NUMBER
            external_id = await send_telnyx_sms(
                str(to_number),
                str(body),
                from_number=str(from_number),
            )
        elif channel == "email":
            to_email = cfg.get("to")
            from_addr = cfg.get("from") or DEFAULT_FROM_EMAIL
            if not from_addr:
                error = "missing from address"
                await set_message_status(redis, task_token, "failed")
                return
            external_id = await send_resend_email(
                to=str(to_email),
                from_addr=str(from_addr),
                subject=str(cfg.get("subject") or ""),
                html=str(cfg.get("body") or cfg.get("html") or ""),
                text=cfg.get("text"),
            )
        else:
            error = f"unknown channel: {channel}"
            await set_message_status(redis, task_token, "failed")
            return

        if external_id:
            webhook_status = "SENT"
            await set_message_status(redis, task_token, "sent")
            if message_type == "on_demand":
                await redis.rpush(
                    msg_start_key(task_token),
                    json.dumps({"id": external_id, "status": "sent"}),
                )
        else:
            webhook_status = "FAILED"
            error = error or "provider send failed"
            await set_message_status(redis, task_token, "failed")
            if message_type == "on_demand":
                await redis.rpush(
                    msg_start_key(task_token),
                    json.dumps({"error": "send_failed", "detail": error}),
                )
    except Exception as e:
        webhook_status = "FAILED"
        error = str(e)
        print(f"[MSG {task_token}] job error: {type(e).__name__}: {e}", flush=True)
        await set_message_status(redis, task_token, "failed")
        if cfg.get("message_type") == "on_demand":
            try:
                await redis.rpush(
                    msg_start_key(task_token),
                    json.dumps({"error": "send_failed", "detail": error}),
                )
            except Exception:
                pass
    finally:
        try:
            if channel == "sms":
                await send_sms_completed_webhook(
                    task_token=task_token,
                    cfg=cfg,
                    external_id=external_id,
                    status=webhook_status,
                    error=error,
                )
            elif channel == "email":
                await send_email_completed_webhook(
                    task_token=task_token,
                    cfg=cfg,
                    external_id=external_id,
                    status=webhook_status,
                    error=error,
                )
        except Exception as e:
            print(f"[MSG {task_token}] webhook error: {type(e).__name__}: {e}", flush=True)
        await cleanup_message_keys(redis, task_token)
