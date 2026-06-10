# serial-mcp 코어 테스트 스위트 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 기존 `serial_mcp` 코어(ring_buffer + 6개 MCP 도구 + SerialReader)의 현재 동작을 단위 테스트로 고정하고, 무한 I/O 루프에 묶여 테스트 불가능하던 라인처리·설정로딩 로직을 작은 순수 단위로 추출해 함께 검증한다.

**Architecture:** 구현은 이미 스캐폴딩돼 있다(`SPEC.md` §부록). 따라서 두 종류의 테스트를 쓴다 — (1) **특성화 테스트**(characterization): `ring_buffer.py`와 6개 도구는 코드를 바꾸지 않고 현재 동작을 SPEC §3/§4/§5에 비추어 고정한다(처음부터 🟢 통과 기대). (2) **TDD 추출**(red→green): `SerialReader._ingest()`와 `_load_config()`는 지금 존재하지 않으므로, 실패 테스트 먼저 → 기계적(behavior-preserving) 추출로 통과시킨다. 이로써 실시리얼·블로킹 없이 라인처리/환경변수 계약을 테스트할 수 있고, 클린 아키텍처(I/O ↔ 순수로직 분리, SPEC §2)도 한 걸음 진전한다.

**Tech Stack:** Python 3.10+ / `pytest>=8.1.1`(dev 의존성) / uv 0.11 / 의존성은 `mcp[cli]`·`pyserial`만. 테스트는 `monkeypatch`로 모듈 전역·`list_ports.comports`·`os.environ`을 주입한다. 시리얼 하드웨어·MCP 클라이언트 불필요.

**검증 사실(사전 조사 완료):**
- FastMCP `@mcp.tool()`는 **원본 함수를 그대로 반환** → `from serial_mcp.server import get_recent_logs; get_recent_logs(lines=5)`로 직접 호출 가능. 도구는 모듈 전역 `_buffer`/`_reader`/`_config`를 읽으므로 `monkeypatch.setattr(srv, "_buffer", buf)`로 주입한다.
- `mcp[cli]`는 pytest를 끌어오지 않는다 → dev 의존성으로 명시 추가.
- `py`(3.14)/`uv`(0.11)만 사용(이 PC의 `python`은 Windows Store 별칭이라 작동 안 함 — CLAUDE.md).

**병렬 실행(ultracode Workflow) 의존성 그래프:**
- **Task 0**(pytest 셋업)이 선행. 이후:
  - **Task 1**(test_ring_buffer.py) ∥ **Task 4**(test_tools.py) — 새 파일만 추가, server.py/ring_buffer.py 미변경 → 완전 병렬.
  - **Task 2 → Task 3** — 둘 다 `src/serial_mcp/server.py`를 편집하므로 **순차**(같은 파일 충돌 방지). worktree 격리를 쓰면 병렬 가능하나, 변경이 작아 순차가 단순·안전.
- **Task 5**(전체 검증)는 마지막. 각 에이전트에 이 계획서 전체를 맥락으로 전달한다(서브에이전트는 대화 맥락을 상속하지 않음 — CLAUDE.md §개발 워크플로 3).

**특성화 테스트 트리아지 규칙:** Task 1·4의 테스트가 🔴이면 추측 금지. 현재 코드가 SPEC를 위반(→ 코드가 정답이 아님, 코드 수정 후 보고)인지, 테스트 기대가 틀렸는지(→ 테스트 수정)를 SPEC §3/§4/§5와 대조해 판별한다(systematic-debugging). CLAUDE.md "문서–코드 일치 유지"의 (A)/(B) 판정과 동일.

---

## File Structure

| 파일 | 책임 | 변경 |
|---|---|---|
| `pyproject.toml` | dev 의존성·pytest 설정 | 수정(Task 0) |
| `tests/test_smoke.py` | 패키지 import·하네스 검증 | 생성(Task 0) |
| `tests/test_ring_buffer.py` | `LineBuffer`/`LogEntry`/`_fmt_ts` 순수 로직(특성화) | 생성(Task 1) |
| `src/serial_mcp/server.py` | `_ingest()`·`_load_config()`·`_env_int()` 추출, `_run()`·`main()` 재배선 | 수정(Task 2, Task 3) |
| `tests/test_serial_reader.py` | `SerialReader._ingest()` 라인처리 | 생성(Task 2) |
| `tests/test_config.py` | `_load_config()`·`_env_int()` 환경변수 계약 | 생성(Task 3) |
| `tests/test_tools.py` | 6개 MCP 도구 계약(전역 주입) | 생성(Task 4) |

> `tests/__init__.py`·`conftest.py`는 만들지 않는다(pytest는 `pythonpath=["src"]` ini로 패키지를 찾고, fixture는 `test_tools.py` 안에 둔다).

