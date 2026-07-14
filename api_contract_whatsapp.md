# WhatsApp API Contract

**Base URL:** `https://calling-agent-ki3j.onrender.com`  
**Provider:** Telnyx (`POST /v2/messages/whatsapp`)  
**Sender Number:** `+15559182309` (configured via `TELNYX_WHATSAPP_NUMBER`)

---

## POST `/whatsapp/send`

Send a WhatsApp message to a recipient. Supports two modes:

- **Text mode** — free-form message, only valid within a **24-hour window** after the recipient last messaged your business number.
- **Template mode** — uses a Meta-approved message template, works **anytime** with no window restriction.

---

### Request

**Headers**
```
Content-Type: application/json
```

**Body**

| Field | Type | Required | Description |
|---|---|---|---|
| `to` | `string` | ✅ Yes | Recipient phone number in E.164 format (e.g. `+918102244713`) |
| `message` | `string` | ✅ Yes (text mode) | Message body. Required when `template_name` is not set. |
| `from` | `string` | No | Sender number in E.164 format. Defaults to `TELNYX_WHATSAPP_NUMBER` env var. |
| `message_type` | `string` | No | `"campaign"` (default) or `"on_demand"`. See [Message Types](#message-types). |
| `template_name` | `string` | No | Name of a Meta-approved template. Enables template mode. |
| `template_language` | `string` | No | Template language code. Default: `"en_US"`. |
| `template_language_policy` | `string` | No | Template language policy. Default: `"deterministic"`. |
| `template_components` | `array` | No | Variable bindings for template placeholders. See [Template Components](#template-components). |
| `preview_url` | `boolean` | No | Whether to expand URLs in text messages. Default: `false`. Text mode only. |
| `companyId` | `string` | No | Forwarded as-is to the completion webhook. |
| `leadId` | `string` | No | Forwarded as-is to the completion webhook. |
| `campaignId` | `string` | No | Forwarded as-is to the completion webhook. |

---

### Responses

#### `200 OK` — Sent directly (when `USE_ARQ_QUEUE=false`)
```json
{
  "status": "sent",
  "id": "0193a8e3-7e46-4e2c-a1b3-6f4d3a5c0f92",
  "task_token": "550e8400-e29b-41d4-a716-446655440000"
}
```

#### `200 OK` — Queued (when `USE_ARQ_QUEUE=true`, `message_type="campaign"`)
```json
{
  "status": "queued",
  "task_token": "550e8400-e29b-41d4-a716-446655440000",
  "job_id": "arq:job:abc123"
}
```

#### `200 OK` — Delivered synchronously (when `USE_ARQ_QUEUE=true`, `message_type="on_demand"`)
```json
{
  "status": "sent",
  "id": "0193a8e3-7e46-4e2c-a1b3-6f4d3a5c0f92",
  "task_token": "550e8400-e29b-41d4-a716-446655440000",
  "job_id": "arq:job:abc123"
}
```

#### `400 Bad Request` — Validation failure
```json
{ "detail": "Missing 'to'" }
{ "detail": "Missing 'message' (required when not using a template)" }
{ "detail": "'to' must be in +E164 format, e.g. +918102244713 (got '9876543210')" }
{ "detail": "'from' must be valid +E164" }
{ "detail": "message_type must be 'on_demand' or 'campaign'" }
{ "detail": "'message' must be non-empty" }
```

#### `502 Bad Gateway` — Telnyx rejected the send
```json
{ "detail": "provider send failed" }
```
Common causes: template not approved, number not registered on WhatsApp, outside 24-hour window (text mode).

#### `503 Service Unavailable` — Agent busy / timeout (on_demand only)
```json
{ "detail": "Agent busy, try again shortly" }
```

---

## GET `/message/status/{task_token}`

Poll the delivery status of a previously sent WhatsApp message.

### Response
```json
{
  "task_token": "550e8400-e29b-41d4-a716-446655440000",
  "status": "sent",
  "channel": "whatsapp"
}
```

**Status values:**

| Status | Meaning |
|---|---|
| `sending` | Telnyx API call in progress |
| `sent` | Telnyx accepted the message |
| `failed` | Telnyx rejected or send failed |
| `queued` | Waiting in ARQ job queue |
| `expired` | `on_demand` job timed out (>15s) |

---

## Completion Webhook (outbound)

When the send attempt completes, the server POSTs to `WEBHOOK_WHATSAPP` (if configured).

**Event:** `whatsapp.completed`

```json
{
  "event": "whatsapp.completed",
  "eventId": "evt_a1b2c3d4e5f6...",
  "occurredAt": "2026-07-14T09:25:00.000Z",
  "companyId": "abc",
  "leadId": "xyz",
  "campaignId": "camp1",
  "message": {
    "taskToken": "550e8400-e29b-41d4-a716-446655440000",
    "externalId": "0193a8e3-7e46-4e2c-a1b3-6f4d3a5c0f92",
    "from": "+15559182309",
    "to": "+918102244713",
    "body": "Hey! Following up on your enquiry.",
    "templateName": null,
    "status": "SENT",
    "messageType": "campaign",
    "provider": "telnyx_whatsapp",
    "error": null
  }
}
```

**`message.status` values:** `SENT` | `FAILED` | `EXPIRED`

The webhook is signed with `WEBHOOK_SECRET` in the header `x-calling-agent-signature: sha256=<hex>`.

---

## Telnyx Delivery Webhooks (inbound)

Telnyx sends real-time delivery events to `POST /webhook` on this server (configured in Telnyx Portal → Messaging → Messaging Profiles → Webhook URL).

| Event | Meaning |
|---|---|
| `message.sent` | Message accepted by WhatsApp |
| `message.delivered` | Delivered to recipient's device |
| `message.read` | Recipient opened the message |
| `message.failed` | Delivery failed (with error details) |

The server currently **logs** these events but does not forward them.

---

## Message Types

| `message_type` | Behaviour |
|---|---|
| `"campaign"` (default) | Fire-and-forget. Returns immediately with `status: queued` (ARQ) or `status: sent` (direct). |
| `"on_demand"` | Waits up to **15 seconds** for delivery confirmation before returning. Returns `503` if timeout exceeded. |

---

## Template Components

Used to fill in `{{1}}`, `{{2}}` ... placeholders in Meta-approved templates.

```json
"template_components": [
  {
    "type": "body",
    "parameters": [
      { "type": "text", "text": "John" },
      { "type": "text", "text": "Monday 9am" }
    ]
  }
]
```

Other supported parameter types: `"currency"`, `"date_time"`, `"image"`, `"document"`, `"video"`.

---

## Example Requests

### Text message (within 24-hour window)
```bash
curl -X POST https://calling-agent-ki3j.onrender.com/whatsapp/send \
  -H "Content-Type: application/json" \
  -d '{
    "to": "+918102244713",
    "message": "Hey! Following up on your enquiry.",
    "message_type": "on_demand"
  }'
```

### Template message (works anytime)
```bash
curl -X POST https://calling-agent-ki3j.onrender.com/whatsapp/send \
  -H "Content-Type: application/json" \
  -d '{
    "to": "+918102244713",
    "message_type": "campaign",
    "template_name": "welcome_message",
    "template_language": "en_US",
    "template_language_policy": "deterministic",
    "template_components": [
      {
        "type": "body",
        "parameters": [
          { "type": "text", "text": "John" }
        ]
      }
    ],
    "companyId": "abc",
    "leadId": "lead_001",
    "campaignId": "camp_001"
  }'
```

### Poll delivery status
```bash
curl https://calling-agent-ki3j.onrender.com/message/status/<task_token>
```

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `TELNYX_API_KEY` | ✅ | Telnyx API key |
| `TELNYX_WHATSAPP_NUMBER` | ✅ | WhatsApp-enabled sender number (e.g. `+15559182309`). Falls back to `TELNYX_PHONE_NUMBER` if unset. |
| `WEBHOOK_WHATSAPP` | No | URL to receive `whatsapp.completed` POST callbacks |
| `WEBHOOK_SECRET` | No | HMAC secret for signing completion webhook payloads |
| `USE_ARQ_QUEUE` | No | `"true"` to use ARQ worker queue, `"false"` for direct send (default: `"true"`) |
