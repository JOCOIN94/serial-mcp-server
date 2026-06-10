# 다중 포트 자동 모니터링 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** USB 시리얼 포트를 시작 시 자동 인식해 **있는 만큼 전부**(N개) 동시 모니터링하고, 별칭(`SSM (COM4)`) 표기·도구 `port` 라우팅·뷰어 포트 셀렉터·dedup 룩백(기본 5)을 구현한다.

**Architecture:** 접근안 A — "리더+버퍼+피드" 묶음을 `PortMonitor`로 일반화해 서버가 `_monitors: dict[str, PortMonitor]`를 N개 보유(단일도 N=1 동일 경로). 순수 로직(스캔 필터·지정 파싱·별칭)은 새 모듈 `ports.py`로 분리. 기존 클래스(SerialReader/LineBuffer/RawFeed)는 무수정 재사용 — 새 로직은 스캔·dict 관리·port 라우팅뿐. 설계: `docs/superpowers/specs/2026-06-10-multi-port-design.md`.

**Tech Stack:** Python 3.10+ stdlib / pytest / 기존 의존성만(`mcp[cli]`+`pyserial`). 이 PC의 `python`은 Store 별칭 — **`uv`/`py`만 사용**(CLAUDE.md).

**현재 상태(시작점):** 88 passed. `server.py`는 단일 `_buffer`/`_reader`/`_feed` 전역 + 6개 도구 + `_viewer_*` 콜백 + `_load_config` 구조. `web_viewer.py`는 단일 feed 주입.

**병렬 실행(ultracode Workflow) 의존성 그래프:**
- Wave 0: **Task 1**(ring_buffer 룩백) ∥ **Task 2**(ports.py 신규) — 서로 다른 파일, 완전 병렬.
- Wave 1: **Task 3**(server.py 전면 개편 — Task 1·2에 의존) ∥ **Task 4**(web_viewer 셀렉터 — viewer_feed에만 의존, server.py 무관).
- Wave 2: **Task 5**(문서 + 전체 검증).
- `server.py`는 Task 3 단독 소유. `web_viewer.py`는 Task 4 단독 소유. 서브에이전트에는 **이 계획서 전체를 맥락으로 전달**한다(대화 맥락 비상속).

**주의(전역 규칙):**
- 기존 테스트 다수가 의도적으로 **개정**된다(계약 변경: dedup int·config 키·도구 시그니처). 본 계획의 "수정" 블록은 특성화 갱신이며 트리아지 대상이 아니다. 계획에 없는 실패가 나면 추측 금지 — 설계문서와 대조해 판별.
- git 커밋 단계는 ultracode 실행 시 메인 세션이 일괄 수행(에이전트는 git 금지).

---

## File Structure

| 파일 | 책임 | 변경 |
|---|---|---|
| `src/serial_mcp/ring_buffer.py` | dedup 룩백 윈도(int) | 수정(Task 1) |
| `src/serial_mcp/ports.py` | 포트 스캔 필터·SERIAL_PORT/NAMES 파싱·별칭(순수) | 생성(Task 2) |
| `src/serial_mcp/server.py` | PortMonitor·_monitors·도구 port 라우팅·자동 스캔 main | 수정(Task 3) |
| `src/serial_mcp/web_viewer.py` | `?port=` 라우팅·`/api/ports`·포트 셀렉터 UI | 수정(Task 4) |
| `tests/test_ring_buffer.py` / `test_ports.py` / `test_config.py` / `test_tools.py` / `test_web_viewer.py` | 대응 테스트 | 수정·생성 |
| `SPEC.md` / `README.md` | §1/§3/§4.2/§5/§10·환경변수 표 | 수정(Task 5) |

`viewer_feed.py`·`SerialReader`·`test_serial_reader.py`·`test_viewer_feed.py`·`test_smoke.py`는 무변경.

---

## Task 1: dedup 룩백 윈도 (ring_buffer.py)

**Files:**
- Modify: `src/serial_mcp/ring_buffer.py`
- Test: `tests/test_ring_buffer.py`

- [ ] **Step 1: 실패 테스트 작성 — `tests/test_ring_buffer.py`의 `# ---- 빈 줄 저장 제외` 섹션 바로 위에 추가**

```python
# ---- dedup 룩백 윈도 (SPEC §4.2 개정: 교차 반복 압축) ----

def test_lookback_folds_alternating_lines():
    # 실장비 패턴: A → B → A → B 교차 — 직전-줄 접기로는 못 잡던 케이스
    buf = LineBuffer(maxlen=10, dedup=5)
    buf.add("A", BASE)
    buf.add("B", BASE)
    assert buf.add("A", datetime(2026, 6, 9, 14, 0, 1, 0)) is False   # 룩백 접힘
    assert buf.add("B", datetime(2026, 6, 9, 14, 0, 2, 0)) is False
    snap = buf.snapshot()
    assert [e["text"] for e in snap] == ["A", "B"]          # 항목 위치 유지(first_ts 순서)
    assert snap[0]["count"] == 2 and snap[0]["last_ts"] == "14:00:01.000"
    assert snap[1]["count"] == 2 and snap[1]["last_ts"] == "14:00:02.000"


def test_lookback_window_limit():
    buf = LineBuffer(maxlen=20, dedup=2)
    buf.add("A", BASE)
    buf.add("B", BASE)
    buf.add("C", BASE)        # 이제 A는 윈도(끝 2개: B,C) 밖
    assert buf.add("A", BASE) is True    # 새 항목
    assert len(buf.snapshot()) == 4


def test_dedup_zero_disables_folding():
    buf = LineBuffer(maxlen=10, dedup=0)
    buf.add("x", BASE)
    assert buf.add("x", BASE) is True


def test_dedup_true_means_window_one():
    # 하위호환: 구버전 dedup=True(불리언)는 '직전 1줄만'과 동일
    buf = LineBuffer(maxlen=10, dedup=True)
    buf.add("A", BASE)
    buf.add("B", BASE)
    assert buf.add("A", BASE) is True    # 직전(B)만 비교 → 안 접힘
```

**그리고 기존 테스트 1곳 수정** — `info()["dedup"]`가 불리언이 아니라 윈도 정수가 되므로:

`test_info_reports_capacity_and_endpoints`의 **기존:** `assert info["dedup"] is True` → **수정:** `assert info["dedup"] == 1`

- [ ] **Step 2: 실행 — 실패 확인**

Run: `uv run pytest tests/test_ring_buffer.py -k "lookback or dedup_zero or window_one"`
Expected: FAIL — `test_lookback_folds_alternating_lines`에서 `assert False is True`(교차는 현재 안 접힘) 등.

- [ ] **Step 3: 구현 — `src/serial_mcp/ring_buffer.py`**

(3-1) import에 추가(`from collections import deque` 아래):

```python
from itertools import islice
```

(3-2) `__init__` 시그니처·본문 — **기존:**

```python
    def __init__(
        self,
        maxlen: int = 2000,
        dedup: bool = True,
        exclude: Optional[str] = None,
        include: Optional[str] = None,
    ) -> None:
        self._buf: deque[LogEntry] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._dedup = dedup
```

**교체 후** (`dedup`은 룩백 윈도 정수 — bool도 허용: True→1, False→0):

```python
    def __init__(
        self,
        maxlen: int = 2000,
        dedup: int = 5,
        exclude: Optional[str] = None,
        include: Optional[str] = None,
    ) -> None:
        self._buf: deque[LogEntry] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        # 룩백 윈도: 버퍼 끝 N개 안에 같은 줄이 있으면 접는다.
        # 0=끔, 1=직전 줄만(구버전 동작), bool 입력도 int()로 자연 호환(True→1).
        self._dedup_window = int(dedup)
```

(3-3) `add()`의 접기 블록 — **기존:**

```python
            # 2) 연속 중복 접기: 직전 줄과 내용이 완전히 동일하면 마지막 항목 갱신
            if self._dedup and self._buf and self._buf[-1].text == text:
                last = self._buf[-1]
                last.count += 1
                last.last_ts = ts
                return False
```

**교체 후:**

```python
            # 2) 룩백 접기: 버퍼 끝 N개 안에 같은 줄이 있으면 그 항목에 접는다
            #    (항목 위치 유지 — first_ts 순서 보존. 교차 반복도 압축, SPEC §4.2)
            if self._dedup_window:
                for e in islice(reversed(self._buf), self._dedup_window):
                    if e.text == text:
                        e.count += 1
                        e.last_ts = ts
                        return False
```

(3-4) `info()`의 dedup 항목 — **기존:** `"dedup": self._dedup,` → **교체:** `"dedup": self._dedup_window,`

