import asyncio
import io
import os
import struct
import time
import wave
from dataclasses import replace

import pytest
from aiohttp import web
from astrbot_plugin_deepseek_tts import speech
from astrbot_plugin_deepseek_tts.settings import Settings, SettingsError, validate_patch
from astrbot_plugin_deepseek_tts.speech import (
    AudioStore,
    DeepSeekClient,
    SpeechError,
    SpeechService,
    normalize_wav,
)
from conftest import Client, wav_bytes


@pytest.fixture
async def endpoint(monkeypatch):
    runner = None

    async def start(handler):
        nonlocal runner
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        monkeypatch.setattr(speech, "ENDPOINT", f"http://127.0.0.1:{port}/tts")

    yield start
    if runner:
        await runner.cleanup()


def test_wav_streaming_sizes_are_normalized_without_changing_pcm():
    raw = bytearray(wav_bytes())
    struct.pack_into("<I", raw, 4, 0xFFFFFFFF)
    struct.pack_into("<I", raw, 40, 0xFFFFFFFF)
    audio = normalize_wav(bytes(raw))
    assert struct.unpack_from("<I", audio.data, 4)[0] == len(audio.data) - 8
    assert audio.data[44:] == raw[44:]
    with wave.open(io.BytesIO(audio.data)) as saved:
        assert saved.getnframes() == 2400
    assert audio.duration == 0.1


@pytest.mark.parametrize(
    "data", [b'{"error":true}', wav_bytes()[:-3], wav_bytes()[:44]]
)
def test_invalid_or_incomplete_audio_is_rejected(data):
    with pytest.raises(SpeechError):
        normalize_wav(data)


async def test_http_uses_official_payload_and_bearer_then_validates_audio(endpoint):
    async def handler(request):
        assert request.headers["Authorization"] == "Bearer test-key"
        assert await request.json() == {
            "text": "你好",
            "voice_id": "tide",
            "format": "wav",
            "filter_markdown": True,
        }
        return web.Response(body=wav_bytes(), content_type="audio/wav")

    await endpoint(handler)
    client = DeepSeekClient()
    try:
        audio = await client.synthesize("test-key", "你好", "tide", Settings())
        assert audio.duration == 0.1
    finally:
        await client.close()


@pytest.mark.parametrize("status", [400, 401, 402, 403, 429, 503, 504, 500])
async def test_http_error_never_echoes_server_body_or_credentials(endpoint, status):
    async def handler(request):
        return web.Response(status=status, text="secret-key and private text")

    await endpoint(handler)
    client = DeepSeekClient()
    try:
        with pytest.raises(SpeechError) as caught:
            await client.synthesize("secret-key", "private text", "stella", Settings())
        assert caught.value.code == f"http_{status}"
        assert "secret-key" not in str(caught.value) and "private text" not in str(
            caught.value
        )
    finally:
        await client.close()


async def test_no_redirects_with_credentials(endpoint):
    visited = []

    async def handler(request):
        visited.append(request.path)
        return web.Response(status=302, headers={"Location": "/trap"})

    await endpoint(handler)
    client = DeepSeekClient()
    try:
        with pytest.raises(SpeechError, match="暂时无法"):
            await client.synthesize("test-key", "hello", "stella", Settings())
        assert visited == ["/tts"]
    finally:
        await client.close()


async def test_http_timeout_and_response_size_bound(endpoint):
    async def handler(request):
        if request.query.get("wait"):
            await asyncio.sleep(0.1)
        return web.Response(body=wav_bytes(), content_type="audio/wav")

    await endpoint(handler)
    client = DeepSeekClient()
    try:
        with pytest.raises(SpeechError) as caught:
            await client.synthesize("k", "hello", "stella", Settings(), max_bytes=50)
        assert caught.value.code == "audio_too_large"
        speech.ENDPOINT += "?wait=1"
        with pytest.raises(SpeechError) as caught:
            await client.synthesize(
                "k", "hello", "stella", replace(Settings(), timeout_seconds=0.01)
            )
        assert caught.value.code == "timeout"
    finally:
        await client.close()


async def test_service_cooldown_limits_and_environment_key(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "environment-test-key")
    client = Client()
    service = SpeechService(client)
    await service.generate(Settings(), "你好", "same", voice="tide")
    assert client.calls == [("environment-test-key", "你好", "tide")]
    with pytest.raises(SpeechError) as caught:
        await service.generate(Settings(), "你好", "same")
    assert caught.value.code == "cooldown"
    with pytest.raises(SpeechError):
        await service.generate(Settings(), "字" * 101, "preview", preview=True)
    assert len(client.calls) == 1


async def test_service_two_jobs_and_cancellation_release_capacity():
    waiting = asyncio.Event()

    class BlockingClient(Client):
        async def synthesize(self, *args):
            await waiting.wait()
            return await super().synthesize(*args)

    service = SpeechService(BlockingClient())
    settings = Settings(api_key="test", cooldown_seconds=0)
    tasks = [
        asyncio.create_task(service.generate(settings, "hi", str(i))) for i in range(2)
    ]
    await asyncio.sleep(0)
    with pytest.raises(SpeechError) as caught:
        await service.generate(settings, "hi", "third")
    assert caught.value.code == "busy"
    tasks[0].cancel()
    with pytest.raises(asyncio.CancelledError):
        await tasks[0]
    waiting.set()
    await tasks[1]
    await service.generate(settings, "hi", "third")
    assert service._active == 0


@pytest.mark.parametrize(
    "patch",
    [
        {"voice_id": "unknown"},
        {"voice_id": []},
        {"max_chars": True},
        {"max_chars": 2001},
        {"enabled": "false"},
        {"allowed_group_ids": ["../private"]},
        {"api_key": "key\nheader"},
        {"base_url": "https://other.example"},
        {"timeout_seconds": 5},
    ],
)
def test_bad_settings_rejected(patch):
    with pytest.raises(SettingsError):
        validate_patch(patch)


def test_key_not_exposed_in_public_settings_or_repr(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "env-secret")
    settings = Settings(api_key="config-secret")
    assert "config-secret" not in repr(settings)
    assert "api_key" not in settings.public()
    assert settings.public()["key_source"] == "config"
    assert Settings().public()["key_source"] == "environment"


def test_cache_prune_only_owns_hex_wavs(tmp_path):
    owned = tmp_path / ("a" * 32 + ".wav")
    unrelated = tmp_path / "keep.wav"
    for path in (owned, unrelated):
        path.write_bytes(b"sample")
        old = time.time() - 90000
        os.utime(path, (old, old))
    store = AudioStore(tmp_path)
    path = store.save(normalize_wav(wav_bytes()))
    assert path.exists() and not owned.exists() and unrelated.exists()
    assert not list(tmp_path.glob("*.tmp"))
