"""Audio HTTP routes dispatch to the real native provider registries, without an agent turn."""
import base64
import io
from pathlib import Path
import wave

import pytest
import yaml
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from agent.transcription_provider import TranscriptionProvider
from agent.tts_provider import TTSProvider
from agent.transcription_registry import register_provider as register_stt
from agent.tts_registry import register_provider as register_tts
from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter


def wav_bytes():
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        wav.writeframes(b"\x00\x01" * 1600)
    return output.getvalue()


@pytest.mark.asyncio
async def test_native_voice_dispatch_and_cleanup(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({
        "voice": {"api_enabled": True}, "stt": {"enabled": True, "provider": "test-voice"},
        "tts": {"provider": "test-voice", "output_format": "wav"}}))
    paths = []

    class Transcriber(TranscriptionProvider):
        name = "test-voice"
        def transcribe(self, file_path, **kwargs):
            paths.append(Path(file_path))
            assert Path(file_path).read_bytes() == wav_bytes()
            return {"success": True, "transcript": "Hello from the box", "provider": self.name}

    class Speaker(TTSProvider):
        name = "test-voice"
        def synthesize(self, text, output_path, **kwargs):
            assert text == "Hello from the box"
            path = Path(output_path).with_suffix(".wav")
            paths.append(path)
            path.write_bytes(wav_bytes())
            return str(path)

    register_stt(Transcriber())
    register_tts(Speaker())
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "test-box-key"}))
    app = web.Application(client_max_size=4 * 1024 * 1024)
    for method, path, handler in adapter._http_route_table():
        app.router.add_route(method, path, handler)
    async with TestClient(TestServer(app)) as client:
        headers = {"Authorization": "Bearer test-box-key"}
        response = await client.post("/v1/voice/transcribe", headers=headers, json={
            "mime": "audio/wav", "audio_base64": base64.b64encode(wav_bytes()).decode()})
        assert response.status == 200, await response.text()
        transcript = await response.json()
        response = await client.post("/v1/voice/speak", headers=headers, json=transcript)
        assert response.status == 200, await response.text()
        spoken = await response.json()
        assert base64.b64decode(spoken["audio_base64"]) == wav_bytes()
        assert spoken["text"] == transcript["text"]
        assert paths and all(not path.exists() for path in paths)
        response = await client.get("/v1/capabilities", headers=headers)
        assert (await response.json())["features"]["audio_api"] is True


@pytest.mark.asyncio
async def test_voice_requires_auth_and_rejects_bad_recording(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("voice:\n  api_enabled: true\n")
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "test-box-key"}))
    app = web.Application()
    for method, path, handler in adapter._http_route_table():
        app.router.add_route(method, path, handler)
    async with TestClient(TestServer(app)) as client:
        assert (await client.post("/v1/voice/speak", json={"text": "private"})).status == 401
        response = await client.post("/v1/voice/transcribe", headers={"Authorization": "Bearer test-box-key"},
                                     json={"mime": "audio/wav", "audio_base64": "broken"})
        assert response.status == 422
        assert "broken" not in await response.text()


@pytest.mark.asyncio
async def test_audio_work_stays_busy_after_client_cancellation(tmp_path, monkeypatch):
    import asyncio
    import threading
    from gateway.platforms import api_server_audio
    from aiohttp.test_utils import make_mocked_request
    monkeypatch.setattr(api_server_audio, "enabled", lambda: True)
    started, release = threading.Event(), threading.Event()
    def speak(body):
        started.set()
        release.wait(5)
        return {"text": body["text"]}
    monkeypatch.setattr(api_server_audio, "_speak", speak)
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "test-box-key"}))
    async def chunks(size):
        yield b'{"text":"hello"}'
    content = type("Content", (), {"iter_chunked": staticmethod(chunks)})()
    request = make_mocked_request("POST", "/v1/voice/speak", headers={"Authorization": "Bearer test-box-key"},
                                  payload=content)
    handler = api_server_audio.http_routes(adapter)[1][2]
    task = asyncio.create_task(handler(request))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        assert adapter._readiness_work_counts()[0] == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert adapter._readiness_work_counts()[0] == 1
    finally:
        release.set()
        await asyncio.gather(*adapter._audio_tasks)
        await asyncio.sleep(0)
    assert adapter._readiness_work_counts()[0] == 0
