import audioop
import asyncio
import base64
import contextlib
import datetime as dt
import json
import re

import websockets
from fastapi import WebSocket, WebSocketDisconnect
from sarvamai import AsyncSarvamAI

from agent_config import (
    ConversationSession,
    append_call_end_instructions,
    apply_collections_hangup_gate,
    evaluate_hangup_confidence,
    parse_agent_reply,
)
from arq_jobs import done_key, hangup_telnyx_call, set_call_status
from calendar_provider import default_calendar_provider
from config import (
    CALL_RECORDING_DISCLOSURE,
    DEEPGRAM_API_KEY,
    ELEVENLABS_VOICE_ID,
    MAX_CONSECUTIVE_SILENCE_NUDGES,
    MIN_WORDS_TO_RESPOND,
    SARVAM_API_KEY,
    GREETING_BARGE_IN_GUARD_SEC,
    SILENCE_HANGUP_LINE,
    SILENCE_NUDGE_SEC,
    deepgram_ws_url,
    to_sarvam_lang,
)
from intent_detection import build_intent_context, detect_intents
from llm import CLAUDE_SILENCE_USER_CUE, LLM_FALLBACK_REPLY, ask_llm, ask_llm_stream
from tts import sarvam_text_to_mp3_chunks, text_to_audio_chunks
from webhook import send_call_completed_webhook, send_call_status_webhook

_VOICEMAIL_PHRASE = (
    "forwarded to voice mail the person you are trying to reach is not available"
)


def _normalize_transcript_for_match(text: str) -> str:
    s = re.sub(r"[^a-z0-9 ]", "", text.lower())
    s = re.sub(r"\s+", " ", s).strip()
    return s.replace("voice mail", "voicemail")


def is_voicemail_transcript(text: str) -> bool:
    normalized = _normalize_transcript_for_match(text)
    target = _normalize_transcript_for_match(_VOICEMAIL_PHRASE)
    return target in normalized


async def _shutdown_task(task: asyncio.Task, call_sid: str, label: str) -> None:
    """Cancel a background task and wait for it to exit."""
    if task.done():
        if not task.cancelled():
            exc = task.exception()
            if exc:
                print(
                    f"[{call_sid}] {label} finished with error: {type(exc).__name__}: {exc}",
                    flush=True,
                )
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    print(f"[{call_sid}] {label} stopped", flush=True)


async def _finalize_call(
    call_sid: str,
    *,
    started_at: dt.datetime | None,
    ended_at: dt.datetime,
    connected: bool,
    turns: list[dict],
    call_cfg: dict,
    redis_client,
    cfg_token: str | None,
) -> None:
    """Update Redis status and deliver call.completed webhook."""
    if redis_client and cfg_token:
        try:
            await redis_client.rpush(done_key(cfg_token), "1")
            await set_call_status(redis_client, cfg_token, "completed")
        except Exception as e:
            print(
                f"[{call_sid}] call:done signal error: {type(e).__name__}: {e}",
                flush=True,
            )

    duration_sec = int((ended_at - started_at).total_seconds()) if started_at else 0
    call_record = {
        "call_sid": call_sid,
        "started_at": started_at,
        "ended_at": ended_at,
        "connected": connected,
        "duration_sec": duration_sec,
        "turns": turns,
    }
    try:
        print(f"[{call_sid}] Sending call.completed webhook…", flush=True)
        if call_cfg.get("_conversation_state"):
            call_cfg.setdefault("conversation_metadata", call_cfg["_conversation_state"])
        await send_call_completed_webhook(call_record, call_cfg)
    except Exception as e:
        print(f"[{call_sid}] Webhook dispatch error: {type(e).__name__}: {e}", flush=True)


