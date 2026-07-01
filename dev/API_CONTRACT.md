# Calling Agent — API Contract

Base URL examples:
- Local: `http://localhost:8000`
- Production: `https://calling-agent-ki3j.onrender.com`

All JSON endpoints use `Content-Type: application/json`.

---

## Overview

Three outbound channels share a **two-lane queue** model:

| Lane | Field name | HTTP behavior | Queue cap |
|------|------------|---------------|-----------|
| Campaign (default) | `call_type` or `message_type` = `"campaign"` | Returns immediately with `queued` | 6 concurrent jobs |
| On-demand | `"on_demand"` | Blocks up to **15 seconds** waiting for start/send | 2 concurrent jobs |

Voice calls and messaging use **separate queues** (8 call slots + 8 message slots max).

**Completion data** is always delivered asynchronously via **signed webhooks** after the task finishes. Instant HTTP responses only confirm enqueue / dial / send initiation.

---

## Execution mode (`USE_ARQ_QUEUE`)

Global env var (default **`true`**). Checked at API startup; exposed on `GET /health` as `use_arq_queue`.

| `USE_ARQ_QUEUE` | Workers required | Calls | SMS / Email |
|-----------------|------------------|-------|-------------|
| `true` (default) | Yes (4 background workers) | Enqueue; campaign returns `queued`; on-demand waits up to 15s | Same |
| `false` | No | API dials Telnyx immediately | API sends via Telnyx/Resend immediately |

**Direct mode** (`USE_ARQ_QUEUE=false`) — for local dev without Arq workers:

- **Calls:** always `200` with `{ "status": "initiated", "call_control_id", "call_leg_id", "opening_greeting", "cfg_token" }` — no `queued`, no `503`, no `job_id`
- **SMS / Email:** always `200` with `{ "status": "sent", "id", "task_token" }` — no `queued`, no `503`, no `job_id`
- Completion webhooks still fire after call end / send
- No Arq concurrency caps
- Redis still required (WebSocket cfg, status polling)

Local `.env` example:
```bash
USE_ARQ_QUEUE=false
```

Production / Render: `USE_ARQ_QUEUE=true` (default).

---

## Webhook delivery (all channels)

Configured via environment:

| Env var | Event | When fired |
|---------|-------|------------|
| `WEBHOOK_CALL` | `call.completed` | After call ends (Telnyx media stream closes) |
| `WEBHOOK_SMS` | `sms.completed` | After SMS job finishes (sent, failed, or expired) |
| `WEBHOOK_EMAIL` | `email.completed` | After email job finishes |

Fallback: if `WEBHOOK_CALL` is unset, posts to `{NEXT_JS_SERVICE_URL}/api/calling-agent/webhook`.

### Request (Calling Agent → your server)

```
POST {WEBHOOK_CALL|WEBHOOK_SMS|WEBHOOK_EMAIL}
Content-Type: application/json
x-calling-agent-signature: sha256={hex_hmac_of_raw_body}
x-calling-agent-event-id: {payload.eventId}
x-calling-agent-event-type: {payload.event}
```

Body is compact JSON (no extra whitespace). Signature:

```
HMAC-SHA256(key=WEBHOOK_SECRET, message=raw_json_body) → hex digest
Header value: "sha256=" + hex
```

Up to **3 retries** with exponential backoff on failure. Your endpoint should return `2xx`.

**Delivery requirements:** Webhooks are only sent when all of the following are true for that channel:

| Channel | URL env | Also required |
|---------|---------|---------------|
| Calls | `WEBHOOK_CALL` **or** `NEXT_JS_SERVICE_URL` (fallback path below) | `WEBHOOK_SECRET` |
| SMS | `WEBHOOK_SMS` | `WEBHOOK_SECRET` |
| Email | `WEBHOOK_EMAIL` | `WEBHOOK_SECRET` |

If `WEBHOOK_SECRET` is unset, delivery is skipped (logged server-side, no HTTP POST). If a channel URL is unset, that channel's webhook is skipped.

---

## Health

### `GET /health`

**Response `200`**
```json
{ "ok": true, "use_arq_queue": true }
```

`use_arq_queue` reflects the current `USE_ARQ_QUEUE` env setting.

---

## Outbound calls

### `POST /call/outbound`

