# """Twilio media WebSocket: inbound audio, Deepgram or Sarvam STT, TTS reply."""

# import asyncio
# import audioop
# import base64
# import json

# import websockets
# from fastapi import WebSocket, WebSocketDisconnect
# from sarvamai import AsyncSarvamAI

# from config import (
#     DEEPGRAM_API_KEY,
#     MIN_WORDS_TO_RESPOND,
#     SARVAM_API_KEY,
#     deepgram_ws_url,
#     to_sarvam_lang,
# )
# from llm import ask_llm
# from tts import sarvam_text_to_mulaw_chunks, text_to_mulaw_chunks


# async def run_media_stream(websocket: WebSocket, call_sid: str, call_cfg: dict) -> None:
#     await websocket.accept()
#     print(f"[{call_sid}] Twilio WebSocket connected", flush=True)

#     system_prompt = call_cfg["system_prompt"]
#     opening_greeting = call_cfg["opening_greeting"]
#     el_model = call_cfg["elevenlabs_model"]
#     voice_id = call_cfg["voice_id"]
#     dg_url = deepgram_ws_url(call_cfg["deepgram_language"])
#     llm_provider = call_cfg["llm_provider"]
#     llm_model = call_cfg["llm_model"]
#     stt_provider = call_cfg["stt_provider"]
#     use_sarvam_tts = call_cfg.get("use_sarvam_tts", False)
#     sarvam_speaker = call_cfg.get("sarvam_speaker", "rohan")
#     sarvam_tts_lang = to_sarvam_lang(call_cfg["deepgram_language"])

#     tts_label = f"sarvam({sarvam_speaker})" if use_sarvam_tts else "elevenlabs"
#     print(f"[{call_sid}] STT: {stt_provider} | TTS: {tts_label} | LLM: {llm_provider}/{llm_model}", flush=True)

#     audio_queue = asyncio.Queue()
#     conversation_history = []
#     transcript_buffer = []
#     stream_sid = None
#     agent_speaking = False

#     async def send_audio_to_twilio(text: str):
#         nonlocal agent_speaking
#         agent_speaking = True
#         print(f"[{call_sid}] 🔊 Speaking: {text[:80]}", flush=True)
#         chunk_count = 0
#         if use_sarvam_tts:
#             tts_stream = sarvam_text_to_mulaw_chunks(text, sarvam_tts_lang, sarvam_speaker)
#         else:
#             tts_stream = text_to_mulaw_chunks(text, el_model, voice_id)
#         async for mulaw_b64 in tts_stream:
#             if not agent_speaking:
#                 print(f"[{call_sid}] ⚡ Interrupted — stopping TTS", flush=True)
#                 break
#             try:
#                 await websocket.send_text(
#                     json.dumps(
#                         {
#                             "event": "media",
#                             "streamSid": stream_sid,
#                             "media": {"payload": mulaw_b64},
#                         }
#                     )
#                 )
#                 chunk_count += 1
#             except Exception as e:
#                 print(f"[{call_sid}] Send error: {e}", flush=True)
#                 break
#         try:
#             await websocket.send_text(
#                 json.dumps(
#                     {
#                         "event": "mark",
#                         "streamSid": stream_sid,
#                         "mark": {"name": "agent_done"},
#                     }
#                 )
#             )
#         except Exception:
#             pass
#         agent_speaking = False
#         print(f"[{call_sid}] ✅ Sent {chunk_count} chunks", flush=True)

