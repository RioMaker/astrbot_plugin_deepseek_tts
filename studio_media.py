"""Local media storage and segment preparation for the web studio."""

import asyncio
import json
import math
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

from .studio_providers import StudioError, audio_type

SONG_LIMIT = 50
VOICE_LIMIT = 20
SONG_BYTES = 12 * 1024 * 1024
SONG_SECONDS = 600
MIN_COVER_SECONDS = 10
MAX_COVER_SECONDS = 300
INPUT_FORMATS = {".mp3": "mp3", ".wav": "wav", ".m4a": "mov", ".flac": "flac"}


def ffmpeg_available():
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


async def run_audio_tool(program, *args):
    executable = shutil.which(program)
    if not executable:
        raise StudioError("请先在 AstrBot 服务端安装 FFmpeg 和 ffprobe，并加入 PATH。")
    process = await asyncio.create_subprocess_exec(
        executable,
        *args,
        stdin=subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}),
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=45)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.communicate()
        raise
    if process.returncode:
        raise StudioError("无法读取或处理该音频，请检查文件格式和完整性。")
    return stdout


async def inspect_song(path, suffix):
    data = await run_audio_tool(
        "ffprobe",
        "-v",
        "error",
        "-protocol_whitelist",
        "file",
        "-f",
        INPUT_FORMATS[suffix],
        "-i",
        str(path),
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=codec_type:format=duration",
        "-of",
        "json",
    )
    try:
        result = json.loads(data)
        seconds = float(result["format"]["duration"])
        if result["streams"][0]["codec_type"] != "audio" or not math.isfinite(seconds):
            raise ValueError
    except (KeyError, IndexError, ValueError, TypeError) as exc:
        raise StudioError("无法读取歌曲时长，或文件没有音轨。") from exc
    if not MIN_COVER_SECONDS <= seconds <= SONG_SECONDS:
        raise StudioError("源歌曲必须在 10 秒到 10 分钟之间。")
    return seconds


async def make_clip(path, suffix, output, start, seconds):
    await run_audio_tool(
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-protocol_whitelist",
        "file",
        "-f",
        INPUT_FORMATS[suffix],
        "-i",
        str(path),
        "-ss",
        str(start),
        "-t",
        str(seconds),
        "-map",
        "0:a:0",
        "-map_metadata",
        "-1",
        "-vn",
        "-ar",
        "44100",
        "-ac",
        "2",
        "-c:a",
        "libmp3lame",
        "-b:a",
        "192k",
        "-f",
        "mp3",
        str(output),
    )
    data = await asyncio.to_thread(output.read_bytes)
    if not data or len(data) > SONG_BYTES or audio_type(data) != "audio/mpeg":
        raise StudioError("无法生成指定的歌曲片段。")
    return data


class AssetLibrary:
    def __init__(self, directory):
        self.directory = Path(directory)

    def _path(self, asset_id, suffix):
        if not isinstance(asset_id, str) or not re.fullmatch(r"[0-9a-f]{32}", asset_id):
            raise StudioError("素材编号无效。", 404)
        return self.directory / f"{asset_id}{suffix}"

    def all(self):
        metadata = sorted(
            self.directory.glob("*.json"), key=lambda item: item.stat().st_mtime_ns
        )
        return [self.get(item.stem) for item in reversed(metadata)]

    def get(self, asset_id):
        try:
            return json.loads(self._path(asset_id, ".json").read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise StudioError("素材不存在，可能已被移除。", 404) from exc

    def audio_path(self, asset_id):
        return self._path(asset_id, ".audio")

    def add(self, source, **fields):
        asset_id = uuid.uuid4().hex
        asset = {"id": asset_id, **fields}
        self.directory.mkdir(parents=True, exist_ok=True)
        audio_path = self.audio_path(asset_id)
        pending = self._path(asset_id, ".pending")
        source.replace(audio_path)
        try:
            pending.write_text(json.dumps(asset, ensure_ascii=False), encoding="utf-8")
            pending.replace(self._path(asset_id, ".json"))
        except OSError:
            pending.unlink(missing_ok=True)
            audio_path.unlink(missing_ok=True)
            raise
        return asset

    def remove(self, asset_id):
        self.get(asset_id)
        self.audio_path(asset_id).unlink(missing_ok=True)
        self._path(asset_id, ".json").unlink()