Places an AI voice call via Telnyx. Conversation runs over WebSocket on this service; completion webhook fires when the call ends.

#### Request body

| Field | Required | Type | Notes |
|-------|----------|------|-------|
| `to` | **yes** | string | E.164 phone, e.g. `+918102244713` |
| `call_type` | no | string | `"campaign"` (default) \| `"on_demand"` |
| `companyId` | no | string | Passed through to completion webhook |
| `leadId` | no | string | Passed through to completion webhook |
| `campaignId` | no | string | Passed through to completion webhook |
| `campaign_type` / `campaignType` | no | string | `Sales` (default if omitted) \| `Collection` \| `Appointment reminders` \| `Feedback / NPS` \| `Renewal / win-back`. Drives live agent system prompt and post-call outcome analysis. |
| `language` / `languageMode` | no | string | Spoken language (default `"English"`) |
| `deepgram_language` / `deepgramLanguage` | no | string | STT code (default `"en"`) |
| `elevenlabs_model` / `voiceMode` | no | string | TTS model |
| `voiceId` | no | string | ElevenLabs voice ID |
| `name` | no | string | Lead name for script |
| `company` | no | string | |
| `product` | no | string | |
| `perks_of_product` | no | string | |
| `info_about_lead` | no | string | |
| `agent_name` / `AGENT_NAME` | no | string | |
| `agent_role` / `AGENT_ROLE` | no | string | |
| `questions_to_ask` / `QUESTIONS_TO_ASK` / `questions` | no | string \| string[] | If omitted, LLM generates questions |
| `system_prompt` | no | string | Overrides templated prompt |
| `opening_greeting` | no | string | If omitted and `dynamic_greeting` true, LLM generates |
| `dynamic_greeting` | no | boolean | Default `true` |
| `previousChatContext` | no | string | Prepended to system prompt |
| `llm_provider` | no | string | `claude` \| `groq` \| `openai` \| `gemini` \| `sarvam` |
| `llm_model` | no | string | Provider-specific model id |
| `stt_provider` | no | string | `"auto"` \| `"deepgram"` \| `"sarvam"` |
| `use_sarvam_tts` | no | boolean | |
| `sarvam_speaker` | no | string | |
| `agent_config` | no | object | Structured agent behavior (see below). Optional — legacy flat fields still work alone. |
| `available_meet_slots` | no | array | Pre-fetched meeting slots for `slot_suggestion` stage (see below). When provided, calendar stub is skipped. |

#### `available_meet_slots` (optional)

Array of slot objects supplied by your backend (e.g. from calendar integration). Used during `slot_suggestion` instead of the internal calendar stub.

| Field | Type | Notes |
|-------|------|-------|
| `id` | string | Slot identifier (ISO start time or external id) |
| `startAt` | string | ISO-8601 UTC start |
| `endAt` | string | ISO-8601 UTC end |
| `label` | string | Spoken label, e.g. `"Tue, Jun 24, 2:00 PM UTC"` |
| `durationMin` | number | Optional duration in minutes |
| `timezone` | string | Optional timezone label |

When the lead picks a slot, `call.completed` webhook includes top-level and `call.meet_scheduled`, e.g. `"Tue Jun 24 2:00 PM"`.

#### `agent_config` (optional)

When present, drives conversation stages, guardrails, objections, compliance, and booking. **Top-level fields win on overlap:**

| Top-level | Fallback inside `agent_config` |
|-----------|-------------------------------|
| `agent_name` | `identity.agentName` |
| `agent_role` | `identity.roleFraming` (`{{companyName}}` → top-level `company`) |
| `voiceId` | `identity.voice.ttsVoiceId` |
| `company`, `name`, `product`, etc. | never overridden by `identity.companyName` |