---

## Task 0: pytest 하네스 셋업

**Files:**
- Modify: `pyproject.toml`
- Create: `tests/test_smoke.py`

- [ ] **Step 1: `pyproject.toml`에 dev 의존성·pytest 설정 추가**

파일 끝(`[tool.hatch.build.targets.wheel]` 블록 다음)에 아래 두 블록을 덧붙인다:

```toml
[dependency-groups]
dev = ["pytest>=8.1.1"]

[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]
addopts = "-q"
```

- [ ] **Step 2: import 스모크 테스트 작성**

`tests/test_smoke.py` 전체:

```python
"""pytest 하네스·import 경로 검증용 최소 스모크."""


def test_package_imports():
    import serial_mcp

    assert serial_mcp.__version__


def test_core_modules_import():
    from serial_mcp.ring_buffer import LineBuffer  # noqa: F401
    from serial_mcp.server import get_recent_logs  # noqa: F401
```

- [ ] **Step 3: dev 의존성 설치 후 스모크 실행**

```powershell
uv sync
uv run pytest tests/test_smoke.py
```
Expected: `2 passed`. (실패하면 `pythonpath=["src"]` ini와 `src/serial_mcp/__init__.py` 존재를 먼저 확인.)

- [ ] **Step 4: 커밋**

```powershell
git add pyproject.toml tests/test_smoke.py
git commit -m "test: pytest 하네스 셋업(dev 의존성·pythonpath·스모크)"
```

---

## Task 1: ring_buffer 순수 로직 특성화 테스트

**성격:** 특성화(코드 미변경, 처음부터 🟢 기대). `ring_buffer.py`의 dedup·필터·ring eviction·query·info·clear·동시성을 SPEC §3/§4·§2(동시성)에 비추어 고정한다.

**Files:**
- Create: `tests/test_ring_buffer.py`

- [ ] **Step 1: 테스트 파일 작성**

`tests/test_ring_buffer.py` 전체:

