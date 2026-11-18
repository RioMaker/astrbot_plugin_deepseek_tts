"""Validated settings shared by the schema, dashboard and runtime."""

import os
import re
from dataclasses import asdict, dataclass, field

PLUGIN_NAME = "astrbot_plugin_deepseek_tts"
VERSION = "0.2.0"
VOICES = {"stella": "海星 · 女声", "tide": "暗潮 · 男声"}
PREVIEW_TEXT = "嗨，我是可可子！很高兴见到你。今天有什么想和我分享的吗？"


class SettingsError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    enabled: bool = True
    api_key: str = field(default="", repr=False)
    voice_id: str = "stella"
    auto_reply: bool = False
    output_mode: str = "voice"
    max_chars: int = 500
    timeout_seconds: int = 60
    cooldown_seconds: int = 3
    filter_markdown: bool = True
    allowed_group_ids: list[str] = field(default_factory=list)
    allow_private: bool = True

    @classmethod
    def read(cls, config):
        defaults = asdict(cls())
        values = {key: config.get(key, value) for key, value in defaults.items()}
        return cls(**validate_patch(values))

    def key(self):
        return self.api_key or os.environ.get("DEEPSEEK_API_KEY", "").strip()

    def public(self):
        values = asdict(self)
        values.pop("api_key")
        values["key_configured"] = bool(self.key())
        values["key_source"] = (
            "config" if self.api_key else "environment" if self.key() else "none"
        )
        return values

    def allows(self, group_id):
        if not self.enabled:
            return False
        if not group_id:
            return self.allow_private
        return not self.allowed_group_ids or str(group_id) in self.allowed_group_ids


def validate_patch(values):
    if not isinstance(values, dict):
        raise SettingsError("配置必须是 JSON 对象。")
    allowed = asdict(Settings())
    if set(values) - set(allowed):
        raise SettingsError("配置包含未知字段，请刷新页面后重试。")
    result = dict(values)
    for key in ("enabled", "auto_reply", "filter_markdown", "allow_private"):
        if key in result and type(result[key]) is not bool:
            raise SettingsError(f"{key} 必须为开关值。")
    for key, minimum, maximum in (
        ("max_chars", 1, 2000),
        ("timeout_seconds", 10, 180),
        ("cooldown_seconds", 0, 300),
    ):
        if key in result and (
            type(result[key]) is not int or not minimum <= result[key] <= maximum
        ):
            raise SettingsError(f"{key} 必须是 {minimum}～{maximum} 之间的整数。")
    for key, options in (("voice_id", VOICES), ("output_mode", ("voice", "both"))):
        if key in result and (
            not isinstance(result[key], str) or result[key] not in options
        ):
            raise SettingsError(f"{key} 的选项无效。")
    if "api_key" in result:
        key = result["api_key"]
        if not isinstance(key, str) or len(key) > 512:
            raise SettingsError("API Key 格式无效。")
        key = key.strip()
        if key and not re.fullmatch(r"[\x21-\x7e]+", key):
            raise SettingsError("API Key 不能包含空白或非英文字符。")
        result["api_key"] = key
    if "allowed_group_ids" in result:
        groups = result["allowed_group_ids"]
        if not isinstance(groups, list) or len(groups) > 100:
            raise SettingsError("群号列表最多填写 100 项。")
        if any(
            not isinstance(g, str) or not re.fullmatch(r"[0-9]{1,30}", g)
            for g in groups
        ):
            raise SettingsError("群号必须是数字字符串。")
        result["allowed_group_ids"] = list(dict.fromkeys(groups))
    return result