| Section | Runtime effect |
|---------|----------------|
| `identity` | Personality tone/formality in system prompt |
| `campaign_type` | Vertical agent mission injected into system prompt; overrides default sales discovery/booking framing for non-Sales campaigns |
| `conversationFlow.stages` | Stage machine: each LLM turn gets active stage goal; advances on `maxTurns`, exit heuristics, or `skipToTargets` |
| `knowledgeGrounding` | Strict claim whitelist/blacklist; `unknownFactFallbackLine` when unsure |
| `objectionHandling` | Objection library injected into prompt; max attempts → soft close + end call |
| `behavioralGuardrails` | Hard `maxSentencesPerTurn` cap; escalation triggers (anger: one retry then exit; human request: immediate exit) |
| `compliance` | AI disclosure prepended to greeting; `maxCallDurationSec` timer; opt-out phrase ends call + sets DNC flag |
| `bookingClose` | At `slot_suggestion` stage, offers slots from `available_meet_slots` when provided; otherwise calendar stub via `calendarSourceId` |
| `personalization.enabledPresetIds` | Suggestive hints only (not enforced) |

**Agent-initiated hangup:** The LLM may append `<<HANGUP>>` to a closing line when the conversation is clearly done (booking confirmed, mutual goodbye, or no interest after objection handling). The service also hangs up automatically on opt-out, escalation, max objections, max call duration, or when the agent reaches the `close` stage with a goodbye. `call.metadata.endCallReason` records why:

| `endCallReason` | When |
|-----------------|------|
| `agent_confident_end` | LLM appended `<<HANGUP>>` |
| `conversation_complete` | `close` stage + goodbye phrasing |
| `booking_confirmed` | Slot booked + goodbye |
| `no_interest` | Max objections, repeated not-interested, or soft close |
| `opt_out` | Lead requested do-not-call |
| `explicit_human_request` | Lead asked for a human |
| `anger_detected` | Anger escalation after one retry |
| `failed_objection_handles` | Objection library exhausted |
| `max_call_duration` | `maxCallDurationSec` timer fired |

If both `system_prompt` and `agent_config` are sent, custom prompt is used **and** agent_config rules are appended.

Invalid `agent_config` returns **`400`** with Pydantic validation errors.

#### Example request
```json
{
  "to": "+918102244713",
  "call_type": "campaign",
  "companyId": "cmp_abc",
  "leadId": "lead_xyz",
  "campaignId": "camp_123",
  "campaign_type": "Sales",
  "name": "Rahul",
  "company": "Acme Corp",
  "product": "GEO optimization services",
  "perks_of_product": "10% off first month",
  "info_about_lead": "Small business owner, price-sensitive",
  "language": "English",
  "deepgram_language": "en"
}
```

#### Instant response — campaign (`call_type: "campaign"`)

**`200 OK`**
```json
{
  "status": "queued",
  "cfg_token": "550e8400-e29b-41d4-a716-446655440000",
  "job_id": "arq-job-id"
}
```

Poll `GET /call/status/{cfg_token}` or wait for `call.completed` webhook.

> **Direct mode** (`USE_ARQ_QUEUE=false`): campaign and on-demand both return immediately with `status: "initiated"` (no `queued`, no `job_id`, no 503).

#### Instant response — on-demand (`call_type: "on_demand"`)

Waits up to **15s** for Telnyx dial to succeed.

**`200 OK`** (slot available, call dialed)
```json
{
  "call_control_id": "v3:xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "call_leg_id": "5dab5d24-73af-11f1-af53-3241a1c6a502",
  "status": "initiated",
  "opening_greeting": "Hi, Rahul, This is Annie calling from Acme Corp...",
  "cfg_token": "550e8400-e29b-41d4-a716-446655440000",
  "job_id": "arq-job-id"
}
```

**`503 Service Unavailable`** (both on-demand slots busy >15s)
```json
{ "detail": "Agent busy, try again shortly" }
```

#### Errors

| Status | `detail` |
|--------|----------|
| `400` | Missing `to`, invalid E.164, invalid `call_type` |
| `502` | Telnyx dial failed (direct mode only) |
| `503` | Service not ready (Redis/Arq unavailable when `USE_ARQ_QUEUE=true`) |

#### Call status polling

### `GET /call/status/{cfg_token}`

**`200 OK`**
```json
{
  "cfg_token": "550e8400-e29b-41d4-a716-446655440000",
  "status": "connected"
}
```

**Status values:** `queued` → `dialling` → `connected` → `completed` | `expired` | `dial_failed` | `timeout`

**`404`** — token not found or TTL expired (24h for status key).

#### Webhook — `call.completed`

Posted to `WEBHOOK_CALL` when the media stream ends.

