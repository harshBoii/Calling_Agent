"""ElevenLabs & Sarvam streaming TTS → audio chunks."""

import audioop
import base64
import io

import httpx

from config import ELEVENLABS_API_KEY, SARVAM_API_KEY, elevenlabs_stream_url, to_sarvam_lang


async def text_to_audio_chunks(text: str, model_id: str, voice_id: str):
    """Stream ElevenLabs MP3 bytes for Telnyx playback."""
    headers = {"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"}
    payload = {
        "text": text,
        "model_id": model_id,
        "voice_settings": {
            "stability": 0.4,
            "similarity_boost": 0.8,
            "style": 0.0,
            "use_speaker_boost": True,
        },
    }
    url = elevenlabs_stream_url(voice_id)
    async with httpx.AsyncClient(timeout=30) as client:
        async with client.stream("POST", url, headers=headers, json=payload) as response:
            if response.status_code != 200:
                body = await response.aread()
                print(f"[ElevenLabs] Error {response.status_code}: {body}", flush=True)
                return
            async for mp3_chunk in response.aiter_bytes(chunk_size=8192):
                if not mp3_chunk:
                    continue
                yield mp3_chunk


async def sarvam_text_to_mp3_chunks(
    text: str,
    target_language_code: str,
    speaker: str = "rohan",
):
    """Stream Sarvam TTS MP3 bytes for Telnyx playback."""
    headers = {
        "api-subscription-key": SARVAM_API_KEY,
        "Content-Type": "application/json",
    }
    payload = {
        "text": text,
        "target_language_code": target_language_code,
        "speaker": speaker,
        "model": "bulbul:v3",
        "pace": 1.1,
        "speech_sample_rate": 8000,
        "output_audio_codec": "mp3",
        "enable_preprocessing": True,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        async with client.stream("POST", SARVAM_TTS_STREAM_URL, headers=headers, json=payload) as response:
            if response.status_code != 200:
                body = await response.aread()
                print(f"[Sarvam TTS] Error {response.status_code}: {body}", flush=True)
                return
            async for chunk in response.aiter_bytes(chunk_size=8192):
                if chunk:
                    yield chunk


SARVAM_TTS_STREAM_URL = "https://api.sarvam.ai/text-to-speech/stream"


async def sarvam_text_to_mulaw_chunks(
    text: str,
    target_language_code: str,
    speaker: str = "rohan",
):
    """Stream Sarvam TTS → μ-law base64 chunks for Twilio (8 kHz)."""
    headers = {
        "api-subscription-key": SARVAM_API_KEY,
        "Content-Type": "application/json",
    }
    payload = {
        "text": text,
        "target_language_code": target_language_code,
        "speaker": speaker,
        "model": "bulbul:v3",
        "pace": 1.1,
        "speech_sample_rate": 8000,
        "output_audio_codec": "mp3",
        "enable_preprocessing": True,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        async with client.stream("POST", SARVAM_TTS_STREAM_URL, headers=headers, json=payload) as response:
            if response.status_code != 200:
                body = await response.aread()
                print(f"[Sarvam TTS] Error {response.status_code}: {body}", flush=True)
                return

            mp3_buffer = io.BytesIO()
            async for chunk in response.aiter_bytes(chunk_size=8192):
                if not chunk:
                    continue
                mp3_buffer.write(chunk)

            mp3_buffer.seek(0)
            mp3_bytes = mp3_buffer.read()
            if not mp3_bytes:
                return

            pcm_audio = _decode_mp3_to_pcm(mp3_bytes, target_rate=8000)
            if not pcm_audio:
                return

            chunk_size = 320  # 160 samples * 2 bytes = 20 ms @ 8 kHz
            for i in range(0, len(pcm_audio), chunk_size):
                pcm_chunk = pcm_audio[i : i + chunk_size]
                if pcm_chunk:
                    yield base64.b64encode(audioop.lin2ulaw(pcm_chunk, 2)).decode("utf-8")


def _decode_mp3_to_pcm(mp3_bytes: bytes, target_rate: int = 8000) -> bytes | None:
    """Decode MP3 bytes to raw 16-bit mono PCM at target_rate using pydub."""
    try:
        # Python 3.14 emits SyntaxWarning from pydub's internal regex strings.
        # This keeps logs clean while we rely on pydub for MP3 decoding.
        import warnings

        warnings.filterwarnings("ignore", category=SyntaxWarning, module=r"pydub\..*")
        from pydub import AudioSegment

        seg = AudioSegment.from_mp3(io.BytesIO(mp3_bytes))
        seg = seg.set_frame_rate(target_rate).set_channels(1).set_sample_width(2)
        return seg.raw_data
    except Exception as e:
        print(f"[Sarvam TTS] MP3 decode error: {e}", flush=True)
        return None
