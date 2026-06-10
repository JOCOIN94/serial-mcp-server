# 핫플러그(런타임 USB 포트 자동 감지) 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 서버 실행 중 새로 꽂힌 USB 시리얼 포트를 주기 스캔으로 감지해 모니터를 자동 추가한다(자동 스캔 모드 한정). 현재는 시작 시 1회 스캔뿐이라 장비를 나중에 꽂으면 서버 재시작이 필요하다.

**Architecture:** `server.py`에 (1) `SERIAL_HOTPLUG` 환경변수 파서, (2) main()의 모니터 생성 블록을 동작 보존 추출한 `_make_monitor()` 팩토리, (3) comports() 재스캔→신규 USB 포트 diff→모니터 등록·리더 시작을 수행하는 `_hotplug_scan_once()`, (4) 그것을 주기 호출하는 데몬 스레드 `_hotplug_loop()`를 추가한다. 전역 `_monitors`는 **copy-on-write**(새 dict를 만들어 전역 참조를 원자적으로 교체)로만 갱신해, 리더 스레드(`_autoname_check`)·도구 호출이 dict 순회 중 `RuntimeError: dictionary changed size during iteration`을 맞지 않게 한다. 포트가 사라져도 모니터는 제거하지 않는다(버퍼·tee 보존, 기존 SerialReader 재연결 루프가 복구 담당).

**Tech Stack:** Python 3.10+, pyserial(`list_ports.comports`), threading(stdlib), pytest. 새 의존성 없음.

**적용 조건(설계 결정):**
- 핫플러그 스캔은 **자동 스캔 모드(`SERIAL_PORT` 미설정)에서만** 돈다. 고정 목록 모드는 사용자가 포트를 못박은 것이고, 그 포트의 늦은 연결은 SerialReader의 3초 재연결 루프가 이미 처리한다.
- `SERIAL_HOTPLUG`: 기본 `5`(초 간격, 켜짐). `0`/`false`/`no`/`off` → 끔. 양수(소수 허용) → 간격(초). 해석 실패·0 이하 → 기본 5초(경고 로그). `_parse_web` 패턴을 따른다.
- 신규 포트의 보드레이트는 전역 `SERIAL_BAUD`(자동 스캔 모드엔 포트별 `@N` 오버라이드가 없음), 별칭은 `SERIAL_NAMES`(포트명/시리얼넘버 키)·`SERIAL_AUTONAME` 훅이 main()과 동일하게 적용된다.

**검증 명령(이 레포 공통):** 테스트는 `uv run pytest`(이 PC의 `python`은 Windows Store 별칭이라 불가, `py` 또는 `uv`만), 문법은 `py -m compileall -q src`.

**참고 파일:**
- `src/serial_mcp/server.py` — 모든 코드 변경이 이 파일. `main()` 599~659행(모니터 생성 1·2패스), `_parse_web` 549행(파서 패턴), `_autoname_check` 202행(dict 순회 지점).
- `src/serial_mcp/ports.py` — `auto_usb_ports`(VID 보유 USB만 필터, 변경 없음·재사용).
- `tests/test_config.py` — 환경변수 계약 테스트(여기에 `_parse_hotplug` 추가).
- `tests/test_tools.py` — `make_monitor` 헬퍼·monkeypatch 주입 패턴(새 테스트 파일이 따라할 표본).

---

### Task 1: `SERIAL_HOTPLUG` 환경변수 파서 + `_load_config` 키 추가

**Files:**
- Modify: `src/serial_mcp/server.py` (`_parse_web` 함수 뒤, `_load_config` 본문)
- Test: `tests/test_config.py`

- [x] **Step 1: 실패 테스트 작성** — `tests/test_config.py` 끝에 추가:

```python
# ---- SERIAL_HOTPLUG (핫플러그 스캔 간격) ----

def test_load_config_hotplug_default_on():
    assert _load_config({})["hotplug"] == 5.0


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "OFF"])
def test_load_config_hotplug_disabled(val):
    assert _load_config({"SERIAL_HOTPLUG": val})["hotplug"] is None


def test_load_config_hotplug_custom_interval():
    assert _load_config({"SERIAL_HOTPLUG": "10"})["hotplug"] == 10.0
    assert _load_config({"SERIAL_HOTPLUG": "2.5"})["hotplug"] == 2.5


@pytest.mark.parametrize("val", ["abc", "-3"])
def test_load_config_hotplug_invalid_falls_back(val):
    assert _load_config({"SERIAL_HOTPLUG": val})["hotplug"] == 5.0
```

그리고 기존 두 equality 테스트에 `hotplug` 키를 반영한다(키 추가로 깨지므로 같은 스텝에서 수정):

`test_load_config_defaults_when_empty`의 기대 dict에 `"hotplug": 5.0` 추가:

```python
def test_load_config_defaults_when_empty():
    assert _load_config({}) == {
        "ports": [], "names": {}, "autoname": [], "baud": 115200, "tee": None,
        "exclude": None, "include": None, "maxlen": 2000, "dedup": 5, "web": 8743,
        "hotplug": 5.0,
    }
```

`test_load_config_reads_all_vars`의 입력에 `"SERIAL_HOTPLUG": "10"`, 기대 dict에 `"hotplug": 10.0` 추가:

```python
def test_load_config_reads_all_vars():
    cfg = _load_config({
        "SERIAL_PORT": "COM4,COM13@9600", "SERIAL_NAMES": "COM4=SSM",
        "SERIAL_AUTONAME": "SB1=STM32",
        "SERIAL_BAUD": "57600", "SERIAL_TEE": "log.txt",
        "SERIAL_EXCLUDE": "DEBUG", "SERIAL_INCLUDE": "ERROR",
        "SERIAL_BUFFER_LINES": "500", "SERIAL_DEDUP": "0", "SERIAL_WEB": "9000",
        "SERIAL_HOTPLUG": "10",
    })
    assert cfg == {
        "ports": [("COM4", None), ("COM13", 9600)], "names": {"COM4": "SSM"},
        "autoname": [("SB1", "STM32")],
        "baud": 57600, "tee": "log.txt", "exclude": "DEBUG", "include": "ERROR",
        "maxlen": 500, "dedup": 0, "web": 9000, "hotplug": 10.0,
    }
```

- [x] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_config.py -q`
Expected: FAIL — `KeyError: 'hotplug'` (새 테스트 4건 + 수정한 equality 2건)

- [x] **Step 3: 최소 구현** — `src/serial_mcp/server.py`의 `_parse_web` 함수 정의 바로 뒤에 추가:

```python
def _parse_hotplug(env: Mapping[str, str]) -> Optional[float]:
    """SERIAL_HOTPLUG 파싱 — 핫플러그 스캔 간격(초). 기본 5(켜짐).

    0/false/no/off → 끔(None), 양수(소수 허용) → 간격. 자동 스캔 모드에서만
    의미가 있다(SERIAL_PORT 고정 목록 모드는 main()이 스캔 스레드를 띄우지 않음).
    """
    raw = env.get("SERIAL_HOTPLUG", "").strip().lower()
    if raw == "":
        return 5.0
    if raw in ("0", "false", "no", "off"):
        return None
    try:
        n = float(raw)
        if n > 0:
            return n
    except ValueError:
        pass
    _log(f"환경변수 SERIAL_HOTPLUG={raw!r} 해석 실패(양수 초 필요) → 기본 5초 사용")
    return 5.0
```

`_load_config` 반환 dict의 `"web"` 줄 아래에 추가:

```python
        "hotplug": _parse_hotplug(env),
```

- [x] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_config.py -q`
Expected: PASS (전건)

- [x] **Step 5: 커밋**

