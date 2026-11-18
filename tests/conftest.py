"""Small AstrBot boundary doubles; transport tests use a real HTTP server."""

import importlib
import io
import logging
import sys
import types
import wave
from dataclasses import dataclass
from enum import Enum

import pytest
from astrbot_plugin_deepseek_tts.settings import Settings
from astrbot_plugin_deepseek_tts.speech import Audio


def wav_bytes(seconds=0.1):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24000)
        output.writeframes(b"\x10\x00" * int(24000 * seconds))
    return buffer.getvalue()


class Kind(Enum):
    LLM_RESULT = 1
    GENERAL_RESULT = 2
    STREAMING_RESULT = 3
    STREAMING_FINISH = 4
    AGENT_RUNNER_ERROR = 5


@dataclass
class Plain:
    text: str


@dataclass
class Record:
    file: str
    url: str
    text: str = ""


class Chain:
    def __init__(self, chain=None, kind=Kind.GENERAL_RESULT):
        self.chain = chain or []
        self.result_content_type = kind
        self.use_t2i_ = None

    def is_llm_result(self):
        return self.result_content_type == Kind.LLM_RESULT

    def use_t2i(self, value):
        self.use_t2i_ = value


class Event:
    unified_msg_origin = "test:GroupMessage:123"

    def __init__(self, result=None, group="123"):
        self.result = result
        self.group = group
        self.sent = []
        self.extras = {}
        self.fail_voice = False
        self.fail_text = False

    def get_platform_name(self):
        return "aiocqhttp"

    def get_group_id(self):
        return self.group

    def get_result(self):
        return self.result

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def set_extra(self, key, value):
        self.extras[key] = value

    def plain_result(self, text):
        return Chain([Plain(text)])

    async def send(self, chain):
        if self.fail_voice and any(isinstance(c, Record) for c in chain.chain):
            raise RuntimeError("delivery failed")
        if self.fail_text and any(isinstance(c, Plain) for c in chain.chain):
            raise RuntimeError("delivery failed")
        self.sent.append(chain)


class Config(dict):
    fail_save = False

    def save_config(self):
        if self.fail_save:
            raise OSError("write failed")
        self.saved = dict(self)


class Context:
    def __init__(self):
        self.routes = []
        self.core_tts = False

    def register_web_api(self, *args):
        self.routes.append(args)

    def get_config(self, **kwargs):
        return {"provider_tts_settings": {"enable": self.core_tts}}


class Request:
    username = "admin"
    payload = None
    uploads = None

    async def json(self, default=None):
        return self.payload

    async def files(self):
        return self.uploads or {}


class Client:
    def __init__(self):
        self.calls = []
        self.failure = None
        self.closed = False

    async def synthesize(self, key, text, voice, settings, max_bytes):
        self.calls.append((key, text, voice))
        if self.failure:
            raise self.failure
        return Audio(wav_bytes(), 0.1, 24000)

    async def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def no_external_keys(monkeypatch):
    for name in ("DEEPSEEK_API_KEY", "VOLCENGINE_VOICE_API_KEY", "S1_COVER_API_KEY"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    modules = {}

    def module(name, **values):
        value = types.ModuleType(name)
        value.__dict__.update(values)
        value.__path__ = []
        modules[name] = value
        monkeypatch.setitem(sys.modules, name, value)
        if "." in name:
            parent, attr = name.rsplit(".", 1)
            if parent in modules:
                setattr(modules[parent], attr, value)
        return value

    class Star:
        def __init__(self, context):
            self.context = context

    class GreedyStr(str):
        pass

    def decorator(*args, **kwargs):
        return lambda fn: fn

    req = Request()
    module("astrbot")
    module("astrbot.api", AstrBotConfig=Config, logger=logging.getLogger("tts-test"))
    module("astrbot.api.message_components", Plain=Plain, Record=Record)
    module(
        "astrbot.api.event",
        AstrMessageEvent=Event,
        MessageChain=Chain,
        filter=types.SimpleNamespace(
            command=decorator, llm_tool=decorator, on_decorating_result=decorator
        ),
    )
    module("astrbot.api.star", Context=Context, Star=Star)
    module(
        "astrbot.api.web",
        request=req,
        json_response=lambda data: data,
        error_response=lambda message, status_code=400: {
            "error": message,
            "status": status_code,
        },
    )
    for name in (
        "astrbot.core",
        "astrbot.core.message",
        "astrbot.core.star",
        "astrbot.core.star.filter",
        "astrbot.core.utils",
    ):
        module(name)
    module("astrbot.core.message.message_event_result", ResultContentType=Kind)
    module("astrbot.core.star.filter.command", GreedyStr=GreedyStr)
    module(
        "astrbot.core.utils.astrbot_path", get_astrbot_data_path=lambda: str(tmp_path)
    )
    monkeypatch.delitem(sys.modules, "astrbot_plugin_deepseek_tts.main", raising=False)
    monkeypatch.delitem(
        sys.modules, "astrbot_plugin_deepseek_tts.studio_web", raising=False
    )
    main = importlib.import_module("astrbot_plugin_deepseek_tts.main")
    config = Config(api_key="test-not-a-real-key", cooldown_seconds=0)
    instance = main.DeepSeekTTSPlugin(Context(), config)
    instance.speech.client = Client()
    return types.SimpleNamespace(
        instance=instance,
        main=main,
        config=config,
        request=req,
        settings=Settings,
        client=instance.speech.client,
    )