```json
{
  "event": "call.completed",
  "eventId": "evt_a1b2c3d4e5f6...",
  "occurredAt": "2026-06-19T12:34:56.789Z",
  "companyId": "cmp_abc",
  "meet_scheduled": "Tuesday June 24 at 2:00 PM",
  "call": {
    "externalCallId": "550e8400-e29b-41d4-a716-446655440000",
    "callLegId": "5dab5d24-73af-11f1-af53-3241a1c6a502",
    "leadId": "lead_xyz",
    "phone": "+918102244713",
    "direction": "OUTBOUND",
    "status": "COMPLETED",
    "startedAt": "2026-06-19T12:30:00.000Z",
    "endedAt": "2026-06-19T12:34:56.789Z",
    "durationSec": 296,
    "connected": true,
    "outcome": "INTERESTED",
    "followUpAgreed": true,
    "followUpAt": "2026-06-20T15:30:00Z",
    "meet_scheduled": "Tuesday June 24 at 2:00 PM",
    "sentiment": "POSITIVE",
    "costCents": null,
    "recordingUrl": null,
    "campaignId": "camp_123",
    "metadata": {
      "provider": "telnyx",
      "language": "en",
      "voiceModel": "eleven_flash_v2_5",
      "llmProvider": "claude",
      "conversationStage": "close",
      "escalationReason": null,
      "dncRequested": false,
      "calendarSourceId": "cal_primary_001",
      "offeredSlots": ["Tue, Jun 24, 2:00 PM UTC", "Tue, Jun 24, 2:30 PM UTC"],
      "selectedSlotId": "2026-06-24T14:00:00.000Z",
      "meetScheduled": "Tue Jun 24 2:00 PM",
      "objectionAttempts": 0,
      "bookingConfirmed": true,
      "endCallReason": "conversation_complete"
    }
  },
  "transcript": {
    "summary": "Lead showed interest and agreed to a follow-up.",
    "turns": [
      { "role": "agent", "text": "Hi, Rahul...", "ts": 0.0 },
      { "role": "user", "text": "Yes, go ahead.", "ts": 3.42 },
      { "role": "agent", "text": "Great, how are you getting customers online?", "ts": 5.1 }
    ],
    "objections": ["price concern"],
    "aiConfidence": 0.85,
    "suggestedNextMove": "Send pricing email and schedule demo",
    "qa": {
      "How are you getting customers online?": "Mostly word of mouth."
    }
  }
}
```

**`call.status`:** `COMPLETED` if media stream started; `FAILED` if never connected.

**`call.outcome`:** LLM-analyzed; values depend on `campaign_type` sent with the call (default `Sales`):

| Campaign type | `call.outcome` values |
|---------------|----------------------|
| **Sales** | `DO_NOT_CALL`, `WRONG_NUMBER`, `MEET_REQUESTED`, `CALLBACK`, `INTERESTED`, `NOT_INTERESTED`, `VOICEMAIL`, `NO_ANSWER`, `UNKNOWN` |
| **Collection** | `DO_NOT_CALL`, `WRONG_NUMBER`, `DISPUTED`, `PAID_IN_FULL`, `PARTIAL_PAYMENT_MADE`, `PROMISE_TO_PAY`, `HARDSHIP_CLAIMED`, `CALLBACK_REQUESTED`, `REFUSED_TO_PAY`, `VOICEMAIL`, `NO_ANSWER`, `UNKNOWN` |
| **Appointment reminders** | `DO_NOT_CALL`, `WRONG_NUMBER`, `CONFIRMED`, `RESCHEDULE_REQUESTED`, `CANCELLED`, `VOICEMAIL`, `NO_ANSWER`, `UNKNOWN` |
| **Feedback / NPS** | `DO_NOT_CALL`, `WRONG_NUMBER`, `RESPONDED_POSITIVE`, `RESPONDED_NEUTRAL`, `RESPONDED_NEGATIVE`, `DECLINED_SURVEY`, `VOICEMAIL`, `NO_ANSWER`, `UNKNOWN` |
| **Renewal / win-back** | `DO_NOT_CALL`, `WRONG_NUMBER`, `RENEWED`, `DECLINED_RENEWAL`, `DOWNGRADE_REQUESTED`, `NEEDS_DISCOUNT_APPROVAL`, `CALLBACK`, `VOICEMAIL`, `NO_ANSWER`, `UNKNOWN` |

