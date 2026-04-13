"""ElevenLabs streaming TTS → μ-law chunks for Twilio."""

import audioop
import base64

import httpx

from config import ELEVENLABS_API_KEY, elevenlabs_stream_url


async def text_to_mulaw_chunks(text: str, model_id: str, voice_id: str):
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
            async for pcm_chunk in response.aiter_bytes(chunk_size=320):
                if not pcm_chunk:
                    continue
                yield base64.b64encode(audioop.lin2ulaw(pcm_chunk, 2)).decode("utf-8")