```python
"""LineBuffer/LogEntry/_fmt_ts 순수 로직 특성화 테스트.

타임스탬프를 명시적으로 주입(add(text, ts))해 결정적으로 검증한다.
SPEC §3(타임스탬프·ring), §4(dedup·필터), §2(동시성 Lock)을 고정한다.
"""

import re
import threading
from datetime import datetime

import pytest

from serial_mcp.ring_buffer import LineBuffer, LogEntry, _fmt_ts

BASE = datetime(2026, 6, 9, 14, 0, 0, 0)


# ---- _fmt_ts / LogEntry.render ----

def test_fmt_ts_millisecond_format():
    assert _fmt_ts(datetime(2026, 6, 9, 14, 2, 17, 123456)) == "14:02:17.123"


def test_logentry_render_single_has_no_repeat_suffix():
    e = LogEntry(text="hello", first_ts=BASE, last_ts=BASE)
    assert e.render() == "[14:00:00.000] hello"


def test_logentry_render_folded_shows_repeat_count_and_span():
    e = LogEntry(text="tick", first_ts=BASE, last_ts=datetime(2026, 6, 9, 14, 0, 5, 0), count=3)
    assert e.render() == "[14:00:00.000] tick  (3회 반복, 14:00:00~14:00:05)"


# ---- add / get_recent ----

def test_add_and_get_recent_returns_rendered_lines():
    buf = LineBuffer(maxlen=10, dedup=False)
    assert buf.add("boot ok", BASE) is True
    assert buf.get_recent(10) == ["[14:00:00.000] boot ok"]


def test_get_recent_zero_or_negative_returns_empty():
    buf = LineBuffer(maxlen=10, dedup=False)
    buf.add("x", BASE)
    assert buf.get_recent(0) == []
    assert buf.get_recent(-1) == []


def test_get_recent_returns_tail_in_chronological_order():
    buf = LineBuffer(maxlen=10, dedup=False)
    for i in range(5):
        buf.add(f"line{i}", BASE)
    assert buf.get_recent(2) == ["[14:00:00.000] line3", "[14:00:00.000] line4"]


# ---- dedup (SPEC §4.2) ----

def test_dedup_folds_consecutive_identical_lines():
    buf = LineBuffer(maxlen=10, dedup=True)
    assert buf.add("tick", BASE) is True
    assert buf.add("tick", datetime(2026, 6, 9, 14, 0, 1, 0)) is False
    assert buf.add("tick", datetime(2026, 6, 9, 14, 0, 2, 0)) is False
    lines = buf.get_recent(10)
    assert lines == ["[14:00:00.000] tick  (3회 반복, 14:00:00~14:00:02)"]
    info = buf.info()
    assert info["total_received"] == 3
    assert info["total_stored"] == 1


def test_dedup_breaks_on_different_line_then_starts_new_group():
    buf = LineBuffer(maxlen=10, dedup=True)
    buf.add("A", BASE)
    buf.add("A", BASE)   # 접힘
    buf.add("B", BASE)   # 묶음 종료
    buf.add("A", BASE)   # 간격 후 재등장 → 새 묶음(첫 묶음에 합쳐지지 않음)
    lines = buf.get_recent(10)
    assert len(lines) == 3
    assert "(2회 반복" in lines[0]
    assert lines[1].endswith("B")
    assert lines[2].endswith("A")
    assert "회 반복" not in lines[2]


def test_dedup_disabled_keeps_every_line():
    buf = LineBuffer(maxlen=10, dedup=False)
    for _ in range(3):
        assert buf.add("tick", BASE) is True
    assert len(buf.get_recent(10)) == 3


# ---- 수집 필터 (SPEC §4.1) ----

def test_exclude_filter_drops_matching_lines():
    buf = LineBuffer(maxlen=10, dedup=False, exclude=r"DEBUG")
    assert buf.add("INFO ok", BASE) is True
    assert buf.add("DEBUG noise", BASE) is False
    assert buf.get_recent(10) == ["[14:00:00.000] INFO ok"]


def test_include_filter_keeps_only_matching_lines():
    buf = LineBuffer(maxlen=10, dedup=False, include=r"ERROR")
    assert buf.add("INFO ok", BASE) is False
    assert buf.add("ERROR boom", BASE) is True
    assert buf.get_recent(10) == ["[14:00:00.000] ERROR boom"]


def test_exclude_takes_precedence_over_include():
    buf = LineBuffer(maxlen=10, dedup=False, include=r"msg", exclude=r"secret")
    assert buf.add("secret msg", BASE) is False   # include 매칭이어도 exclude 우선
    assert buf.add("public msg", BASE) is True
    assert buf.get_recent(10) == ["[14:00:00.000] public msg"]


# ---- ring eviction (SPEC §3) ----

def test_ring_buffer_evicts_oldest_beyond_maxlen():
    buf = LineBuffer(maxlen=3, dedup=False)
    for i in range(5):
        buf.add(f"line{i}", BASE)
    lines = buf.get_recent(100)
    assert len(lines) == 3
    assert lines[0].endswith("line2")
    assert lines[-1].endswith("line4")


# ---- query (SPEC §5) ----

def test_query_matches_regex_and_returns_chronological():
    buf = LineBuffer(maxlen=10, dedup=False)
    buf.add("ERROR one", BASE)
    buf.add("info", BASE)
    buf.add("ERROR two", BASE)
    assert buf.query(r"ERROR") == ["[14:00:00.000] ERROR one", "[14:00:00.000] ERROR two"]


def test_query_max_results_keeps_most_recent_matches():
    buf = LineBuffer(maxlen=10, dedup=False)
    for i in range(5):
        buf.add(f"ERROR {i}", BASE)
    assert buf.query(r"ERROR", max_results=2) == ["[14:00:00.000] ERROR 3", "[14:00:00.000] ERROR 4"]


def test_query_invalid_regex_raises_re_error():
    buf = LineBuffer(maxlen=10, dedup=False)
    with pytest.raises(re.error):
        buf.query("[")


# ---- info / clear ----

def test_info_reports_capacity_and_endpoints():
    buf = LineBuffer(maxlen=5, dedup=True)
    buf.add("first", BASE)
    buf.add("last", BASE)
    info = buf.info()
    assert info["entries"] == 2
    assert info["capacity"] == 5
    assert info["oldest"].endswith("first")
    assert info["newest"].endswith("last")
    assert info["dedup"] is True


def test_info_empty_buffer_has_none_endpoints():
    info = LineBuffer(maxlen=5).info()
    assert info["entries"] == 0
    assert info["oldest"] is None
    assert info["newest"] is None


def test_clear_empties_and_returns_prior_count():
    buf = LineBuffer(maxlen=10, dedup=False)
    buf.add("a", BASE)
    buf.add("b", BASE)
    assert buf.clear() == 2
    assert buf.get_recent(10) == []
    assert buf.clear() == 0


# ---- 동시성 (SPEC §2) ----

def test_concurrent_adds_are_thread_safe():
    buf = LineBuffer(maxlen=100_000, dedup=False)

    def worker(tid: int) -> None:
        for i in range(1000):
            buf.add(f"t{tid}-{i}", BASE)   # 모두 고유 → dedup 무관, 전부 저장

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    info = buf.info()
    assert info["total_received"] == 8000   # Lock 하에 손실 없음
    assert info["total_stored"] == 8000
    assert info["entries"] == 8000
```

