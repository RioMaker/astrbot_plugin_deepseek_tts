"""Official API client and bounded speech generation/cache lifecycle."""

import asyncio
import io
import re
import struct
import time
import uuid
import wave
from dataclasses import dataclass
from pathlib import Path

import aiohttp

from .settings import VOICES

ENDPOINT = "https://api.deepseek.com/v1/tools/tts"
MAX_AUDIO_BYTES = 24 * 1024 * 1024
PREVIEW_MAX_BYTES = 2 * 1024 * 1024


class SpeechError(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class Audio:
    data: bytes
    duration: float
    sample_rate: int


def validate_text(text, limit):
    if not isinstance(text, str) or not text.strip():
        raise SpeechError("empty_text", "请填写要朗读的文字。")
    text = text.strip()
    if len(text) > limit:
        raise SpeechError("text_too_long", f"本次文字超过 {limit} 字，请缩短后再试。")
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", text):
        raise SpeechError("invalid_text", "文字中含有无法朗读的控制字符。")
    return text


def normalize_wav(data):
    """Repair unknown streaming lengths without accepting a truncated known length."""
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise SpeechError("invalid_audio", "服务返回的内容不是有效的 WAV 音频。")
    riff_size = struct.unpack_from("<I", data, 4)[0]
    if riff_size != 0xFFFFFFFF and riff_size + 8 > len(data):
        raise SpeechError("incomplete_audio", "音频传输不完整，请稍后重试。")
    offset = 12
    data_size = None
    while offset + 8 <= len(data):
        name, size = struct.unpack_from("<4sI", data, offset)
        offset += 8
        if name == b"data":
            data_size = len(data) - offset if size == 0xFFFFFFFF else size
            if data_size > len(data) - offset:
                raise SpeechError("incomplete_audio", "音频传输不完整，请稍后重试。")
            break
        if size > len(data) - offset:
            raise SpeechError("invalid_audio", "服务返回的音频文件头不完整。")
        offset += size + (size % 2)
    try:
        with wave.open(io.BytesIO(data), "rb") as source:
            channels = source.getnchannels()
            width = source.getsampwidth()
            rate = source.getframerate()
            pcm = source.readframes(source.getnframes())
        if (
            data_size is None
            or not pcm
            or len(pcm) != data_size
            or channels not in (1, 2)
            or width != 2
            or not 8000 <= rate <= 96000
            or len(pcm) % (channels * width)
        ):
            raise ValueError("Invalid PCM")
        output = io.BytesIO()
        with wave.open(output, "wb") as target:
            target.setnchannels(channels)
            target.setsampwidth(width)
            target.setframerate(rate)
            target.writeframes(pcm)
        return Audio(output.getvalue(), len(pcm) / (channels * width * rate), rate)
    except (wave.Error, EOFError, ValueError, struct.error) as exc:
        raise SpeechError("invalid_audio", "服务返回的音频无法完整解码。") from exc


class DeepSeekClient:
    def __init__(self):
        self._session = None

    async def synthesize(self, key, text, voice, settings, max_bytes=MAX_AUDIO_BYTES):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        try:
            async with self._session.post(
                ENDPOINT,
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "text": text,
                    "voice_id": voice,
                    "format": "wav",
                    "filter_markdown": settings.filter_markdown,
                },
                timeout=aiohttp.ClientTimeout(total=settings.timeout_seconds),
                allow_redirects=False,
            ) as response:
                if response.status != 200:
                    messages = {
                        400: "文字无法合成，可能没有可朗读内容或未通过服务审核。",
                        401: "DeepSeek API Key 无效，请检查配置。",
                        402: "DeepSeek 账户余额不足，请检查官方账户。",
                        403: "DeepSeek 拒绝了请求，请检查账户权限。",
                        429: "DeepSeek 请求过于频繁，请稍后再试。",
                        503: "DeepSeek 服务繁忙，请稍后再试。",
                        504: "DeepSeek 合成超时，请稍后再试。",
                    }
                    raise SpeechError(
                        f"http_{response.status}",
                        messages.get(
                            response.status, "DeepSeek 暂时无法合成语音，请稍后再试。"
                        ),
                    )
                if response.content_type != "audio/wav":
                    raise SpeechError("invalid_audio", "服务没有返回 WAV 音频。")
                chunks = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    if len(chunks) + len(chunk) > max_bytes:
                        raise SpeechError(
                            "audio_too_large", "生成的音频过长，请缩短文字。"
                        )
                    chunks.extend(chunk)
                return normalize_wav(bytes(chunks))
        except asyncio.TimeoutError as exc:
            raise SpeechError("timeout", "语音合成超时，请稍后再试。") from exc
        except aiohttp.ClientPayloadError as exc:
            raise SpeechError("incomplete_audio", "音频传输中断，请稍后再试。") from exc
        except aiohttp.ClientError as exc:
            raise SpeechError("network", "无法连接 DeepSeek，请检查网络。") from exc

    async def close(self):
        if self._session is not None:
            await self._session.close()