```bash
git add tests/test_config.py src/serial_mcp/server.py
git commit -m "feat: SERIAL_HOTPLUG 환경변수 파싱 — 핫플러그 스캔 간격(기본 5초, 0=끔)"
```

---

### Task 2: 모니터 생성 팩토리 `_make_monitor()` 추출 (동작 보존 리팩토링)

main()의 1패스 루프 본문(버퍼·피드·별칭·autoname 훅·리더 조립)을 모듈 함수로 추출한다. 핫플러그 스캔이 main()과 **동일한 조립 규칙**을 재사용하기 위함. TDD 원칙: 추출 전에 새 함수의 계약을 테스트로 먼저 고정한다.

**Files:**
- Modify: `src/serial_mcp/server.py` (`_tee_path_for` 뒤에 함수 신설, `main()` 1패스 루프 치환)
- Test: `tests/test_hotplug.py` (신규 파일)

- [x] **Step 1: 실패 테스트 작성** — `tests/test_hotplug.py` 신규 작성:

```python
"""핫플러그 — 모니터 팩토리(_make_monitor)·런타임 스캔(_hotplug_scan_once)·루프.

실제 시리얼 I/O 없이 검증한다: SerialReader 를 StubReader 로 monkeypatch 해
포트 열기·스레드 기동을 차단하고, comports() 는 SimpleNamespace 목록을 주입.
패턴은 tests/test_tools.py(전역 monkeypatch 주입)와 동일.
"""

import threading
from types import SimpleNamespace

import pytest

import serial_mcp.server as srv


class StubReader:
    """SerialReader 대역 — 생성 인자만 기록하고 I/O·스레드는 만들지 않는다."""

    def __init__(self, port, baud, buffer, tee_path=None, feed=None, on_line=None, **kw):
        self.port, self.baud, self.buffer = port, baud, buffer
        self.tee_path, self.feed, self.on_line = tee_path, feed, on_line
        self.started = False
        self.connected = False
        self.last_error = None
        self.opened_at = None

    def start(self):
        self.started = True


BASE_CFG = {
    "ports": [], "names": {}, "autoname": [], "baud": 115200, "tee": None,
    "exclude": None, "include": None, "maxlen": 2000, "dedup": 5, "web": None,
    "hotplug": 5.0,
}


@pytest.fixture
def stub_reader(monkeypatch):
    monkeypatch.setattr(srv, "SerialReader", StubReader)


# ---- _make_monitor (main 1패스와 동일 조립 규칙) ----

def test_make_monitor_basic_assembly(stub_reader):
    cfg = {**BASE_CFG, "baud": 9600, "tee": "log.txt"}
    mon = srv._make_monitor("COM7", None, {}, cfg)
    assert mon.port == "COM7"
    assert mon.name is None
    assert mon.reader.baud == 9600
    assert mon.reader.tee_path == "log.COM7.txt"     # 별칭 없으면 포트명 태그
    assert mon.reader.on_line is None                # autoname 규칙 없음 → 훅 없음
    assert mon.reader.started is False               # 팩토리는 시작하지 않는다(2패스 분리)


def test_make_monitor_resolves_name_and_baud_override(stub_reader):
    cfg = {**BASE_CFG, "names": {"COM7": "SB2"}, "tee": "log.txt"}
    mon = srv._make_monitor("COM7", 57600, {}, cfg)
    assert mon.name == "SB2"
    assert mon.reader.baud == 57600                  # 포트별 오버라이드 우선
    assert mon.reader.tee_path == "log.SB2.txt"      # 별칭 태그


def test_make_monitor_resolves_name_by_serial_number(stub_reader):
    cfg = {**BASE_CFG, "names": {"5909024173": "SSM"}}
    mon = srv._make_monitor("COM9", None, {"COM9": "5909024173"}, cfg)
    assert mon.name == "SSM"


def test_make_monitor_hooks_autoname_only_when_unnamed(stub_reader, monkeypatch):
    monkeypatch.setattr(srv, "_autoname_rules", srv.compile_autoname([("SB1", r"STM32")]))
    named = srv._make_monitor("COM7", None, {}, {**BASE_CFG, "names": {"COM7": "SSM"}})
    unnamed = srv._make_monitor("COM8", None, {}, BASE_CFG)
    assert named.reader.on_line is None              # 명시 별칭 보유 → 훅 생략
    assert unnamed.reader.on_line is not None        # 무명 → autoname 훅 장착
    unnamed.reader.on_line(None, "***Send to the STM32")
    assert unnamed.name is None or unnamed.name == "SB1"   # 훅이 _autoname_check로 연결됨
```

