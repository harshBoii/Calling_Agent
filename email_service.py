"""Outbound email via Resend. Errors are logged and never raised to callers."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from config import RESEND_API_KEY

logger = logging.getLogger(__name__)

RESEND_EMAILS_URL = "https://api.resend.com/emails"


async def send_resend_email(
    *,
    to: str | list[str],
    from_addr: str,
    subject: str,
    html: str,
    text: str | None = None,
) -> str | None:
    """
    POST to Resend /emails. On success return message id; on failure log and return None.
    """
    try:
        if not RESEND_API_KEY:
            logger.warning("[EMAIL] RESEND_API_KEY is not set; skip")
            return None

        from_s = (from_addr or "").strip()
        subj = (subject or "").strip()
        html_body = (html or "").strip()
        if not from_s or not subj or not html_body:
            logger.warning("[EMAIL] missing from, subject, or html body; skip")
            return None

        if isinstance(to, str):
            to_list = [t.strip() for t in to.split(",") if t.strip()]
        else:
            to_list = [str(t).strip() for t in to if str(t).strip()]
        if not to_list:
            logger.warning("[EMAIL] missing recipient; skip")
            return None

        payload: dict[str, Any] = {
            "from": from_s,
            "to": to_list,
            "subject": subj,
            "html": html_body,
        }
        if text and str(text).strip():
            payload["text"] = str(text).strip()

        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                RESEND_EMAILS_URL,
                headers={
                    "Authorization": f"Bearer {RESEND_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )

        if r.status_code >= 400:
            logger.warning(
                "[EMAIL] Resend HTTP %s: %s", r.status_code, (r.text or "")[:800]
            )
            return None

        data = r.json()
        msg_id = data.get("id")
        if not msg_id:
            logger.warning("[EMAIL] unexpected response (no id): %s", data)
            return None
        return str(msg_id)
    except Exception as e:
        logger.exception("[EMAIL] send_resend_email failed: %s", e)
        return None
