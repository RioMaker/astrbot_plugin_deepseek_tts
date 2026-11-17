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
    bootstrap = """
    window.testState = STATE;
    window.testCalls = [];
    window.failPreview = false;
    window.failSave = false;
    window.AstrBotPluginPage = {
      ready: async () => ({ isDark: false }),
      apiGet: async () => structuredClone(window.testState),
      apiPost: async (endpoint, body) => {
        window.testCalls.push({ endpoint, body: structuredClone(body) });
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
    """.replace("STATE", json.dumps(state)).replace(
        "AUDIO", base64.b64encode(wav.getvalue()).decode()
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