#     async def receive_from_twilio():
#         nonlocal stream_sid
#         try:
#             while True:
#                 raw = await websocket.receive_text()
#                 data = json.loads(raw)
#                 event = data.get("event")
#                 if event == "connected":
#                     print(f"[{call_sid}] Twilio connected", flush=True)
#                 elif event == "start":
#                     stream_sid = data["start"]["streamSid"]
#                     print(f"[{call_sid}] Stream started → {stream_sid}", flush=True)
#                     conversation_history.append({"role": "assistant", "content": opening_greeting})
#                     asyncio.create_task(send_audio_to_twilio(opening_greeting))
#                 elif event == "media":
#                     await audio_queue.put(base64.b64decode(data["media"]["payload"]))
#                 elif event == "mark":
#                     print(f"[{call_sid}] Mark: {data['mark']['name']}", flush=True)
#                 elif event == "stop":
#                     print(f"[{call_sid}] Stream stopped", flush=True)
#                     break
#         except WebSocketDisconnect:
#             print(f"[{call_sid}] Twilio disconnected", flush=True)
#         except Exception as e:
#             print(f"[{call_sid}] Twilio error: {e}", flush=True)
#         finally:
#             await audio_queue.put(None)

#     async def stream_to_deepgram():
#         nonlocal agent_speaking
#         headers = {"Authorization": f"Token {DEEPGRAM_API_KEY}"}
#         try:
#             async with websockets.connect(
#                 dg_url,
#                 additional_headers=headers,
#                 ping_interval=5,
#                 ping_timeout=20,
#             ) as dg_ws:
#                 print(f"[{call_sid}] Deepgram connected ✅", flush=True)

#                 async def send_audio():
#                     while True:
#                         chunk = await audio_queue.get()
#                         if chunk is None:
#                             try:
#                                 await dg_ws.send(json.dumps({"type": "CloseStream"}))
#                             except Exception:
#                                 pass
#                             break
#                         await dg_ws.send(chunk)

#                 async def receive_transcripts():
#                     nonlocal agent_speaking
#                     async for raw_msg in dg_ws:
#                         try:
#                             msg = json.loads(raw_msg)
#                             if msg.get("type") != "Results":
#                                 continue
#                             alt = msg["channel"]["alternatives"][0]
#                             transcript = alt.get("transcript", "").strip()
#                             if not transcript:
#                                 continue
#                             is_final = msg.get("is_final", False)
#                             speech_final = msg.get("speech_final", False)

#                             if agent_speaking:
#                                 agent_speaking = False
#                                 print(f"[{call_sid}] ⚡ Human interrupted agent", flush=True)
#                                 try:
#                                     await websocket.send_text(
#                                         json.dumps({"event": "clear", "streamSid": stream_sid})
#                                     )
#                                 except Exception:
#                                     pass

#                             if is_final:
#                                 label = "FINAL ✅" if speech_final else "FINAL"
#                                 print(f"[{call_sid}] [{label}] {transcript}", flush=True)
#                                 transcript_buffer.append(transcript)

#                             if speech_final and transcript_buffer:
#                                 full_turn = " ".join(transcript_buffer)
#                                 transcript_buffer.clear()
#                                 if len(full_turn.split()) < MIN_WORDS_TO_RESPOND:
#                                     print(f"[{call_sid}] ⏭ Skipping short turn: '{full_turn}'", flush=True)
#                                     continue
#                                 print(f"[{call_sid}] 🎤 Human: {full_turn}", flush=True)
#                                 conversation_history.append({"role": "user", "content": full_turn})
#                                 agent_reply = await ask_llm(
#                                     conversation_history,
#                                     system_prompt,
#                                     llm_provider,
#                                     llm_model,
#                                 )
#                                 conversation_history.append({"role": "assistant", "content": agent_reply})
#                                 print(f"[{call_sid}] 🤖 [{llm_provider}] Agent: {agent_reply}", flush=True)
#                                 await send_audio_to_twilio(agent_reply)

#                         except Exception as e:
#                             print(f"[{call_sid}] Transcript error: {e}", flush=True)

#                 await asyncio.gather(send_audio(), receive_transcripts())

#         except websockets.exceptions.InvalidStatus as e:
#             print(f"[{call_sid}] ❌ Deepgram rejected: {e}", flush=True)
#         except Exception as e:
#             print(f"[{call_sid}] Deepgram error: {type(e).__name__}: {e}", flush=True)