`transcript.objections` is reused semantically: sales/collections objections, or survey feedback themes for NPS campaigns.

**`call.sentiment`:** `POSITIVE` | `NEUTRAL` | `NEGATIVE`.

---

## Outbound SMS

### `POST /sms/send`

Queues SMS via Telnyx when `USE_ARQ_QUEUE=true`; sends inline when `false`.

#### Request body

| Field | Required | Type | Notes |
|-------|----------|------|-------|
| `to` | **yes** | string | E.164 recipient |
| `message` | **yes** | string | SMS body |
| `from` | no | string | E.164 sender; defaults to `TELNYX_PHONE_NUMBER` |
| `message_type` | no | string | `"campaign"` (default) \| `"on_demand"` |
| `companyId` | no | string | Webhook metadata |
| `leadId` | no | string | |
| `campaignId` | no | string | |

#### Example request
```json
{
  "to": "+918102244713",
  "message": "Hi Rahul, following up on our offer.",
  "from": "+18148591037",
  "message_type": "campaign",
  "companyId": "cmp_abc",
  "leadId": "lead_xyz",
  "campaignId": "camp_123"
}
```

#### Instant response — campaign

**`200 OK`**
```json
{
  "status": "queued",
  "task_token": "660e8400-e29b-41d4-a716-446655440001",
  "job_id": "arq-job-id"
}
```

> **Direct mode:** always returns `{ "status": "sent", "id", "task_token" }` immediately (no `queued`, no `job_id`).

#### Instant response — on-demand

**`200 OK`**
```json
{
  "status": "sent",
  "id": "telnyx-message-uuid",
  "task_token": "660e8400-e29b-41d4-a716-446655440001",
  "job_id": "arq-job-id"
}
```

**`503`**
```json
{ "detail": "Agent busy, try again shortly" }
```

#### Errors

| Status | `detail` |
|--------|----------|
| `400` | Missing `to`/`message`, invalid E.164, empty message, invalid `message_type` |
| `502` | Provider send failed (direct mode) |
| `503` | Service not ready (when `USE_ARQ_QUEUE=true`) |

#### Message status polling

### `GET /message/status/{task_token}`

**`200 OK`**
```json
{
  "task_token": "660e8400-e29b-41d4-a716-446655440001",
  "status": "sent",
  "channel": "sms"
}
```

**Status values:** `queued` → `sending` → `sent` | `failed` | `expired`

`channel` is `"sms"` or `"email"` while job config still in Redis; may be `null` after job completes.

#### Webhook — `sms.completed`

Posted to `WEBHOOK_SMS` after send attempt (from messaging worker).

```json
{
  "event": "sms.completed",
  "eventId": "evt_a1b2c3d4e5f6...",
  "occurredAt": "2026-06-19T12:00:00.000Z",
  "companyId": "cmp_abc",
  "leadId": "lead_xyz",
  "campaignId": "camp_123",
  "message": {
    "taskToken": "660e8400-e29b-41d4-a716-446655440001",
    "externalId": "telnyx-message-uuid",
    "from": "+18148591037",
    "to": "+918102244713",
    "body": "Hi Rahul, following up on our offer.",
    "status": "SENT",
    "messageType": "campaign",
    "provider": "telnyx",
    "error": null
  }
}
```

**`message.status`:** `SENT` | `FAILED` | `EXPIRED`

---

## Outbound email

### `POST /email/send`

Queues email via Resend when `USE_ARQ_QUEUE=true`; sends inline when `false`.

#### Request body

| Field | Required | Type | Notes |
|-------|----------|------|-------|
| `to` | **yes** | string | Recipient email (comma-separated for multiple) |
| `subject` | **yes** | string | Email subject |
| `body` | **yes** | string | HTML body |
| `from` | no* | string | Must be verified in Resend; falls back to `DEFAULT_FROM_EMAIL` env |
| `text` | no | string | Plain-text fallback |
| `message_type` | no | string | `"campaign"` (default) \| `"on_demand"` |
| `companyId` | no | string | |
| `leadId` | no | string | |
| `campaignId` | no | string | |

\*Required if `DEFAULT_FROM_EMAIL` is not set on the server.

