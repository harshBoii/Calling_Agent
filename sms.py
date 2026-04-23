"""Telnyx outbound SMS. Errors are logged and never raised to callers."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from config import (
    TELNYX_API_KEY,
    TELNYX_MESSAGING_PROFILE_ID,
    TELNYX_PHONE_NUMBER,
)

logger = logging.getLogger(__name__)

TELNYX_MESSAGES_URL = "https://api.telnyx.com/v2/messages"


async def send_telnyx_sms(to: str, message: str) -> str | None:
    """
    POST to Telnyx /v2/messages. On success return message id; on any failure
    log and return None (never raises).
    """
    try:
        to_s = (to or "").strip()
        text = (message or "").strip()
        if not to_s or not text:
            logger.warning("[SMS] missing to or message; skip")
            return None

        payload: dict[str, Any] = {
            "from": TELNYX_PHONE_NUMBER,
            "to": to_s,
            "text": text,
        }
        if TELNYX_MESSAGING_PROFILE_ID:
            payload["messaging_profile_id"] = TELNYX_MESSAGING_PROFILE_ID

        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(
                TELNYX_MESSAGES_URL,
                headers={
                    "Authorization": f"Bearer {TELNYX_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )

        if r.status_code >= 400:
            logger.warning(
                "[SMS] Telnyx HTTP %s: %s", r.status_code, (r.text or "")[:800]
            )
            return None

        data = r.json()
        msg_id = (data.get("data") or {}).get("id")
        if not msg_id:
            logger.warning("[SMS] unexpected response (no data.id): %s", data)
            return None
        return str(msg_id)
    except Exception as e:
        logger.exception("[SMS] send_telnyx_sms failed: %s", e)
        return None


def schedule_telnyx_sms_if_phone(cfg: dict | None, message: str) -> None:
    """
    Fire-and-forget: send SMS to cfg['_phone'] if present. If no _phone, no-op
    (inbound or tests). Failed SMS does not affect the caller. Never raises.
    """
    if not cfg or not (message and str(message).strip()):
        return
    to = cfg.get("_phone")
    if not to:
        return
    to_s = str(to).strip()
    if not to_s:
        return
    text = str(message).strip()

    async def _run() -> None:
        await send_telnyx_sms(to_s, text)

    try:
        asyncio.create_task(_run())
    except RuntimeError as e:
        logger.warning("[SMS] schedule: could not create_task (%s); skipped", e)
