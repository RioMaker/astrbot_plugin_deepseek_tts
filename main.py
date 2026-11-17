"""AstrBot handlers and authenticated dashboard endpoints for DeepSeek TTS."""

import asyncio
import base64
import hashlib
import json
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star
from astrbot.api.web import error_response, json_response, request
from astrbot.core.message.message_event_result import ResultContentType
from astrbot.core.star.filter.command import GreedyStr
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .settings import (
    PLUGIN_NAME,
    PREVIEW_TEXT,
    VOICES,
    Settings,
    SettingsError,
    validate_patch,
)
from .speech import AudioStore, SpeechError, SpeechService, validate_text

HANDLED = PLUGIN_NAME + ":handled"
TOOL_RESULT = PLUGIN_NAME + ":tool_result"


class DeepSeekTTSPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.speech = SpeechService()
        self.audio_store = AudioStore(
            Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME / "audio"
        )
        self._config_lock = asyncio.Lock()
        for path, handler, method in (
            ("state", self.page_state, "GET"),
            ("settings/save", self.page_save, "POST"),
            ("preview", self.page_preview, "POST"),
        ):
            context.register_web_api(
                f"/{PLUGIN_NAME}/{path}", handler, [method], "DeepSeek TTS"
            )

    def settings(self):
        return Settings.read(self.config)

    def _revision(self):
        data = json.dumps(dict(self.config), sort_keys=True, ensure_ascii=True)
        return hashlib.sha256(data.encode()).hexdigest()

    def _allowed(self, event, settings):
        return event.get_platform_name() == "aiocqhttp" and settings.allows(
            event.get_group_id()
        )

    def _state(self):
        return {
            "settings": self.settings().public(),
            "revision": self._revision(),
            "voices": VOICES,
            "preview_text": PREVIEW_TEXT,
            "runtime": dict(self.speech.status),
            "version": "0.1.0",
        }

    async def _record(self, settings, text, scope):
        audio = await self.speech.generate(settings, text, scope)
        path = await asyncio.to_thread(self.audio_store.save, audio)
        return Comp.Record(file=str(path), url=str(path), text=text), audio

    async def _text_fallback(self, event, text, reason):
        try:
            await event.send(MessageChain([Comp.Plain(text)]))
            return {
                "ok": True,
                "delivery": "text",
                "reason": reason,
                "instruction": "已发送文字回退，未发送语音。不要重复发送，也不要声称语音成功。",
            }
        except Exception as exc:
            logger.warning("[DeepSeek TTS] 文字回退发送失败：%s", type(exc).__name__)
            return {"ok": False, "error": "语音与文字发送均未确认成功，请正常回复。"}

    async def _deliver(self, event, text):
        settings = self.settings()
        if not self._allowed(event, settings):
            return {
                "ok": False,
                "error": "当前会话未启用 DeepSeek TTS；本插件支持 OneBot v11。",
            }
        text = validate_text(text, settings.max_chars)
        if event.get_extra(HANDLED):
            return event.get_extra(TOOL_RESULT) or {
                "ok": False,
                "error": "本轮已经处理过语音请求，请勿重复发送。",
            }
        event.set_extra(HANDLED, True)
        try:
            record, audio = await self._record(settings, text, event.unified_msg_origin)
            await event.send(MessageChain([record]))
        except SpeechError as exc:
            result = await self._text_fallback(event, text, str(exc))
        except Exception as exc:
            logger.warning(
                "[DeepSeek TTS] 语音生成或发送未完成：%s", type(exc).__name__
            )
            result = await self._text_fallback(event, text, "语音生成或发送未完成。")
        else:
            caption_sent = False
            if settings.output_mode == "both":
                try:
                    await event.send(MessageChain([Comp.Plain(text)]))
                    caption_sent = True
                except Exception as exc:
                    logger.warning(
                        "[DeepSeek TTS] 语音已发送，附带文字发送失败：%s",
                        type(exc).__name__,
                    )
            result = {
                "ok": True,
                "delivery": "audio",
                "voice_id": settings.voice_id,
                "duration_seconds": round(audio.duration, 2),
                "caption_sent": caption_sent,
                "instruction": "语音已发送到当前会话，不要重复发送该内容。",
            }
        event.set_extra(TOOL_RESULT, result)
        return result

    @filter.llm_tool(name="deepseek_tts_speak")
    async def speak(self, event: AstrMessageEvent, text: str) -> str:
        """用当前配置的音色，将一段文字作为语音发送到当前会话。

        用户要求语音回复、朗读，或当前对话适合用语音表达时使用。只能发送到当前会话。
        text 是实际要说的话，不要包含工具说明、思考过程或动作旁白；遵守用户对语音的偏好。
        每轮最多发送一次。读取返回结果：delivery=audio 才表示语音已发送；text 表示文字回退。
        成功发送后不要再用其他发送工具或普通回复重复同一内容。

        Args:
            text(string): 要朗读的完整内容，应简短自然，默认最多500字，具体上限由管理员配置。
        """
        try:
            result = await self._deliver(event, text)
        except (SettingsError, SpeechError) as exc:
            result = {"ok": False, "error": str(exc)}
        return json.dumps(result, ensure_ascii=False)

    @filter.command("ds语音", alias={"dstts"})
    async def command_speak(self, event: AstrMessageEvent, text: GreedyStr):
        if not str(text).strip():
            yield event.plain_result("用法：/ds语音 想朗读的内容\n试听：/ds语音试听")
            return
        result = json.loads(await self.speak(event, str(text)))
        if not result["ok"]:
            yield event.plain_result(result["error"])

    @filter.command("ds语音试听")
    async def command_preview(self, event: AstrMessageEvent):
        result = json.loads(await self.speak(event, PREVIEW_TEXT))
        if not result["ok"]:
            yield event.plain_result(result["error"])

    @filter.on_decorating_result(priority=-10)
    async def auto_speak(self, event: AstrMessageEvent):
        try:
            settings = self.settings()
            result = event.get_result()
            if (
                not settings.auto_reply
                or not self._allowed(event, settings)
                or event.get_extra(HANDLED)
                or result is None
                or not result.chain
            ):
                return
            streaming = result.result_content_type == ResultContentType.STREAMING_FINISH
            if not result.is_llm_result() and not streaming:
                return
            if any(isinstance(item, Comp.Record) for item in result.chain):
                return
            # Avoid duplicate conversion by the core TTS stage that runs after this hook.
            core = self.context.get_config(umo=event.unified_msg_origin)
            if core.get("provider_tts_settings", {}).get("enable", False):
                return
            text = "\n".join(
                item.text for item in result.chain if isinstance(item, Comp.Plain)
            ).strip()
            if not text:
                return
            validate_text(text, settings.max_chars)
            event.set_extra(HANDLED, True)
            record, _ = await self._record(settings, text, event.unified_msg_origin)
            if streaming:
                # The text was already delivered; never replay or mutate that completed stream.
                await event.send(MessageChain([record]))
            else:
                if settings.output_mode == "voice":
                    result.chain = [
                        item
                        for item in result.chain
                        if not isinstance(item, Comp.Plain)
                    ]
                result.chain.append(record)
                result.use_t2i(False)
        except (SettingsError, SpeechError) as exc:
            logger.info("[DeepSeek TTS] 保留原文字回复：%s", str(exc))
        except Exception as exc:
            logger.warning(
                "[DeepSeek TTS] 自动语音失败，保留原回复：%s", type(exc).__name__
            )

    async def page_state(self):
        if not request.username:
            return error_response("请先登录 AstrBot 管理面板。", status_code=401)
        try:
            return json_response(self._state())
        except SettingsError as exc:
            return error_response(str(exc))

    async def page_save(self):
        if not request.username:
            return error_response("请先登录 AstrBot 管理面板。", status_code=401)
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求格式无效。")
        async with self._config_lock:
            if payload.get("revision") != self._revision():
                return error_response(
                    "配置已经变更，请刷新页面后重新修改。", status_code=409
                )
            try:
                changes = validate_patch(payload.get("changes"))
                clear_key = payload.get("clear_api_key", False)
                if type(clear_key) is not bool:
                    raise SettingsError("清除密钥选项无效。")
                if clear_key and changes.get("api_key"):
                    raise SettingsError("不能同时填写新密钥和清除密钥。")
                if not changes.get("api_key"):
                    changes.pop("api_key", None)
                if clear_key:
                    changes["api_key"] = ""
                before = dict(self.config)
                updated = {**before, **changes}
                Settings.read(updated)
            except SettingsError as exc:
                return error_response(str(exc))
            try:
                self.config.clear()
                self.config.update(updated)
                self.config.save_config()
            except Exception as exc:
                self.config.clear()
                self.config.update(before)
                logger.warning("[DeepSeek TTS] 保存配置失败：%s", type(exc).__name__)
                return error_response(
                    "保存失败，请检查配置文件写入权限后重试。", status_code=500
                )
            return json_response(self._state())

    async def page_preview(self):
        if not request.username:
            return error_response("请先登录 AstrBot 管理面板。", status_code=401)
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求格式无效。")
        try:
            settings = self.settings()
            voice = payload.get("voice_id", settings.voice_id)
            audio = await self.speech.generate(
                settings,
                payload.get("text", ""),
                f"preview:{request.username}",
                voice=voice,
                preview=True,
            )
            return json_response(
                {
                    "audio_base64": base64.b64encode(audio.data).decode("ascii"),
                    "content_type": "audio/wav",
                    "voice_id": voice,
                    "duration_seconds": round(audio.duration, 2),
                }
            )
        except (SettingsError, SpeechError) as exc:
            return error_response(str(exc))
        except Exception as exc:
            logger.warning("[DeepSeek TTS] 试听失败：%s", type(exc).__name__)
            return error_response("试听失败，请稍后重试。", status_code=502)

    async def terminate(self):
        await self.speech.close()
