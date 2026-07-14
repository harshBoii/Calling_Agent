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
from webhook import send_email_completed_webhook, send_sms_completed_webhook, send_whatsapp_completed_webhook


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


async def execute_message_send(cfg: dict) -> tuple[str | None, str, str | None]:
    """
    Send SMS or email per cfg. Returns (external_id, webhook_status, error).
    webhook_status is SENT | FAILED | EXPIRED.
    """
    channel = cfg.get("channel", "sms")
    message_type = cfg.get("message_type", "campaign")
    error: str | None = None

    enqueue_time = float(cfg.get("_enqueue_time") or 0)
    if message_type == "on_demand" and time.time() - enqueue_time > ON_DEMAND_DEADLINE_SEC:
        return None, "EXPIRED", "expired"

    external_id: str | None = None
    if channel == "sms":
        external_id = await send_telnyx_sms(
            str(cfg.get("to")),
            str(cfg.get("message") or cfg.get("body")),
            from_number=str(cfg.get("from") or TELNYX_PHONE_NUMBER),
        )
    elif channel == "email":
        from_addr = cfg.get("from") or DEFAULT_FROM_EMAIL
        if not from_addr:
            return None, "FAILED", "missing from address"
        external_id = await send_resend_email(
            to=str(cfg.get("to")),
            from_addr=str(from_addr),
            subject=str(cfg.get("subject") or ""),
            html=str(cfg.get("body") or cfg.get("html") or ""),
            text=cfg.get("text"),
        )
    elif channel == "whatsapp":
        from whatsapp import send_telnyx_whatsapp
        external_id = await send_telnyx_whatsapp(
            str(cfg.get("to")),
            str(cfg.get("message") or cfg.get("body") or ""),
            from_number=cfg.get("from"),
            template_name=cfg.get("template_name"),
            template_language_code=str(cfg.get("template_language") or "en_US"),
            template_language_policy=str(cfg.get("template_language_policy") or "deterministic"),
            template_components=cfg.get("template_components"),
            preview_url=bool(cfg.get("preview_url", False)),
        )
    else:
        return None, "FAILED", f"unknown channel: {channel}"

    if external_id:
        return external_id, "SENT", None
    return None, "FAILED", error or "provider send failed"


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

        external_id, webhook_status, error = await execute_message_send(cfg)

        if webhook_status == "EXPIRED":
            await set_message_status(redis, task_token, "expired")
            if message_type == "on_demand":
                await redis.rpush(msg_start_key(task_token), json.dumps({"error": "expired"}))
            return

        if webhook_status == "SENT":
            await set_message_status(redis, task_token, "sent")
            if message_type == "on_demand":
                await redis.rpush(
                    msg_start_key(task_token),
                    json.dumps({"id": external_id, "status": "sent"}),
                )
        else:
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
            elif channel == "whatsapp":
                await send_whatsapp_completed_webhook(
                    task_token=task_token,
                    cfg=cfg,
                    external_id=external_id,
                    status=webhook_status,
                    error=error,
                )
        except Exception as e:
            print(f"[MSG {task_token}] webhook error: {type(e).__name__}: {e}", flush=True)
        await cleanup_message_keys(redis, task_token)