(3-5) 모듈 docstring 보완 — **기존:** `수신 라인을 타임스탬프와 함께 보관하고, 연속 중복을 접으며(dedup),` → **교체:** `수신 라인을 타임스탬프와 함께 보관하고, 근접 중복을 룩백 윈도로 접으며(dedup=N),`

- [ ] **Step 4: 실행 — 통과 확인**

Run: `uv run pytest tests/test_ring_buffer.py`
Expected: `30 passed` (기존 26 중 1개 수정 + 신규 4). 기존 dedup 테스트들은 `dedup=True`(=윈도 1)라 동작 불변.

- [ ] **Step 5: 커밋**

```powershell
git add src/serial_mcp/ring_buffer.py tests/test_ring_buffer.py
git commit -m "feat: dedup 룩백 접기(윈도 N) — 교차 반복 압축"
```

---

## Task 2: 포트 스캔·파싱·별칭 순수 모듈 (ports.py)

**Files:**
- Create: `src/serial_mcp/ports.py`
- Test: `tests/test_ports.py`

- [ ] **Step 1: 실패 테스트 작성 — `tests/test_ports.py` 전체**

```python
"""ports.py — USB 포트 스캔 필터·SERIAL_PORT/SERIAL_NAMES 파싱·별칭(순수 로직)."""

from types import SimpleNamespace

from serial_mcp.ports import auto_usb_ports, label, name_for, parse_names, parse_port_list


def test_parse_port_list_single():
    assert parse_port_list("COM4") == [("COM4", None)]


def test_parse_port_list_multi_with_baud():
    assert parse_port_list("COM4, COM13@9600") == [("COM4", None), ("COM13", 9600)]


def test_parse_port_list_empty_means_auto():
    assert parse_port_list("") == []


def test_parse_port_list_bad_baud_becomes_none():
    assert parse_port_list("COM4@fast") == [("COM4", None)]


def test_auto_usb_ports_filters_by_vid():
    ports = [
        SimpleNamespace(device="COM4", vid=0x1A86),
        SimpleNamespace(device="COM5", vid=None),      # 블루투스 가상 — 제외
        SimpleNamespace(device="COM13", vid=0x067B),
    ]
    assert auto_usb_ports(ports) == ["COM4", "COM13"]


def test_parse_names_port_and_serial_keys():
    # 키는 대문자 정규화, '=' 없는 항목은 무시
    assert parse_names("com4=SSM, 5909024173=SSM2,bad") == {"COM4": "SSM", "5909024173": "SSM2"}


def test_name_for_prefers_port_key_then_serial_number():
    names = {"COM4": "SSM", "5909024173": "BYSERIAL"}
    assert name_for("com4", "5909024173", names) == "SSM"     # 포트명 키 우선
    assert name_for("COM9", "5909024173", names) == "BYSERIAL"
    assert name_for("COM9", None, names) is None


def test_label_formats():
    assert label("COM4", "SSM") == "SSM (COM4)"
    assert label("COM13", None) == "COM13"
```

- [ ] **Step 2: 실행 — 실패 확인**

Run: `uv run pytest tests/test_ports.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'serial_mcp.ports'`

- [ ] **Step 3: 구현 — `src/serial_mcp/ports.py` 전체**

```python
"""포트 스캔 필터·SERIAL_PORT 목록 파싱·별칭(SERIAL_NAMES) — 순수 로직.

다중 포트 설계(docs/superpowers/specs/2026-06-10-multi-port-design.md §3·§4).
comports() 결과 같은 외부 입력은 인자로 주입받아, 시리얼 I/O 없이 단위 테스트한다.
"""

from __future__ import annotations

from typing import Iterable, Optional


def parse_port_list(raw: str) -> list[tuple[str, Optional[int]]]:
    """SERIAL_PORT 목록 파싱: "COM4,COM13@9600" → [("COM4", None), ("COM13", 9600)].

    빈 문자열이면 [] — 자동 스캔 모드를 뜻한다. '@' 뒤가 정수가 아니면 그 항목의
    보드레이트는 None(전역 SERIAL_BAUD 적용).
    """
    out: list[tuple[str, Optional[int]]] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        port, sep, baud_s = item.partition("@")
        baud: Optional[int] = None
        if sep:
            try:
                baud = int(baud_s)
            except ValueError:
                baud = None
        out.append((port.strip(), baud))
    return out


def auto_usb_ports(comports: Iterable) -> list[str]:
    """USB 시리얼만 골라 device 목록 반환 — VID 보유 = USB(CH343/CP210x/FTDI/Prolific…).

    블루투스 가상 포트 등 VID 없는 포트는 제외한다(열기 시도가 블록될 수 있음).
    """
    return [p.device for p in comports if getattr(p, "vid", None) is not None]


def parse_names(raw: str) -> dict[str, str]:
    """SERIAL_NAMES 파싱: "COM4=SSM,5909024173=SB1" → {"COM4": "SSM", "5909024173": "SB1"}.

    키는 포트명 또는 USB 시리얼넘버(대문자 정규화 — 포트 번호가 바뀌어도 시리얼넘버
    키는 따라간다). '='가 없거나 키/값이 비면 그 항목은 무시.
    """
    names: dict[str, str] = {}
    for item in raw.split(","):
        key, sep, val = item.partition("=")
        key, val = key.strip(), val.strip()
        if sep and key and val:
            names[key.upper()] = val
    return names


def name_for(port: str, serial_number: Optional[str], names: dict[str, str]) -> Optional[str]:
    """포트의 별칭 — 포트명 키 우선, 없으면 USB 시리얼넘버 키, 둘 다 없으면 None."""
    if port.upper() in names:
        return names[port.upper()]
    if serial_number and serial_number.upper() in names:
        return names[serial_number.upper()]
    return None


def label(port: str, name: Optional[str]) -> str:
    """표기 문자열 — 별칭 있으면 'SSM (COM4)', 없으면 'COM4'."""
    return f"{name} ({port})" if name else port
```

- [ ] **Step 4: 실행 — 통과 확인**

Run: `uv run pytest tests/test_ports.py`
Expected: `8 passed`

- [ ] **Step 5: 커밋**

```powershell
git add src/serial_mcp/ports.py tests/test_ports.py
git commit -m "feat: 포트 스캔·별칭 파싱 순수 모듈(ports.py) 추가"
```

---

## Task 3: server.py 다중 포트 개편 (PortMonitor·도구 라우팅·자동 스캔)

선행: Task 1·2 완료. `server.py`·`tests/test_config.py`·`tests/test_tools.py`는 이 Task 단독 소유.

**Files:**
- Modify: `src/serial_mcp/server.py`
- Test: `tests/test_config.py` (개정), `tests/test_tools.py` (전면 재작성)

- [ ] **Step 1: 실패 테스트 — `tests/test_config.py` 개정**

(1-1) **기존 3개 테스트를 다음으로 교체** (`test_load_config_defaults_when_empty`, `test_load_config_reads_all_vars`, `test_load_config_strips_port_whitespace`):

```python
def test_load_config_defaults_when_empty():
    assert _load_config({}) == {
        "ports": [], "names": {}, "baud": 115200, "tee": None, "exclude": None,
        "include": None, "maxlen": 2000, "dedup": 5, "web": 8743,
    }


def test_load_config_reads_all_vars():
    cfg = _load_config({
        "SERIAL_PORT": "COM4,COM13@9600", "SERIAL_NAMES": "COM4=SSM",
        "SERIAL_BAUD": "57600", "SERIAL_TEE": "log.txt",
        "SERIAL_EXCLUDE": "DEBUG", "SERIAL_INCLUDE": "ERROR",
        "SERIAL_BUFFER_LINES": "500", "SERIAL_DEDUP": "0", "SERIAL_WEB": "9000",
    })
    assert cfg == {
        "ports": [("COM4", None), ("COM13", 9600)], "names": {"COM4": "SSM"},
        "baud": 57600, "tee": "log.txt", "exclude": "DEBUG", "include": "ERROR",
        "maxlen": 500, "dedup": 0, "web": 9000,
    }


def test_load_config_strips_port_whitespace():
    assert _load_config({"SERIAL_PORT": "  COM4  "})["ports"] == [("COM4", None)]
```

(1-2) **기존 dedup 진릿값 parametrize 테스트를 윈도 의미로 교체** (`test_load_config_dedup_truthiness` 삭제 후):

```python
@pytest.mark.parametrize(
    "env,expected",
    [
        ({}, 5),                              # 미설정 → 기본 룩백 5
        ({"SERIAL_DEDUP": "0"}, 0),
        ({"SERIAL_DEDUP": "false"}, 0),
        ({"SERIAL_DEDUP": "no"}, 0),
        ({"SERIAL_DEDUP": "off"}, 0),
        ({"SERIAL_DEDUP": "1"}, 1),           # 구버전: 직전 줄만
        ({"SERIAL_DEDUP": "true"}, 1),
        ({"SERIAL_DEDUP": "yes"}, 1),
        ({"SERIAL_DEDUP": "12"}, 12),
        ({"SERIAL_DEDUP": "abc"}, 5),         # 해석 실패 → 기본
        ({"SERIAL_DEDUP": "-3"}, 5),          # 음수 → 기본
    ],
)
def test_load_config_dedup_window(env, expected):
    assert _load_config(env)["dedup"] == expected
```

