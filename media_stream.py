import audioop
import asyncio
import base64
import datetime as dt
import json

import websockets
from fastapi import WebSocket, WebSocketDisconnect
from sarvamai import AsyncSarvamAI

from config import (
    DEEPGRAM_API_KEY,
    MIN_WORDS_TO_RESPOND,
    SARVAM_API_KEY,
    deepgram_ws_url,
    to_sarvam_lang,
)
from llm import ask_llm
from tts import sarvam_text_to_mp3_chunks, text_to_audio_chunks
from webhook import send_call_completed_webhook


async def run_media_stream(websocket: WebSocket, call_sid: str, call_cfg: dict) -> None:
    await websocket.accept()
    print(f"[{call_sid}] Telnyx WebSocket connected", flush=True)

    system_prompt = call_cfg["system_prompt"]
    opening_greeting = call_cfg["opening_greeting"]
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

    def _ts() -> float:
        if started_at is None:
            return 0.0
        return round((dt.datetime.now(dt.timezone.utc) - started_at).total_seconds(), 2)

    # ── TTS sender ─────────────────────────────────────────────────────────────
    async def send_audio(text: str) -> None:
        nonlocal agent_speaking
        agent_speaking = True
        barge_in_event.clear()  # arm: clear any previous interrupt signal
        print(f"[{call_sid}] 🔊 Speaking: {text[:80]}", flush=True)
        chunk_count = 0

        if use_sarvam_tts:
            tts_stream = sarvam_text_to_mp3_chunks(text, sarvam_tts_lang, sarvam_speaker)
        else:
            tts_stream = text_to_audio_chunks(text, el_model, voice_id)

        async for audio_b64 in tts_stream:
            # ── Barge-in check BEFORE sending each chunk ──────────────────────
            # Because tts.py now yields small chunks (~250 ms each), this check
            # fires frequently enough to stop playback almost instantly when the
            # user starts speaking.
            if barge_in_event.is_set():
                print(f"[{call_sid}] ⚡ Barge-in — discarding remaining TTS chunks", flush=True)
                break

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

    # ── Clear helper ───────────────────────────────────────────────────────────
    async def clear_stream() -> None:
        """Tell Telnyx to discard its audio buffer (barge-in)."""
        try:
            await websocket.send_text(json.dumps({"event": "clear"}))
        except Exception:
            pass

    # ── Telnyx receiver ────────────────────────────────────────────────────────
    async def receive_from_telnyx() -> None:
        nonlocal stream_id, started_at, connected, agent_speaking

        last_barge_in_at: dt.datetime | None = None
        # ── Tunables ──────────────────────────────────────────────────────────
        # Lower RMS threshold = more sensitive to quiet speech.
        # Raise it if you get false interrupts from background noise.
        barge_in_rms_threshold = 450   # was 700 — lowered for faster response
        barge_in_cooldown_sec = 0.6    # ignore further triggers for this long

        try:
            while True:
                raw = await websocket.receive_text()
                data = json.loads(raw)
                event = data.get("event")

                if event == "connected":
                    print(f"[{call_sid}] Telnyx connected", flush=True)

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
                    conversation_history.append({"role": "assistant", "content": opening_greeting})
                    turns.append({"role": "agent", "text": opening_greeting, "ts": 0.0})
                    asyncio.create_task(send_audio(opening_greeting))

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
                            if agent_speaking:
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
                    print(f"[{call_sid}] Mark: {data['mark']['name']}", flush=True)

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
                            if agent_speaking:
                                agent_speaking = False
                                barge_in_event.set()
                                print(f"[{call_sid}] ⚡ STT barge-in (Deepgram)", flush=True)
                                await clear_stream()

                            if is_final:
                                label = "FINAL ✅" if speech_final else "FINAL"
                                print(f"[{call_sid}] [{label}] {transcript}", flush=True)
                                transcript_buffer.append(transcript)

                            if speech_final and transcript_buffer:
                                full_turn = " ".join(transcript_buffer)
                                transcript_buffer.clear()
                                if len(full_turn.split()) < MIN_WORDS_TO_RESPOND:
                                    print(f"[{call_sid}] ⏭ Skipping short turn: '{full_turn}'", flush=True)
                                    continue
                                print(f"[{call_sid}] 🎤 Human: {full_turn}", flush=True)
                                conversation_history.append({"role": "user", "content": full_turn})
                                turns.append({"role": "user", "text": full_turn, "ts": _ts()})
                                agent_reply = await ask_llm(
                                    conversation_history, system_prompt, llm_provider, llm_model
                                )
                                conversation_history.append({"role": "assistant", "content": agent_reply})
                                turns.append({"role": "agent", "text": agent_reply, "ts": _ts()})
                                print(f"[{call_sid}] 🤖 [{llm_provider}] Agent: {agent_reply}", flush=True)
                                await send_audio(agent_reply)

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
                            if msg_type == "speech_start" and agent_speaking:
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
                                if agent_speaking:
                                    agent_speaking = False
                                    barge_in_event.set()
                                    await clear_stream()

                                print(f"[{call_sid}] [SARVAM FINAL ✅] {transcript}", flush=True)

                                if len(transcript.split()) < MIN_WORDS_TO_RESPOND:
                                    print(f"[{call_sid}] ⏭ Skipping short turn: '{transcript}'", flush=True)
                                    continue

                                print(f"[{call_sid}] 🎤 Human: {transcript}", flush=True)
                                conversation_history.append({"role": "user", "content": transcript})
                                turns.append({"role": "user", "text": transcript, "ts": _ts()})
                                agent_reply = await ask_llm(
                                    conversation_history, system_prompt, llm_provider, llm_model
                                )
                                conversation_history.append({"role": "assistant", "content": agent_reply})
                                turns.append({"role": "agent", "text": agent_reply, "ts": _ts()})
                                print(f"[{call_sid}] 🤖 [{llm_provider}] Agent: {agent_reply}", flush=True)
                                await send_audio(agent_reply)

                        except Exception as e:
                            print(f"[{call_sid}] Sarvam transcript error: {e}", flush=True)

                await asyncio.gather(send_audio_to_sarvam(), receive_transcripts())

        except Exception as e:
            print(f"[{call_sid}] Sarvam STT error: {type(e).__name__}: {e}", flush=True)

    # ── Pipeline ───────────────────────────────────────────────────────────────
    stt_task = stream_to_deepgram if stt_provider == "deepgram" else stream_to_sarvam
    await asyncio.gather(receive_from_telnyx(), stt_task())
    ended_at = dt.datetime.now(dt.timezone.utc)
    print(f"[{call_sid}] Pipeline finished", flush=True)

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
        await send_call_completed_webhook(call_record, call_cfg)
    except Exception as e:
        print(f"[{call_sid}] Webhook dispatch error: {type(e).__name__}: {e}", flush=True)