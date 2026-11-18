"""Browser QA with a simulated AstrBot bridge; never uses a real API key."""

import argparse
import base64
import functools
import io
import json
import os
import sys
import threading
import wave
from dataclasses import asdict
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_deepseek_tts.settings import PREVIEW_TEXT, Settings

ROOT = Path(__file__).resolve().parents[1]


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def check_studio(page, sample, output):
    page.get_by_role("link", name="音色与翻唱 ↓").click()
    page.locator("#studio-credentials summary").click()
    page.locator("#studio-volcengine-key").fill("test-volc-key")
    page.locator("#studio-s1-key").fill("test-s1-key")
    page.locator("#studio-save-keys").click()
    page.get_by_text("平台密钥设置已保存。", exact=True).wait_for()
    assert page.locator("#studio-volcengine-key").input_value() == ""
    assert page.locator("#studio-s1-key").input_value() == ""

    page.locator("#studio-slot").fill("S_test")
    page.locator("#studio-name").fill("薄荷女声")
    page.locator("#studio-prompt").fill("清亮、语速放松的年轻女声")
    page.locator("#studio-design").click()
    page.get_by_text(
        "音色已保存，可直接用于翻唱。剩余设计次数：7。", exact=True
    ).wait_for()
    assert page.locator("#studio-voices").input_value() == "designed-one"
    page.locator("#studio-voice-audio").click()
    page.locator("#studio-voice-download").wait_for()
    assert (
        page.locator("#studio-voice-download").get_attribute("download")
        == "薄荷女声.wav"
    )
    page.locator("#studio-use-voice").click()
    assert page.locator("#studio-cover-voice").input_value() == "designed-one"
    assert page.locator("#studio-cover-voice option[value=tide]").count() == 1

    page.locator("#studio-import-details summary").click()
    page.locator("#studio-song-file").set_input_files(
        {"name": "original-name.wav", "mimeType": "audio/wav", "buffer": sample}
    )
    page.locator("#studio-song-name").fill("夜风 - 示例歌手")
    page.locator("#studio-import").click()
    page.get_by_text("已导入 夜风 - 示例歌手。", exact=True).wait_for()
    assert page.evaluate("window.testUpload") == "夜风 - 示例歌手.wav"
    page.locator("#studio-song-query").fill("不存在")
    assert page.locator("#studio-cover").is_disabled()
    page.locator("#studio-song-query").fill("夜风")
    assert page.locator("#studio-song").input_value() == "imported-one"
    page.locator("#studio-start").fill("12")
    page.locator("#studio-duration").fill("30")
    page.locator("#studio-cover").click()
    assert (
        page.evaluate("testCalls.some(({ endpoint }) => endpoint === 'studio/cover')")
        is False
    )
    page.locator("#studio-rights").check()
    page.locator("#studio-cover").click()
    page.get_by_text(
        "夜风 - 示例歌手 · 薄荷女声 · 所选片段 30 秒 · 已完成", exact=True
    ).wait_for()
    assert page.evaluate(
        "testCalls.find(({ endpoint }) => endpoint === 'studio/cover').body"
    ) == {
        "song_id": "imported-one",
        "voice_id": "designed-one",
        "start_seconds": 12,
        "duration_seconds": 30,
        "rights_confirmed": True,
    }
    page.locator("#studio-cover-audio").click()
    page.locator("#studio-cover-download").wait_for()
    page.wait_for_function(
        "document.getElementById('studio-cover-player').readyState >= 1"
    )
    with page.expect_download() as received:
        page.locator("#studio-cover-download").click()
    assert received.value.suggested_filename == "夜风 - 示例歌手 - 薄荷女声.mp3"
    assert page.locator("#save-state").inner_text() == "有未保存的修改"
    page.screenshot(path=str(output / "studio-desktop.png"), full_page=True)

    page.locator("#studio-remove-song").click()
    page.get_by_role("button", name="取消", exact=True).click()
    assert page.locator("#studio-song").input_value() == "imported-one"
    page.locator("#studio-remove-song").click()
    page.get_by_role("button", name="确认移除", exact=True).click()
    page.get_by_text("已移除本地素材。", exact=True).wait_for()
    assert page.locator("#studio-cover").is_disabled()