- [ ] **Step 2: 실행 — 실패 확인**

Run: `uv run pytest tests/test_config.py`
Expected: FAIL — `KeyError: 'ports'`·dedup 불일치 다수.

- [ ] **Step 3: 구현(전반) — `server.py` 설정 계층**

(3-1) import 변경 — **기존:**

```python
from .ring_buffer import LineBuffer
from .viewer_feed import RawFeed
from .web_viewer import ViewerServer
```

**교체 후:**

```python
from .ports import auto_usb_ports, label, name_for, parse_names, parse_port_list
from .ring_buffer import LineBuffer
from .viewer_feed import RawFeed
from .web_viewer import ViewerServer
```

그리고 stdlib import 블록(`import threading` 아래)에 추가:

```python
from dataclasses import dataclass
from pathlib import Path
```

(3-2) `_load_config`의 본문 — **기존:**

```python
    port = env.get("SERIAL_PORT", "").strip()
    baud = _env_int(env, "SERIAL_BAUD", 115200)
    tee = env.get("SERIAL_TEE", "").strip() or None
    exclude = env.get("SERIAL_EXCLUDE", "").strip() or None
    include = env.get("SERIAL_INCLUDE", "").strip() or None
    maxlen = _env_int(env, "SERIAL_BUFFER_LINES", 2000)
    dedup = env.get("SERIAL_DEDUP", "1").strip().lower() not in ("0", "false", "no", "off")
    return {
        "port": port, "baud": baud, "tee": tee, "exclude": exclude,
        "include": include, "maxlen": maxlen, "dedup": dedup,
        "web": _parse_web(env),
    }
```

**교체 후:**

```python
    return {
        "ports": parse_port_list(env.get("SERIAL_PORT", "")),   # [] = USB 자동 스캔
        "names": parse_names(env.get("SERIAL_NAMES", "")),
        "baud": _env_int(env, "SERIAL_BAUD", 115200),
        "tee": env.get("SERIAL_TEE", "").strip() or None,
        "exclude": env.get("SERIAL_EXCLUDE", "").strip() or None,
        "include": env.get("SERIAL_INCLUDE", "").strip() or None,
        "maxlen": _env_int(env, "SERIAL_BUFFER_LINES", 2000),
        "dedup": _parse_dedup(env),
        "web": _parse_web(env),
    }
```

(3-3) `_parse_web` 바로 위에 `_parse_dedup` 신설:

```python
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
```

- [ ] **Step 4: 실행 — config 통과 확인**

Run: `uv run pytest tests/test_config.py`
Expected: `26 passed` (env_int 4 + defaults/reads/strips 3 + dedup 11케이스 + web 8케이스). 이 시점에 test_tools는 깨져 있어도 정상(Step 5에서 재작성).

- [ ] **Step 5: 실패 테스트 — `tests/test_tools.py` 전면 재작성(파일 전체 교체)**

```python
"""MCP 도구 6종 계약 테스트(다중 포트, SPEC §5 개정).

도구는 모듈 전역 _monitors(dict[str, PortMonitor])를 읽는다 → monkeypatch 주입.
@mcp.tool()은 원본 함수를 반환하므로 직접 호출.
"""

from datetime import datetime
from types import SimpleNamespace

import pytest

import serial_mcp.server as srv
from serial_mcp.ring_buffer import LineBuffer
from serial_mcp.viewer_feed import RawFeed

BASE = datetime(2026, 6, 9, 14, 0, 0, 0)


def make_monitor(port="COM_T", name=None, connected=True, last_error=None, opened_at=None):
    """실제 LineBuffer/RawFeed + 가짜 reader로 PortMonitor 구성."""
    reader = SimpleNamespace(connected=connected, port=port, baud=115200,
                             last_error=last_error, opened_at=opened_at)
    return srv.PortMonitor(port=port, name=name,
                           buffer=LineBuffer(maxlen=100, dedup=1),
                           feed=RawFeed(), reader=reader)


@pytest.fixture
def single(monkeypatch):
    """포트 1개(COM_A) 주입 — 미지정 호환 경로 검증용."""
    mon = make_monitor("COM_A")
    monkeypatch.setattr(srv, "_monitors", {"COM_A": mon})
    monkeypatch.setattr(srv, "_viewer", None)
    return mon


@pytest.fixture
def dual(monkeypatch):
    """포트 2개(SSM=COM_A, COM_B) 주입 — 라우팅·별칭·미지정 에러 검증용."""
    a = make_monitor("COM_A", name="SSM")
    b = make_monitor("COM_B", connected=False, last_error="포트 열기 실패(COM_B): busy")
    monkeypatch.setattr(srv, "_monitors", {"COM_A": a, "COM_B": b})
    monkeypatch.setattr(srv, "_viewer", None)
    return a, b


# ---- _resolve_port / port 라우팅 공통 계약 ----

def test_single_port_allows_omitted_port(single):
    single.buffer.add("boot ok", BASE)
    out = srv.get_recent_logs(lines=5)
    assert out["status"] == "ok"
    assert out["port"] == "COM_A"
    assert out["lines"] == ["[14:00:00.000] boot ok"]


def test_multi_port_requires_port(dual):
    out = srv.get_recent_logs()
    assert out["status"] == "error"
    assert "지정" in out["message"]
    assert out["ports"] == ["SSM (COM_A)", "COM_B"]   # AI가 바로 재호출할 목록
    assert out["lines"] == []


def test_port_resolves_alias_case_insensitive(dual):
    a, _ = dual
    a.buffer.add("hello", BASE)
    assert srv.get_recent_logs(port="ssm")["count"] == 1
    assert srv.get_recent_logs(port="com_a")["count"] == 1


def test_unknown_port_lists_available(dual):
    out = srv.get_recent_logs(port="COM_X")
    assert out["status"] == "error"
    assert "COM_X" in out["message"]
    assert "SSM (COM_A)" in out["ports"]


def test_no_monitors_reports_error(monkeypatch):
    monkeypatch.setattr(srv, "_monitors", {})
    out = srv.get_recent_logs()
    assert out["status"] == "error"
    assert out["ports"] == []


# ---- get_serial_status ----

def test_status_without_port_returns_all_ports(dual):
    out = srv.get_serial_status()
    assert out["status"] == "ok"
    assert out["message"] == "1/2 포트 연결됨"
    labels = [p["label"] for p in out["ports"]]
    assert labels == ["SSM (COM_A)", "COM_B"]
    assert out["ports"][1]["connected"] is False
    assert "busy" in out["ports"][1]["last_error"]


def test_status_with_port_returns_single(dual):
    out = srv.get_serial_status(port="SSM")
    assert out["status"] == "ok"
    assert out["connected"] is True
    assert out["port"] == "COM_A"
    assert out["message"] == "연결됨"


def test_status_includes_viewer_url(monkeypatch, single):
    monkeypatch.setattr(srv, "_viewer", SimpleNamespace(url="http://127.0.0.1:8743"))
    assert srv.get_serial_status()["viewer_url"] == "http://127.0.0.1:8743"


# ---- get_recent_logs / query_serial_logs / get_log_buffer_info ----

def test_query_routes_by_port(dual):
    a, b = dual
    a.buffer.add("ERROR boom", BASE)
    b.buffer.add("ERROR other", BASE)
    out = srv.query_serial_logs(r"ERROR", port="COM_B")
    assert out["count"] == 1
    assert out["lines"][0].endswith("ERROR other")


def test_query_invalid_regex_still_reports(single):
    out = srv.query_serial_logs("[")
    assert out["status"] == "error"
    assert "정규식" in out["message"]


def test_buffer_info_routes_and_includes_label(dual):
    a, _ = dual
    a.buffer.add("x", BASE)
    out = srv.get_log_buffer_info(port="COM_A")
    assert out["status"] == "ok"
    assert out["entries"] == 1
    assert out["port"] == "COM_A"


# ---- clear_log_buffer ----

def test_clear_without_port_clears_all(dual):
    a, b = dual
    a.buffer.add("a", BASE)
    b.buffer.add("b1", BASE)
    b.buffer.add("b2", BASE)
    out = srv.clear_log_buffer()
    assert out["status"] == "ok"
    assert out["cleared"] == 3
    assert out["ports"] == {"COM_A": 1, "COM_B": 2}
    assert a.buffer.info()["entries"] == 0


def test_clear_with_port_clears_only_that(dual):
    a, b = dual
    a.buffer.add("a", BASE)
    b.buffer.add("b", BASE)
    out = srv.clear_log_buffer(port="SSM")
    assert out["cleared"] == 1
    assert b.buffer.info()["entries"] == 1


# ---- list_serial_ports ----

def test_list_serial_ports_marks_monitored(monkeypatch, dual):
    fake = [
        SimpleNamespace(device="COM_A", description="USB-SERIAL CH343",
                        hwid="USB VID:PID=1A86:55D3", vid=0x1A86, pid=0x55D3,
                        manufacturer="wch.cn", serial_number="5909024173"),
        SimpleNamespace(device="COM_Z", description="기타", hwid="X", vid=None,
                        pid=None, manufacturer=None, serial_number=None),
    ]
    monkeypatch.setattr(srv.list_ports, "comports", lambda: fake)
    out = srv.list_serial_ports()
    assert out["status"] == "ok"
    assert out["monitored_ports"] == ["SSM (COM_A)", "COM_B"]
    by_dev = {p["device"]: p for p in out["ports"]}
    assert by_dev["COM_A"]["monitored"] is True
    assert by_dev["COM_A"]["name"] == "SSM"
    assert by_dev["COM_Z"]["monitored"] is False
```