#     async def stream_to_sarvam():
#         nonlocal agent_speaking

#         if not SARVAM_API_KEY:
#             print(f"[{call_sid}] ❌ SARVAM_API_KEY not set", flush=True)
#             return

#         sarvam_lang = to_sarvam_lang(call_cfg["deepgram_language"])
#         sarvam_client = AsyncSarvamAI(api_subscription_key=SARVAM_API_KEY)
#         pcm_buffer_target = 3200  # 200ms @ 8kHz PCM16

#         print(f"[{call_sid}] Connecting to Sarvam STT (lang={sarvam_lang})…", flush=True)

#         try:
#             async with sarvam_client.speech_to_text_streaming.connect(
#                 model="saaras:v3",
#                 mode="transcribe",
#                 language_code=sarvam_lang,
#                 sample_rate=8000,
#                 input_audio_codec="pcm_s16le",
#                 high_vad_sensitivity=True,
#                 vad_signals=True,
#             ) as ws:
#                 print(f"[{call_sid}] Sarvam STT connected ✅", flush=True)

#                 async def send_audio():
#                     pcm_buffer = bytearray()
#                     while True:
#                         chunk = await audio_queue.get()
#                         if chunk is None:
#                             if pcm_buffer:
#                                 audio_b64 = base64.b64encode(bytes(pcm_buffer)).decode()
#                                 await ws.transcribe(
#                                     audio=audio_b64,
#                                     encoding="audio/wav",
#                                     sample_rate=8000,
#                                 )
#                             break
#                         pcm_chunk = audioop.ulaw2lin(chunk, 2)
#                         pcm_buffer.extend(pcm_chunk)
#                         if len(pcm_buffer) >= pcm_buffer_target:
#                             audio_b64 = base64.b64encode(bytes(pcm_buffer)).decode()
#                             await ws.transcribe(
#                                 audio=audio_b64,
#                                 encoding="audio/wav",
#                                 sample_rate=8000,
#                             )
#                             pcm_buffer.clear()

#                 async def receive_transcripts():
#                     nonlocal agent_speaking
#                     async for message in ws:
#                         try:
#                             msg_type = getattr(message, "type", None) or ""

#                             if msg_type == "speech_start" and agent_speaking:
#                                 agent_speaking = False
#                                 print(f"[{call_sid}] ⚡ Human interrupted agent (Sarvam)", flush=True)
#                                 try:
#                                     await websocket.send_text(
#                                         json.dumps({"event": "clear", "streamSid": stream_sid})
#                                     )
#                                 except Exception:
#                                     pass

#                             elif msg_type == "data":
#                                 data_obj = getattr(message, "data", None)
#                                 transcript = (getattr(data_obj, "transcript", None) or "").strip()

#                                 if not transcript:
#                                     continue

#                                 print(f"[{call_sid}] [SARVAM FINAL ✅] {transcript}", flush=True)

#                                 if len(transcript.split()) < MIN_WORDS_TO_RESPOND:
#                                     print(f"[{call_sid}] ⏭ Skipping short turn: '{transcript}'", flush=True)
#                                     continue

#                                 print(f"[{call_sid}] 🎤 Human: {transcript}", flush=True)
#                                 conversation_history.append({"role": "user", "content": transcript})
#                                 agent_reply = await ask_llm(
#                                     conversation_history,
#                                     system_prompt,
#                                     llm_provider,
#                                     llm_model,
#                                 )
#                                 conversation_history.append({"role": "assistant", "content": agent_reply})
#                                 print(f"[{call_sid}] 🤖 [{llm_provider}] Agent: {agent_reply}", flush=True)
#                                 await send_audio_to_twilio(agent_reply)

#                         except Exception as e:
#                             print(f"[{call_sid}] Sarvam transcript error: {e}", flush=True)

#                 await asyncio.gather(send_audio(), receive_transcripts())