- [ ] **Step 2: 실행 — 전부 통과(특성화) 기대**

```powershell
uv run pytest tests/test_ring_buffer.py
```
Expected: `20 passed`. 🔴가 나오면 위 "특성화 테스트 트리아지 규칙"에 따라 코드/테스트 중 무엇이 SPEC에 맞는지 판별 후 처리.

- [ ] **Step 3: 커밋**

```powershell
git add tests/test_ring_buffer.py
git commit -m "test: ring_buffer 순수 로직 특성화 테스트 추가"
```

---

## Task 2: SerialReader `_ingest()` 추출 + 라인처리 테스트 (TDD red→green)

**성격:** TDD. 무한 블로킹 루프 `_run()`에 묶인 "한 줄 처리"(decode/rstrip/buffer.add/tee)를 `_ingest()`로 추출해 실시리얼 없이 테스트한다. 추출은 기계적(behavior-preserving).

**Files:**
- Create: `tests/test_serial_reader.py`
- Modify: `src/serial_mcp/server.py` (`SerialReader._run()` 내부 → `_ingest()` 추출)

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_serial_reader.py` 전체:

```python
"""SerialReader._ingest() — 무한 I/O 루프에서 분리한 '한 줄 처리' 단위 테스트.

실제 시리얼 포트 없이 디코드 정책·개행 제거·tee 타임스탬프 형식(SPEC §3)을 고정한다.
"""

import io
from datetime import datetime

from serial_mcp.ring_buffer import LineBuffer
from serial_mcp.server import SerialReader

BASE = datetime(2026, 6, 9, 14, 0, 0, 0)


def _make_reader(buf: LineBuffer, tee=None) -> SerialReader:
    r = SerialReader(port="COM_TEST", baud=115200, buffer=buf)
    r._tee = tee   # 실제 파일 대신 StringIO 주입(또는 None)
    return r


def test_ingest_decodes_and_strips_eol_then_stores():
    buf = LineBuffer(maxlen=10, dedup=False)
    _make_reader(buf)._ingest(b"boot ok\r\n", BASE)
    assert buf.get_recent(10) == ["[14:00:00.000] boot ok"]


def test_ingest_replaces_invalid_utf8_without_crashing():
    buf = LineBuffer(maxlen=10, dedup=False)
    _make_reader(buf)._ingest(b"\xff\xfe bad\n", BASE)   # 예외 없이 저장돼야 함
    assert buf.info()["entries"] == 1


def test_ingest_writes_to_tee_with_full_datetime_stamp():
    buf = LineBuffer(maxlen=10, dedup=False)
    tee = io.StringIO()
    _make_reader(buf, tee=tee)._ingest(b"hello\n", datetime(2026, 6, 9, 14, 2, 17, 123000))
    assert tee.getvalue() == "[2026-06-09 14:02:17.123] hello\n"


def test_ingest_without_tee_only_buffers():
    buf = LineBuffer(maxlen=10, dedup=False)
    _make_reader(buf, tee=None)._ingest(b"x\n", BASE)   # _tee is None → 크래시 없음
    assert buf.info()["entries"] == 1
```

- [ ] **Step 2: 실행 — 실패 확인**

```powershell
uv run pytest tests/test_serial_reader.py
```
Expected: FAIL — `AttributeError: 'SerialReader' object has no attribute '_ingest'`.

- [ ] **Step 3: `_ingest()` 추출 + `_run()` 재배선**

`src/serial_mcp/server.py`의 `SerialReader` 클래스에 메서드를 추가한다(`_open` 메서드 바로 위, 또는 `_run` 바로 아래 — 클래스 내부면 무방):

```python
    def _ingest(self, raw: bytes, ts: datetime) -> None:
        """수신 바이트 한 줄을 디코드·정리해 버퍼에 적재하고, tee가 열렸으면 함께 기록.

        무한 I/O 루프(_run)에서 분리한 '한 줄 처리' 단위 — 실제 시리얼 없이 단위
        테스트할 수 있다. 디코드(utf-8/replace)·개행 제거·tee 타임스탬프 형식을
        여기에 고정한다(SPEC §3).
        """
        text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        self.buffer.add(text, ts)
        if self._tee is not None:
            try:
                stamp = ts.strftime("%Y-%m-%d %H:%M:%S.") + f"{ts.microsecond // 1000:03d}"
                self._tee.write(f"[{stamp}] {text}\n")
            except Exception as e:  # noqa: BLE001
                _log(f"tee 기록 실패: {e}")
