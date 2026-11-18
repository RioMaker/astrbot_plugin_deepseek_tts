"""Dashboard-only voice library and cover workflow."""

import asyncio
import contextvars
import logging
import os
import re
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .settings import VOICES, Settings
from .speech import PREVIEW_MAX_BYTES, DeepSeekClient, SpeechError
from .studio_media import (
    INPUT_FORMATS,
    MAX_COVER_SECONDS,
    MIN_COVER_SECONDS,
    SONG_BYTES,
    SONG_LIMIT,
    VOICE_LIMIT,
    AssetLibrary,
    ffmpeg_available,
    inspect_song,
    make_clip,
)
from .studio_providers import (
    SAMPLE_LIMIT,
    S1CoverGenerator,
    StudioError,
    StudioHTTP,
    VolcengineVoiceDesigner,
    audio_type,
)

KEYS = {
    "volcengine": ("volcengine_voice_api_key", "VOLCENGINE_VOICE_API_KEY"),
    "s1": ("s1_cover_api_key", "S1_COVER_API_KEY"),
}
STUDIO_FIELDS = frozenset(config_field for config_field, _ in KEYS.values())
REFERENCE_TEXT = (
    "晚风轻轻吹过树梢，街角的灯一盏接一盏亮起。我们慢慢走过熟悉的路，"
    "听见远处有人在笑，也听见风和树叶说话。今天很平常，却让人想记住这一刻。"
)
logger = logging.getLogger(__name__)


def secret(config, field, variable):
    value = config.get(field)
    if isinstance(value, str) and value.strip():
        return value.strip(), "config"
    value = os.environ.get(variable, "").strip()
    return value, "environment" if value else "none"


def clean_key(value):
    if not isinstance(value, str) or len(value) > 512:
        raise StudioError("API Key 格式无效。")
    if value.strip() and not re.fullmatch(r"[\x21-\x7e]+", value.strip()):
        raise StudioError("API Key 不能包含空白或非英文字符。")
    return value.strip()


def key_changes(payload):
    changes = {}
    for config_field, _ in KEYS.values():
        value = clean_key(payload.get(config_field, ""))
        clear = payload.get(f"clear_{config_field}", False)
        if type(clear) is not bool or (clear and value):
            raise StudioError("不能同时输入新密钥并清除已保存的密钥。")
        if clear or value:
            changes[config_field] = value
    return changes


def required_text(value, label, maximum):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= maximum:
        raise StudioError(f"{label}须填写 1～{maximum} 字。")
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", value):
        raise StudioError(f"{label}含有无效字符。")
    return value.strip()


@dataclass
class Job:
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: str = "preparing"
    error: str = ""
    result: dict = field(default_factory=dict)
    audio: bytes | None = field(default=None, repr=False)
    voice_id: str | None = None

    def public(self):
        return {
            "id": self.id,
            "status": self.status,
            "error": self.error,
            **self.result,
        }