> 마지막 assert 보충: `_autoname_check`는 전역 `_monitors`의 중복 이름을 검사한다. 이 테스트는 `_monitors`를 주입하지 않으므로(빈 dict 또는 기존 상태), 정확한 확정값 대신 "훅이 예외 없이 `_autoname_check`로 배선됐는가"를 본다. 확정 동작 자체는 `test_tools.py`의 autoname 테스트가 이미 고정하고 있다.

- [x] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_hotplug.py -q`
Expected: FAIL — `AttributeError: ... no attribute '_make_monitor'`

- [x] **Step 3: 구현 — 함수 추출** — `src/serial_mcp/server.py`의 `_tee_path_for` 함수 뒤에 신설:

```python
def _make_monitor(
    port: str,
    baud_override: Optional[int],
    sn_map: Mapping[str, Optional[str]],
    cfg: dict,
) -> PortMonitor:
    """포트 하나의 모니터 조립 — main() 기동과 핫플러그 추가가 공유하는 단일 규칙.

    버퍼·피드 생성, 별칭 해석(SERIAL_NAMES — 포트명/시리얼넘버 키), 무명 포트의
    autoname 훅 장착, tee 경로 산출까지. 리더 start()는 호출자가 등록을 끝낸 뒤
    수행한다(등록 전 시작 금지 — _autoname_check의 _monitors 순회와 race 방지).
    """
    baud = baud_override or cfg["baud"]
    name = name_for(port, sn_map.get(port.upper()), cfg["names"])
    buf = LineBuffer(maxlen=cfg["maxlen"], dedup=cfg["dedup"],
                     exclude=cfg["exclude"], include=cfg["include"])
    feed = RawFeed()
    mon = PortMonitor(port=port, name=name, buffer=buf, feed=feed, reader=None)
    on_line = None
    if _autoname_rules and name is None:   # 명시 별칭 없을 때만 자동 식별 후킹
        on_line = (lambda ts, text, m=mon: _autoname_check(m, text))
    mon.reader = SerialReader(port=port, baud=baud, buffer=buf,
                              tee_path=_tee_path_for(cfg["tee"], name or port),
                              feed=feed, on_line=on_line)
    return mon
```

`main()`의 1패스 루프를 이 함수 호출로 치환(기존 본문 삭제):

```python
    # 1패스: 모니터 전부 생성·등록 (리더 시작 전 — 리더 스레드의 _autoname_check가
    # _monitors를 순회하므로, 순회 중 dict 변경이 없도록 등록을 먼저 끝낸다)
    for port, baud_override in specs:
        if port.upper() in _monitors:
            _log(f"중복 포트 무시: {port}")
            continue
        _monitors[port.upper()] = _make_monitor(port, baud_override, sn_map, cfg)
```

- [x] **Step 4: 전체 테스트로 동작 보존 확인** (이 태스크는 리팩토링 — 신규 + 기존 전부)

Run: `uv run pytest -q`
Expected: PASS (전건 — 기존 58+개 + 신규 4개)

- [x] **Step 5: 커밋**

```bash
git add tests/test_hotplug.py src/serial_mcp/server.py
git commit -m "refactor: 모니터 조립을 _make_monitor()로 추출 — 기동·핫플러그 공유 규칙(동작 보존)"
```

---

### Task 3: 런타임 1회 스캔 `_hotplug_scan_once()`

**Files:**
- Modify: `src/serial_mcp/server.py` (`_make_monitor` 뒤)
- Test: `tests/test_hotplug.py`

- [ ] **Step 1: 실패 테스트 작성** — `tests/test_hotplug.py` 끝에 추가:

```python
# ---- _hotplug_scan_once (재스캔 → 신규 USB 포트만 모니터 추가) ----