- [ ] **Step 6: 실행 — 실패 확인**

Run: `uv run pytest tests/test_tools.py`
Expected: FAIL — `AttributeError: module 'serial_mcp.server' has no attribute 'PortMonitor'`(수집 단계).

- [ ] **Step 7: 구현(후반) — `server.py` 본체 개편**

(7-1) 전역 블록 — **기존:**

```python
# ---- 전역 상태 (main 에서 초기화) ----
mcp = FastMCP("serial-mcp")
_buffer: Optional[LineBuffer] = None
_reader: Optional[SerialReader] = None
_config: dict = {}
_feed: Optional[RawFeed] = None
_viewer: Optional[ViewerServer] = None


def _viewer_url() -> Optional[str]:
    """웹 뷰어 URL — 비활성/기동 실패 시 None."""
    return _viewer.url if _viewer is not None else None
```

**교체 후:**

```python
# ---- 전역 상태 (main 에서 초기화) ----
mcp = FastMCP("serial-mcp")
_monitors: dict[str, "PortMonitor"] = {}   # key = 포트명 대문자
_config: dict = {}
_viewer: Optional[ViewerServer] = None


@dataclass
class PortMonitor:
    """포트 하나의 모니터링 묶음 — 리더·버퍼·생중계 허브(설계 §4)."""

    port: str
    name: Optional[str]               # SERIAL_NAMES 별칭(없으면 None)
    buffer: LineBuffer
    feed: RawFeed
    reader: Optional[SerialReader]    # 테스트에선 SimpleNamespace 주입 가능

    @property
    def label(self) -> str:
        return label(self.port, self.name)


def _viewer_url() -> Optional[str]:
    """웹 뷰어 URL — 비활성/기동 실패 시 None."""
    return _viewer.url if _viewer is not None else None


def _resolve_port(port: str) -> tuple[Optional[PortMonitor], Optional[dict]]:
    """도구의 port 인자(별칭/포트명/빈값)를 PortMonitor로 해석.

    반환: (monitor, None) 또는 (None, 에러 dict — 도구가 그대로 반환).
    미지정: 포트 1개면 그 포트(단일 장비 호환), 복수면 목록과 함께 지정 요구.
    """
    if not _monitors:
        return None, {
            "status": "error",
            "message": "모니터링 중인 포트 없음 — USB 연결 또는 SERIAL_PORT 를 확인하라.",
            "ports": [],
        }
    key = (port or "").strip().upper()
    if not key:
        if len(_monitors) == 1:
            return next(iter(_monitors.values())), None
        return None, {
            "status": "error",
            "message": "포트가 여러 개다 — port 인자로 지정하라(별칭/포트명 모두 가능).",
            "ports": [m.label for m in _monitors.values()],
        }
    for m in _monitors.values():
        if m.port.upper() == key or (m.name and m.name.upper() == key):
            return m, None
    return None, {
        "status": "error",
        "message": f"포트 '{port}' 를 모르겠다 — ports 목록에서 골라 다시 호출하라.",
        "ports": [m.label for m in _monitors.values()],
    }
```

(7-2) **도구 6종을 다음으로 전부 교체** (`@mcp.tool()` 블록 6개 — `list_serial_ports`부터 `clear_log_buffer`까지를 아래 코드로):

