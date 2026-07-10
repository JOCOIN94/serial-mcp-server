"""환경변수 기반 서버 설정 파싱."""

from __future__ import annotations

import math
from typing import Mapping, Optional

from .diagnostics import log as _log
from .ports import parse_autoname, parse_names, parse_port_list


def _env_int(env: Mapping[str, str], name: str, default: int) -> int:
    """정수 환경변수 파싱 — 미설정/빈값/오류 시 기본값. env 주입형(테스트 용이)."""
    v = env.get(name)
    if v is None or v.strip() == "":
        return default
    try:
        return int(v)
    except ValueError:
        _log(f"환경변수 {name}={v!r} 정수 변환 실패 → 기본값 {default} 사용")
        return default


def _parse_dedup(env: Mapping[str, str]) -> int:
    """SERIAL_DEDUP 파싱 — 룩백 윈도. 기본 5, 0/false=끔, 1/true=직전 줄만(구버전)."""
    raw = env.get("SERIAL_DEDUP", "").strip().lower()
    if raw == "":
        return 5
    if raw in ("0", "false", "no", "off"):
        return 0
    if raw in ("1", "true", "yes", "on"):
        return 1
    try:
        n = int(raw)
        if n >= 0:
            return n
    except ValueError:
        pass
    _log(f"환경변수 SERIAL_DEDUP={raw!r} 해석 실패 → 기본 룩백 5 사용")
    return 5


def _parse_maxlen(env: Mapping[str, str]) -> int:
    """SERIAL_BUFFER_LINES 파싱 — 0 이하면 기본 2000(deque(maxlen<0)은 기동 실패)."""
    n = _env_int(env, "SERIAL_BUFFER_LINES", 2000)
    if n <= 0:
        _log(f"환경변수 SERIAL_BUFFER_LINES={n} 은 1 이상이어야 함 → 기본 2000 사용")
        return 2000
    return n


def _parse_web(env: Mapping[str, str]) -> tuple[int, bool]:
    """SERIAL_WEB 파싱 — 기본 8743(켜짐). 0/false/no/off → 8743 잠금만, UI 미서빙.

    1~65535 밖이면 기본값으로 — 범위 밖 포트는 socket.bind에서 OverflowError로
    서버 기동 자체를 죽일 수 있다(뷰어 실패는 본체에 영향 없어야 한다는 불변식).
    """
    raw = env.get("SERIAL_WEB", "").strip()
    if raw == "":
        return 8743, True
    if raw.lower() in ("0", "false", "no", "off"):
        return 8743, False
    try:
        n = int(raw)
        if 1 <= n <= 65535:
            return n, True
    except ValueError:
        pass
    _log(f"환경변수 SERIAL_WEB={raw!r} 해석 실패(1~65535 필요) → 기본 포트 8743 사용")
    return 8743, True


def _parse_hotplug(env: Mapping[str, str]) -> Optional[float]:
    """SERIAL_HOTPLUG 파싱 — 핫플러그 스캔 간격(초). 기본 5(켜짐).

    0(0.0 포함)/false/no/off → 끔(None), 유한 양수(소수 허용) → 간격. 자동 스캔
    모드에서만 의미가 있다(SERIAL_PORT 고정 목록 모드는 스캔 스레드를 띄우지 않음).
    """
    raw = env.get("SERIAL_HOTPLUG", "").strip().lower()
    if raw == "":
        return 5.0
    if raw in ("false", "no", "off"):
        return None
    try:
        n = float(raw)
        if n == 0:
            return None
        if n > 0 and math.isfinite(n):
            return n
    except ValueError:
        pass
    _log(f"환경변수 SERIAL_HOTPLUG={raw!r} 해석 실패(양수 초 필요) → 기본 5초 사용")
    return 5.0


def _parse_char_delay(env: Mapping[str, str]) -> float:
    """SERIAL_CHAR_DELAY 파싱 — 전송 문자 간 지연(ms). 기본 10(켜짐), 반환은 초.

    폴링 수신 펌웨어(SB-STM32 등)가 기계 속도 연속 바이트를 흘리는 문자 유실 대응
    (실측 근거 2026-06-12: 'HELP' 송신 → 보드 수신 'HLP'). 0/false/no/off → 끔(0.0),
    유한 양수(소수 허용, 상한 100ms) → 지연. 해석 실패는 기본값.
    """
    raw = env.get("SERIAL_CHAR_DELAY", "").strip().lower()
    if raw == "":
        return 0.010
    if raw in ("false", "no", "off"):
        return 0.0
    try:
        n = float(raw)
        if n == 0:
            return 0.0
        if n > 0 and math.isfinite(n):
            return min(n, 100.0) / 1000.0
    except ValueError:
        pass
    _log(f"환경변수 SERIAL_CHAR_DELAY={raw!r} 해석 실패 → 기본 10ms 사용")
    return 0.010


def _parse_flag(env: Mapping[str, str], name: str, default: bool = True) -> bool:
    """불리언 환경변수 파싱 — 미설정/빈값은 기본값, 해석 실패도 _log 후 기본값."""
    raw = env.get(name, "").strip().lower()
    if raw == "":
        return default
    if raw in ("0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    _log(f"환경변수 {name}={raw!r} 해석 실패 → 기본값 {default} 사용")
    return default


def _parse_confirm_mode(env: Mapping[str, str], name: str) -> str:
    """SERIAL_WRITE_CONFIRM 3-state 파싱 → "all" | "r3" | "off"."""
    raw = env.get(name, "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return "off"
    if raw in ("r3", "risky", "risk"):
        return "r3"
    if raw not in ("", "1", "true", "yes", "on"):
        _log(f"환경변수 {name}={raw!r} 해석 실패 → 기본값 'all' 사용")
    return "all"


def _load_config(env: Mapping[str, str]) -> dict:
    """환경변수 매핑에서 서버 설정을 파싱해 dict로 반환."""
    web_port, web_ui = _parse_web(env)
    return {
        "ports": parse_port_list(env.get("SERIAL_PORT", "")),
        "names": parse_names(env.get("SERIAL_NAMES", "")),
        "autoname": parse_autoname(env.get("SERIAL_AUTONAME", "")),
        "baud": _env_int(env, "SERIAL_BAUD", 115200),
        "tee": env.get("SERIAL_TEE", "").strip() or None,
        "exclude": env.get("SERIAL_EXCLUDE", "").strip() or None,
        "include": env.get("SERIAL_INCLUDE", "").strip() or None,
        "maxlen": _parse_maxlen(env),
        "dedup": _parse_dedup(env),
        "web": web_port,
        "web_ui": web_ui,
        "hotplug": _parse_hotplug(env),
        "write": _parse_flag(env, "SERIAL_WRITE"),
        "write_confirm": _parse_confirm_mode(env, "SERIAL_WRITE_CONFIRM"),
        "char_delay": _parse_char_delay(env),
        "topology_bootstrap": _parse_flag(env, "SERIAL_TOPOLOGY_BOOTSTRAP", default=False),
    }