def usb(device, sn=None):
    return SimpleNamespace(device=device, vid=0x1A86, pid=0x55D3, serial_number=sn)


def bt(device):
    return SimpleNamespace(device=device, vid=None, pid=None, serial_number=None)


@pytest.fixture
def scan_env(monkeypatch, stub_reader):
    """기존 모니터 1개(COM_A) + 설정 주입. comports 는 테스트별로 덮는다."""
    existing = srv._make_monitor("COM_A", None, {}, BASE_CFG)
    monkeypatch.setattr(srv, "_monitors", {"COM_A": existing})
    monkeypatch.setattr(srv, "_config", dict(BASE_CFG))
    return existing


def test_scan_adds_new_usb_port_and_starts_reader(monkeypatch, scan_env):
    monkeypatch.setattr(srv.list_ports, "comports",
                        lambda: [usb("COM_A"), usb("COM_B")])
    added = srv._hotplug_scan_once()
    assert added == ["COM_B"]
    assert "COM_B" in srv._monitors
    assert srv._monitors["COM_B"].reader.started is True


def test_scan_ignores_known_ports_case_insensitive(monkeypatch, scan_env):
    monkeypatch.setattr(srv.list_ports, "comports", lambda: [usb("com_a")])
    assert srv._hotplug_scan_once() == []
    assert len(srv._monitors) == 1


def test_scan_ignores_non_usb_ports(monkeypatch, scan_env):
    monkeypatch.setattr(srv.list_ports, "comports",
                        lambda: [usb("COM_A"), bt("COM_BT")])
    assert srv._hotplug_scan_once() == []


def test_scan_applies_serial_names_to_new_port(monkeypatch, scan_env):
    srv._config["names"] = {"SN777": "SB2"}
    monkeypatch.setattr(srv.list_ports, "comports",
                        lambda: [usb("COM_A"), usb("COM_C", sn="SN777")])
    srv._hotplug_scan_once()
    assert srv._monitors["COM_C"].name == "SB2"


def test_scan_replaces_dict_copy_on_write(monkeypatch, scan_env):
    """리더 스레드가 순회 중인 옛 dict 객체는 불변 — 전역 참조만 교체돼야 한다."""
    before = srv._monitors
    monkeypatch.setattr(srv.list_ports, "comports",
                        lambda: [usb("COM_A"), usb("COM_B")])
    srv._hotplug_scan_once()
    assert "COM_B" not in before                 # 옛 객체는 변형 금지
    assert srv._monitors is not before           # 새 dict 로 교체
    assert srv._monitors["COM_A"] is scan_env    # 기존 모니터는 동일 객체 유지(버퍼 보존)