class SpeechService:
    def __init__(self, client=None):
        self.client = client or DeepSeekClient()
        self._lock = asyncio.Lock()
        self._active = 0
        self._recent = {}
        self.status = {"success_count": 0, "last_error": "", "last_duration": None}

    async def generate(self, settings, text, scope, voice=None, preview=False):
        text = validate_text(
            text, min(settings.max_chars, 100) if preview else settings.max_chars
        )
        voice = settings.voice_id if voice is None else voice
        if not isinstance(voice, str) or voice not in VOICES:
            raise SpeechError("invalid_voice", "请选择海星或暗潮音色。")
        if not settings.enabled and not preview:
            raise SpeechError("disabled", "DeepSeek TTS 已关闭。")
        key = settings.key()
        if not key:
            raise SpeechError(
                "missing_key", "请先在 DeepSeek TTS 配置中填写官方 API Key。"
            )
        now = time.monotonic()
        async with self._lock:
            self._recent = {k: t for k, t in self._recent.items() if now - t < 300}
            if self._active >= 2:
                raise SpeechError("busy", "语音任务正在处理中，请稍后再试。")
            last = self._recent.get(scope)
            if last is not None and now - last < settings.cooldown_seconds:
                raise SpeechError("cooldown", "这个会话刚刚生成过语音，请稍后再试。")
            self._recent[scope] = now
            self._active += 1
        try:
            audio = await self.client.synthesize(
                key,
                text,
                voice,
                settings,
                PREVIEW_MAX_BYTES if preview else MAX_AUDIO_BYTES,
            )
            self.status.update(
                success_count=self.status["success_count"] + 1,
                last_error="",
                last_duration=round(audio.duration, 2),
            )
            return audio
        except SpeechError as exc:
            self.status["last_error"] = str(exc)
            raise
        finally:
            async with self._lock:
                self._active -= 1

    async def close(self):
        await self.client.close()


class AudioStore:
    def __init__(self, directory):
        self.directory = Path(directory)

    def save(self, audio):
        self.directory.mkdir(parents=True, exist_ok=True)
        self.prune()
        path = self.directory / f"{uuid.uuid4().hex}.wav"
        temporary = path.with_suffix(".tmp")
        try:
            temporary.write_bytes(audio.data)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        return path

    def prune(self):
        """Only touch files owned by this plugin, and retain recent deliveries."""
        now = time.time()
        entries = []
        for path in self.directory.glob("*.wav"):
            if path.is_symlink() or not re.fullmatch(r"[a-f0-9]{32}\.wav", path.name):
                continue
            try:
                stat = path.stat()
                if now - stat.st_mtime > 86400:
                    path.unlink()
                else:
                    entries.append((stat.st_mtime, stat.st_size, path))
            except OSError:
                continue
        total = sum(size for _, size, _ in entries)
        for modified, size, path in sorted(entries):
            if total <= 256 * 1024 * 1024:
                break
            if now - modified < 600:
                continue
            try:
                path.unlink()
                total -= size
            except OSError:
                continue
