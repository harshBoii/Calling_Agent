"""Telnyx outbound WhatsApp messaging. Errors are logged and never raised to callers."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from config import (
    TELNYX_API_KEY,
    TELNYX_PHONE_NUMBER,
    TELNYX_WHATSAPP_NUMBER,
)

logger = logging.getLogger(__name__)

TELNYX_WHATSAPP_URL = "https://api.telnyx.com/v2/messages/whatsapp"


async def send_telnyx_whatsapp(
    to: str,
    message: str,
    *,
    from_number: str | None = None,
    template_name: str | None = None,
    template_language_code: str = "en_US",
    template_language_policy: str = "deterministic",
    template_components: list | None = None,
    preview_url: bool = False,
) -> str | None:
    """
    Send a WhatsApp message via Telnyx POST /v2/messages/whatsapp.

    Modes:
    - Template mode (template_name is set)  → works anytime; requires Meta-approved template.
    - Text mode (template_name not set)     → free-form text; only valid within the
                                              24-hour customer-initiated conversation window.

    Note: messaging_profile_id is NOT included — Telnyx auto-resolves it from the
    'from' phone number as documented in the Telnyx WhatsApp quickstart.

    Returns the Telnyx message id (data.id) on success, or None on any failure
    (never raises — all errors are logged).
    """
    try:
        to_s = (to or "").strip()
        if not to_s:
            logger.warning("[WA] missing 'to'; skip")
            return None

        sender = (from_number or TELNYX_WHATSAPP_NUMBER or TELNYX_PHONE_NUMBER or "").strip()
        if not sender:
            logger.warning("[WA] no sender number configured; skip")
            return None

        whatsapp_message: dict[str, Any]
        if template_name:
            # Template message — no 24-hour window restriction.
            # Language object requires 'policy' field per Telnyx docs.
            whatsapp_message = {
                "type": "template",
                "template": {
                    "name": template_name,
                    "language": {
                        "policy": template_language_policy,
                        "code": template_language_code,
                    },
                    "components": template_components or [],
                },
            }
        else:
            # Free-form text — only valid within 24-hour customer-initiated window.
            text = (message or "").strip()
            if not text:
                logger.warning("[WA] missing 'message' for text mode; skip")
                return None
            whatsapp_message = {
                "type": "text",
                "text": {
                    "body": text,
                    "preview_url": preview_url,
                },
            }

        payload: dict[str, Any] = {
            "from": sender,
            "to": to_s,
            "whatsapp_message": whatsapp_message,
        }

        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(
                TELNYX_WHATSAPP_URL,
                headers={
                    "Authorization": f"Bearer {TELNYX_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )

        if r.status_code >= 400:
            logger.warning(
                "[WA] Telnyx HTTP %s: %s", r.status_code, (r.text or "")[:800]
            )
            return None

        data = r.json()
        msg_id = (data.get("data") or {}).get("id")
        if not msg_id:
            logger.warning("[WA] unexpected response (no data.id): %s", data)
            return None

        # Log initial delivery status from the API response (data.to[0].status)
        to_list = (data.get("data") or {}).get("to") or []
        initial_status = to_list[0].get("status") if to_list else "unknown"
        logger.info("[WA] sent msg_id=%s initial_status=%s", msg_id, initial_status)

        return str(msg_id)

    except Exception as e:
        logger.exception("[WA] send_telnyx_whatsapp failed: %s", e)
        return None