#         except Exception as e:
#             print(f"[{call_sid}] Sarvam STT error: {type(e).__name__}: {e}", flush=True)

#     stt_task = stream_to_deepgram if stt_provider == "deepgram" else stream_to_sarvam
#     await asyncio.gather(receive_from_twilio(), stt_task())
#     print(f"[{call_sid}] Pipeline finished", flush=True)




import audioop
import base64
import json
import asyncio
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
from tts import sarvam_text_to_mulaw_chunks, text_to_audio_chunks


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

    audio_queue = asyncio.Queue()
    conversation_history = []
    transcript_buffer = []
    stream_id = None
    agent_speaking = False

    async def send_audio(text: str):
        nonlocal agent_speaking
        agent_speaking = True
        print(f"[{call_sid}] 🔊 Speaking: {text[:80]}", flush=True)
        chunk_count = 0
        if use_sarvam_tts:
            tts_stream = sarvam_text_to_mulaw_chunks(text, sarvam_tts_lang, sarvam_speaker)
        else:
            tts_stream = text_to_audio_chunks(text, el_model, voice_id)
        async for audio_b64 in tts_stream:
            if not agent_speaking:
                print(f"[{call_sid}] ⚡ Interrupted — stopping TTS", flush=True)
                break
            try:
                await websocket.send_text(
                    json.dumps({
                        "event": "media",
                        "media": {"payload": audio_b64},
                    })
                )
                chunk_count += 1
            except Exception as e:
                print(f"[{call_sid}] Send error: {e}", flush=True)
                break
        try:
            await websocket.send_text(
                json.dumps({
                    "event": "mark",
                    "mark": {"name": "agent_done"},
                })
            )
        except Exception:
            pass
        agent_speaking = False
        print(f"[{call_sid}] ✅ Sent {chunk_count} chunks", flush=True)

    async def clear_stream():
        """Send barge-in clear to Telnyx — no streamSid needed."""
        try:
            await websocket.send_text(json.dumps({"event": "clear"}))
        except Exception:
            pass

    async def receive_from_telnyx():
        nonlocal stream_id
        try:
            while True:
                raw = await websocket.receive_text()
                data = json.loads(raw)
                event = data.get("event")
                if event == "connected":
                    print(f"[{call_sid}] Telnyx connected", flush=True)
                elif event == "start":
                    # Telnyx sends stream_id as a top-level field
                    stream_id = data.get("stream_id") or data.get("start", {}).get("streamSid")
                    call_control_id = data.get("start", {}).get("call_control_id", call_sid)
                    print(f"[{call_sid}] Stream started → stream_id={stream_id} call_control_id={call_control_id}", flush=True)
                    conversation_history.append({"role": "assistant", "content": opening_greeting})
                    asyncio.create_task(send_audio(opening_greeting))
                elif event == "media":
                    await audio_queue.put(base64.b64decode(data["media"]["payload"]))
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

    async def stream_to_deepgram():
        nonlocal agent_speaking
        headers = {"Authorization": f"Token {DEEPGRAM_API_KEY}"}
        try:
            async with websockets.connect(
                dg_url,
                additional_headers=headers,
                ping_interval=5,
                ping_timeout=20,
            ) as dg_ws:
                print(f"[{call_sid}] Deepgram connected ✅", flush=True)

                async def send_audio_to_dg():
                    while True:
                        chunk = await audio_queue.get()
                        if chunk is None:
                            try:
                                await dg_ws.send(json.dumps({"type": "CloseStream"}))
                            except Exception:
                                pass
                            break
                        await dg_ws.send(chunk)

                async def receive_transcripts():
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

                            if agent_speaking:
                                agent_speaking = False
                                print(f"[{call_sid}] ⚡ Human interrupted agent", flush=True)
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
                                agent_reply = await ask_llm(
                                    conversation_history,
                                    system_prompt,
                                    llm_provider,
                                    llm_model,
                                )
                                conversation_history.append({"role": "assistant", "content": agent_reply})
                                print(f"[{call_sid}] 🤖 [{llm_provider}] Agent: {agent_reply}", flush=True)
                                await send_audio(agent_reply)

                        except Exception as e:
                            print(f"[{call_sid}] Transcript error: {e}", flush=True)

                await asyncio.gather(send_audio_to_dg(), receive_transcripts())

        except websockets.exceptions.InvalidStatus as e:
            print(f"[{call_sid}] ❌ Deepgram rejected: {e}", flush=True)
        except Exception as e:
            print(f"[{call_sid}] Deepgram error: {type(e).__name__}: {e}", flush=True)

    async def stream_to_sarvam():
        nonlocal agent_speaking

        if not SARVAM_API_KEY:
            print(f"[{call_sid}] ❌ SARVAM_API_KEY not set", flush=True)
            return

        sarvam_lang = to_sarvam_lang(call_cfg["deepgram_language"])
        sarvam_client = AsyncSarvamAI(api_subscription_key=SARVAM_API_KEY)
        pcm_buffer_target = 3200  # 200ms @ 8kHz PCM16

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

                async def send_audio_to_sarvam():
                    pcm_buffer = bytearray()
                    while True:
                        chunk = await audio_queue.get()
                        if chunk is None:
                            if pcm_buffer:
                                audio_b64 = base64.b64encode(bytes(pcm_buffer)).decode()
                                await ws.transcribe(
                                    audio=audio_b64,
                                    encoding="audio/wav",
                                    sample_rate=8000,
                                )
                            break
                        pcm_chunk = audioop.ulaw2lin(chunk, 2)
                        pcm_buffer.extend(pcm_chunk)
                        if len(pcm_buffer) >= pcm_buffer_target:
                            audio_b64 = base64.b64encode(bytes(pcm_buffer)).decode()
                            await ws.transcribe(
                                audio=audio_b64,
                                encoding="audio/wav",
                                sample_rate=8000,
                            )
                            pcm_buffer.clear()

                async def receive_transcripts():
                    nonlocal agent_speaking
                    async for message in ws:
                        try:
                            msg_type = getattr(message, "type", None) or ""

                            if msg_type == "speech_start" and agent_speaking:
                                agent_speaking = False
                                print(f"[{call_sid}] ⚡ Human interrupted agent (Sarvam)", flush=True)
                                await clear_stream()

                            elif msg_type == "data":
                                data_obj = getattr(message, "data", None)
                                transcript = (getattr(data_obj, "transcript", None) or "").strip()

                                if not transcript:
                                    continue

                                print(f"[{call_sid}] [SARVAM FINAL ✅] {transcript}", flush=True)

                                if len(transcript.split()) < MIN_WORDS_TO_RESPOND:
                                    print(f"[{call_sid}] ⏭ Skipping short turn: '{transcript}'", flush=True)
                                    continue

                                print(f"[{call_sid}] 🎤 Human: {transcript}", flush=True)
                                conversation_history.append({"role": "user", "content": transcript})
                                agent_reply = await ask_llm(
                                    conversation_history,
                                    system_prompt,
                                    llm_provider,
                                    llm_model,
                                )
                                conversation_history.append({"role": "assistant", "content": agent_reply})
                                print(f"[{call_sid}] 🤖 [{llm_provider}] Agent: {agent_reply}", flush=True)
                                await send_audio(agent_reply)

                        except Exception as e:
                            print(f"[{call_sid}] Sarvam transcript error: {e}", flush=True)

                await asyncio.gather(send_audio_to_sarvam(), receive_transcripts())

        except Exception as e:
            print(f"[{call_sid}] Sarvam STT error: {type(e).__name__}: {e}", flush=True)

    stt_task = stream_to_deepgram if stt_provider == "deepgram" else stream_to_sarvam
    await asyncio.gather(receive_from_telnyx(), stt_task())
    print(f"[{call_sid}] Pipeline finished", flush=True)