```python
@mcp.tool()
def list_serial_ports() -> dict:
    """[언제 호출] 어느 포트가 어느 보드인지 확인할 때, 모니터링 대상을 점검할 때.

    [무엇을 반환] 현재 PC의 시리얼 포트 목록. 각 포트의 device/description/vid/pid/
    manufacturer/serial_number 에 더해, 이 서버가 모니터링 중이면 monitored=true 와
    별칭 name 이 붙는다. monitored_ports 는 현재 모니터링 목록(별칭 표기).
    VID/PID·description 으로 칩(CH343, CP210x 등)을 추론하라.

    [루프 단계] 사전 점검 — 보통 한 번만.
    """
    monitored = {m.port.upper(): m for m in _monitors.values()}
    ports = []
    for p in list_ports.comports():
        mon = monitored.get(p.device.upper())
        ports.append(
            {
                "device": p.device,
                "description": p.description,
                "hwid": p.hwid,
                "vid": p.vid,
                "pid": p.pid,
                "manufacturer": p.manufacturer,
                "serial_number": p.serial_number,
                "monitored": mon is not None,
                "name": mon.name if mon else None,
            }
        )
    return {
        "status": "ok",
        "message": f"{len(ports)}개 포트 발견, {len(_monitors)}개 모니터링 중",
        "monitored_ports": [m.label for m in _monitors.values()],
        "ports": ports,
    }


@mcp.tool()
def get_serial_status(port: str = "") -> dict:
    """[언제 호출] 로그가 안 들어올 때 '어느 보드가 연결돼 있는지'부터 확인할 때.
    포트 점유/미연결/미인식 원인을 구분한다.

    [무엇을 반환] port 미지정 시 모니터링 중인 전 포트의 상태 배열(ports).
    port(별칭 "SSM" 또는 포트명 "COM4") 지정 시 그 포트의 단일 상태.
    connected 가 false 이고 last_error 에 점유/권한 에러가 있으면 사람에게 같은
    포트를 쓰는 다른 프로그램(테라텀 등) 종료를 요청하라.

    사람이 로그를 직접 눈으로 보고 싶어 하면 viewer_url 링크를 안내하라(웹 뷰어).
    [루프 단계] 문제 진단.
    """

    def one(m: PortMonitor) -> dict:
        r = m.reader
        return {
            "name": m.name,
            "label": m.label,
            "port": m.port,
            "connected": bool(r and r.connected),
            "baud": r.baud if r else None,
            "last_error": r.last_error if r else None,
            "opened_at": r.opened_at.isoformat() if r and r.opened_at else None,
        }

    if (port or "").strip():
        mon, err = _resolve_port(port)
        if err:
            return {**err, "connected": False, "viewer_url": _viewer_url()}
        d = one(mon)
        d["status"] = "ok"
        d["message"] = "연결됨" if d["connected"] else "연결 안 됨"
        d["viewer_url"] = _viewer_url()
        return d
    if not _monitors:
        return {
            "status": "error",
            "message": "모니터링 중인 포트 없음 — USB 연결 또는 SERIAL_PORT 를 확인하라.",
            "connected": False,
            "ports": [],
            "viewer_url": _viewer_url(),
        }
    plist = [one(m) for m in _monitors.values()]
    n_on = sum(1 for x in plist if x["connected"])
    return {
        "status": "ok",
        "message": f"{n_on}/{len(plist)} 포트 연결됨",
        "ports": plist,
        "viewer_url": _viewer_url(),
    }


@mcp.tool()
def get_recent_logs(lines: int = 200, port: str = "") -> dict:
    """[언제 호출] 블랙박스 루프의 '결과 확인' 단계 — 사람이 장비를 동작시킨 뒤
    쌓인 로그를 확인할 때. 가장 자주 쓰는 도구.

    [port 규약] 보드가 여러 개면 port 를 지정하라(별칭 "SSM" 또는 "COM4", 대소문자
    무관). 미지정: 포트 1개면 그 포트, 복수면 에러와 함께 ports 목록을 돌려준다 —
    목록에서 골라 즉시 재호출하면 된다.

    [무엇을 반환] 최근 N개 라인(시간 오름차순). 근접 중복은 룩백으로 접혀
    '(N회 반복, HH:MM:SS~HH:MM:SS)' 표기 — 접힘은 요약이라 반복 줄들의 정밀한
    교차 순서는 뭉개진다. 정밀 순서가 필요하면 SERIAL_DEDUP=1 또는 0 으로 낮춰
    재시험하라(tee 파일엔 원본 보존).

    [팁] 결과가 많으면 query_serial_logs 로 좁혀라. 비어 있으면 get_serial_status
    로 연결을 확인하고, 그래도 비면 사람에게 장비 동작/리셋을 요청하라.

    [루프 단계] 결과 확인.
    """
    mon, err = _resolve_port(port)
    if err:
        return {**err, "count": 0, "lines": []}
    got = mon.buffer.get_recent(lines)
    return {
        "status": "ok",
        "message": f"{mon.label}: {len(got)}줄 반환",
        "port": mon.port,
        "name": mon.name,
        "count": len(got),
        "lines": got,
    }


@mcp.tool()
def query_serial_logs(pattern: str, max_results: int = 100, port: str = "") -> dict:
    """[언제 호출] 특정 키워드/에러/마커를 버퍼에서 찾을 때. 예: 부팅 완료 문구,
    'ERROR', 특정 상태 출력의 등장 여부.

    [port 규약] get_recent_logs 와 동일 — 복수 포트면 지정, 미지정 에러 시 ports
    목록에서 골라 재호출.

    [무엇을 반환] 정규식 pattern 매칭 라인들(최신 우선 max_results개, 반환은 시간
    오름차순, 접힌 묶음 표기 포함). 매칭 0이면 그 문구가 아직 안 나온 것 — 사람에게
    해당 동작을 요청하거나 더 기다린 뒤 재조회하라.

    [루프 단계] 결과 확인(표적 검색).
    """
    mon, err = _resolve_port(port)
    if err:
        return {**err, "count": 0, "lines": []}
    try:
        got = mon.buffer.query(pattern, max_results)
    except re.error as e:
        return {"status": "error", "message": f"정규식 오류: {e}", "count": 0, "lines": []}
    return {
        "status": "ok",
        "message": f"{mon.label}: {len(got)}줄 매칭",
        "port": mon.port,
        "name": mon.name,
        "pattern": pattern,
        "count": len(got),
        "lines": got,
    }


@mcp.tool()
def get_log_buffer_info(port: str = "") -> dict:
    """[언제 호출] 버퍼가 얼마나 찼는지, 최근/최오래 항목이 무엇인지 빠르게 볼 때.
    clear_log_buffer 직후 새 로그 유입을 폴링할 때 특히 유용.

    [port 규약] get_recent_logs 와 동일.

    [무엇을 반환] entries/capacity, oldest/newest, 누적 total_received/total_stored,
    dedup(룩백 윈도 — 0이면 끔).

    [루프 단계] 진행 점검(폴링).
    """
    mon, err = _resolve_port(port)
    if err:
        return err
    info = mon.buffer.info()
    info["status"] = "ok"
    info["message"] = f"{mon.label}: {info['entries']}/{info['capacity']} 항목"
    info["port"] = mon.port
    info["name"] = mon.name
    info["viewer_url"] = _viewer_url()
    return info


@mcp.tool()
def clear_log_buffer(port: str = "") -> dict:
    """[언제 호출] 블랙박스 시험의 '시작' 단계 — 새 시험을 깨끗한 상태에서
    관측하려고 직전 로그를 비울 때. 표준 절차: 비우고 → 사람에게 장비 동작/리셋
    요청 → 잠시 후 get_recent_logs 로 회수.

    [port 규약] 다른 도구와 달리 **미지정 = 전체 포트 비우기**(시험 시작 시 모든
    보드를 함께 리셋 관측하는 게 보통이므로). 특정 보드만 비우려면 port 지정.

    [무엇을 반환] cleared(총 비운 항목 수)와 ports(포트별 내역).

    [루프 단계] 시험 시작.
    """
    if not _monitors:
        return {"status": "error", "message": "모니터링 중인 포트 없음", "cleared": 0, "ports": {}}
    if (port or "").strip():
        mon, err = _resolve_port(port)
        if err:
            return {**err, "cleared": 0}
        n = mon.buffer.clear()
        return {"status": "ok", "message": f"{mon.label}: {n}개 항목 비움",
                "cleared": n, "ports": {mon.port: n}}
    detail = {m.port: m.buffer.clear() for m in _monitors.values()}
    total = sum(detail.values())
    return {"status": "ok", "message": f"전체 {len(detail)}개 포트에서 {total}개 비움",
            "cleared": total, "ports": detail}
```

(7-3) 뷰어 콜백 — **기존 `_viewer_buffer_info`·`_viewer_status_info` 두 함수를 다음 네 함수로 교체:**

```python
def _viewer_ports_info() -> list[dict]:
    """웹 뷰어 /api/ports — 셀렉터 구성용 [{port, label}] 목록."""
    return [{"port": m.port, "label": m.label} for m in _monitors.values()]


def _viewer_feed_for(port: str) -> Optional[RawFeed]:
    """웹 뷰어 /api/stream?port= — 해당 포트의 RawFeed(없으면 None→404)."""
    m = _monitors.get((port or "").strip().upper())
    return m.feed if m else None


def _viewer_buffer_info(port: str) -> dict:
    """웹 뷰어 /api/buffer?port= — 해당 포트의 구조화 스냅샷 + 카운터."""
    m = _monitors.get((port or "").strip().upper())
    if m is None:
        return {"status": "error", "entries": [], "capacity": 0}
    info = m.buffer.info()
    return {
        "status": "ok",
        "port": m.port,
        "entries": m.buffer.snapshot(),
        "capacity": info["capacity"],
        "total_received": info["total_received"],
        "total_stored": info["total_stored"],
        "dedup": info["dedup"],
    }


def _viewer_status_info() -> dict:
    """웹 뷰어 /api/status — 전 포트 상태 배열(+버퍼 적재 현황, 탭 카운터용)."""
    plist = []
    for m in _monitors.values():
        r = m.reader
        binfo = m.buffer.info()
        plist.append({
            "port": m.port,
            "label": m.label,
            "connected": bool(r and r.connected),
            "baud": r.baud if r else None,
            "last_error": r.last_error if r else None,
            "buffer_entries": binfo["entries"],
            "buffer_capacity": binfo["capacity"],
        })
    return {"ports": plist}
```

(7-4) `main()` — **기존 본문 전체를 다음으로 교체** (시그니처·마지막 `mcp.run()` 줄 포함):

```python
def _tee_path_for(base: Optional[str], tag: str) -> Optional[str]:
    """포트별 tee 파일 경로 — 'log.txt' + 'SSM' → 'log.SSM.txt'(파일명 안전화)."""
    if not base:
        return None
    safe = re.sub(r"[^\w\-]", "_", tag)
    p = Path(base)
    return str(p.with_name(f"{p.stem}.{safe}{p.suffix}"))


def main() -> None:
    """엔트리포인트. USB 자동 스캔(또는 SERIAL_PORT 목록)으로 포트별 모니터를
    띄우고 stdio 로 MCP 서버 구동."""
    global _config, _viewer

    cfg = _load_config(os.environ)
    _config = cfg

    com = list(list_ports.comports())
    specs = cfg["ports"]
    if not specs:
        specs = [(dev, None) for dev in auto_usb_ports(com)]
        _log(f"자동 스캔: USB 시리얼 {len(specs)}개 발견")
    sn_map = {p.device.upper(): getattr(p, "serial_number", None) for p in com}

    for port, baud_override in specs:
        baud = baud_override or cfg["baud"]
        name = name_for(port, sn_map.get(port.upper()), cfg["names"])
        buf = LineBuffer(maxlen=cfg["maxlen"], dedup=cfg["dedup"],
                         exclude=cfg["exclude"], include=cfg["include"])
        feed = RawFeed()
        reader = SerialReader(port=port, baud=baud, buffer=buf,
                              tee_path=_tee_path_for(cfg["tee"], name or port), feed=feed)
        mon = PortMonitor(port=port, name=name, buffer=buf, feed=feed, reader=reader)
        _monitors[port.upper()] = mon
        reader.start()
        _log(f"모니터 시작: {mon.label} @ {baud}")

    if not _monitors:
        _log("경고: 모니터링할 포트 없음 — USB 시리얼이 안 보이고 SERIAL_PORT 도 "
             "비어 있다. 장비 연결 후 서버를 재시작하라(핫플러그 없음).")

    if cfg["web"] is not None:
        _viewer = ViewerServer(
            ports_info=_viewer_ports_info,
            feed_for=_viewer_feed_for,
            buffer_info=_viewer_buffer_info,
            status_info=_viewer_status_info,
            port=cfg["web"],
        )
        _viewer.start()   # 실패해도 예외 없음 — url이 None으로 남을 뿐
        _log(f"웹 뷰어: {_viewer.url or '기동 실패'}")
    else:
        _log("웹 뷰어 꺼짐 (SERIAL_WEB=0)")

    _log(f"시작 (포트 {len(_monitors)}개, dedup={cfg['dedup']}, "
         f"buffer={cfg['maxlen']}, tee={cfg['tee'] or '없음'})")
    mcp.run()  # stdio transport(기본)
```