#### Example request
```json
{
  "to": "lead@example.com",
  "from": "Annie <sales@yourdomain.com>",
  "subject": "Follow up on your inquiry",
  "body": "<p>Hi Rahul,</p><p>Thanks for your interest...</p>",
  "text": "Hi Rahul, Thanks for your interest...",
  "message_type": "campaign",
  "companyId": "cmp_abc",
  "leadId": "lead_xyz",
  "campaignId": "camp_123"
}
```

#### Instant response

Same shape as SMS (queued mode). In **direct mode** (`USE_ARQ_QUEUE=false`), both campaign and on-demand return `{ "status": "sent", "id", "task_token" }` immediately.

**Campaign `200`**
```json
{
  "status": "queued",
  "task_token": "770e8400-e29b-41d4-a716-446655440002",
  "job_id": "arq-job-id"
}
```

**On-demand `200`**
```json
{
  "status": "sent",
  "id": "resend-message-id",
  "task_token": "770e8400-e29b-41d4-a716-446655440002",
  "job_id": "arq-job-id"
}
```

**On-demand `503`**
```json
{ "detail": "Agent busy, try again shortly" }
```

Poll `GET /message/status/{task_token}` for campaign sends.

#### Webhook — `email.completed`

Posted to `WEBHOOK_EMAIL` after send attempt.

```json
{
  "event": "email.completed",
  "eventId": "evt_a1b2c3d4e5f6...",
  "occurredAt": "2026-06-19T12:00:00.000Z",
  "companyId": "cmp_abc",
  "leadId": "lead_xyz",
  "campaignId": "camp_123",
  "message": {
    "taskToken": "770e8400-e29b-41d4-a716-446655440002",
    "externalId": "resend-message-id",
    "from": "Annie <sales@yourdomain.com>",
    "to": "lead@example.com",
    "subject": "Follow up on your inquiry",
    "status": "SENT",
    "messageType": "campaign",
    "provider": "resend",
    "error": null
  }
}
```

**`message.status`:** `SENT` | `FAILED` | `EXPIRED`

On failure, `externalId` is `null` and `error` contains a short reason string.

---

## Telnyx inbound webhook (stub)

### `POST /webhook`

Telnyx call-control events (not used for completion today).

**Response `200`**
```json
{ "ok": true }
```

---

## WebSocket (internal — Telnyx media)

### `WS /media-stream/{cfg_token}`

Opened by Telnyx when a call is dialed. Not called by your frontend directly. `cfg_token` matches `cfg_token` from `/call/outbound`.

---

## Quick reference — instant vs webhook

| Action | Instant HTTP | Async webhook |
|--------|--------------|---------------|
| Call campaign | `{ status: "queued", cfg_token }` | `call.completed` |
| Call on-demand | `{ status: "initiated", call_control_id, call_leg_id }` or `503` | `call.completed` |
| SMS campaign | `{ status: "queued", task_token }` | `sms.completed` |
| SMS on-demand | `{ status: "sent", id }` or `503` | `sms.completed` |
| Email campaign | `{ status: "queued", task_token }` | `email.completed` |
| Email on-demand | `{ status: "sent", id }` or `503` | `email.completed` |

---

## Verify webhook signature (example)

```python
import hmac
import hashlib

def verify_webhook(raw_body: bytes, signature_header: str, secret: str) -> bool:
    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)
```

Read the **raw request body** before JSON parsing when verifying.

---

## Environment variables (integration)

```bash
# Required for API
REDIS_URL=rediss://...
PUBLIC_BASE_URL=https://your-api.onrender.com
TELNYX_API_KEY=...
TELNYX_PHONE_NUMBER=+1...
TELNYX_CONNECTION_ID=...

# Email
RESEND_API_KEY=re_...
DEFAULT_FROM_EMAIL="Annie <sales@yourdomain.com>"

# Webhooks (full URLs)
WEBHOOK_CALL=https://your-app.com/webhook/call
WEBHOOK_SMS=https://your-app.com/webhook/sms
WEBHOOK_EMAIL=https://your-app.com/webhook/email
WEBHOOK_SECRET=shared-hmac-secret

# Legacy call webhook fallback
NEXT_JS_SERVICE_URL=https://your-app.com
```