```

그리고 `_run()` 끝의 기존 블록을 교체한다. **기존:**

```python
            if not raw:
                continue  # timeout — 수신 데이터 없음
            ts = datetime.now()
            text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            self.buffer.add(text, ts)
            if self._tee is not None:
                try:
                    stamp = ts.strftime("%Y-%m-%d %H:%M:%S.") + f"{ts.microsecond // 1000:03d}"
                    self._tee.write(f"[{stamp}] {text}\n")
                except Exception as e:  # noqa: BLE001
                    _log(f"tee 기록 실패: {e}")
```

**교체 후:**

```python
            if not raw:
                continue  # timeout — 수신 데이터 없음
            self._ingest(raw, datetime.now())
```

- [ ] **Step 4: 실행 — 통과 확인 + 문법 검증**

```powershell
uv run pytest tests/test_serial_reader.py
py -m compileall -q src
```
Expected: `4 passed`, compileall 무출력(오류 없음).

- [ ] **Step 5: 커밋**

```powershell
git add src/serial_mcp/server.py tests/test_serial_reader.py
git commit -m "refactor: SerialReader 라인처리 _ingest 추출 + 단위 테스트"
```

---

## Task 3: `_load_config()`/`_env_int()` 추출 + 환경변수 계약 테스트 (TDD red→green)

**성격:** TDD. `main()`에 인라인된 환경변수→설정 파싱을 부작용 없는 순수 함수 `_load_config(env)`로 추출하고, `_env_int`가 주입된 env 매핑을 받도록 시그니처를 바꾼다. SPEC §3(baud 기본 115200)·§4(필터)·README 환경변수표(SERIAL_DEDUP 진릿값)를 테스트로 고정한다.

**Files:**
- Create: `tests/test_config.py`
- Modify: `src/serial_mcp/server.py` (`_env_int` 시그니처 변경, `_load_config` 신설, `main()` 재배선, `typing.Mapping` import)

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_config.py` 전체:

```python
"""_load_config()/_env_int() — 환경변수 계약 고정(SPEC §3/§4, README 환경변수표).

env를 인자로 주입하는 순수 함수라 os.environ 변경 없이 결정적으로 검증한다.
"""

import pytest

from serial_mcp.server import _env_int, _load_config


# ---- _env_int ----

def test_env_int_parses_valid():
    assert _env_int({"X": "9600"}, "X", 115200) == 9600


def test_env_int_missing_returns_default():
    assert _env_int({}, "X", 115200) == 115200


def test_env_int_blank_returns_default():
    assert _env_int({"X": "  "}, "X", 115200) == 115200


def test_env_int_invalid_returns_default():
    assert _env_int({"X": "fast"}, "X", 115200) == 115200


# ---- _load_config ----

def test_load_config_defaults_when_empty():
    assert _load_config({}) == {
        "port": "", "baud": 115200, "tee": None, "exclude": None,
        "include": None, "maxlen": 2000, "dedup": True,
    }


def test_load_config_reads_all_vars():
    cfg = _load_config({
        "SERIAL_PORT": "COM4", "SERIAL_BAUD": "9600", "SERIAL_TEE": "log.txt",
        "SERIAL_EXCLUDE": "DEBUG", "SERIAL_INCLUDE": "ERROR",
        "SERIAL_BUFFER_LINES": "500", "SERIAL_DEDUP": "0",
    })
    assert cfg == {
        "port": "COM4", "baud": 9600, "tee": "log.txt", "exclude": "DEBUG",
        "include": "ERROR", "maxlen": 500, "dedup": False,
    }


def test_load_config_strips_port_whitespace():
    assert _load_config({"SERIAL_PORT": "  COM4  "})["port"] == "COM4"


@pytest.mark.parametrize(
    "val,expected",
    [
        ("0", False), ("false", False), ("FALSE", False), ("no", False),
        ("off", False), ("1", True), ("true", True), ("yes", True), ("", True),
    ],
)
def test_load_config_dedup_truthiness(val, expected):
    assert _load_config({"SERIAL_DEDUP": val})["dedup"] is expected
```

- [ ] **Step 2: 실행 — 실패 확인**

```powershell
uv run pytest tests/test_config.py
```
Expected: FAIL — `ImportError: cannot import name '_load_config'`.

- [ ] **Step 3: import 수정 + `_env_int` 시그니처 변경 + `_load_config` 신설 + `main()` 재배선**

(3-1) import 줄 변경. **기존:** `from typing import Optional` → **변경:** `from typing import Mapping, Optional`

(3-2) `_env_int`를 env 주입형으로 교체. **기존 전체:**

```python
def _env_int(name: str, default: int) -> int:
    """정수 환경변수 파싱 — 미설정/빈값/오류 시 기본값."""
    v = os.environ.get(name)
    if v is None or v.strip() == "":
        return default
    try:
        return int(v)
    except ValueError:
        _log(f"환경변수 {name}={v!r} 정수 변환 실패 → 기본값 {default} 사용")
        return default
```

