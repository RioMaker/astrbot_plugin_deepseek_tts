"""Cloud adapters for the dashboard studio, independent of chat TTS."""

import asyncio
import ipaddress
import json
import re
import uuid
from dataclasses import dataclass

import aiohttp
from aiohttp.resolver import DefaultResolver
from yarl import URL

VOICE_URL = "https://openspeech.bytedance.com/api/v3/tts"
COVER_URL = "https://s1solutions.pro/v1/jobs"
SAMPLE_LIMIT = 2 * 1024 * 1024
COVER_LIMIT = 16 * 1024 * 1024


class StudioError(Exception):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


def audio_type(data):
    if len(data) >= 44 and data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav"
    if len(data) > 10 and (
        data.startswith(b"ID3") or (data[0] == 0xFF and data[1] & 0xE6 == 0xE2)
    ):
        return "audio/mpeg"
    raise StudioError("音频为空或不是有效的 MP3 / WAV。", 502)


def media_url(value):
    try:
        if not isinstance(value, str) or not value or len(value) > 8192:
            raise ValueError
        url = URL(value, encoded=True)
        host = url.raw_host or ""
        if (
            url.scheme != "https"
            or url.raw_user is not None
            or url.raw_password is not None
            or url.port != 443
            or not re.fullmatch(r"[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+", host)
            or not host.rsplit(".", 1)[-1].isalpha()
        ):
            raise ValueError
        return url
    except ValueError as exc:
        raise StudioError("服务返回的音频链接无效或已过期。", 502) from exc


class PublicResolver(aiohttp.abc.AbstractResolver):
    def __init__(self):
        self.resolver = DefaultResolver()

    async def resolve(self, host, port=0, family=0):
        addresses = await self.resolver.resolve(host, port, family)
        for address in addresses:
            ip = ipaddress.ip_address(address["host"])
            if not ip.is_global or ip.is_multicast:
                raise OSError("non-public address")
        return addresses

    async def close(self):
        await self.resolver.close()


class StudioHTTP:
    def __init__(self):
        self.session = None
        self.resolver = None

    async def request(self, service, method, url, *, limit=128 * 1024, **options):
        if self.session is None:
            self.resolver = PublicResolver()
            self.session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(resolver=self.resolver),
                timeout=aiohttp.ClientTimeout(total=55, connect=10),
                cookie_jar=aiohttp.DummyCookieJar(),
            )
        try:
            async with self.session.request(
                method, url, allow_redirects=False, **options
            ) as response:
                if response.status in (401, 403):
                    raise StudioError(
                        f"{service}拒绝访问，请检查密钥、资源权限或链接。", 502
                    )
                if response.status == 402:
                    raise StudioError(f"{service}账户余额不足。", 502)
                if not 200 <= response.status < 300:
                    raise StudioError(
                        f"{service}请求失败（HTTP {response.status}）。", 502
                    )
                if not limit:
                    return b""
                if (
                    response.content_length is not None
                    and response.content_length > limit
                ):
                    raise StudioError(f"{service}返回的数据超过大小上限。", 502)
                data = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    if len(data) + len(chunk) > limit:
                        raise StudioError(f"{service}返回的数据超过大小上限。", 502)
                    data.extend(chunk)
                return bytes(data)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise StudioError(
                f"连接{service}失败或超时，请检查服务端网络。", 502
            ) from exc

    async def json(self, service, method, url, **options):
        data = await self.request(service, method, url, **options)
        try:
            result = json.loads(data)
        except (UnicodeError, ValueError) as exc:
            raise StudioError(f"{service}返回了无法识别的数据。", 502) from exc
        if not isinstance(result, dict):
            raise StudioError(f"{service}返回了无法识别的数据。", 502)
        return result

    async def close(self):
        if self.session is not None:
            await self.session.close()
            await self.resolver.close()


@dataclass(frozen=True)
class VoiceDesign:
    sample: bytes
    remaining: int | None = None