class Studio:
    def __init__(self, root, config):
        self.root = Path(root)
        self.config = config
        self.voices = AssetLibrary(self.root / "voices")
        self.songs = AssetLibrary(self.root / "songs")
        self.http = StudioHTTP()
        self.designers = {"volcengine": VolcengineVoiceDesigner(self.http)}
        self.cover_provider = S1CoverGenerator(self.http)
        self.preset_client = DeepSeekClient()
        self.jobs = {}
        self.tasks = {}
        self.import_lock = asyncio.Lock()

    def credentials(self):
        return {name: secret(self.config, *fields)[1] for name, fields in KEYS.items()}

    def state(self):
        default = self.config.get("voice_id")
        return {
            "credentials": self.credentials(),
            "has_preset_key": bool(
                secret(self.config, "api_key", "DEEPSEEK_API_KEY")[0]
            ),
            "designers": {
                name: designer.label for name, designer in self.designers.items()
            },
            "presets": VOICES,
            "default_voice": default
            if isinstance(default, str) and default in VOICES
            else "stella",
            "voices": self.voices.all(),
            "songs": self.songs.all(),
            "jobs": {kind: job.public() for kind, job in self.jobs.items()},
            "ffmpeg_ready": ffmpeg_available(),
            "reference_text": REFERENCE_TEXT,
        }

    def _active_job(self, kind):
        task = self.tasks.get(kind)
        if task is not None and not task.done():
            return self.jobs[kind]
        return None

    def _ensure_idle(self, kind):
        if self._active_job(kind):
            raise StudioError("已有同类任务在运行，请等它完成。", 409)

    def _launch(self, kind, operation, timeout, *, voice_id=None):
        job = self.jobs[kind] = Job(voice_id=voice_id)
        self.tasks[kind] = contextvars.Context().run(
            asyncio.create_task, self._run(job, operation, timeout)
        )
        return job.public()

    async def _run(self, job, operation, timeout):
        try:
            await asyncio.wait_for(operation(job), timeout=timeout)
        except asyncio.TimeoutError:
            job.error = "任务等待超时。若已提交到平台，请先查状态，勿重复提交。"
        except (StudioError, SpeechError) as exc:
            job.error = str(exc)
        except Exception as exc:
            logger.warning("[Voice Studio] 任务失败：%s", type(exc).__name__)
            job.error = "任务未完成，请检查服务端音频工具和数据目录权限。"
        else:
            job.status = "done"
            return
        job.audio = None
        job.status = "failed"

    def start_design(self, payload):
        self._ensure_idle("design")
        provider = payload.get("provider")
        if not isinstance(provider, str) or provider not in self.designers:
            raise StudioError("请选择有效的音色设计平台。")
        key = clean_key(secret(self.config, *KEYS[provider])[0])
        if not key:
            raise StudioError("请先保存所选平台的 API Key。")
        name = required_text(payload.get("name"), "音色名称", 60)
        slot = required_text(payload.get("speaker_id"), "Speaker ID", 256)
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]*", slot):
            raise StudioError("Speaker ID 应为控制台分配的音色代号。")
        prompt = required_text(payload.get("prompt"), "音色描述", 200)
        text = required_text(payload.get("text"), "试听文案", 300)
        language = payload.get("language", 0)
        if type(language) is not int or language not in (0, 1):
            raise StudioError("试听语种应为中文或英文。")
        saved = self.voices.all()
        existing = next(
            (
                item
                for item in saved
                if item["provider"] == provider and item["speaker_id"] == slot
            ),
            None,
        )
        if existing and payload.get("replace_slot") is not True:
            raise StudioError("该槽位已有本地音色。若要重新设计，请勾选覆盖同槽位。")
        replacing = existing["id"] if existing else None
        active_cover = self._active_job("cover")
        if replacing and active_cover and active_cover.voice_id == replacing:
            raise StudioError("该音色正在用于翻唱，请等任务完成再覆盖。", 409)
        if not existing and len(saved) >= VOICE_LIMIT:
            raise StudioError(
                f"音色库最多保存 {VOICE_LIMIT} 个音色，请先移除不用的音色。"
            )

        async def design(job):
            job.status = "designing"
            self.root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=self.root) as work:
                result = await self.designers[provider].design(
                    key, slot, prompt, text, language
                )
                source = Path(work) / "sample"
                await asyncio.to_thread(source.write_bytes, result.sample)
                voice = self.voices.add(
                    source,
                    name=name,
                    provider=provider,
                    speaker_id=slot,
                    content_type=audio_type(result.sample),
                )
            if existing:
                self.voices.remove(existing["id"])
            job.result = {"voice_id": voice["id"], "remaining": result.remaining}

        return self._launch("design", design, 150, voice_id=replacing)

    async def import_song(self, upload):
        if upload is None or not upload.filename:
            raise StudioError("请选择要上传的歌曲文件。")
        if not ffmpeg_available():
            raise StudioError(
                "请先在 AstrBot 服务端安装 FFmpeg 和 ffprobe，并加入 PATH。"
            )
        filename = Path(upload.filename.replace("\\", "/")).name
        suffix = Path(filename).suffix.lower()
        if suffix not in INPUT_FORMATS:
            raise StudioError("曲库支持 MP3、WAV、M4A 或 FLAC 音频。")
        title = required_text(Path(filename).stem, "歌曲名称", 100)
        if self.import_lock.locked():
            raise StudioError("正在导入另一首歌，请稍后再试。", 409)
        async with self.import_lock:
            if len(self.songs.all()) >= SONG_LIMIT:
                raise StudioError(
                    f"曲库最多保存 {SONG_LIMIT} 首歌，请先移除不用的歌曲。"
                )
            data = await upload.read(SONG_BYTES + 1)
            if not data or len(data) > SONG_BYTES:
                raise StudioError("请选择不超过 12 MiB 的音频文件。", 413)
            self.root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=self.root) as work:
                source = Path(work) / f"source{suffix}"
                await asyncio.to_thread(source.write_bytes, data)
                seconds = await inspect_song(source, suffix)
                return self.songs.add(
                    source, title=title, suffix=suffix, seconds=seconds
                )

    def start_cover(self, payload):
        self._ensure_idle("cover")
        if payload.get("rights_confirmed") is not True:
            raise StudioError("请确认你有权使用歌曲及音色，并允许上传给翻唱平台。")
        key = clean_key(secret(self.config, *KEYS["s1"])[0])
        if not key:
            raise StudioError("请先保存 S1 Solutions API Key。")
        if not ffmpeg_available():
            raise StudioError(
                "请先在 AstrBot 服务端安装 FFmpeg 和 ffprobe，并加入 PATH。"
            )
        song = self.songs.get(payload.get("song_id"))
        start = payload.get("start_seconds", 0)
        seconds = payload.get("duration_seconds")
        if type(start) is not int or start < 0:
            raise StudioError("起点必须是不小于 0 的整数秒。")
        if (
            type(seconds) is not int
            or not MIN_COVER_SECONDS <= seconds <= MAX_COVER_SECONDS
        ):
            raise StudioError("翻唱时长须为 10～300 之间的整数秒。")
        if start + seconds > song["seconds"] + 0.01:
            raise StudioError("起点和时长超出了歌曲范围，请缩短时长或提前起点。")
        voice_id = payload.get("voice_id")
        if isinstance(voice_id, str) and voice_id in VOICES:
            preset_key = clean_key(
                secret(self.config, "api_key", "DEEPSEEK_API_KEY")[0]
            )
            if not preset_key:
                raise StudioError("默认音色需要 DeepSeek API Key，或改选已设计的音色。")
            voice_name = VOICES[voice_id]
            sample_path = None
        else:
            voice = self.voices.get(voice_id)
            active_design = self._active_job("design")
            if active_design and active_design.voice_id == voice_id:
                raise StudioError(
                    "该音色正在重新设计，请等任务完成后选择新的音色。", 409
                )
            voice_name = voice["name"]
            sample_path = self.voices.audio_path(voice["id"])
        song_path = self.songs.audio_path(song["id"])

        async def cover(job):
            with tempfile.TemporaryDirectory(dir=self.root) as work:
                track = await make_clip(
                    song_path, song["suffix"], Path(work) / "clip.mp3", start, seconds
                )
                if sample_path is None:
                    audio = await self.preset_client.synthesize(
                        preset_key,
                        REFERENCE_TEXT,
                        voice_id,
                        Settings(),
                        PREVIEW_MAX_BYTES,
                    )
                    sample = audio.data
                else:
                    sample = await read_sample(sample_path)
                job.audio = await self.cover_provider.generate(
                    key, track, sample, lambda status: setattr(job, "status", status)
                )
            job.result = {
                "title": song["title"],
                "voice_name": voice_name,
                "seconds": seconds,
            }

        return self._launch("cover", cover, 900, voice_id=voice_id)

    def remove_voice(self, voice_id):
        self._ensure_idle("design")
        self._ensure_idle("cover")
        self.voices.remove(voice_id)

    def remove_song(self, song_id):
        self._ensure_idle("cover")
        self.songs.remove(song_id)

    async def voice_audio(self, voice_id):
        self.voices.get(voice_id)
        return await read_sample(self.voices.audio_path(voice_id))

    def cover_audio(self, job_id):
        job = self.jobs.get("cover")
        if job is None or job.id != job_id or job.status != "done" or not job.audio:
            raise StudioError("该翻唱结果不可用，请检查最新任务。", 404)
        return job.audio

    async def close(self):
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        await self.http.close()
        await self.preset_client.close()


async def read_sample(path):
    def read():
        with path.open("rb") as file:
            return file.read(SAMPLE_LIMIT + 1)

    try:
        data = await asyncio.to_thread(read)
    except FileNotFoundError as exc:
        raise StudioError("音色样音文件不存在，请重新设计音色。", 404) from exc
    if len(data) > SAMPLE_LIMIT:
        raise StudioError("音色样音超过大小限制，请重新设计较短样音。")
    audio_type(data)
    return data
