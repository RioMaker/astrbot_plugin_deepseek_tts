"""Authenticated AstrBot bridge endpoints for the dashboard studio."""

import asyncio
import base64
from functools import wraps

from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request

from .settings import PLUGIN_NAME
from .studio import key_changes
from .studio_providers import StudioError, audio_type


async def body():
    payload = await request.json(default={})
    if not isinstance(payload, dict):
        raise StudioError("请求格式无效。")
    return payload


def media(data):
    return {
        "content_type": audio_type(data),
        "audio_base64": base64.b64encode(data).decode("ascii"),
    }


def authenticated(handler):
    @wraps(handler)
    async def dispatch(**params):
        if not request.username:
            return error_response("请先登录 AstrBot 管理面板。", status_code=401)
        try:
            return json_response(await handler(**params))
        except StudioError as exc:
            return error_response(str(exc), status_code=exc.status_code)
        except Exception as exc:
            logger.warning("[Voice Studio] 页面请求未完成：%s", type(exc).__name__)
            return error_response(
                "操作未完成，请检查 AstrBot 服务端日志。", status_code=500
            )

    return dispatch


class StudioPage:
    def __init__(self, context, studio, config_lock):
        self.studio = studio
        self.config_lock = config_lock
        for path, handler, method in (
            ("state", self.state, "GET"),
            ("keys", self.save_keys, "POST"),
            ("design", self.design, "POST"),
            ("voices/<voice_id>/audio", self.voice_audio, "GET"),
            ("voices/<voice_id>/remove", self.remove_voice, "POST"),
            ("songs/import", self.import_song, "POST"),
            ("songs/<song_id>/remove", self.remove_song, "POST"),
            ("cover", self.cover, "POST"),
            ("cover/<job_id>/audio", self.cover_audio, "GET"),
        ):
            context.register_web_api(
                f"/{PLUGIN_NAME}/studio/{path}",
                authenticated(handler),
                [method],
                "音色与翻唱",
            )

    async def state(self):
        return self.studio.state()

    async def save_keys(self):
        changes = key_changes(await body())
        if not changes:
            return self.studio.credentials()
        config = self.studio.config
        async with self.config_lock:
            before = dict(config)
            try:
                config.update(changes)
                config.save_config()
            except Exception:
                config.clear()
                config.update(before)
                raise
            return self.studio.credentials()

    async def design(self):
        return self.studio.start_design(await body())

    async def voice_audio(self, voice_id):
        return media(await self.studio.voice_audio(voice_id))

    async def remove_voice(self, voice_id):
        self.studio.remove_voice(voice_id)
        return {"removed": True}

    async def import_song(self):
        return await self.studio.import_song((await request.files()).get("file"))

    async def remove_song(self, song_id):
        self.studio.remove_song(song_id)
        return {"removed": True}

    async def cover(self):
        return self.studio.start_cover(await body())

    async def cover_audio(self, job_id):
        return await asyncio.to_thread(media, self.studio.cover_audio(job_id))