def test_scan_noop_when_nothing_new(monkeypatch, scan_env):
    before = srv._monitors
    monkeypatch.setattr(srv.list_ports, "comports", lambda: [usb("COM_A")])
    assert srv._hotplug_scan_once() == []
    assert srv._monitors is before               # 변화 없으면 dict 교체도 없음
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_hotplug.py -q`
Expected: FAIL — `AttributeError: ... no attribute '_hotplug_scan_once'`

- [ ] **Step 3: 구현** — `src/serial_mcp/server.py`의 `_make_monitor` 뒤에 추가:

```python
def _hotplug_scan_once() -> list[str]:
    """핫플러그 1회 스캔 — 새 USB 시리얼 포트를 모니터에 추가하고 포트명 목록 반환.

    _monitors 는 copy-on-write 로만 갱신한다(새 dict 생성 → 전역 참조 원자 교체).
    리더 스레드(_autoname_check)·도구 호출이 옛 dict 를 순회 중이어도 안전하다.
    사라진 포트의 모니터는 제거하지 않는다 — 버퍼·tee 를 보존하고, 재연결은
    SerialReader 의 재시도 루프가 담당한다.
    """
    global _monitors
    com = list(list_ports.comports())
    fresh = [d for d in auto_usb_ports(com) if d.upper() not in _monitors]
    if not fresh:
        return []
    sn_map = {p.device.upper(): getattr(p, "serial_number", None) for p in com}
    added = [_make_monitor(d, None, sn_map, _config) for d in fresh]
    # 등록을 먼저 끝낸 뒤 리더 시작(main 의 1·2패스와 동일한 순서 보장)
    _monitors = {**_monitors, **{m.port.upper(): m for m in added}}
    for m in added:
        m.reader.start()
        _log(f"핫플러그: 모니터 추가 {m.label} @ {m.reader.baud}")
    return [m.port for m in added]
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_hotplug.py -q`
Expected: PASS (전건)

- [ ] **Step 5: 커밋**

```bash
git add tests/test_hotplug.py src/serial_mcp/server.py
git commit -m "feat: _hotplug_scan_once — 신규 USB 포트 감지·모니터 추가(copy-on-write)"
```

---

### Task 4: 스캔 루프 스레드 + main() 통합

**Files:**
- Modify: `src/serial_mcp/server.py` (`_hotplug_scan_once` 뒤에 루프 함수, `main()` 끝부분)
- Test: `tests/test_hotplug.py`

- [ ] **Step 1: 실패 테스트 작성** — `tests/test_hotplug.py` 끝에 추가:

```python
# ---- _hotplug_loop (주기 호출·예외 생존) ----

def test_hotplug_loop_survives_scan_exceptions(monkeypatch):
    """스캔이 예외를 던져도 루프는 죽지 않고 다음 주기를 돈다(서버 생존 우선)."""
    stop = threading.Event()
    calls = []

    def boom():
        calls.append(1)
        if len(calls) >= 2:
            stop.set()          # 2회 호출을 확인했으면 루프 종료
        raise RuntimeError("scan failed")

    monkeypatch.setattr(srv, "_hotplug_scan_once", boom)
    srv._hotplug_loop(0.01, stop)   # stop 세트 후 리턴해야 한다(무한 루프 금지)
    assert len(calls) >= 2          # 1회차 예외에도 2회차가 돌았다


def test_hotplug_loop_exits_immediately_when_stopped():
    stop = threading.Event()
    stop.set()
    srv._hotplug_loop(0.01, stop)   # 호출 0회로 즉시 리턴(블록되면 테스트 타임아웃)
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_hotplug.py -q`
Expected: FAIL — `AttributeError: ... no attribute '_hotplug_loop'`

- [ ] **Step 3: 구현** — `_hotplug_scan_once` 뒤에 추가:

```python
def _hotplug_loop(interval: float, stop: threading.Event) -> None:
    """핫플러그 스캔 루프(데몬 스레드 본체) — 어떤 예외에도 죽지 않는다.

    stop.wait(interval) 가 타이머 겸 종료 신호 수신을 겸한다(즉시 반응).
    """
    while not stop.wait(interval):
        try:
            _hotplug_scan_once()
        except Exception as e:  # noqa: BLE001 - 스캔 실패가 스레드를 죽이면 안 됨
            _log(f"핫플러그 스캔 오류: {e!r}")