async def run_media_stream(
    websocket: WebSocket,
    call_sid: str,
    call_cfg: dict,
    *,
    redis_client=None,
    cfg_token: str | None = None,
) -> None:
    await websocket.accept()
    print(f"[{call_sid}] Telnyx WebSocket connected", flush=True)

    system_prompt = append_call_end_instructions(
        call_cfg["system_prompt"], call_cfg.get("campaign_type")
    )
    opening_greeting = call_cfg["opening_greeting"]
    base_system_prompt = system_prompt
    session: ConversationSession | None = None
    if call_cfg.get("agent_config"):
        session = ConversationSession(call_cfg)
        print(
            f"[{call_sid}] campaign_vertical={session.campaign_vertical()}",
            flush=True,
        )
        if call_cfg.get("_ai_disclosure_done"):
            session.ai_disclosure_done = True
    duration_timer: asyncio.Task | None = None
    call_control_id: str | None = None
    hangup_requested = False
    el_model = call_cfg["elevenlabs_model"]
    voice_id = call_cfg["voice_id"]
    dg_url = deepgram_ws_url(call_cfg["deepgram_language"])
    llm_provider = call_cfg["llm_provider"]
    llm_model = call_cfg["llm_model"]
    stt_provider = call_cfg["stt_provider"]
    use_sarvam_tts = call_cfg.get("use_sarvam_tts", False)
    sarvam_speaker = call_cfg.get("sarvam_speaker", "rohan")
    sarvam_tts_lang = to_sarvam_lang(call_cfg["deepgram_language"])

    tts_label = f"sarvam({sarvam_speaker})" if use_sarvam_tts else "elevenlabs"
    print(f"[{call_sid}] STT: {stt_provider} | TTS: {tts_label} | LLM: {llm_provider}/{llm_model}", flush=True)

    audio_queue: asyncio.Queue = asyncio.Queue()
    conversation_history: list[dict] = []
    transcript_buffer: list[str] = []
    stream_id: str | None = None

    # ── Barge-in state ─────────────────────────────────────────────────────────
    # Use asyncio.Event so setting it from receive_from_telnyx() wakes up
    # the send_audio() coroutine between chunks without polling.
    agent_speaking = False
    barge_in_event = asyncio.Event()   # set = user is speaking → stop TTS

    started_at: dt.datetime | None = None
    ended_at: dt.datetime | None = None
    connected = False
    turns: list[dict] = []
    first_user_turn = True
    user_has_spoken = False
    silence_nudge_count = 0
    silence_timer_task: asyncio.Task | None = None
    silence_handling = False
    greeting_barge_guard_until: dt.datetime | None = None
    waiting_for_user_since: dt.datetime | None = None
    _last_silence_log_sec = -1

    def _barge_in_allowed() -> bool:
        if greeting_barge_guard_until is None:
            return True
        return dt.datetime.now(dt.timezone.utc) >= greeting_barge_guard_until

    def _silence_watch_active() -> bool:
        if hangup_requested:
            return False
        if session and session.call_should_end:
            return False
        if not connected:
            return False
        return True

    def _silence_elapsed_sec() -> float:
        if waiting_for_user_since is None:
            return 0.0
        return (
            dt.datetime.now(dt.timezone.utc) - waiting_for_user_since
        ).total_seconds()

    async def _cancel_silence_timer(reason: str = "") -> None:
        nonlocal silence_timer_task, waiting_for_user_since, _last_silence_log_sec
        elapsed = _silence_elapsed_sec()
        if silence_timer_task and not silence_timer_task.done():
            silence_timer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await silence_timer_task
        silence_timer_task = None
        if waiting_for_user_since is not None and elapsed > 0:
            suffix = f" ({reason})" if reason else ""
            print(
                f"[{call_sid}] 🔇 Silence timer cancelled after {elapsed:.1f}s{suffix}",
                flush=True,
            )
        waiting_for_user_since = None
        _last_silence_log_sec = -1

    async def _arm_silence_timer(reason: str = "agent_done") -> None:
        nonlocal silence_timer_task, waiting_for_user_since, _last_silence_log_sec
        if not user_has_spoken:
            print(
                f"[{call_sid}] 🔇 Silence watch skipped — "
                "no user speech yet (timer starts after first user turn)",
                flush=True,
            )
            return
        if not _silence_watch_active():
            print(f"[{call_sid}] 🔇 Silence watch skipped — call inactive", flush=True)
            return
        if agent_speaking:
            print(f"[{call_sid}] 🔇 Silence watch skipped — agent speaking", flush=True)
            return
        if silence_handling:
            print(
                f"[{call_sid}] 🔇 Silence watch skipped — LLM nudge in flight",
                flush=True,
            )
            return
        await _cancel_silence_timer("re-arm")
        waiting_for_user_since = dt.datetime.now(dt.timezone.utc)
        _last_silence_log_sec = -1
        print(
            f"[{call_sid}] 🔇 Silence watch armed ({reason}) — "
            f"nudge at {SILENCE_NUDGE_SEC}s",
            flush=True,
        )

        async def _watch() -> None:
            nonlocal _last_silence_log_sec
            try:
                while True:
                    await asyncio.sleep(0.25)
                    if not _silence_watch_active() or agent_speaking or silence_handling:
                        return
                    elapsed = _silence_elapsed_sec()
                    whole = int(elapsed)
                    if whole > 0 and whole != _last_silence_log_sec:
                        _last_silence_log_sec = whole
                        print(
                            f"[{call_sid}] 🔇 Silence {whole}s / {SILENCE_NUDGE_SEC:.0f}s",
                            flush=True,
                        )
                    if elapsed >= SILENCE_NUDGE_SEC:
                        await _on_silence_timeout(elapsed)
                        return
            except asyncio.CancelledError:
                pass

        silence_timer_task = asyncio.create_task(_watch())

    async def _handle_silence_nudge() -> None:
        nonlocal silence_handling
        silence_handling = True
        silence_hint = (
            "\n\n## Silence\n"
            "The user has been silent for several seconds. In one short sentence, "
            "check they are still on the line and continue the conversation toward "
            "the current goal."
        )
        try:
            # Build the prompt only — no I/O here so the try block is fast
            if session:
                turn_prompt = session.build_turn_prompt(base_system_prompt) + silence_hint
            else:
                turn_prompt = system_prompt + silence_hint
        finally:
            # Must be False before _stream_speak so the silence timer can arm
            # normally after TTS finishes playing the nudge phrase.
            silence_handling = False

        await _stream_speak(turn_prompt, trailing_user_cue=CLAUDE_SILENCE_USER_CUE)
        if session:
            session.stage_turn_count += 1
            session.sync_to_cfg()
        print(f"[{call_sid}] 📤 Silence nudge complete — waiting for user", flush=True)

    async def _on_silence_timeout(elapsed_at_fire: float | None = None) -> None:
        nonlocal silence_nudge_count
        if not _silence_watch_active() or agent_speaking or silence_handling:
            return

        elapsed = elapsed_at_fire if elapsed_at_fire is not None else _silence_elapsed_sec()
        silence_nudge_count += 1
        print(
            f"[{call_sid}] 🔇 Silence threshold reached: {elapsed:.1f}s "
            f"(nudge {silence_nudge_count}/{MAX_CONSECUTIVE_SILENCE_NUDGES}) — agent speaking",
            flush=True,
        )
        await _cancel_silence_timer("timeout")

        if silence_nudge_count > MAX_CONSECUTIVE_SILENCE_NUDGES:
            await _speak_and_maybe_hangup(SILENCE_HANGUP_LINE, "no_response")
            return

        await _handle_silence_nudge()

    def _ts() -> float:
        if started_at is None:
            return 0.0
        return round((dt.datetime.now(dt.timezone.utc) - started_at).total_seconds(), 2)

    async def _hangup_call(reason: str) -> None:
        nonlocal hangup_requested
        if hangup_requested:
            return
        hangup_requested = True
        if session:
            session.call_should_end = True
            session.end_call_reason = reason
            session.sync_to_cfg()
        elif call_cfg is not None:
            call_cfg["_conversation_state"] = {
                **(call_cfg.get("_conversation_state") or {}),
                "endCallReason": reason,
            }
        if call_control_id:
            await hangup_telnyx_call(call_control_id)
        print(f"[{call_sid}] Call ended by agent ({reason})", flush=True)

    async def _speak_and_maybe_hangup(text: str, hangup_reason: str | None = None) -> None:
        spoken, marker_hangup = parse_agent_reply(text)
        if session:
            spoken, marker_hangup = apply_collections_hangup_gate(
                session, spoken, marker_hangup
            )
        should_hangup = bool(hangup_reason)
        reason = hangup_reason

        if not should_hangup:
            if session:
                should_hangup, reason = evaluate_hangup_confidence(
                    session, spoken, marker_hangup
                )
            elif marker_hangup:
                should_hangup = True
                reason = "agent_confident_end"

        if spoken.strip() != LLM_FALLBACK_REPLY:
            conversation_history.append({"role": "assistant", "content": spoken})
        else:
            print(
                f"[{call_sid}] ⚠️ LLM fallback spoken — omitted from history",
                flush=True,
            )
        turns.append({"role": "agent", "text": spoken, "ts": _ts()})
        print(f"[{call_sid}] 🤖 [{llm_provider}] Agent: {spoken}", flush=True)
        await send_audio(spoken)

        if should_hangup and reason:
            await _cancel_silence_timer("hangup")
            await _hangup_call(reason)

    async def _stream_speak(
        prompt: str,
        *,
        trailing_user_cue: str | None = None,
    ) -> str:
        """
        Stream the LLM response phrase-by-phrase directly to TTS.

        Each phrase is dispatched to send_audio() the moment the LLM produces
        it, overlapping generation and synthesis for lower first-audio latency.

        Handles:
        - Barge-in contract: only text audibly delivered is appended to history
        - <<HANGUP>> marker: dual-layer (per-chunk + post-loop belt-and-suspenders)
        - Max-sentences guardrail: stops streaming once session limit is reached
        - Collections hangup gate and evaluate_hangup_confidence on final text

        Returns full_spoken — the text that was audibly delivered to the caller.
        """
        nonlocal hangup_requested

        max_sentences = session._max_sentences() if session else 99
        sentence_count = 0
        full_spoken = ""
        marker_found = False   # tracks whether <<HANGUP>> appeared anywhere
        pending_hangup = False
        pending_reason: str | None = None

        try:
            async for phrase in ask_llm_stream(
                conversation_history,
                prompt,
                llm_provider,
                llm_model,
                stop_event=barge_in_event,
                trailing_user_cue=trailing_user_cue,
            ):
                if barge_in_event.is_set():
                    print(
                        f"[{call_sid}] ⚡ Barge-in during LLM stream — stopping phrase loop",
                        flush=True,
                    )
                    break

                # Per-chunk HANGUP detection
                phrase_clean, chunk_hangup = parse_agent_reply(phrase)
                if chunk_hangup:
                    marker_found = True
                    pending_hangup = True
                    pending_reason = "agent_confident_end"

                if not phrase_clean.strip():
                    continue
                if phrase_clean.strip() == LLM_FALLBACK_REPLY:
                    print(
                        f"[{call_sid}] ⚠️ LLM fallback in stream — skipping phrase",
                        flush=True,
                    )
                    continue

                await send_audio(phrase_clean)
                full_spoken += phrase_clean + " "

                # Count sentence-ending punctuation for max_sentences guardrail
                sentence_count += sum(1 for ch in phrase_clean if ch in ".?!")
                if sentence_count >= max_sentences:
                    print(
                        f"[{call_sid}] ✂️ max_sentences={max_sentences} reached — stopping stream",
                        flush=True,
                    )
                    break

                if pending_hangup:
                    break  # Speak goodbye phrase then stop

        except Exception as e:
            import traceback as _tb
            print(f"[{call_sid}] _stream_speak error: {type(e).__name__}: {e}", flush=True)
            print(_tb.format_exc(), flush=True)

        full_spoken = full_spoken.strip()

        # ── Belt-and-suspenders: check assembled text for a straddled marker ──
        # Catches the edge case where the sentence-boundary flush split <<HANGUP>>
        # across two chunks so the per-chunk check missed it.
        if full_spoken:
            spoken_clean, belt_hangup = parse_agent_reply(full_spoken)
            if belt_hangup:
                full_spoken = spoken_clean.strip()
                marker_found = True
                if not pending_hangup:
                    pending_hangup = True
                    pending_reason = "agent_confident_end"

        # ── Collections hangup gate ────────────────────────────────────────────
        if session:
            full_spoken, pending_hangup = apply_collections_hangup_gate(
                session, full_spoken, pending_hangup
            )

        # ── Evaluate hangup confidence (goodbyes, stage completion, etc.) ──────
        if not pending_hangup:
            if session:
                pending_hangup, pending_reason = evaluate_hangup_confidence(
                    session, full_spoken, marker_found
                )
            elif marker_found:
                pending_hangup = True
                pending_reason = "agent_confident_end"

        # ── Append ONLY what was audibly delivered to conversation history ──────
        # On barge-in, full_spoken is the partial text the user actually heard.
        # We never append unspoken LLM output — doing so causes hallucinated
        # continuity where the next turn references things the agent never said.
        if full_spoken and full_spoken != LLM_FALLBACK_REPLY:
            conversation_history.append({"role": "assistant", "content": full_spoken})
        else:
            print(
                f"[{call_sid}] ⚠️ LLM stream: nothing/fallback spoken — omitted from history",
                flush=True,
            )

        turns.append({"role": "agent", "text": full_spoken or "", "ts": _ts()})
        print(f"[{call_sid}] 🤖 [{llm_provider}] Agent (streamed): {full_spoken}", flush=True)

        # ── Trigger hangup if signaled ─────────────────────────────────────────
        if pending_hangup and pending_reason and not hangup_requested:
            await _cancel_silence_timer("hangup")
            await _hangup_call(pending_reason)

        return full_spoken

    async def _check_and_handle_voicemail(text: str) -> bool:
        nonlocal connected
        if hangup_requested or not is_voicemail_transcript(text):
            return False
        print(f"[{call_sid}] 📵 Voicemail detected — hanging up immediately", flush=True)
        conversation_history.append({"role": "user", "content": text})
        turns.append({"role": "user", "text": text, "ts": _ts()})
        connected = False
        if session:
            session.call_should_end = True
            session.end_call_reason = "voicemail"
            session.sync_to_cfg()
        await _hangup_call("voicemail")
        return True

    async def _handle_user_turn(user_text: str) -> None:
        nonlocal agent_speaking, first_user_turn, silence_nudge_count, user_has_spoken

        await _cancel_silence_timer("user_turn")
        silence_nudge_count = 0

        if session and session.call_should_end:
            return

        if first_user_turn:
            first_user_turn = False
            if await _check_and_handle_voicemail(user_text):
                return

        user_has_spoken = True
        print(f"[{call_sid}] 🎤 Human: {user_text}", flush=True)
        conversation_history.append({"role": "user", "content": user_text})
        turns.append({"role": "user", "text": user_text, "ts": _ts()})

        if session:
            opt_out_line = session.check_compliance_on_user_input(user_text)
            if opt_out_line:
                await _speak_and_maybe_hangup(opt_out_line, "opt_out")
                return

            escalation_line = session.check_escalation(user_text)
            if escalation_line:
                reason = session.escalation_reason or "escalation"
                await _speak_and_maybe_hangup(escalation_line, reason)
                return

            objection_hint = session.handle_objection(user_text)
            if (
                session.uses_sales_objection_hangup()
                and session.objection_attempts >= session._max_objection_attempts()
            ):
                soft_line = session._soft_close_line()
                session.escalation_reason = "failed_objection_handles"
                await _speak_and_maybe_hangup(soft_line, "no_interest")
                return

            if session.allows_meeting_slots():
                session.ensure_offered_slots()

                if session.current_stage_key == "slot_suggestion":
                    session.detect_slot_selection(user_text)
                elif session.current_stage_key == "confirmation":
                    session.detect_slot_selection(user_text)
                    session.maybe_confirm_booking(user_text)

            session.advance_stage(user_text)

            if (
                session.allows_meeting_slots()
                and session.current_stage_key == "slot_suggestion"
                and not session.offered_slots
            ):
                booking = (call_cfg.get("agent_config") or {}).get("bookingClose") or {}
                source_id = booking.get("calendarSourceId") or "default"
                count = int(booking.get("slotsToOffer") or 2)
                session.offered_slots = await default_calendar_provider.get_available_slots(
                    source_id, count
                )
                session.offered_slot_records = []

            turn_prompt = session.build_turn_prompt(base_system_prompt)

            intents = await asyncio.to_thread(detect_intents, user_text)
            if intents:
                print(f"[{call_sid}] intents={intents}", flush=True)
            intent_block = build_intent_context(intents, session, call_cfg)
            if intent_block:
                turn_prompt += f"\n\n{intent_block}"

            if objection_hint:
                turn_prompt += (
                    f"\n\n## Objection detected\nUse this guidance: {objection_hint}"
                )
            await _stream_speak(turn_prompt)
            session.stage_turn_count += 1
            session.sync_to_cfg()
            print(
                f"[{call_sid}] 📤 User turn handled — waiting for user response",
                flush=True,
            )
        else:
            await _stream_speak(system_prompt)
            print(
                f"[{call_sid}] 📤 User turn handled — waiting for user response",
                flush=True,
            )

    async def _duration_watchdog() -> None:
        if not session:
            return
        max_sec = session._max_call_duration_sec()
        if not max_sec:
            return
        await asyncio.sleep(max_sec)
        if session.call_should_end:
            return
        close_line = (
            "I want to respect your time, so I'll wrap up here. "
            "Thank you — we'll follow up with details. Goodbye."
        )
        session.escalation_reason = session.escalation_reason or "max_call_duration"
        await _speak_and_maybe_hangup(close_line, "max_call_duration")
        print(f"[{call_sid}] Max call duration reached ({max_sec}s)", flush=True)

    # ── TTS sender ─────────────────────────────────────────────────────────────
    async def send_audio(
        text: str,
        override_voice_id: str | None = None,
        *,
        greeting_barge_guard_sec: float | None = None,
    ) -> None:
        nonlocal agent_speaking, greeting_barge_guard_until
        await _cancel_silence_timer("agent_speaking")
        if greeting_barge_guard_sec:
            greeting_barge_guard_until = dt.datetime.now(dt.timezone.utc) + dt.timedelta(
                seconds=greeting_barge_guard_sec
            )
            print(
                f"[{call_sid}] 🛡️ Greeting barge-in blocked for "
                f"{greeting_barge_guard_sec}s",
                flush=True,
            )
        agent_speaking = True
        barge_in_event.clear()  # arm: clear any previous interrupt signal
        print(f"[{call_sid}] 🔊 Speaking: {text[:80]}", flush=True)
        chunk_count = 0

        if use_sarvam_tts:
            tts_stream = sarvam_text_to_mp3_chunks(text, sarvam_tts_lang, sarvam_speaker)
        else:
            tts_stream = text_to_audio_chunks(text, el_model, override_voice_id or voice_id)

        async for audio_b64 in tts_stream:
            # ── Barge-in check BEFORE sending each chunk ──────────────────────
            # Because tts.py now yields small chunks (~250 ms each), this check
            # fires frequently enough to stop playback almost instantly when the
            # user starts speaking.
            if barge_in_event.is_set() and _barge_in_allowed():
                print(f"[{call_sid}] ⚡ Barge-in — discarding remaining TTS chunks", flush=True)
                break
            if barge_in_event.is_set() and not _barge_in_allowed():
                barge_in_event.clear()

            if not audio_b64:
                continue

            try:
                await websocket.send_text(
                    json.dumps({"event": "media", "media": {"payload": audio_b64}})
                )
                chunk_count += 1
            except Exception as e:
                print(f"[{call_sid}] Send error: {e}", flush=True)
                break

        # Only send the mark if we weren't interrupted (avoids a spurious
        # agent_done mark confusing the STT state machine).
        if not barge_in_event.is_set():
            try:
                await websocket.send_text(
                    json.dumps({"event": "mark", "mark": {"name": "agent_done"}})
                )
            except Exception:
                pass

        agent_speaking = False
        print(f"[{call_sid}] ✅ Sent {chunk_count} chunks (interrupted={barge_in_event.is_set()})", flush=True)
        if not barge_in_event.is_set():
            await _arm_silence_timer("agent_done")

    # ── Clear helper ───────────────────────────────────────────────────────────
    async def clear_stream() -> None:
        """Tell Telnyx to discard its audio buffer (barge-in)."""
        try:
            await websocket.send_text(json.dumps({"event": "clear"}))
        except Exception:
            pass

    # ── Telnyx receiver ────────────────────────────────────────────────────────
    async def receive_from_telnyx() -> None:
        nonlocal stream_id, started_at, connected, agent_speaking, duration_timer, call_control_id

        last_barge_in_at: dt.datetime | None = None
        # ── Tunables ──────────────────────────────────────────────────────────
        # Lower RMS threshold = more sensitive to quiet speech.
        # Raise it if you get false interrupts from background noise.
        barge_in_rms_threshold = 520   # was 700 — lowered for faster response
        barge_in_cooldown_sec = 0.6    # ignore further triggers for this long

        try:
            while True:
                raw = await websocket.receive_text()
                data = json.loads(raw)
                event = data.get("event")

                if event == "connected":
                    print(f"[{call_sid}] Telnyx connected", flush=True)
                    if redis_client and cfg_token:
                        asyncio.create_task(
                            set_call_status(redis_client, cfg_token, "dialling")
                        )
                    asyncio.create_task(
                        send_call_status_webhook(
                            call_sid=call_sid,
                            cfg=call_cfg,
                            status="dialling",
                        )
                    )

                elif event == "start":
                    stream_id = data.get("stream_id") or data.get("start", {}).get("streamSid")
                    call_control_id = data.get("start", {}).get("call_control_id", call_sid)
                    media_format = data.get("start", {}).get("media_format", {})
                    print(f"[{call_sid}] ⚠️ MEDIA FORMAT: {media_format}", flush=True)
                    print(
                        f"[{call_sid}] Stream started → stream_id={stream_id} "
                        f"call_control_id={call_control_id}",
                        flush=True,
                    )
                    started_at = dt.datetime.now(dt.timezone.utc)
                    connected = True
                    rec_msg = "This Call May Be Recorded For Audit and Training Purposes" if CALL_RECORDING_DISCLOSURE else None
                    if rec_msg:
                        conversation_history.append({"role": "assistant", "content": rec_msg})
                        turns.append({"role": "agent", "text": rec_msg, "ts": 0.0})

                    conversation_history.append({"role": "assistant", "content": opening_greeting})
                    turns.append({"role": "agent", "text": opening_greeting, "ts": 0.0})
                    if session and call_cfg.get("_ai_disclosure_done"):
                        session.ai_disclosure_done = True
                    if session and session._max_call_duration_sec():
                        duration_timer = asyncio.create_task(_duration_watchdog())

                    async def _play_intro():
                        if rec_msg:
                            await send_audio(rec_msg, override_voice_id=ELEVENLABS_VOICE_ID)
                            await asyncio.sleep(0.5)
                        await send_audio(opening_greeting, greeting_barge_guard_sec=GREETING_BARGE_IN_GUARD_SEC)

                    asyncio.create_task(_play_intro())

                elif event == "media":
                    media = data.get("media") or {}
                    track = media.get("track", "inbound")
                    if track == "inbound":
                        payload = media.get("payload")
                        if payload:
                            ulaw = base64.b64decode(payload)

                            # ── Immediate RMS barge-in ─────────────────────────
                            # This fires BEFORE STT has a chance to produce a
                            # transcript, giving sub-100 ms interrupt latency.
                            if agent_speaking and _barge_in_allowed():
                                try:
                                    pcm = audioop.ulaw2lin(ulaw, 2)
                                    rms = audioop.rms(pcm, 2)
                                    now = dt.datetime.now(dt.timezone.utc)
                                    cooldown_ok = (
                                        last_barge_in_at is None
                                        or (now - last_barge_in_at).total_seconds()
                                        >= barge_in_cooldown_sec
                                    )
                                    if cooldown_ok and rms >= barge_in_rms_threshold:
                                        last_barge_in_at = now
                                        agent_speaking = False
                                        barge_in_event.set()  # wake up send_audio()
                                        print(
                                            f"[{call_sid}] ⚡ Barge-in detected (rms={rms}) — clearing",
                                            flush=True,
                                        )
                                        await clear_stream()
                                except Exception:
                                    pass

                            await audio_queue.put(ulaw)

                elif event == "mark":
                    mark_name = (data.get("mark") or {}).get("name", "")
                    print(f"[{call_sid}] Mark: {mark_name}", flush=True)
                    if mark_name == "agent_done":
                        elapsed = _silence_elapsed_sec()
                        if elapsed > 0:
                            print(
                                f"[{call_sid}] 🔇 Post agent_done silence: {elapsed:.1f}s",
                                flush=True,
                            )

                elif event == "stop":
                    print(f"[{call_sid}] Stream stopped", flush=True)
                    break

        except WebSocketDisconnect:
            print(f"[{call_sid}] Telnyx disconnected", flush=True)
        except Exception as e:
            print(f"[{call_sid}] Telnyx error: {e}", flush=True)
        finally:
            await audio_queue.put(None)

    # ── Deepgram STT ───────────────────────────────────────────────────────────
    async def stream_to_deepgram() -> None:
        nonlocal agent_speaking
        headers = {"Authorization": f"Token {DEEPGRAM_API_KEY}"}
        print(f"[DG URL] {dg_url}", flush=True)
        print(f"[DG KEY] {DEEPGRAM_API_KEY[:10]}...", flush=True)
        try:
            async with websockets.connect(
                dg_url,
                additional_headers=headers,
                ping_interval=5,
                ping_timeout=20,
            ) as dg_ws:
                print(f"[{call_sid}] Deepgram connected ✅", flush=True)

                async def send_audio_to_dg() -> None:
                    while True:
                        chunk = await audio_queue.get()
                        if chunk is None:
                            try:
                                await dg_ws.send(json.dumps({"type": "CloseStream"}))
                            except Exception:
                                pass
                            break
                        await dg_ws.send(chunk)

                async def receive_transcripts() -> None:
                    nonlocal agent_speaking
                    async for raw_msg in dg_ws:
                        try:
                            msg = json.loads(raw_msg)
                            if msg.get("type") != "Results":
                                continue
                            alt = msg["channel"]["alternatives"][0]
                            transcript = alt.get("transcript", "").strip()
                            if not transcript:
                                continue
                            is_final = msg.get("is_final", False)
                            speech_final = msg.get("speech_final", False)

                            # STT-level barge-in (fires after RMS already did, but
                            # handles the case where RMS missed a quiet start).
                            if agent_speaking and _barge_in_allowed():
                                agent_speaking = False
                                barge_in_event.set()
                                print(f"[{call_sid}] ⚡ STT barge-in (Deepgram)", flush=True)
                                await clear_stream()
                            elif agent_speaking and not _barge_in_allowed():
                                continue

                            if is_final:
                                label = "FINAL ✅" if speech_final else "FINAL"
                                print(f"[{call_sid}] [{label}] {transcript}", flush=True)
                                transcript_buffer.append(transcript)

                            if speech_final and transcript_buffer:
                                full_turn = " ".join(transcript_buffer)
                                transcript_buffer.clear()
                                if len(full_turn.split()) <= MIN_WORDS_TO_RESPOND:
                                    print(f"[{call_sid}] ⏭ Skipping short turn: '{full_turn}'", flush=True)
                                    continue
                                if session and session.call_should_end:
                                    continue
                                await _handle_user_turn(full_turn)

                        except Exception as e:
                            print(f"[{call_sid}] Transcript error: {e}", flush=True)

                await asyncio.gather(send_audio_to_dg(), receive_transcripts())

        except websockets.exceptions.InvalidStatus as e:
            print(f"[{call_sid}] ❌ Deepgram rejected: {e}", flush=True)
        except Exception as e:
            print(f"[{call_sid}] Deepgram error: {type(e).__name__}: {e}", flush=True)

    # ── Sarvam STT ─────────────────────────────────────────────────────────────
    async def stream_to_sarvam() -> None:
        nonlocal agent_speaking

        if not SARVAM_API_KEY:
            print(f"[{call_sid}] ❌ SARVAM_API_KEY not set", flush=True)
            return

        sarvam_lang = to_sarvam_lang(call_cfg["deepgram_language"])
        sarvam_client = AsyncSarvamAI(api_subscription_key=SARVAM_API_KEY)
        pcm_buffer_target = 3200  # 200 ms @ 8 kHz PCM16

        print(f"[{call_sid}] Connecting to Sarvam STT (lang={sarvam_lang})…", flush=True)

        try:
            async with sarvam_client.speech_to_text_streaming.connect(
                model="saaras:v3",
                mode="transcribe",
                language_code=sarvam_lang,
                sample_rate=8000,
                input_audio_codec="pcm_s16le",
                high_vad_sensitivity=True,
                vad_signals=True,
            ) as ws:
                print(f"[{call_sid}] Sarvam STT connected ✅", flush=True)

                async def send_audio_to_sarvam() -> None:
                    pcm_buffer = bytearray()
                    while True:
                        chunk = await audio_queue.get()
                        if chunk is None:
                            if pcm_buffer:
                                audio_b64 = base64.b64encode(bytes(pcm_buffer)).decode()
                                await ws.transcribe(
                                    audio=audio_b64, encoding="audio/wav", sample_rate=8000
                                )
                            break
                        pcm_chunk = audioop.ulaw2lin(chunk, 2)
                        pcm_buffer.extend(pcm_chunk)
                        if len(pcm_buffer) >= pcm_buffer_target:
                            audio_b64 = base64.b64encode(bytes(pcm_buffer)).decode()
                            await ws.transcribe(
                                audio=audio_b64, encoding="audio/wav", sample_rate=8000
                            )
                            pcm_buffer.clear()

                async def receive_transcripts() -> None:
                    nonlocal agent_speaking
                    async for message in ws:
                        try:
                            msg_type = getattr(message, "type", None) or ""

                            # speech_start fires as soon as Sarvam detects voice —
                            # use it as a barge-in signal (faster than waiting for data).
                            if msg_type == "speech_start":
                                if agent_speaking and _barge_in_allowed():
                                    agent_speaking = False
                                    barge_in_event.set()
                                    print(f"[{call_sid}] ⚡ STT barge-in (Sarvam speech_start)", flush=True)
                                    await clear_stream()

                            elif msg_type == "data":
                                data_obj = getattr(message, "data", None)
                                transcript = (getattr(data_obj, "transcript", None) or "").strip()

                                if not transcript:
                                    continue

                                # Catch any remaining agent_speaking state missed by RMS/speech_start.
                                if agent_speaking and _barge_in_allowed():
                                    agent_speaking = False
                                    barge_in_event.set()
                                    await clear_stream()
                                elif agent_speaking and not _barge_in_allowed():
                                    continue

                                print(f"[{call_sid}] [SARVAM FINAL ✅] {transcript}", flush=True)

                                if len(transcript.split()) <= MIN_WORDS_TO_RESPOND:
                                    print(f"[{call_sid}] ⏭ Skipping short turn: '{transcript}'", flush=True)
                                    continue

                                if session and session.call_should_end:
                                    continue
                                await _handle_user_turn(transcript)

                        except Exception as e:
                            print(f"[{call_sid}] Sarvam transcript error: {e}", flush=True)

                await asyncio.gather(send_audio_to_sarvam(), receive_transcripts())

        except Exception as e:
            print(f"[{call_sid}] Sarvam STT error: {type(e).__name__}: {e}", flush=True)

    # ── Pipeline ───────────────────────────────────────────────────────────────
    stt_fn = stream_to_deepgram if stt_provider == "deepgram" else stream_to_sarvam
    telnyx_task = asyncio.create_task(receive_from_telnyx())
    stt_task_handle = asyncio.create_task(stt_fn())

    try:
        await telnyx_task
    finally:
        await _cancel_silence_timer("shutdown")
        if duration_timer and not duration_timer.done():
            duration_timer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await duration_timer
        if session:
            session.sync_to_cfg()
        print(f"[{call_sid}] Telnyx stream ended — shutting down STT", flush=True)
        await _shutdown_task(stt_task_handle, call_sid, "STT")
        ended_at = dt.datetime.now(dt.timezone.utc)
        print(f"[{call_sid}] Pipeline finished", flush=True)
        await _finalize_call(
            call_sid,
            started_at=started_at,
            ended_at=ended_at,
            connected=connected,
            turns=turns,
            call_cfg=call_cfg,
            redis_client=redis_client,
            cfg_token=cfg_token,
        )