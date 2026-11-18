import base64
import uuid
from types import SimpleNamespace

import pytest
from astrbot_plugin_deepseek_tts import studio as studio_module
from astrbot_plugin_deepseek_tts import studio_providers
from astrbot_plugin_deepseek_tts.settings import PLUGIN_NAME
from astrbot_plugin_deepseek_tts.studio import Studio
from astrbot_plugin_deepseek_tts.studio_media import (
    ffmpeg_available,
    inspect_song,
    make_clip,
)
from astrbot_plugin_deepseek_tts.studio_providers import (
    COVER_URL,
    VOICE_URL,
    S1CoverGenerator,
    StudioError,
    VoiceDesign,
    VolcengineVoiceDesigner,
    media_url,
)
from conftest import Client, wav_bytes

SAMPLE_URL = "https://audio.example.com/sample?signature=a%2Fb%2Bc"
TRACK_URL = "https://audio.example.com/track?signature=track"
RESULT_URL = "https://audio.example.com/result?signature=result"
MP3 = bytes.fromhex("fffb50c4") + bytes(204)
DESIGN = {
    "provider": "volcengine",
    "speaker_id": "S_test",
    "name": "薄荷女声",
    "prompt": "年轻清亮女声",
    "text": "欢迎来到音色工作台。",
    "language": 0,
}


class ScriptedTransport:
    def __init__(self, *replies):
        self.replies = iter(replies)
        self.calls = []

    async def request(self, service, method, url, **options):
        self.calls.append((method, str(url), options))
        result = next(self.replies)
        if isinstance(result, Exception):
            raise result
        return result

    json = request


class Upload:
    def __init__(self, filename, data):
        self.filename = filename
        self.data = data

    async def read(self, size):
        return self.data[:size]


async def no_wait(seconds):
    pass


def route(plugin, path):
    return next(
        handler
        for name, handler, *_ in plugin.instance.context.routes
        if name == f"/{PLUGIN_NAME}/studio/{path}"
    )


async def test_volcengine_auth_payload_poll_and_saveable_sample(monkeypatch):
    monkeypatch.setattr(studio_providers, "asyncio", SimpleNamespace(sleep=no_wait))
    sample = wav_bytes()
    transport = ScriptedTransport(
        {"status": 1, "speaker_id": "S_test"},
        {
            "status": 2,
            "speaker_id": "S_test",
            "available_training_times": 3,
            "speaker_status": [{"model_type": 5, "demo_audio": SAMPLE_URL}],
        },
        sample,
    )
    result = await VolcengineVoiceDesigner(transport).design(
        "voice-key", "S_test", "清亮", "试音", 0
    )
    assert result == VoiceDesign(sample, 3)
    design, query, download = transport.calls
    assert design[:2] == ("POST", f"{VOICE_URL}/voice_design")
    assert design[2]["json"] == {
        "speaker_id": "S_test",
        "prompt": {"text_prompt": "清亮"},
        "text": "试音",
        "language": 0,
    }
    assert design[2]["headers"]["X-Api-Key"] == "voice-key"
    assert uuid.UUID(design[2]["headers"]["X-Api-Request-Id"]).version == 4
    assert query[:2] == ("POST", f"{VOICE_URL}/get_voice")
    assert query[2]["json"] == {"speaker_id": "S_test"}
    assert download == ("GET", SAMPLE_URL, {"limit": studio_providers.SAMPLE_LIMIT})


async def test_s1_raw_upload_and_lost_start_response_do_not_repeat_billed_calls(
    monkeypatch,
):
    monkeypatch.setattr(studio_providers, "asyncio", SimpleNamespace(sleep=no_wait))
    job_id = "0b6f0c1e-7c0a-4a52-9d1b-2f4d5c1f8a11"
    endpoint = f"{COVER_URL}/{job_id}"
    transport = ScriptedTransport(
        {"id": job_id, "upload": {"track_url": TRACK_URL, "voice_url": SAMPLE_URL}},
        b"",
        b"",
        StudioError("lost response", 502),
        {"status": "queued"},
        {"status": "running"},
        {"status": "done", "result_url": RESULT_URL},
        MP3,
    )
    statuses = []
    sample = wav_bytes()
    result = await S1CoverGenerator(transport).generate(
        "cover-key", MP3, sample, statuses.append
    )
    headers = {"Authorization": "Bearer cover-key"}
    assert result == MP3 and statuses == ["uploading", "queued", "running"]
    assert transport.calls == [
        (
            "POST",
            COVER_URL,
            {"headers": headers, "json": {"mode": "cover", "format": "mp3"}},
        ),
        ("PUT", TRACK_URL, {"data": MP3, "limit": 0}),
        ("PUT", SAMPLE_URL, {"data": sample, "limit": 0}),
        ("POST", f"{endpoint}/start", {"headers": headers}),
        ("GET", endpoint, {"headers": headers}),
        ("GET", endpoint, {"headers": headers}),
        ("GET", endpoint, {"headers": headers}),
        ("GET", RESULT_URL, {"limit": studio_providers.COVER_LIMIT}),
    ]