def run(output, browser_path):
    # Keep browser profiles and driver artifacts inside the task workspace.
    scratch = output.resolve() / "browser-temp"
    scratch.mkdir(parents=True, exist_ok=True)
    os.environ["TEMP"] = str(scratch)
    os.environ["TMP"] = str(scratch)
    os.environ["TMPDIR"] = str(scratch)
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        functools.partial(QuietHandler, directory=str(ROOT / "pages/settings")),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    wav = io.BytesIO()
    with wave.open(wav, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(24000)
        stream.writeframes(b"\0\0" * 2400)
    settings = asdict(Settings())
    settings.pop("api_key")
    settings.update(key_configured=True, key_source="config")
    state = {"settings": settings, "revision": "test-1", "preview_text": PREVIEW_TEXT}
    mp3 = (bytes.fromhex("fffb50c4") + bytes(204)) * 50
    bootstrap = (
        """
    window.testState = STATE;
    const studio = {
      credentials: { volcengine: 'none', s1: 'none' }, has_preset_key: true,
      designers: { volcengine: '火山引擎 · 豆包音色设计' },
      presets: { stella: '海星 · 女声', tide: '暗潮 · 男声' }, default_voice: 'stella',
      voices: [], songs: [], jobs: {}, ffmpeg_ready: true, reference_text: '欢迎来到音色工作台。',
    };
    window.testCalls = [];
    window.failPreview = false;
    window.failSave = false;
    window.AstrBotPluginPage = {
      ready: async () => ({ isDark: false }),
      apiGet: async (endpoint) => {
        if (endpoint === 'studio/state') return structuredClone(studio);
        if (endpoint === 'studio/voices/designed-one/audio') return { audio_base64: 'AUDIO', content_type: 'audio/wav' };
        if (endpoint === 'studio/cover/cover-one/audio') return { audio_base64: 'COVER_AUDIO', content_type: 'audio/mpeg' };
        if (endpoint !== 'state') throw new Error(`Unexpected GET ${endpoint}`);
        return structuredClone(window.testState);
      },
      upload: async (endpoint, file) => {
        if (endpoint !== 'studio/songs/import') throw new Error('Unexpected upload');
        window.testUpload = file.name;
        const song = { id: 'imported-one', title: file.name.slice(0, -4), suffix: '.wav', seconds: 160 };
        studio.songs.push(song);
        return song;
      },
      apiPost: async (endpoint, body) => {
        window.testCalls.push({ endpoint, body: structuredClone(body) });
        if (endpoint === 'studio/keys') {
          if (body.volcengine_voice_api_key) studio.credentials.volcengine = 'config';
          if (body.s1_cover_api_key) studio.credentials.s1 = 'config';
          return structuredClone(studio.credentials);
        }
        if (endpoint === 'studio/design') {
          const job = studio.jobs.design = { id: 'design-one', status: 'designing', error: '' };
          setTimeout(() => {
            studio.voices.push({ id: 'designed-one', name: body.name, provider: 'volcengine' });
            Object.assign(job, { status: 'done', voice_id: 'designed-one', remaining: 7 });
          }, 150);
          return structuredClone(job);
        }
        if (endpoint === 'studio/cover') {
          const job = studio.jobs.cover = { id: 'cover-one', status: 'running', error: '' };
          setTimeout(() => Object.assign(job, {
            status: 'done', title: studio.songs[0].title, voice_name: studio.voices[0].name,
            seconds: body.duration_seconds,
          }), 150);
          return structuredClone(job);
        }
        if (endpoint === 'studio/songs/imported-one/remove') {
          studio.songs = [];
          return { removed: true };
        }
        if (endpoint === 'preview') {
          if (window.failPreview) throw new Error('DeepSeek 服务繁忙，请稍后再试。');
          return { audio_base64: 'AUDIO', content_type: 'audio/wav', voice_id: body.voice_id, duration_seconds: .1 };
        }
        if (window.failSave) throw new Error('保存失败，请重试。');
        const changes = { ...body.changes }; delete changes.api_key;
        Object.assign(window.testState.settings, changes);
        if (body.clear_api_key) window.testState.settings.key_configured = false;
        if (body.changes.api_key) window.testState.settings.key_configured = true;
        window.testState.revision += 'x';
        return structuredClone(window.testState);
      }
    };
    """.replace("STATE", json.dumps(state))
        .replace("COVER_AUDIO", base64.b64encode(mp3).decode())
        .replace("AUDIO", base64.b64encode(wav.getvalue()).decode())
    )
    output.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, executable_path=browser_path)
            context = browser.new_context(
                viewport={"width": 1280, "height": 1100}, device_scale_factor=1
            )
            context.add_init_script(bootstrap)
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{server.server_port}/")
            page.get_by_text("已配置 · 可以使用", exact=True).wait_for()
            page.screenshot(path=str(output / "settings-desktop.png"), full_page=True)
            page.locator('input[name="voice"][value="tide"]').check()
            assert page.locator("#preview-voice").inner_text() == "暗潮"
            page.locator("#preview").click()
            page.locator("#audio-info").get_by_text("暗潮 · 0.1 秒").wait_for()
            assert page.evaluate("window.testCalls.at(-1).body.voice_id") == "tide"
            assert page.locator("#player").get_attribute("src").startswith("blob:")
            page.locator("#reply-mode").select_option("automatic")
            page.locator("#output-mode").select_option("both")
            page.locator("#api-key").fill("test-key-never-a-real-secret")
            page.locator("#save").click()
            page.get_by_text(
                "设置已保存，新的语音请求会立即使用这些设置。", exact=True
            ).wait_for()
            assert page.locator("#api-key").input_value() == ""
            assert page.evaluate("window.testState.settings.auto_reply") is True
            assert page.evaluate("window.testState.settings.output_mode") == "both"
            page.evaluate("window.failPreview = true")
            page.locator("#preview").click()
            page.get_by_text("DeepSeek 服务繁忙，请稍后再试。", exact=True).wait_for()
            assert page.locator("#preview").is_enabled()
            page.evaluate("window.failSave = true")
            page.locator("#output-mode").select_option("voice")
            page.locator("#save").click()
            page.get_by_text("保存失败，请重试。", exact=True).wait_for()
            assert page.locator("#output-mode").input_value() == "voice"
            assert page.locator("#save-state").inner_text() == "有未保存的修改"
            check_studio(page, wav.getvalue(), output)
            page.evaluate(
                "document.getElementById('notice').hidden = true; document.documentElement.dataset.theme = 'dark'"
            )
            page.screenshot(path=str(output / "settings-dark.png"), full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(path=str(output / "settings-mobile.png"), full_page=True)
            assert not errors, errors
            offline = browser.new_page()
            offline.goto(f"http://127.0.0.1:{server.server_port}/")
            offline.get_by_text("连接未完成", exact=True).wait_for()
            assert offline.locator("#save").is_disabled()
            assert offline.locator("#studio-cover").is_disabled()
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
    print(
        json.dumps(
            {
                "result": "passed",
                "checks": [
                    "voice selection",
                    "preview playback source",
                    "save and secret clearing",
                    "error recovery",
                    "studio credential isolation and voice design",
                    "song import, title search and confirmed removal",
                    "cover settings, consent and MP3 playback/download",
                    "desktop",
                    "dark",
                    "mobile no overflow",
                    "missing bridge",
                ],
                "screenshots": str(output.resolve()),
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/ui")
    parser.add_argument(
        "--browser", default=r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    )
    args = parser.parse_args()
    run(args.output, args.browser)