주의: `ViewerServer` 생성자는 Task 4에서 새 시그니처로 바뀐다. **Task 3 검증 시점에 Task 4가 아직이면** `uv run pytest tests/test_tools.py tests/test_config.py tests/test_ring_buffer.py tests/test_ports.py` 처럼 본인 소유 테스트만 돌린다(웹 뷰어 테스트는 Task 4 소유). `py -m compileall -q src` 는 시그니처 불일치를 잡지 않으므로 안전.

- [ ] **Step 8: 실행 — 통과 확인**

```powershell
uv run pytest tests/test_tools.py tests/test_config.py tests/test_serial_reader.py tests/test_smoke.py
py -m compileall -q src
```
Expected: tools `14 passed`, config·serial_reader·smoke 회귀 없음, compileall 무출력.

- [ ] **Step 9: 커밋**

```powershell
git add src/serial_mcp/server.py tests/test_config.py tests/test_tools.py
git commit -m "feat: 다중 포트 모니터링 — PortMonitor·도구 port 라우팅·자동 스캔"
```

---

## Task 4: 웹 뷰어 포트 셀렉터 (web_viewer.py)

선행: 없음(`viewer_feed.py`만 의존 — server.py와 무관, Task 3과 병렬 가능). `web_viewer.py`·`tests/test_web_viewer.py` 단독 소유.

**Files:**
- Modify: `src/serial_mcp/web_viewer.py`
- Test: `tests/test_web_viewer.py` (전면 재작성)

- [ ] **Step 1: 실패 테스트 — `tests/test_web_viewer.py` 전면 재작성(파일 전체 교체)**

```python
"""ViewerServer — 다중 포트 라우팅 통합 테스트(임시 포트, 실제 HTTP 기동)."""

import json
import socket
import urllib.error
import urllib.request
from datetime import datetime

import pytest

from serial_mcp.viewer_feed import RawFeed
from serial_mcp.web_viewer import ViewerServer

BASE = datetime(2026, 6, 9, 14, 0, 0, 0)


@pytest.fixture
def viewer():
    feeds = {"COM_A": RawFeed(), "COM_B": RawFeed()}
    v = ViewerServer(
        ports_info=lambda: [{"port": "COM_A", "label": "SSM (COM_A)"},
                            {"port": "COM_B", "label": "COM_B"}],
        feed_for=lambda p: feeds.get(p),
        buffer_info=lambda p: {"status": "ok", "port": p, "entries": [], "capacity": 2000},
        status_info=lambda: {"ports": [
            {"port": "COM_A", "label": "SSM (COM_A)", "connected": True, "baud": 115200,
             "last_error": None, "buffer_entries": 3, "buffer_capacity": 2000},
            {"port": "COM_B", "label": "COM_B", "connected": False, "baud": 115200,
             "last_error": "busy", "buffer_entries": 0, "buffer_capacity": 2000},
        ]},
        port=0,   # 테스트는 임시 포트
    )
    v.start()
    assert v.url is not None
    yield v, feeds
    v.stop()


def _get_json(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.loads(r.read())


def test_root_serves_html(viewer):
    v, _ = viewer
    with urllib.request.urlopen(v.url + "/", timeout=5) as r:
        assert r.status == 200
        body = r.read().decode("utf-8")
    assert "serial-mcp" in body
    assert "psel" in body            # 포트 셀렉터 존재


def test_api_ports_lists_monitors(viewer):
    v, _ = viewer
    d = _get_json(v.url + "/api/ports")
    assert [p["label"] for p in d["ports"]] == ["SSM (COM_A)", "COM_B"]


def test_api_status_returns_port_array(viewer):
    v, _ = viewer
    d = _get_json(v.url + "/api/status")
    assert d["ports"][0]["connected"] is True
    assert d["ports"][1]["last_error"] == "busy"


def test_api_buffer_routes_by_port(viewer):
    v, _ = viewer
    assert _get_json(v.url + "/api/buffer?port=COM_B")["port"] == "COM_B"


def test_stream_sse_isolated_per_port(viewer):
    v, feeds = viewer
    resp = urllib.request.urlopen(v.url + "/api/stream?port=COM_A", timeout=5)
    try:
        assert "text/event-stream" in resp.headers["Content-Type"]
        feeds["COM_B"].publish(BASE, "B쪽 줄 — 받으면 안 됨")
        feeds["COM_A"].publish(BASE, "hello A")
        line = resp.readline().decode("utf-8")
        payload = json.loads(line[len("data: "):])
        assert payload == {"ts": "14:00:00.000", "text": "hello A"}   # A만 수신
    finally:
        resp.close()


def test_stream_unknown_port_404(viewer):
    v, _ = viewer
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(v.url + "/api/stream?port=NOPE", timeout=5)
    assert exc.value.code == 404


def test_unknown_path_returns_404(viewer):
    v, _ = viewer
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(v.url + "/nope", timeout=5)
    assert exc.value.code == 404


def test_port_fallback_when_preferred_busy():
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    busy_port = blocker.getsockname()[1]
    try:
        v = ViewerServer(ports_info=lambda: [], feed_for=lambda p: None,
                         buffer_info=lambda p: {}, status_info=lambda: {"ports": []},
                         port=busy_port)
        v.start()
        try:
            assert v.url is not None
            assert v.url != f"http://127.0.0.1:{busy_port}"
        finally:
            v.stop()
    finally:
        blocker.close()
```

- [ ] **Step 2: 실행 — 실패 확인**

Run: `uv run pytest tests/test_web_viewer.py`
Expected: FAIL — `TypeError: ViewerServer.__init__() got an unexpected keyword argument 'ports_info'`(수집/실행).

- [ ] **Step 3: 구현 — `web_viewer.py` 파이썬부**

(3-1) import에 추가(`from http.server import ...` 아래):

```python
from urllib.parse import parse_qs, urlparse
```

(3-2) `_ViewerHTTPServer`의 주입 속성 — **기존:**

```python
    feed: RawFeed
    buffer_info: Callable[[], dict]
    status_info: Callable[[], dict]
```

**교체 후:**

```python
    ports_info: Callable[[], list]
    feed_for: Callable[[str], Optional[RawFeed]]
    buffer_info: Callable[[str], dict]
    status_info: Callable[[], dict]
```

(3-3) `do_GET` — **기존 전체를 교체:**

```python
    def do_GET(self) -> None:  # noqa: N802 - http.server 규약
        parsed = urlparse(self.path)
        path = parsed.path
        port = (parse_qs(parsed.query).get("port") or [""])[0]
        if path == "/":
            body = _HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/stream":
            self._serve_stream(port)
        elif path == "/api/ports":
            self._send_json({"ports": self.server.ports_info()})
        elif path == "/api/buffer":
            self._send_json(self.server.buffer_info(port))
        elif path == "/api/status":
            self._send_json(self.server.status_info())
        else:
            self.send_error(404)
```

(3-4) `_serve_stream` — **기존 시그니처·구독 줄을 교체.** 기존:

```python
    def _serve_stream(self) -> None:
        """SSE — RawFeed를 구독해 한 줄당 한 이벤트로 흘려보낸다."""
        # 구독을 헤더 전송보다 먼저: 클라이언트가 응답 헤더를 받은 시점에는
        # 이미 구독이 살아 있어야 발행 누락이 없다(테스트·실사용 레이스 방지).
        sub = self.server.feed.subscribe()
        try:
```

**교체 후:**

```python
    def _serve_stream(self, port: str) -> None:
        """SSE — 지정 포트의 RawFeed를 구독해 한 줄당 한 이벤트로 흘려보낸다."""
        feed = self.server.feed_for(port)
        if feed is None:
            self.send_error(404, "unknown port")
            return
        # 구독을 헤더 전송보다 먼저: 클라이언트가 응답 헤더를 받은 시점에는
        # 이미 구독이 살아 있어야 발행 누락이 없다(테스트·실사용 레이스 방지).
        sub = feed.subscribe()
        try:
```

그리고 같은 함수의 `finally` — **기존:** `self.server.feed.unsubscribe(sub)` → **교체:** `feed.unsubscribe(sub)`

(3-5) `ViewerServer.__init__`·`start()`의 주입부 — **기존:**

```python
    def __init__(
        self,
        feed: RawFeed,
        buffer_info: Callable[[], dict],
        status_info: Callable[[], dict],
        port: int = 8743,
    ) -> None:
        self._feed = feed
        self._buffer_info = buffer_info
        self._status_info = status_info
```

