import ast
import base64
import inspect
import json
import types
from pathlib import Path
from typing import Any

import docstring_parser
import pytest
from astrbot_plugin_deepseek_tts.settings import PLUGIN_NAME
from astrbot_plugin_deepseek_tts.speech import SpeechError
from conftest import Chain, Event, Kind, Plain, Record


async def test_tool_sends_voice_and_prevents_duplicate_calls(plugin):
    event = Event()
    result = json.loads(await plugin.instance.speak(event, "可可子的声音"))
    assert result["delivery"] == "audio"
    assert len(event.sent) == 1 and isinstance(event.sent[0].chain[0], Record)
    assert Path(event.sent[0].chain[0].file).is_file()
    await plugin.instance.speak(event, "再次发送")
    assert len(plugin.client.calls) == 1 and len(event.sent) == 1
    await plugin.instance.terminate()
    assert plugin.client.closed


async def test_both_mode_sends_separate_audio_and_text(plugin):
    plugin.config["output_mode"] = "both"
    event = Event()
    result = json.loads(await plugin.instance.speak(event, "你好"))
    assert result["caption_sent"] is True
    assert [type(chain.chain[0]) for chain in event.sent] == [Record, Plain]


@pytest.mark.parametrize("failure", ["synthesis", "send"])
async def test_tool_failure_falls_back_to_text_and_does_not_claim_audio(
    plugin, failure
):
    event = Event()
    if failure == "synthesis":
        plugin.client.failure = SpeechError("network", "网络失败")
    else:
        event.fail_voice = True
    result = json.loads(await plugin.instance.speak(event, "保留这句话"))
    assert result["delivery"] == "text"
    assert event.sent[0].chain == [Plain("保留这句话")]
    assert "未发送语音" in result["instruction"]


async def test_text_failure_after_voice_does_not_send_voice_twice(plugin):
    plugin.config["output_mode"] = "both"
    event = Event()
    event.fail_text = True
    result = json.loads(await plugin.instance.speak(event, "你好"))
    assert result["delivery"] == "audio" and result["caption_sent"] is False
    assert len(event.sent) == 1


async def test_disabled_or_denied_group_never_calls_api(plugin):
    plugin.config["enabled"] = False
    assert json.loads(await plugin.instance.speak(Event(), "你好"))["ok"] is False
    plugin.config.update(enabled=True, allowed_group_ids=["999"], allow_private=False)
    for group in ("123", ""):
        assert (
            json.loads(await plugin.instance.speak(Event(group=group), "你好"))["ok"]
            is False
        )
    assert not plugin.client.calls


@pytest.mark.parametrize("mode", ["voice", "both"])
async def test_auto_reply_preserves_other_components(plugin, mode):
    plugin.config.update(auto_reply=True, output_mode=mode)
    image = object()
    result = Chain([Plain("你好"), image], Kind.LLM_RESULT)
    event = Event(result)
    await plugin.instance.auto_speak(event)
    assert image in result.chain and isinstance(result.chain[-1], Record)
    assert any(isinstance(c, Plain) for c in result.chain) == (mode == "both")
    assert result.use_t2i_ is False
    await plugin.instance.auto_speak(event)
    assert len(plugin.client.calls) == 1


async def test_auto_failure_preserves_original_text_and_images(plugin):
    plugin.config["auto_reply"] = True
    plugin.client.failure = SpeechError("network", "连接失败")
    original = [Plain("你好"), object()]
    event = Event(Chain(original.copy(), Kind.LLM_RESULT))
    await plugin.instance.auto_speak(event)
    assert event.result.chain == original


@pytest.mark.parametrize(
    "kind", [Kind.GENERAL_RESULT, Kind.STREAMING_RESULT, Kind.AGENT_RUNNER_ERROR]
)
async def test_auto_does_not_read_commands_partial_stream_or_errors(plugin, kind):
    plugin.config["auto_reply"] = True
    await plugin.instance.auto_speak(Event(Chain([Plain("不应该朗读")], kind)))
    assert not plugin.client.calls


async def test_stream_finish_sends_only_audio_once(plugin):
    plugin.config["auto_reply"] = True
    result = Chain([Plain("已经发出的文字")], Kind.STREAMING_FINISH)
    event = Event(result)
    await plugin.instance.auto_speak(event)
    assert result.chain == [Plain("已经发出的文字")]
    assert len(event.sent) == 1 and isinstance(event.sent[0].chain[0], Record)
    await plugin.instance.auto_speak(event)
    assert len(event.sent) == 1


async def test_builtin_tts_or_existing_voice_does_not_double_generate(plugin):
    plugin.config["auto_reply"] = True
    plugin.instance.context.core_tts = True
    await plugin.instance.auto_speak(Event(Chain([Plain("你好")], Kind.LLM_RESULT)))
    plugin.instance.context.core_tts = False
    await plugin.instance.auto_speak(
        Event(Chain([Plain("你好"), Record("x", "x")], Kind.LLM_RESULT))
    )
    assert not plugin.client.calls


async def test_auto_long_reply_falls_back_without_truncating(plugin):
    plugin.config.update(auto_reply=True, max_chars=3)
    event = Event(Chain([Plain("这句话很长")], Kind.LLM_RESULT))
    await plugin.instance.auto_speak(event)
    assert event.result.chain == [Plain("这句话很长")]
    assert not plugin.client.calls