class VolcengineVoiceDesigner:
    label = "火山引擎 · 豆包音色设计"

    def __init__(self, http):
        self.http = http

    async def _post(self, endpoint, key, payload):
        return await self.http.json(
            "火山引擎",
            "POST",
            f"{VOICE_URL}/{endpoint}",
            headers={"X-Api-Key": key, "X-Api-Request-Id": str(uuid.uuid4())},
            json=payload,
        )

    async def design(self, key, slot, prompt, text, language):
        result = await self._post(
            "voice_design",
            key,
            {
                "speaker_id": slot,
                "prompt": {"text_prompt": prompt},
                "text": text,
                "language": language,
            },
        )
        while True:
            status = result.get("status")
            if (
                result.get("speaker_id", slot) != slot
                or type(status) is not int
                or status not in (1, 2, 4)
            ):
                raise StudioError(
                    "火山音色设计未成功，请检查槽位权限与剩余设计次数。", 502
                )
            if status in (2, 4):
                demo = result.get("demo_audio")
                statuses = result.get("speaker_status")
                if not demo and isinstance(statuses, list):
                    demo = next(
                        (
                            item["demo_audio"]
                            for item in statuses
                            if isinstance(item, dict)
                            and item.get("model_type") == 5
                            and item.get("demo_audio")
                        ),
                        None,
                    )
                if demo:
                    sample = await self.http.request(
                        "火山引擎", "GET", media_url(demo), limit=SAMPLE_LIMIT
                    )
                    audio_type(sample)
                    remaining = result.get("available_training_times")
                    return VoiceDesign(
                        sample, remaining if type(remaining) is int else None
                    )
            await asyncio.sleep(3)
            result = await self._post("get_voice", key, {"speaker_id": slot})


class S1CoverGenerator:
    label = "S1 Solutions"

    def __init__(self, http):
        self.http = http

    async def generate(self, key, track, reference, on_status):
        headers = {"Authorization": f"Bearer {key}"}
        created = await self.http.json(
            self.label,
            "POST",
            COVER_URL,
            headers=headers,
            json={"mode": "cover", "format": "mp3"},
        )
        job_id = created.get("id")
        upload = created.get("upload")
        if (
            not isinstance(job_id, str)
            or not re.fullmatch(r"[0-9a-f-]{36}", job_id)
            or not isinstance(upload, dict)
        ):
            raise StudioError("翻唱服务没有返回有效的任务。", 502)
        uploads = (
            (media_url(upload.get("track_url")), track),
            (media_url(upload.get("voice_url")), reference),
        )
        on_status("uploading")
        for url, data in uploads:
            await self.http.request(self.label, "PUT", url, data=data, limit=0)
        endpoint = f"{COVER_URL}/{job_id}"
        try:
            result = await self.http.json(
                self.label, "POST", f"{endpoint}/start", headers=headers
            )
        except StudioError:
            result = await self.http.json(self.label, "GET", endpoint, headers=headers)
            if result.get("status") == "awaiting_upload":
                raise
        poll_errors = 0
        while result.get("status") in ("queued", "running"):
            on_status(result["status"])
            await asyncio.sleep(5)
            try:
                result = await self.http.json(
                    self.label, "GET", endpoint, headers=headers
                )
            except StudioError:
                poll_errors += 1
                if poll_errors >= 3:
                    raise StudioError(
                        "暂时无法查询已提交的翻唱任务，请勿立即重复生成。", 502
                    )
            else:
                poll_errors = 0
        if result.get("status") != "done":
            raise StudioError("翻唱任务未完成，请在 S1 控制台查看。", 502)
        audio = await self.http.request(
            self.label, "GET", media_url(result.get("result_url")), limit=COVER_LIMIT
        )
        if audio_type(audio) != "audio/mpeg":
            raise StudioError("翻唱服务未返回 MP3。", 502)
        return audio
