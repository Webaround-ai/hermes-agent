"""Bounded recording/read-aloud API backed by Hermes's configured native voice providers."""
from __future__ import annotations

import asyncio
import base64
import binascii
import io
import json
import tempfile
import wave
from pathlib import Path

from aiohttp import web

MAX_AUDIO = 2 * 1024 * 1024
MAX_OUTPUT = 16 * 1024 * 1024


def enabled() -> bool:
    from hermes_cli.config import load_config
    return load_config().get("voice", {}).get("api_enabled") is True


def _transcribe(body: dict) -> dict:
    from tools.transcription_tools import transcribe_audio
    if body.get("mime") != "audio/wav" or not isinstance(body.get("audio_base64"), str):
        raise ValueError("invalid recording")
    raw = base64.b64decode(body["audio_base64"], validate=True)
    if not raw or len(raw) > MAX_AUDIO:
        raise ValueError("invalid recording size")
    with wave.open(io.BytesIO(raw)) as audio:
        if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getcomptype()) != (1, 2, 16000, "NONE"):
            raise ValueError("invalid recording format")
        if not 0 < audio.getnframes() <= 45 * 16000:
            raise ValueError("invalid recording duration")
        if len(audio.readframes(audio.getnframes())) != audio.getnframes() * 2:
            raise ValueError("truncated recording")
    with tempfile.TemporaryDirectory(prefix="hermes-voice-") as directory:
        path = Path(directory) / "recording.wav"
        path.write_bytes(raw)
        result = transcribe_audio(str(path), source="api_server")
    if not result.get("success") or not str(result.get("transcript") or "").strip():
        raise RuntimeError("transcription unavailable")
    return {"text": result["transcript"].strip()}


def _speak(body: dict) -> dict:
    from tools.tts_tool import text_to_speech_tool
    text, style = body.get("text"), body.get("style")
    if not isinstance(text, str) or not text.strip() or len(text) > 1200:
        raise ValueError("invalid speech text")
    if style is not None and (not isinstance(style, str) or len(style) > 200):
        raise ValueError("invalid speech style")
    with tempfile.TemporaryDirectory(prefix="hermes-voice-") as directory:
        result = json.loads(text_to_speech_tool(text, output_path=str(Path(directory) / "speech.wav"),
                                              instructions=style))
        if not result.get("success") or len(result.get("file_paths", [])) > 1:
            raise RuntimeError("speech unavailable")
        path = Path(result["file_path"]).resolve()
        if not path.is_relative_to(Path(directory).resolve()) or path.stat().st_size > MAX_OUTPUT:
            raise RuntimeError("invalid speech output")
        raw = path.read_bytes()
        with wave.open(io.BytesIO(raw)) as audio:
            if audio.getnframes() == 0:
                raise RuntimeError("empty speech output")
        return {"audio_base64": base64.b64encode(raw).decode(), "mime": "audio/wav", "text": text}


def http_routes(adapter):
    if not hasattr(adapter, "_audio_tasks"):
        adapter._audio_tasks = set()
    async def handle(request, action):
        # Audio never inherits the API server's optional unauthenticated mode.
        if not adapter._api_key:
            return web.json_response({"error": "voice requires authentication"}, status=503)
        denied = adapter._check_auth(request)
        if denied is not None:
            return denied
        if not enabled():
            return web.json_response({"error": "voice unavailable"}, status=503)
        limit = 4 * ((MAX_AUDIO + 2) // 3) + 4096 if action == "transcribe" else 16384
        data = bytearray()
        async for chunk in request.content.iter_chunked(65536):
            data.extend(chunk)
            if len(data) > limit:
                return web.json_response({"error": "voice request too large"}, status=413)
        try:
            body = json.loads(data)
            if not isinstance(body, dict):
                raise ValueError("invalid body")
            if len(adapter._audio_tasks) >= 2:
                return web.json_response({"error": "voice busy"}, status=429)
            task = asyncio.create_task(asyncio.to_thread({"transcribe": _transcribe, "speak": _speak}[action], body))
            adapter._audio_tasks.add(task)
            def finished(done):
                adapter._audio_tasks.discard(done)
                if not done.cancelled():
                    done.exception()  # Consume failures even when the HTTP client disconnected.
            task.add_done_callback(finished)
            # Native providers are synchronous. Keep work accounted for until their cleanup
            # finishes, even if the requesting client disconnects or cancels playback.
            result = await asyncio.shield(task)
            return web.json_response(result)
        except (ValueError, binascii.Error, wave.Error, EOFError):
            return web.json_response({"error": "invalid voice request or audio"}, status=422)
        except Exception:
            # Provider exceptions may contain text, paths or keys. Never echo them.
            return web.json_response({"error": "voice unavailable"}, status=502)

    async def transcribe(request):
        return await handle(request, "transcribe")

    async def speak(request):
        return await handle(request, "speak")

    return [("POST", "/v1/voice/transcribe", transcribe), ("POST", "/v1/voice/speak", speak)]