@pytest.mark.parametrize(
    "url",
    [
        "http://audio.example.com/file",
        "https://127.0.0.1/file",
        "https://user@audio.example.com/file",
    ],
)
def test_only_public_https_media_link_format_is_accepted(url):
    with pytest.raises(StudioError, match="音频链接无效"):
        media_url(url)


async def test_studio_routes_require_login_and_keep_secrets_and_tts_revision_separate(
    plugin, monkeypatch
):
    monkeypatch.setenv("S1_COVER_API_KEY", "fake-env-key")
    plugin.request.username = None
    studio_routes = [
        handler
        for name, handler, *_ in plugin.instance.context.routes
        if "/studio/" in name
    ]
    assert len(studio_routes) == 9
    for handler in studio_routes:
        assert (await handler())["status"] == 401
    plugin.request.username = "admin"
    revision = plugin.instance._revision()
    plugin.request.payload = {"volcengine_voice_api_key": "fake-volc-key"}
    assert await route(plugin, "keys")() == {
        "volcengine": "config",
        "s1": "environment",
    }
    state = await route(plugin, "state")()
    assert state["has_preset_key"] is True
    assert "fake-" not in str(state)
    assert plugin.config.saved["volcengine_voice_api_key"] == "fake-volc-key"
    assert plugin.instance._revision() == revision
    plugin.config.fail_save = True
    plugin.request.payload = {"clear_volcengine_voice_api_key": True}
    assert (await route(plugin, "keys")())["status"] == 500
    assert plugin.config["volcengine_voice_api_key"] == "fake-volc-key"
    plugin.config.fail_save = False
    assert (await route(plugin, "keys")())["volcengine"] == "none"


