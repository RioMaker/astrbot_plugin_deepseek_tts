"""Explicit, billed live test. Reads DEEPSEEK_API_KEY only from the environment."""

import argparse
import asyncio
import hashlib
import json
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_deepseek_tts.settings import Settings
from astrbot_plugin_deepseek_tts.speech import SpeechError, SpeechService


async def run(args):
    settings = Settings(cooldown_seconds=0)
    if not settings.key():
        raise SystemExit("Missing DEEPSEEK_API_KEY; no request sent.")
    # Verify the output location before making any billed request.
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile(dir=args.output):
        pass
    service = SpeechService()
    results = []
    try:
        for voice in ("stella", "tide") if args.voice == "both" else (args.voice,):
            start = time.perf_counter()
            audio = await service.generate(settings, args.text, "smoke", voice=voice)
            args.output.mkdir(parents=True, exist_ok=True)
            path = args.output / f"deepseek-{voice}.wav"
            path.write_bytes(audio.data)
            results.append(
                {
                    "voice_id": voice,
                    "result": "success",
                    "status": 200,
                    "duration_seconds": round(audio.duration, 3),
                    "elapsed_seconds": round(time.perf_counter() - start, 3),
                    "sample_rate": audio.sample_rate,
                    "bytes": len(audio.data),
                    "sha256": hashlib.sha256(audio.data).hexdigest(),
                    "file": str(path.resolve()),
                }
            )
    except SpeechError as exc:
        results.append({"result": "failed", "code": exc.code, "message": str(exc)})
    finally:
        await service.close()
    report = {
        "tested_at_utc": datetime.now(timezone.utc).isoformat(),
        "results": results,
    }
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "live-result.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=True))
    return int(any(result["result"] != "success" for result in results))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--voice", choices=("stella", "tide", "both"), default="stella")
    parser.add_argument(
        "--text", default="你好，我是可可子。很高兴见到你，今天也一起开心地聊天吧！"
    )
    parser.add_argument("--output", type=Path, default=Path("artifacts/live"))
    raise SystemExit(asyncio.run(run(parser.parse_args())))