**교체 후:**

```python
    def __init__(
        self,
        ports_info: Callable[[], list],
        feed_for: Callable[[str], Optional[RawFeed]],
        buffer_info: Callable[[str], dict],
        status_info: Callable[[], dict],
        port: int = 8743,
    ) -> None:
        self._ports_info = ports_info
        self._feed_for = feed_for
        self._buffer_info = buffer_info
        self._status_info = status_info
```

`start()`의 — **기존:**

```python
        self._httpd.feed = self._feed
        self._httpd.buffer_info = self._buffer_info
        self._httpd.status_info = self._status_info
```

**교체 후:**

```python
        self._httpd.ports_info = self._ports_info
        self._httpd.feed_for = self._feed_for
        self._httpd.buffer_info = self._buffer_info
        self._httpd.status_info = self._status_info
```

- [ ] **Step 4: 구현 — `_HTML` UI(셀렉터·포트별 연결)**

(4-1) CSS — `#port { color:#8b949e; }` 다음 줄에 추가:

```
  #psel { background:#21262d; color:#c9d1d9; border:1px solid #2d333b;
          border-radius:4px; padding:3px 6px; font:inherit; }
```

(4-2) 헤더 HTML — **기존:** `  <span id="port">…</span>` → **교체:**

```
  <span id="port">…</span>
  <select id="psel" title="보드 선택"></select>
```

(4-3) JS 스트림 연결부 — **기존**(`const es = new EventSource("/api/stream");`부터 `es.onmessage = ev => { ... };`의 닫는 `};`까지, `es.onopen` 마커 블록 포함)을 **다음으로 교체**:

```
let es = null, currentPort = null;
function connectStream(port) {   // 포트 전환 = 스트림 화면 리셋 + 새 SSE 구독
  if (es) es.close();
  currentPort = port;
  $("stream").innerHTML = "";
  streamLines = 0; streamLastSec = null; newCount = 0;
  $("newpill").style.display = "none";
  updateStreamTab();
  es = new EventSource("/api/stream?port=" + encodeURIComponent(port));
  es.onopen = () => {   // 스트림은 "지금부터" 생중계 — 시작 지점 명시(이전 기록은 버퍼 탭)
    const g = document.createElement("div");
    g.className = "gap";
    g.textContent = "실시간 수신 시작 — 이전 기록은 [버퍼] 탭";
    $("stream").appendChild(g);
  };
  es.onmessage = ev => {
    if (paused) return;
    const d = JSON.parse(ev.data);
    const box = $("stream");
    const sec = toSec(d.ts);
    const dim = streamLastSec === sec;   // 같은 초 반복 → 거터 흐리게
    if (streamLastSec !== null && sec - streamLastSec >= GAP_SEC) {
      const g = gapDivider(sec - streamLastSec);
      if (filterRe) g.style.display = "none";
      box.appendChild(g);
    }
    streamLastSec = sec;
    box.appendChild(renderLine(d.ts, d.text, "", dim));
    streamLines++;
    while (box.childNodes.length > MAX_STREAM) {   // 한도 초과분은 위에서 제거
      const removed = box.firstChild;
      if (removed.classList && removed.classList.contains("ln")) streamLines--;
      box.removeChild(removed);
    }
    updateStreamTab();
    if ($("follow").checked && activeTab === "stream") {
      window.scrollTo(0, document.body.scrollHeight);
    } else if (!nearBottom()) {
      newCount++;   // 바닥을 안 보고 있을 때만 새 로그 배지
      $("newpill").textContent = "↓ 새 로그 " + newCount + "건";
      $("newpill").style.display = "block";
    }
  };
}
```

(4-4) `refreshBuffer` 도입부 — **기존:**

```
async function refreshBuffer() {
  if (activeTab !== "buffer" || paused) return;
  const d = await (await fetch("/api/buffer")).json();
```

**교체 후:**

```
async function refreshBuffer() {
  if (activeTab !== "buffer" || paused || !currentPort) return;
  const d = await (await fetch("/api/buffer?port=" + encodeURIComponent(currentPort))).json();
```

(4-5) `refreshStatus` — **기존 함수 전체를 교체:**

```
async function refreshStatus() {
  try {
    const d = await (await fetch("/api/status")).json();
    const p = (d.ports || []).find(x => x.port === currentPort) || (d.ports || [])[0];
    if (!p) { $("dot").className = "dot"; $("port").textContent = "(모니터링 포트 없음)"; return; }
    $("dot").className = "dot" + (p.connected ? " on" : "");
    $("port").textContent = p.label + " @ " + p.baud +
      (p.last_error ? " — " + p.last_error : "");
    setBufferTab(p.buffer_entries, p.buffer_capacity);
  } catch (e) { $("dot").className = "dot"; }
}
```

(4-6) 초기화 — **기존:** `updateStreamTab();   // 초기 표시 "스트림 0/5000"` 다음에 추가:

```
async function initPorts() {   // 포트 목록 → 셀렉터 구성 → 첫 포트 스트림 연결
  try {
    const d = await (await fetch("/api/ports")).json();
    const sel = $("psel");
    for (const p of d.ports || []) {
      const o = document.createElement("option");
      o.value = p.port;
      o.textContent = p.label;
      sel.appendChild(o);
    }
    if ((d.ports || []).length <= 1) sel.style.display = "none";   // 1개면 셀렉터 불필요
    sel.onchange = () => { connectStream(sel.value); refreshStatus(); };
    if ((d.ports || []).length) connectStream(d.ports[0].port);
    refreshStatus();
  } catch (e) { $("port").textContent = "(포트 목록 조회 실패)"; }
}
initPorts();
```

- [ ] **Step 5: 실행 — 통과 확인 + 문법 검증**

```powershell
uv run pytest tests/test_web_viewer.py tests/test_viewer_feed.py
py -m compileall -q src
```
Expected: web_viewer `8 passed`, viewer_feed 회귀 없음, compileall 무출력. (주의: 이 시점에 Task 3 미완이면 `uv run pytest` 전체는 돌리지 말 것 — server.py가 옛 시그니처로 ViewerServer를 부르는 동안은 tools 테스트가 깨져 있는 게 정상.)

- [ ] **Step 6: 커밋**

```powershell
git add src/serial_mcp/web_viewer.py tests/test_web_viewer.py
git commit -m "feat: 뷰어 포트 셀렉터 — 포트별 스트림/버퍼 전환(?port= 라우팅)"
```

---

## Task 5: 문서 동기화 + 전체 검증

선행: Task 1~4 전부. 문서 2개만 수정(코드 금지).

**Files:**
- Modify: `SPEC.md`, `README.md`

- [ ] **Step 1: SPEC.md §1 사용 맥락 — 기존:**

```
사용 맥락: 동일한 PC에 연결된 단일 장비의 로그를 복수의 코드베이스에서 공통으로 참조한다.
```

(해당 문단의 첫 문장만) **교체 후:**

```
사용 맥락: 동일한 PC에 연결된 **복수 장비**(USB 시리얼 자동 인식 — 2개면 2개, 10개면 10개)의 로그를 복수의 코드베이스에서 공통으로 참조한다. 장비 식별은 포트명이 아니라 별칭(`SERIAL_NAMES`, 예: `SSM (COM4)`)으로 한다.
```

- [ ] **Step 2: SPEC.md §3 — 첫 불릿 앞에 불릿 추가:**

```
- 포트 결정: `SERIAL_PORT` 미설정이면 시작 시 1회 USB 시리얼(VID 보유)을 자동 스캔해 전부 모니터링한다(블루투스 가상 포트 제외, 핫플러그 없음 — 장비 추가는 서버 재시작). 설정 시 그 목록만(`COM4` 또는 `COM4,COM13@9600` — `@N`은 포트별 보드레이트). 포트마다 독립 버퍼·리더·tee(`log.txt`→`log.SSM.txt`)를 갖는다.
```

- [ ] **Step 3: SPEC.md §4.2 마지막 불릿 — 기존:**

```
- 동일 줄 판정 기준은 우선 "직전 줄과 완전히 동일"로 단순하게 시작한다(실제 로그 확인 후 조정).
```

**교체 후:**

```
- 접기 판정은 **룩백 N**(기본 5, `SERIAL_DEDUP=N`): 버퍼 끝 N개 안에 같은 줄이 있으면 그 항목의 횟수·최종 시각을 갱신한다(항목 위치는 유지 — first_ts 순서 보존). `1`=직전 줄만(구버전 동작), `0`/`false`=끔. 근거: 실로그(2026-06-10)에서 메시지가 교차 출력돼 연속 접기가 무력했음. 접힘은 요약이므로 반복 줄들의 정밀한 교차 순서가 필요하면 N을 낮춰 재시험한다(tee에 원본 보존).
```