async def test_page_requires_dashboard_identity(plugin):
    plugin.request.username = None
    for handler in (
        plugin.instance.page_state,
        plugin.instance.page_save,
        plugin.instance.page_preview,
    ):
        assert (await handler())["status"] == 401
    assert not plugin.client.calls


async def test_page_masks_key_preserves_blank_and_updates_voice(plugin):
    state = await plugin.instance.page_state()
    assert plugin.config["api_key"] not in json.dumps(state)
    plugin.request.payload = {
        "revision": state["revision"],
        "changes": {"api_key": "", "voice_id": "tide"},
    }
    saved = await plugin.instance.page_save()
    assert saved["settings"]["voice_id"] == "tide"
    assert plugin.config.saved["api_key"] == "test-not-a-real-key"
    assert saved["revision"] != state["revision"]
    # Stale tabs must not overwrite a newer voice or key.
    assert (await plugin.instance.page_save())["status"] == 409
    plugin.request.payload = {
        "revision": saved["revision"],
        "changes": {},
        "clear_api_key": True,
    }
    cleared = await plugin.instance.page_save()
    assert (
        cleared["settings"]["key_configured"] is False
        and plugin.config["api_key"] == ""
    )


async def test_page_validation_and_failed_save_rollback(plugin):
    state = await plugin.instance.page_state()
    before = dict(plugin.config)
    for changes in (
        {"api_key": "secret\nheader"},
        {"voice_id": "bad"},
        {"enabled": "false"},
    ):
        plugin.request.payload = {"revision": state["revision"], "changes": changes}
        assert (await plugin.instance.page_save())["status"] == 400
        assert dict(plugin.config) == before
    plugin.config.fail_save = True
    plugin.request.payload = {
        "revision": state["revision"],
        "changes": {"voice_id": "tide"},
    }
    assert (await plugin.instance.page_save())["status"] == 500
    assert dict(plugin.config) == before


async def test_preview_uses_selected_voice_without_saving_or_returning_secrets(plugin):
    plugin.config["enabled"] = False  # Preview works before enabling replies.
    plugin.request.payload = {"text": "试听", "voice_id": "tide"}
    result = await plugin.instance.page_preview()
    assert (
        result["voice_id"] == "tide"
        and plugin.config.get("voice_id", "stella") == "stella"
    )
    assert base64.b64decode(result["audio_base64"])[:4] == b"RIFF"
    assert plugin.config["api_key"] not in json.dumps(result)
    plugin.request.payload = {"text": "试听", "voice_id": "not-a-voice"}
    assert (await plugin.instance.page_preview())["status"] == 400
    assert len(plugin.client.calls) == 1


async def test_manual_command_consumes_complete_text_and_empty_shows_help(plugin):
    event = Event()
    assert [
        result
        async for result in plugin.instance.command_speak(event, "hello 可可子 world")
    ] == []
    assert plugin.client.calls[0][1] == "hello 可可子 world"
    help_results = [
        result async for result in plugin.instance.command_speak(Event(), "")
    ]
    assert "用法" in help_results[0].chain[0].text


def test_tool_docstring_has_the_required_astrbot_schema(plugin):
    schema = docstring_parser.parse(plugin.instance.speak.__doc__)
    assert [(arg.arg_name, arg.type_name) for arg in schema.params] == [
        ("text", "string")
    ]


def test_real_astrbot_4294_greedy_parser_contract(plugin):
    source = Path(__file__).parents[1] / ".devtools/astrbot-command-v4.29.4.py"
    if not source.is_file():
        pytest.skip(
            "Optional upstream source snapshot is not included in release archives"
        )
    tree = ast.parse(source.read_text(encoding="utf-8"))
    cls = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "CommandFilter"
    )
    methods = [
        node
        for node in cls.body
        if isinstance(node, ast.FunctionDef)
        and node.name in {"init_handler_md", "validate_and_convert_params"}
    ]
    for method in methods:
        for arg in method.args.args:
            arg.annotation = None
        method.returns = None
    namespace = {"inspect": inspect, "Any": Any, "GreedyStr": plugin.main.GreedyStr}
    exec(
        compile(ast.Module(body=methods, type_ignores=[]), str(source), "exec"),
        namespace,
    )
    holder = types.SimpleNamespace()
    namespace["init_handler_md"](
        holder, types.SimpleNamespace(handler=type(plugin.instance).command_speak)
    )
    assert holder.handler_params["text"] is plugin.main.GreedyStr
    assert namespace["validate_and_convert_params"](
        holder, ["hello", "可可子", "world"], holder.handler_params
    ) == {"text": "hello 可可子 world"}


def test_manifest_and_registered_routes_match_name(plugin):
    import yaml

    root = Path(__file__).parents[1]
    metadata = yaml.safe_load((root / "metadata.yaml").read_text(encoding="utf-8"))
    assert metadata["display_name"] == "DeepSeek TTS"
    assert metadata["name"] == root.name == PLUGIN_NAME
    assert all(
        route[0].startswith(f"/{PLUGIN_NAME}/")
        for route in plugin.instance.context.routes
    )
