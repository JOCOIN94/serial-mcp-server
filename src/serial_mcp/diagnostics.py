"""MCP stdout을 오염시키지 않는 공용 진단 로그 경계."""

from __future__ import annotations

import sys


def log(msg: str) -> None:
    """진단 메시지를 반드시 stderr로만 기록한다."""
    print(f"[serial-mcp] {msg}", file=sys.stderr, flush=True)