**교체 후(시그니처에 env 추가 + `_load_config` 신설):**

```python
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


def _load_config(env: Mapping[str, str]) -> dict:
    """환경변수 매핑에서 서버 설정을 파싱해 dict로 반환(부작용 없음, 순수 함수).

    main()이 이 결과로 LineBuffer/SerialReader를 구성한다. I/O·스레드 시작과
    분리돼 있어 환경변수 계약(SPEC §3/§4)을 단독 테스트할 수 있다.
    """
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
    }
```

(3-3) `main()` 상단의 환경변수 파싱 블록을 `_load_config` 호출로 교체. **기존:**

```python
    port = os.environ.get("SERIAL_PORT", "").strip()
    baud = _env_int("SERIAL_BAUD", 115200)
    tee = os.environ.get("SERIAL_TEE", "").strip() or None
    exclude = os.environ.get("SERIAL_EXCLUDE", "").strip() or None
    include = os.environ.get("SERIAL_INCLUDE", "").strip() or None
    maxlen = _env_int("SERIAL_BUFFER_LINES", 2000)
    dedup = os.environ.get("SERIAL_DEDUP", "1").strip().lower() not in ("0", "false", "no", "off")

    _config = {"port": port, "baud": baud, "tee": tee, "exclude": exclude, "include": include}
    _buffer = LineBuffer(maxlen=maxlen, dedup=dedup, exclude=exclude, include=include)

    if not port:
        _log("경고: SERIAL_PORT 미설정 — 서버는 뜨지만 리더는 시작하지 않는다. "
             "list_serial_ports 로 포트를 확인하고 환경변수를 설정하라.")
    else:
        _reader = SerialReader(port=port, baud=baud, buffer=_buffer, tee_path=tee)
        _reader.start()

    _log(f"시작 (port={port or '(미설정)'}, baud={baud}, dedup={dedup}, "
         f"buffer={maxlen}, tee={tee or '없음'})")
```

**교체 후:**

```python
    cfg = _load_config(os.environ)
    _config = {
        "port": cfg["port"], "baud": cfg["baud"], "tee": cfg["tee"],
        "exclude": cfg["exclude"], "include": cfg["include"],
    }
    _buffer = LineBuffer(
        maxlen=cfg["maxlen"], dedup=cfg["dedup"],
        exclude=cfg["exclude"], include=cfg["include"],
    )

    if not cfg["port"]:
        _log("경고: SERIAL_PORT 미설정 — 서버는 뜨지만 리더는 시작하지 않는다. "
             "list_serial_ports 로 포트를 확인하고 환경변수를 설정하라.")
    else:
        _reader = SerialReader(
            port=cfg["port"], baud=cfg["baud"], buffer=_buffer, tee_path=cfg["tee"],
        )
        _reader.start()

    _log(f"시작 (port={cfg['port'] or '(미설정)'}, baud={cfg['baud']}, dedup={cfg['dedup']}, "
         f"buffer={cfg['maxlen']}, tee={cfg['tee'] or '없음'})")
```

- [ ] **Step 4: 실행 — 통과 확인 + 문법 검증**

```powershell
uv run pytest tests/test_config.py
py -m compileall -q src
```
Expected: `16 passed`, compileall 무출력.

- [ ] **Step 5: 커밋**

```powershell
git add src/serial_mcp/server.py tests/test_config.py
git commit -m "refactor: 설정 로딩 _load_config 추출 + env 파싱 테스트"
```

---

## Task 4: MCP 도구 6종 계약 테스트

**성격:** 특성화(코드 미변경). 6개 도구를 직접 호출(`@mcp.tool()`이 원본 함수 반환)하고 전역 `_buffer`/`_reader`/`_config`·`list_ports.comports`를 `monkeypatch`로 주입한다. SPEC §5 반환 계약(`status`/`message` + 구조)을 고정한다.

**Files:**
- Create: `tests/test_tools.py`

- [ ] **Step 1: 테스트 파일 작성**

`tests/test_tools.py` 전체:

```python
"""MCP 도구 6종 계약 테스트(SPEC §5). @mcp.tool()은 원본 함수를 반환하므로 직접 호출.

도구는 모듈 전역 _buffer/_reader/_config를 읽는다 → monkeypatch로 주입한다.
"""

from datetime import datetime
from types import SimpleNamespace

import pytest

import serial_mcp.server as srv
from serial_mcp.ring_buffer import LineBuffer

BASE = datetime(2026, 6, 9, 14, 0, 0, 0)


@pytest.fixture
def buffer(monkeypatch):
    """전역 _buffer에 빈 LineBuffer를 주입하고 그 핸들을 돌려준다(테스트 후 자동 복원)."""
    buf = LineBuffer(maxlen=100, dedup=True)
    monkeypatch.setattr(srv, "_buffer", buf)
    monkeypatch.setattr(srv, "_config", {"port": "COM_TEST", "baud": 115200, "tee": None})
    return buf


# ---- list_serial_ports ----

def test_list_serial_ports_maps_fields(monkeypatch):
    fake = SimpleNamespace(
        device="COM4", description="USB-SERIAL CH343",
        hwid="USB VID:PID=1A86:55D3", vid=0x1A86, pid=0x55D3,
        manufacturer="wch.cn", serial_number=None,
    )
    monkeypatch.setattr(srv.list_ports, "comports", lambda: [fake])
    monkeypatch.setattr(srv, "_config", {"port": "COM4"})
    out = srv.list_serial_ports()
    assert out["status"] == "ok"
    assert out["configured_port"] == "COM4"
    assert out["ports"][0]["device"] == "COM4"
    assert out["ports"][0]["vid"] == 0x1A86


# ---- get_serial_status ----

def test_get_serial_status_without_reader_reports_error(monkeypatch):
    monkeypatch.setattr(srv, "_reader", None)
    monkeypatch.setattr(srv, "_config", {"port": ""})
    out = srv.get_serial_status()
    assert out["status"] == "error"
    assert out["connected"] is False


def test_get_serial_status_with_connected_reader(monkeypatch):
    fake_reader = SimpleNamespace(
        connected=True, port="COM4", baud=115200, last_error=None,
        opened_at=datetime(2026, 6, 9, 14, 0, 0, 0),
    )
    monkeypatch.setattr(srv, "_reader", fake_reader)
    monkeypatch.setattr(srv, "_config", {"tee": None})
    out = srv.get_serial_status()
    assert out["status"] == "ok"
    assert out["connected"] is True
    assert out["port"] == "COM4"
    assert out["baud"] == 115200
    assert out["opened_at"] == "2026-06-09T14:00:00"


# ---- get_recent_logs ----

def test_get_recent_logs_returns_buffer_lines(buffer):
    buffer.add("boot ok", BASE)
    out = srv.get_recent_logs(lines=5)
    assert out["status"] == "ok"
    assert out["count"] == 1
    assert out["lines"] == ["[14:00:00.000] boot ok"]


def test_get_recent_logs_without_buffer_errors(monkeypatch):
    monkeypatch.setattr(srv, "_buffer", None)
    out = srv.get_recent_logs()
    assert out["status"] == "error"
    assert out["count"] == 0


# ---- query_serial_logs ----

def test_query_serial_logs_matches(buffer):
    buffer.add("ERROR boom", BASE)
    buffer.add("info", BASE)
    out = srv.query_serial_logs(r"ERROR")
    assert out["status"] == "ok"
    assert out["count"] == 1
    assert out["pattern"] == "ERROR"
    assert out["lines"][0].endswith("ERROR boom")


def test_query_serial_logs_invalid_regex_returns_error(buffer):
    out = srv.query_serial_logs("[")
    assert out["status"] == "error"
    assert "정규식" in out["message"]
    assert out["count"] == 0


# ---- get_log_buffer_info ----

def test_get_log_buffer_info_reports_status_and_counts(buffer):
    buffer.add("x", BASE)
    out = srv.get_log_buffer_info()
    assert out["status"] == "ok"
    assert out["entries"] == 1
    assert out["capacity"] == 100


# ---- clear_log_buffer ----

def test_clear_log_buffer_empties_and_reports(buffer):
    buffer.add("a", BASE)
    out = srv.clear_log_buffer()
    assert out["status"] == "ok"
    assert out["cleared"] == 1
    assert srv.get_recent_logs()["count"] == 0


def test_clear_log_buffer_without_buffer_errors(monkeypatch):
    monkeypatch.setattr(srv, "_buffer", None)
    out = srv.clear_log_buffer()
    assert out["status"] == "error"
    assert out["cleared"] == 0
```

- [ ] **Step 2: 실행 — 전부 통과(특성화) 기대**

```powershell
uv run pytest tests/test_tools.py
```
Expected: `10 passed`. 🔴면 트리아지 규칙 적용.

- [ ] **Step 3: 커밋**

```powershell
git add tests/test_tools.py
git commit -m "test: MCP 도구 6종 계약 테스트 추가"
```

---

## Task 5: 전체 스위트 검증 + 마무리

**Files:** (없음 — 검증·커밋만)

- [ ] **Step 1: 전체 테스트 실행**

```powershell
uv run pytest
```
Expected: `52 passed`(스모크 2 + ring_buffer 20 + serial_reader 4 + config 16 + tools 10 = 52). 합계는 실제 `uv run pytest` 출력으로 확정한다 — 테스트를 가감하면 이 숫자도 갱신. 모두 🟢여야 한다.