- [ ] **Step 4: SPEC.md §5 — 도입 문단(`각 도구는 ...`) 끝에 문장 추가:**

```
 다중 포트에서는 모든 조회 도구가 `port` 인자(별칭/포트명, 대소문자 무관)를 받는다 — 미지정 시 포트 1개면 그 포트, 복수면 에러와 함께 `ports` 목록을 반환한다(`get_serial_status` 미지정은 전 포트 상태 배열, `clear_log_buffer` 미지정은 전체 비우기).
```

- [ ] **Step 5: SPEC.md §10 — "탭 2개:" 불릿 끝에 문장 추가:**

```
 다중 포트에서는 헤더의 포트 셀렉터로 보드를 전환한다(`SSM (COM4)` 표기, 1개면 셀렉터 숨김).
```

- [ ] **Step 6: SPEC.md 부록 — `- 미완:` 줄 위에 추가:**

```
- 다중 포트 자동 모니터링 구현(2026-06-10): USB 자동 스캔·PortMonitor×N·별칭(SERIAL_NAMES)·도구 port 라우팅·뷰어 포트 셀렉터·dedup 룩백(기본 5). 설계: `docs/superpowers/specs/2026-06-10-multi-port-design.md`.
```

- [ ] **Step 7: README.md 환경변수 표 — 기존 3행을 교체하고 1행 추가:**

`SERIAL_PORT` 행 — **기존:**

```
| `SERIAL_PORT` | (필수) | 대상 포트. Windows 의 COM10 이상은 `\\.\COM10` 형식 |
```

**교체 후:**

```
| `SERIAL_PORT` | (없음=자동) | 미설정이면 USB 시리얼 전부 자동 모니터링(시작 시 1회 스캔). 지정 시 그 목록만: `COM4` 또는 `COM4,COM13@9600`. COM10 이상은 `\\.\COM10` 형식 |
```

`SERIAL_DEDUP` 행 — **기존:**

```
| `SERIAL_DEDUP` | `1` | 연속 중복 접기 (`0`/`false` 로 끔) |
```

**교체 후:**

```
| `SERIAL_DEDUP` | `5` | 중복 접기 룩백 윈도 — 최근 N줄 안의 같은 줄을 접음. `1`=직전 줄만, `0`으로 끔 |
```

`SERIAL_TEE` 행 — **기존:**

```
| `SERIAL_TEE` | (없음) | 로그를 파일에도 영구 기록할 경로(버퍼에서 밀려난 줄도 보존) |
```

**교체 후:**

```
| `SERIAL_TEE` | (없음) | 로그 영구 기록 경로 — 포트별 파일로 분리(`log.txt`→`log.SSM.txt`). 버퍼에서 밀려난 줄도 보존 |
```

그리고 `SERIAL_PORT` 행 바로 아래 행 추가:

```
| `SERIAL_NAMES` | (없음) | 포트→보드 별칭. `COM4=SSM,COM13=SB1` 또는 USB 시리얼넘버 키 `5909024173=SSM`(포트 번호가 바뀌어도 유지). 표기·도구 port 인자에 별칭 사용 가능 |
```

- [ ] **Step 8: README.md "자기 포트 설정" 절 — 기존:**

```
### 자기 포트 설정

- **Windows** (PowerShell): `setx SERIAL_PORT COM4`  (새 터미널부터 적용)
- **macOS / Linux**: `export SERIAL_PORT=/dev/cu.usbserial-XXXX`
```

**교체 후:**

```
### 다중 포트 · 별칭

기본값(미설정)이면 USB 시리얼을 전부 자동 모니터링한다 — 보드 2개면 2개, 10개면 10개. 사람이 보는 모든 표기는 별칭을 설정하면 `SSM (COM4)` 형태가 된다:

- **Windows** (PowerShell): `setx SERIAL_NAMES "COM4=SSM,COM13=SB1"`  (새 터미널부터 적용)
- **macOS / Linux**: `export SERIAL_NAMES="COM4=SSM"`
- 특정 포트만 보려면: `setx SERIAL_PORT "COM4,COM13@9600"` (`@N`=포트별 보드레이트)

AI 도구는 보드가 여러 개면 `port` 인자(별칭/포트명)를 지정해 호출한다. `clear_log_buffer`만 미지정 시 전체를 비운다.
```

- [ ] **Step 9: README.md 웹 뷰어 절 — `- **스트림 탭**` 불릿 위에 불릿 추가:**

```
- **포트 셀렉터** — 보드가 여러 개면 헤더에서 `SSM (COM4)` 식으로 전환(1개면 숨김).
```

- [ ] **Step 10: 전체 검증**

```powershell
uv run pytest
py -m compileall -q src
```
Expected: 전체 🟢(예상 합계 ≈ 104 — smoke 2 + ring 30 + ports 8 + reader 9 + feed 7 + config 26 + tools 14 + web 8. 정확 수는 출력으로 확정), compileall 무출력.

- [ ] **Step 11: 커밋**

```powershell
git add SPEC.md README.md
git commit -m "docs: 다중 포트 반영 — SPEC §1/§3/§4.2/§5/§10·부록, README"
```

- [ ] **Step 12: (수동, 계획 범위 밖 후속) 실장비 스모크**

COM4(SSM)+COM13 연결 상태에서: `$env:SERIAL_NAMES="COM4=SSM"; uv run serial-mcp`(SERIAL_PORT 미설정 = 자동) → stderr에 "자동 스캔: USB 시리얼 2개"·"모니터 시작: SSM (COM4)" 확인 → 브라우저 `http://127.0.0.1:8743`에서 셀렉터로 두 보드 전환 → MCP 클라이언트로 `get_serial_status`(전 포트 배열)·`get_recent_logs(port="SSM")`·`clear_log_buffer`(전체) 확인 → dedup 룩백으로 `[IOc]`/`>> It doen't…` 교차가 접히는지 `get_log_buffer_info`의 total_received≫entries로 확인.

---

## Verification (end-to-end)

1. `uv run pytest` 전체 🟢 + `py -m compileall -q src` 무출력.
2. 계약 확인: 도구 이름 6종 불변(시그니처 확장만) / 기존 단일 포트 사용(`SERIAL_PORT=COM4`)이 무변경 동작(미지정 호환 경로) / `SERIAL_DEDUP=1`·`true` 구버전 의미 유지.
3. 실장비 스모크(Task 5 Step 12) — 사람 확인.

## Self-Review

**1. Spec coverage:** 설계 §2 표 — USB만 자동(T3 main+T2 auto_usb_ports)·1회 스캔(T3, 핫플러그 없음 로그)·port 지정 계약(T3 도구+_resolve_port)·병합 없음(어디에도 없음 ✅)·뷰어 전환만(T4 셀렉터)·별칭(T2+T3 라우팅/표기)·dedup 5(T1+T3 _parse_dedup) ✅. §3 환경변수 전부(T3 _load_config·_tee_path_for, T5 문서) ✅. §4 PortMonitor·_resolve_port(T3) ✅. §5 도구별 계약(T3 — status 배열/단일·clear 전체/지정·미지정 에러+ports·docstring dedup 주의) ✅. §6 룩백 명세(T1, 항목 위치 유지) ✅. §7 뷰어(`/api/ports`·`?port=`·셀렉터·1개면 숨김·전환 시 스트림 리셋) ✅. §8 테스트 전략 대응(T1~T4) ✅. §9 문서(T5) ✅. §10 비범위 침범 없음 ✅.
**2. Placeholder scan:** TBD/TODO/"적절히" 없음. 모든 코드 스텝 전체 코드 포함 ✅.
**3. Type consistency:** `parse_port_list→list[tuple[str,Optional[int]]]`·`auto_usb_ports→list[str]`·`parse_names→dict`·`name_for`·`label`이 T2 정의 = T3 사용처와 일치. `PortMonitor(port,name,buffer,feed,reader)`+`.label`이 T3 정의 = test_tools `make_monitor`·뷰어 콜백과 일치. `ViewerServer(ports_info, feed_for, buffer_info(port), status_info, port)`가 T4 정의 = T3 main() 사용처와 일치(주의 박스로 순서 의존 명시). `_resolve_port` 반환 `(mon, err)` 패턴이 6개 도구에서 동일. `info()["dedup"]`=윈도 int가 T1 = T3 도구 docstring과 일치. JS `currentPort`/`connectStream`/`initPorts`가 (4-3)~(4-6)에서 일관 ✅.
**4. 주의:** Task 3과 4는 병렬이지만 **전체 pytest는 둘 다 끝난 뒤에만** 의미 있음(중간엔 상대 영역 테스트가 깨져 있는 게 정상 — 각자 소유 테스트만 돌릴 것). 예상 통과 수는 어림 — 실행 출력으로 확정.