```

전역 상태 구획(`_autoname_lock` 줄 아래)에 종료 이벤트 추가:

```python
_hotplug_stop = threading.Event()   # 핫플러그 스캔 루프 종료 신호(테스트·향후 정리용)
```

`main()` 통합 — 현재의 `if not _monitors:` 경고 블록(640~642행)을 다음으로 치환하고, 바로 뒤(웹 뷰어 블록 앞)에 스레드 기동을 추가:

```python
    hotplug_on = cfg["hotplug"] is not None and not cfg["ports"]
    if not _monitors:
        if hotplug_on:
            _log("경고: 모니터링할 포트 없음 — USB 장비를 연결하면 핫플러그 스캔이 자동 추가한다.")
        else:
            _log("경고: 모니터링할 포트 없음 — USB 시리얼이 안 보이고 SERIAL_PORT 도 "
                 "비어 있다. 장비 연결 후 서버를 재시작하라(핫플러그 꺼짐).")

    if hotplug_on:
        threading.Thread(target=_hotplug_loop, args=(cfg["hotplug"], _hotplug_stop),
                         name="hotplug-scan", daemon=True).start()
        _log(f"핫플러그 스캔 켜짐 ({cfg['hotplug']:g}초 간격)")
    elif cfg["ports"]:
        _log("핫플러그 스캔 없음 — SERIAL_PORT 고정 목록 모드(늦은 연결은 재연결 루프가 잡음)")
    else:
        _log("핫플러그 스캔 꺼짐 (SERIAL_HOTPLUG=0)")
```

- [ ] **Step 4: 전체 통과 확인 + 문법 검증**

Run: `uv run pytest -q && py -m compileall -q src`
Expected: PASS (전건), compileall 무출력

- [ ] **Step 5: 커밋**

```bash
git add tests/test_hotplug.py src/serial_mcp/server.py
git commit -m "feat: 핫플러그 스캔 루프 — 자동 스캔 모드에서 새 USB 포트 런타임 자동 추가"
```

---

### Task 5: 문서 동기화 (SPEC·README·배포측 plugin.json·SKILL.md)

CLAUDE.md "문서–코드 일치" 규칙의 (B) — 새 결정이 정답이므로 문서를 코드에 맞게 갱신한다. 환경변수는 SPEC·README·plugin.json 세 곳에 중복 서술되므로 **반드시 함께** 갱신한다.

**Files:**
- Modify: `SPEC.md` (§3 첫 불릿, 부록)
- Modify: `README.md` (환경변수 표 45행 부근, "다중 포트 · 별칭" 절 58행 부근)
- Modify: `C:\Users\User\projects\silotek-tools\plugins\serial-mcp\.claude-plugin\plugin.json` (env, version)
- Modify: `C:\Users\User\projects\silotek-tools\plugins\serial-mcp\skills\serial-debugging\SKILL.md` (함정·해석 절)

- [ ] **Step 1: SPEC.md §3 첫 불릿 교체** — 기존:

> `- 포트 결정: `SERIAL_PORT` 미설정이면 시작 시 1회 USB 시리얼(VID 보유)을 자동 스캔해 전부 모니터링한다(블루투스 가상 포트 제외, 핫플러그 없음 — 장비 추가는 서버 재시작). 설정 시 그 목록만(...)`

다음으로 교체:

```markdown
- 포트 결정: `SERIAL_PORT` 미설정이면 시작 시 USB 시리얼(VID 보유)을 자동 스캔해 전부 모니터링하고, 이후에도 `SERIAL_HOTPLUG` 간격(기본 5초, `0`/`false`로 끔)으로 재스캔해 **새로 꽂힌 USB 포트를 런타임에 자동 추가**한다(핫플러그 — 블루투스 가상 포트 제외). 사라진 포트의 모니터는 제거하지 않는다(버퍼·tee 보존, 재연결은 리더의 재시도 루프 담당). `SERIAL_PORT` 설정 시 그 목록만 고정 모니터링하며 핫플러그 스캔은 돌지 않는다(`COM4` 또는 `COM4,COM13@9600` — `@N`은 포트별 보드레이트, 늦게 꽂힌 포트는 재연결 루프가 잡음). 포트마다 독립 버퍼·리더·tee(`log.txt`→`log.SSM.txt`)를 갖는다.
```

- [ ] **Step 2: SPEC.md 부록에 항목 추가** (구현 상태 목록 끝):

```markdown
- 핫플러그 구현(2026-06-11): 자동 스캔 모드에서 `SERIAL_HOTPLUG` 간격(기본 5초)으로 comports() 재스캔, 신규 USB 포트를 런타임 모니터 추가. `_monitors`는 copy-on-write로 원자 교체(리더 스레드 순회와 무충돌). 모니터 조립 규칙은 `_make_monitor()`로 추출해 기동·핫플러그가 공유. 계획서: `docs/superpowers/plans/2026-06-11-serial-hotplug.md`.
```

- [ ] **Step 3: README.md 환경변수 표에 행 추가** (`SERIAL_WEB` 행 아래):

```markdown
| `SERIAL_HOTPLUG` | `5` | 자동 스캔 모드에서 새 USB 포트 감지 간격(초) — 서버 실행 중 꽂은 보드를 자동 추가. `0`으로 끄면 시작 시 1회 스캔만. `SERIAL_PORT` 지정 시 스캔 없음 |
```

"다중 포트 · 별칭" 절 첫 문단(58행) 끝에 한 문장 추가:

```markdown
서버 실행 중에 보드를 새로 꽂아도 몇 초 안에 자동으로 모니터링이 시작된다(핫플러그, `SERIAL_HOTPLUG`).
```

- [ ] **Step 4: silotek-tools plugin.json 갱신** — `env`에 패스스루 추가(`SERIAL_WEB` 줄 아래) + 버전 패치 범프:

```json
        "SERIAL_WEB": "${SERIAL_WEB:-}",
        "SERIAL_HOTPLUG": "${SERIAL_HOTPLUG:-}"