async def test_studio_import_design_and_cover_with_custom_and_preset_voices(
    plugin, monkeypatch
):
    studio = plugin.instance.studio
    plugin.config.update(
        volcengine_voice_api_key="voice-key", s1_cover_api_key="cover-key"
    )
    studio.preset_client = Client()
    inputs = []
    design_sample = wav_bytes(seconds=5)

    async def design(*args):
        inputs.append(args)
        return VoiceDesign(design_sample, 7)

    async def probe(file, suffix):
        assert file.read_bytes() == b"authorized fixture"
        assert suffix == ".wav"
        return 100.8

    async def clip(file, suffix, output, start, duration):
        assert file.read_bytes() == b"authorized fixture"
        assert (suffix, start, duration) == (".wav", 15, 30)
        assert output.suffix == ".mp3"
        return MP3

    async def generate(key, track, reference, on_status):
        assert key == "cover-key" and track == MP3
        inputs.append(reference)
        on_status("running")
        return MP3

    monkeypatch.setattr(studio_module, "ffmpeg_available", lambda: True)
    monkeypatch.setattr(studio_module, "inspect_song", probe)
    monkeypatch.setattr(studio_module, "make_clip", clip)
    studio.designers["volcengine"] = SimpleNamespace(design=design, label="mock")
    studio.cover_provider = SimpleNamespace(generate=generate)

    plugin.request.uploads = {"file": Upload("窗边.wav", b"authorized fixture")}
    song = await route(plugin, "songs/import")()
    assert (song["title"], song["seconds"]) == ("窗边", 100.8)
    plugin.request.payload = DESIGN
    assert (await route(plugin, "design")())["status"] == "preparing"
    assert (await route(plugin, "design")())["status"] == 409
    await studio.tasks["design"]
    state = await route(plugin, "state")()
    voice = state["voices"][0]
    assert state["jobs"]["design"]["remaining"] == 7
    assert (voice["name"], voice["provider"]) == ("薄荷女声", "volcengine")
    assert inputs == [
        ("voice-key", "S_test", "年轻清亮女声", "欢迎来到音色工作台。", 0)
    ]
    assert (await route(plugin, "design")())["status"] == 400
    reloaded = Studio(studio.root, plugin.config)
    assert reloaded.voices.all() == [voice] and reloaded.songs.all() == [song]
    preview = await route(plugin, "voices/<voice_id>/audio")(voice_id=voice["id"])
    assert preview["content_type"] == "audio/wav"
    assert base64.b64decode(preview["audio_base64"]) == design_sample

    cover_payload = {
        "song_id": song["id"],
        "voice_id": voice["id"],
        "start_seconds": 15,
        "duration_seconds": 30,
        "rights_confirmed": True,
    }
    plugin.request.payload = cover_payload
    cover_route = route(plugin, "cover")
    scheduled = await cover_route()
    assert (await cover_route())["status"] == 409
    assert (await route(plugin, "songs/<song_id>/remove")(song_id=song["id"]))[
        "status"
    ] == 409
    plugin.request.payload = {**DESIGN, "replace_slot": True}
    assert (await route(plugin, "design")())["status"] == 409
    await studio.tasks["cover"]
    result = await route(plugin, "cover/<job_id>/audio")(job_id=scheduled["id"])
    assert result == {
        "content_type": "audio/mpeg",
        "audio_base64": base64.b64encode(MP3).decode(),
    }
    assert studio.jobs["cover"].result == {
        "title": "窗边",
        "voice_name": "薄荷女声",
        "seconds": 30,
    }
    assert inputs[-1] == design_sample and not studio.preset_client.calls

    plugin.request.payload = {**cover_payload, "voice_id": "tide"}
    await cover_route()
    await studio.tasks["cover"]
    assert studio.jobs["cover"].status == "done" and inputs[-1] == wav_bytes()
    assert studio.preset_client.calls[0][2] == "tide" and not plugin.client.calls
    assert (await route(plugin, "cover/<job_id>/audio")(job_id=scheduled["id"]))[
        "status"
    ] == 404
    plugin.request.payload = {**DESIGN, "replace_slot": True, "name": "新薄荷女声"}
    await route(plugin, "design")()
    plugin.request.payload = cover_payload
    assert (await cover_route())["status"] == 409
    await studio.tasks["design"]
    replacement = studio.voices.all()[0]
    assert replacement["name"] == "新薄荷女声" and replacement["id"] != voice["id"]
    assert (await route(plugin, "voices/<voice_id>/audio")(voice_id=voice["id"]))[
        "status"
    ] == 404
    assert (
        await route(plugin, "voices/<voice_id>/remove")(voice_id=replacement["id"])
    )["removed"]
    assert (await route(plugin, "songs/<song_id>/remove")(song_id=song["id"]))[
        "removed"
    ]
    assert studio.state()["songs"] == [] and studio.state()["voices"] == []
    await plugin.instance.terminate()
    assert studio.preset_client.closed and plugin.client.closed


async def test_invalid_cover_payload_does_not_start_job(plugin, monkeypatch, tmp_path):
    monkeypatch.setattr(studio_module, "ffmpeg_available", lambda: True)
    plugin.config["s1_cover_api_key"] = "cover-key"
    source = tmp_path / "song.wav"
    source.write_bytes(wav_bytes())
    studio = plugin.instance.studio
    song = studio.songs.add(source, title="窗边", suffix=".wav", seconds=24.9)
    payload = {
        "song_id": song["id"],
        "voice_id": "stella",
        "start_seconds": 15,
        "duration_seconds": 10,
    }
    plugin.request.payload = payload
    cover = route(plugin, "cover")
    assert "有权" in (await cover())["error"]
    payload["rights_confirmed"] = True
    assert "超出" in (await cover())["error"]
    payload.update(start_seconds=0, duration_seconds=10.5)
    assert "整数秒" in (await cover())["error"]
    payload.update(duration_seconds=10, song_id="invalid-id")
    assert (await cover())["status"] == 404
    assert studio.jobs == {}


@pytest.mark.skipif(not ffmpeg_available(), reason="FFmpeg / ffprobe not installed")
async def test_ffmpeg_creates_requested_length_clip(tmp_path):
    original = tmp_path / "原唱素材.audio"
    original.write_bytes(wav_bytes(seconds=24))
    assert await inspect_song(original, ".wav") == 24
    output = tmp_path / "clip.mp3"
    clip = await make_clip(original, ".wav", output, 9, 11)
    assert clip == output.read_bytes()
    assert await inspect_song(output, ".mp3") == pytest.approx(11, abs=0.1)
