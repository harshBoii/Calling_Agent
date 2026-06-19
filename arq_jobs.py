"""Arq job: dial Telnyx and hold the worker slot until the call ends."""

import json
import time

import httpx

from config import (
    CALL_DONE_TIMEOUT_SEC,
    ON_DEMAND_DEADLINE_SEC,
    PUBLIC_BASE_URL,
    STATUS_TTL_SEC,
    TELNYX_API_KEY,
    TELNYX_CONNECTION_ID,
    TELNYX_PHONE_NUMBER,
)


def cfg_key(cfg_token: str) -> str:
    return f"cfg:{cfg_token}"


def start_key(cfg_token: str) -> str:
    return f"call:start:{cfg_token}"


def done_key(cfg_token: str) -> str:
    return f"call:done:{cfg_token}"


def status_key(cfg_token: str) -> str:
    return f"call:status:{cfg_token}"


async def set_call_status(redis, cfg_token: str, status: str) -> None:
    await redis.set(status_key(cfg_token), status, ex=STATUS_TTL_SEC)


async def cleanup_ephemeral_keys(redis, cfg_token: str) -> None:
    await redis.delete(cfg_key(cfg_token), start_key(cfg_token), done_key(cfg_token))


def _decode_redis_value(raw) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, bytes):
        return raw.decode("utf-8")
    return str(raw)


async def run_call_job(ctx, cfg_token: str) -> None:
    redis = ctx["redis"]
    try:
        await set_call_status(redis, cfg_token, "dialling")

        raw_cfg = await redis.get(cfg_key(cfg_token))
        cfg_text = _decode_redis_value(raw_cfg)
        if not cfg_text:
            print(f"[JOB {cfg_token}] No cfg found in Redis", flush=True)
            await set_call_status(redis, cfg_token, "dial_failed")
            return

        cfg = json.loads(cfg_text)
        call_type = cfg.get("call_type", "campaign")
        to_number = cfg.get("_phone")
        if not to_number:
            print(f"[JOB {cfg_token}] Missing _phone in cfg", flush=True)
            await set_call_status(redis, cfg_token, "dial_failed")
            return

        enqueue_time = float(cfg.get("_enqueue_time") or 0)
        if call_type == "on_demand" and time.time() - enqueue_time > ON_DEMAND_DEADLINE_SEC:
            await set_call_status(redis, cfg_token, "expired")
            await redis.rpush(start_key(cfg_token), json.dumps({"error": "expired"}))
            return

        ws_base = PUBLIC_BASE_URL.replace("https://", "wss://").replace("http://", "ws://")
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    "https://api.telnyx.com/v2/calls",
                    headers={
                        "Authorization": f"Bearer {TELNYX_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "connection_id": TELNYX_CONNECTION_ID,
                        "to": to_number,
                        "from": TELNYX_PHONE_NUMBER,
                        "stream_url": f"{ws_base}/media-stream/{cfg_token}",
                        "stream_track": "inbound_track",
                        "stream_codec": "PCMU",
                    },
                )
                print(f"[JOB {cfg_token}] [TELNYX] {resp.status_code}: {resp.text}", flush=True)
                resp.raise_for_status()
                data = resp.json()["data"]
        except Exception as e:
            await set_call_status(redis, cfg_token, "dial_failed")
            if call_type == "on_demand":
                await redis.rpush(
                    start_key(cfg_token),
                    json.dumps({"error": "dial_failed", "detail": str(e)}),
                )
            raise

        call_control_id = data["call_control_id"]
        await set_call_status(redis, cfg_token, "connected")

        if call_type == "on_demand":
            await redis.rpush(
                start_key(cfg_token),
                json.dumps(
                    {
                        "call_control_id": call_control_id,
                        "opening_greeting": cfg.get("opening_greeting", ""),
                    }
                ),
            )

        result = await redis.blpop(done_key(cfg_token), timeout=CALL_DONE_TIMEOUT_SEC)
        if result is None:
            await set_call_status(redis, cfg_token, "timeout")
        else:
            await set_call_status(redis, cfg_token, "completed")
    finally:
        await cleanup_ephemeral_keys(redis, cfg_token)