- [ ] **Step 2: 문법 검증**

```powershell
py -m compileall -q src
```
Expected: 무출력(오류 없음).

- [ ] **Step 3: (선택) 순수 로직 스모크 — 의존성 없이도 동작 확인**

```powershell
$env:PYTHONPATH="src"; py -c "from serial_mcp.ring_buffer import LineBuffer; b=LineBuffer(dedup=True); from datetime import datetime; t=datetime(2026,6,9,14,0,0,0); b.add('x',t); b.add('x',t); print(b.get_recent(5))"
```
Expected: `['[14:00:00.000] x  (2회 반복, 14:00:00~14:00:00)']`

- [ ] **Step 4: 최종 커밋(필요 시)**

직전 태스크들에서 이미 커밋됐다면 생략. 누락분이 있으면:

```powershell
git add -A
git commit -m "test: serial-mcp 코어 테스트 스위트 마무리"
```

---

## Verification (end-to-end)

1. **전체 테스트:** `uv run pytest` → 전부 🟢.
2. **문법:** `py -m compileall -q src` → 무출력.
3. **리팩토링 안전성:** Task 2·3의 추출은 behavior-preserving. `_ingest`/`_load_config` 테스트 + 기존 도구 테스트가 모두 통과하면 회귀 없음.
4. **(다음 단계, 수동) 실장비 스모크:** 테스트 🟢 확인 후 — `$env:SERIAL_PORT="COM4"; uv run serial-mcp` 실행 → MCP 클라이언트(Claude Code)에서 `list_serial_ports`로 COM4(CH343) 식별 → `clear_log_buffer` → 사람이 ESP32-S3 리셋 → `get_recent_logs`로 부팅 로그 회수. (SPEC §부록 테스트 장비 기준.)

## 다음 단계 (이 계획 범위 밖 — 구현 검증 후 진행)

사용자 합의: **패키징은 구현이 잘 작동하는지 확인된 뒤** 진행한다. 별도 계획으로 다룰 항목:
- **silotek-tools 플러그인**(별도 레포 `C:\Users\User\projects\silotek-tools`): `plugins/serial-mcp/.claude-plugin/plugin.json`(mcpServers·env 참조) + `plugins/serial-mcp/skills/serial-debugging/SKILL.md`(SPEC §9) + 루트 `marketplace.json` 항목 추가. 도구 이름·시그니처는 "안정 계약"(SPEC §6.1) — 이 테스트 스위트가 그 계약을 고정한다.
- **실장비 검증**: 위 Verification 4번(사람+하드웨어 수동 루프).
- **GitHub push**: `https://github.com/JOCOIN94/silotek-serial-mcp` 게시(uvx가 git에서 직접 실행하므로 push가 곧 배포).

---

## Self-Review

**1. Spec coverage:**
- §3 타임스탬프/ring/재연결: `_fmt_ts`·render·eviction 테스트 ✅ / 재연결 루프는 무한 I/O라 단위테스트 제외(다음 단계 실장비 스모크에서 확인).
- §4.1 exclude/include(+우선순위): ✅ Task 1.
- §4.2 dedup(접기/경계/표기/비활성): ✅ Task 1.
- §5 도구 6종 반환 계약: ✅ Task 4. query 정규식/ordering/max_results: ✅ Task 1·4.
- §2 동시성 Lock: ✅ Task 1 concurrent 테스트. stdout 금지·읽기전용: 구조상 유지(테스트 비대상).
- §3/§4 환경변수 계약(baud 기본·필터·dedup 진릿값·buffer_lines): ✅ Task 3.
- §3 tee 형식·decode 정책: ✅ Task 2.

**2. Placeholder scan:** 모든 단계에 실제 코드·정확한 명령·기대 출력 포함. "TBD"/"적절히 처리" 류 없음. ✅

**3. Type/이름 일관성:** `_ingest(self, raw: bytes, ts: datetime)`·`_load_config(env)`·`_env_int(env, name, default)`가 추출 정의와 호출부(`_run`·`main`)·테스트에서 동일. 도구 함수명(`get_recent_logs` 등)·반환 키(`status`/`message`/`count`/`lines`/`cleared`/`entries`/`capacity`)가 `server.py` 현행과 일치. dedup 표기 `(N회 반복, …)`(두 칸 들여쓰기) 기대 문자열이 `LogEntry.render`와 일치. ✅

**4. 테스트 합계:** 스모크 2 + ring_buffer 20 + serial_reader 4 + config 16 + tools 10 = **52**. config의 dedup 진릿값은 9-케이스 parametrize라 9로 집계. 실제 합계는 `uv run pytest` 출력으로 확정하고, 테스트를 가감하면 이 숫자도 갱신.
