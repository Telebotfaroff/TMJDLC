from __future__ import annotations

import argparse
import json
from pathlib import Path

from tmjdlc.providers.javtiful import JavtifulStreamDetector, StreamDetectionError


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Resolve a Javtiful playback stream using the reusable Playwright provider."
    )
    parser.add_argument("--url", required=True)
    parser.add_argument("--quality", type=int, default=None, help="Preferred quality, e.g. 720")
    parser.add_argument("--timeout", type=int, default=45)
    parser.add_argument("--capture-seconds", type=int, default=20)
    parser.add_argument("--output", default="artifacts/javtiful-resolution")
    args = parser.parse_args()

    detector = JavtifulStreamDetector(
        timeout_ms=args.timeout * 1000,
        capture_ms=args.capture_seconds * 1000,
    )
    try:
        result = detector.detect(args.url, preferred_quality=args.quality)
    except (StreamDetectionError, ValueError) as exc:
        print(json.dumps({"success": False, "error": str(exc)}, indent=2))
        return 2

    report = {"success": True, **result.to_public_dict()}
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    (output / "javtiful-stream.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"Safe report written to {output / 'javtiful-stream.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
