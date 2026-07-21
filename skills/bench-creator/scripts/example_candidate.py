#!/usr/bin/env python3
"""Minimal ai-work-bench candidate adapter used for protocol smoke tests."""

import json
import sys


def main() -> int:
    request = json.load(sys.stdin)
    task = request.get("task", {})
    result = {
        "protocol": "ai-work-bench/result-v1",
        "text": task.get("prompt", ""),
        "data": {"context": task.get("context", {})},
        "artifacts": [],
        "metadata": {"candidate": "example-echo"},
    }
    json.dump(result, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