```

`"version": "0.1.0"` → `"version": "0.1.1"`

- [ ] **Step 5: SKILL.md 함정·해석 절에 한 줄 추가** (포트 점유 에러 불릿 아래):

```markdown
- **서버 기동 후에 꽂은 보드**도 핫플러그 스캔(기본 5초 간격)이 자동 추가한다 — 새 보드가 `list_serial_ports`에 monitored=false로 나오면 몇 초 뒤 재조회하고, 그래도 안 붙으면 `SERIAL_PORT` 고정 모드인지(고정 모드는 스캔 없음) 확인하라.
```

- [ ] **Step 6: 두 레포 각각 커밋**

```bash
# silotek-serial-mcp 레포
git add SPEC.md README.md
git commit -m "docs: 핫플러그 반영 — SPEC §3·부록, README 환경변수표(SERIAL_HOTPLUG)"
```

```powershell
# silotek-tools 레포 (C:\Users\User\projects\silotek-tools)
git -C C:\Users\User\projects\silotek-tools add plugins/serial-mcp
git -C C:\Users\User\projects\silotek-tools commit -m "feat: serial-mcp 0.1.1 — SERIAL_HOTPLUG env 패스스루, 스킬에 핫플러그 안내 추가"
```

---

### Task 6: 최종 검증

- [ ] **Step 1: 전체 테스트 + 문법**

Run: `uv run pytest -q && py -m compileall -q src`
Expected: 전건 PASS, compileall 무출력

- [ ] **Step 2: 순수 로직 스모크** (PowerShell)

```powershell
$env:PYTHONPATH="src"
py -c "from serial_mcp.server import _parse_hotplug; assert _parse_hotplug({})==5.0; assert _parse_hotplug({'SERIAL_HOTPLUG':'0'}) is None; print('hotplug parse ok')"
```

Expected: `hotplug parse ok`

- [ ] **Step 3: 실장비 검증(사람 협조 필요 — 선택)**: 서버 재시작 → 보드 하나를 뽑았다 다시 꽂기 → `list_serial_ports`로 몇 초 내 monitored=true 확인. 이 단계는 메인 세션에서 사용자와 함께 수행한다.
