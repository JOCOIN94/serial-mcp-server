# 웹 로그 뷰어 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** serial-mcp 프로세스에 localhost 전용 웹 로그 뷰어(실시간 스트림 + 링버퍼 탭, 컬러 하이라이트)를 내장한다 — 새 의존성 0(stdlib `http.server`).

**Architecture:** 리더 스레드의 `_ingest`가 기존 ①링버퍼 ②tee에 더해 ③`RawFeed` 허브(신규, 순수 로직)에 수신 원본을 발행하고, `ViewerServer`(신규, stdlib HTTP + SSE)가 이를 브라우저로 중계한다. 뷰어는 보조 기능 — 기동 실패해도 MCP 서버는 정상 동작하며, 브라우저가 느려도 시리얼 경로는 막히지 않는다(bounded queue drop-oldest). 전체 명세: `docs/superpowers/specs/2026-06-10-web-log-viewer-design.md`.

**Tech Stack:** Python 3.10+ stdlib(`http.server`/`threading`/`json`) / pytest / `urllib.request`(테스트). 이 PC의 `python`은 Store 별칭이라 **`uv`/`py`만 사용**(CLAUDE.md).

**병렬 실행(ultracode Workflow) 의존성 그래프:**
- Wave 0: **Task 1**(viewer_feed) ∥ **Task 2**(snapshot) — 서로 다른 새 파일/기존 파일, 완전 병렬.
- Wave 1: **Task 5**(web_viewer, Task 1에 의존) ∥ **[Task 3 → Task 4]**(둘 다 `server.py` 편집 → 한 에이전트가 순차).
- Wave 2: **Task 6**(server.py 통합 — Task 1·2·3·4·5 전부 선행).
- Wave 3: **Task 7**(문서·전체 검증).
- `server.py`를 건드리는 Task 3·4·6은 반드시 순차(또는 단일 에이전트). 서브에이전트는 대화 맥락을 상속하지 않으므로 **이 계획서 전체를 맥락으로 전달**한다.

---

## File Structure

| 파일 | 책임 | 변경 |
|---|---|---|
| `src/serial_mcp/viewer_feed.py` | RawFeed 생중계 허브(순수 로직, I/O 없음) | 생성(Task 1) |
| `src/serial_mcp/ring_buffer.py` | `snapshot()` 구조화 뷰 추가 | 수정(Task 2) |
| `src/serial_mcp/web_viewer.py` | ViewerServer: HTTP 4라우트 + SSE + 인라인 HTML | 생성(Task 5) |
| `src/serial_mcp/server.py` | SERIAL_WEB 파싱(T3) · feed 배선(T4) · 뷰어 기동/viewer_url(T6) | 수정 |
| `tests/test_viewer_feed.py` / `test_web_viewer.py` | 신규 테스트 | 생성 |
| `tests/test_ring_buffer.py` / `test_config.py` / `test_serial_reader.py` / `test_tools.py` | 기존 스위트 확장 | 수정 |
| `SPEC.md` / `README.md` | §2 보완·§5·§10·환경변수 표 | 수정(Task 7) |

현재 스위트는 **58 passed** 상태에서 시작한다.

---

## Task 1: RawFeed 허브 (viewer_feed.py)

**Files:**
- Create: `src/serial_mcp/viewer_feed.py`
- Test: `tests/test_viewer_feed.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_viewer_feed.py` 전체:

```python
"""RawFeed — 수신 라인 생중계 허브(웹 뷰어 스트림 데이터원) 단위 테스트.

핵심 불변식: publish는 논블로킹이며, 구독자가 느리거나 없어도 발행자를 막지 않는다.
"""

import threading
from datetime import datetime

from serial_mcp.viewer_feed import RawFeed

BASE = datetime(2026, 6, 9, 14, 0, 0, 0)


def test_publish_without_subscribers_is_noop():
    RawFeed().publish(BASE, "no one listening")   # 예외 없이 통과해야 함


def test_subscribe_then_receive():
    feed = RawFeed()
    sub = feed.subscribe()
    feed.publish(BASE, "hello")
    assert sub.get(timeout=1.0) == (BASE, "hello")


def test_get_timeout_returns_none():
    sub = RawFeed().subscribe()
    assert sub.get(timeout=0.05) is None


def test_multiple_subscribers_each_receive():
    feed = RawFeed()
    a, b = feed.subscribe(), feed.subscribe()
    feed.publish(BASE, "x")
    assert a.get(timeout=1.0) == (BASE, "x")
    assert b.get(timeout=1.0) == (BASE, "x")


def test_unsubscribed_stops_receiving():
    feed = RawFeed()
    sub = feed.subscribe()
    feed.unsubscribe(sub)
    feed.publish(BASE, "after")
    assert sub.get(timeout=0.05) is None


def test_overflow_drops_oldest():
    feed = RawFeed(queue_maxlen=3)
    sub = feed.subscribe()
    for i in range(5):
        feed.publish(BASE, f"line{i}")
    got = [sub.get(timeout=0.1) for _ in range(3)]
    assert [t for _, t in got] == ["line2", "line3", "line4"]
    assert sub.get(timeout=0.05) is None   # 오래된 line0/1은 버려짐


def test_cross_thread_delivery():
    feed = RawFeed()
    sub = feed.subscribe()
    t = threading.Thread(target=lambda: [feed.publish(BASE, f"n{i}") for i in range(100)])
    t.start()
    got = [sub.get(timeout=1.0) for _ in range(100)]
    t.join()
    assert [x[1] for x in got] == [f"n{i}" for i in range(100)]
```

- [ ] **Step 2: 실행 — 실패 확인**

Run: `uv run pytest tests/test_viewer_feed.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'serial_mcp.viewer_feed'`

- [ ] **Step 3: 구현**

`src/serial_mcp/viewer_feed.py` 전체:

```python
"""수신 라인 생중계 허브(RawFeed) — 웹 뷰어 실시간 스트림의 데이터원.

리더 스레드가 publish()로 한 줄씩 흘리고, SSE 핸들러가 구독자 큐에서 꺼내 간다.
이 모듈은 순수 로직만 담는다 — 시리얼 I/O·HTTP 의존성이 없어 단위 테스트가 쉽다.

핵심 불변식: publish는 논블로킹이다. 구독자(브라우저)가 느리거나 끊겨도 시리얼
수신 경로를 막지 않는다 — 큐가 가득 차면 가장 오래된 항목을 버린다(drop-oldest).
"""

from __future__ import annotations

import threading
from collections import deque
from datetime import datetime
from typing import Optional


class Subscription:
    """구독자 하나의 수신 큐. RawFeed.subscribe()가 만들어 준다."""

    def __init__(self, maxlen: int) -> None:
        self._q: deque[tuple[datetime, str]] = deque(maxlen=maxlen)
        self._cond = threading.Condition()

    def _put(self, ts: datetime, text: str) -> None:
        with self._cond:
            self._q.append((ts, text))   # maxlen 초과 시 deque가 oldest를 자동으로 버림
            self._cond.notify()

    def get(self, timeout: float = 1.0) -> Optional[tuple[datetime, str]]:
        """다음 (ts, text)를 반환. timeout까지 없으면 None."""
        with self._cond:
            if not self._q:
                self._cond.wait(timeout)
            if self._q:
                return self._q.popleft()
            return None


class RawFeed:
    """구독자들에게 수신 라인을 분배하는 허브. 스레드 안전."""

    def __init__(self, queue_maxlen: int = 1000) -> None:
        self._subs: list[Subscription] = []
        self._lock = threading.Lock()
        self._queue_maxlen = queue_maxlen

    def subscribe(self) -> Subscription:
        sub = Subscription(self._queue_maxlen)
        with self._lock:
            self._subs.append(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        with self._lock:
            if sub in self._subs:
                self._subs.remove(sub)

    def publish(self, ts: datetime, text: str) -> None:
        """리더 스레드가 호출. 논블로킹 — 구독자가 없으면 no-op."""
        with self._lock:
            subs = list(self._subs)
        for sub in subs:
            sub._put(ts, text)
```

- [ ] **Step 4: 실행 — 통과 확인**

Run: `uv run pytest tests/test_viewer_feed.py`
Expected: `7 passed`

- [ ] **Step 5: 커밋**

```powershell
git add src/serial_mcp/viewer_feed.py tests/test_viewer_feed.py
git commit -m "feat: RawFeed 생중계 허브 추가(웹 뷰어 스트림 데이터원)"
```

---

## Task 2: LineBuffer.snapshot() (ring_buffer.py)

**Files:**
- Modify: `src/serial_mcp/ring_buffer.py` (`clear()` 메서드 뒤에 추가)
- Test: `tests/test_ring_buffer.py` (끝에 추가)

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_ring_buffer.py` 끝에 추가:

```python
# ---- snapshot (웹 뷰어 버퍼 탭용 구조화 뷰) ----

def test_snapshot_empty_buffer():
    assert LineBuffer(maxlen=5).snapshot() == []


def test_snapshot_returns_structured_entries_with_fold():
    buf = LineBuffer(maxlen=10, dedup=True)
    buf.add("tick", BASE)
    buf.add("tick", datetime(2026, 6, 9, 14, 0, 5, 0))
    assert buf.snapshot() == [
        {"text": "tick", "first_ts": "14:00:00.000", "last_ts": "14:00:05.000", "count": 2}
    ]


def test_snapshot_chronological_order():
    buf = LineBuffer(maxlen=10, dedup=False)
    buf.add("first", BASE)
    buf.add("second", BASE)
    assert [e["text"] for e in buf.snapshot()] == ["first", "second"]
```

- [ ] **Step 2: 실행 — 실패 확인**

Run: `uv run pytest tests/test_ring_buffer.py -k snapshot`
Expected: FAIL — `AttributeError: 'LineBuffer' object has no attribute 'snapshot'`

- [ ] **Step 3: 구현 — `clear()` 메서드 아래에 추가**

```python
    def snapshot(self) -> list[dict]:
        """웹 뷰어 버퍼 탭용 구조화 뷰(시간 오름차순).

        render() 문자열 대신 본문/시각/반복수를 분리해 반환한다 — 클라이언트가
        메타(타임스탬프·반복 표기)를 본문과 다른 색으로 칠할 수 있게.
        """
        with self._lock:
            items = list(self._buf)
        return [
            {
                "text": e.text,
                "first_ts": _fmt_ts(e.first_ts),
                "last_ts": _fmt_ts(e.last_ts),
                "count": e.count,
            }
            for e in items
        ]
```

- [ ] **Step 4: 실행 — 통과 확인**

Run: `uv run pytest tests/test_ring_buffer.py`
Expected: `26 passed` (기존 23 + 신규 3)

- [ ] **Step 5: 커밋**

```powershell
git add src/serial_mcp/ring_buffer.py tests/test_ring_buffer.py
git commit -m "feat: LineBuffer.snapshot 구조화 뷰 추가(웹 뷰어 버퍼 탭용)"
```

---

## Task 3: SERIAL_WEB 설정 파싱 (server.py)

**Files:**
- Modify: `src/serial_mcp/server.py` (`_load_config` 직전에 `_parse_web` 추가, `_load_config` 반환에 `"web"` 키)
- Test: `tests/test_config.py` (기존 2개 수정 + 신규 4개)

- [ ] **Step 1: 실패 테스트 작성 — 신규 4개를 `tests/test_config.py` 끝에 추가**

```python
# ---- SERIAL_WEB (웹 뷰어 포트) ----

def test_load_config_web_default_on():
    assert _load_config({})["web"] == 8743


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "OFF"])
def test_load_config_web_disabled(val):
    assert _load_config({"SERIAL_WEB": val})["web"] is None


def test_load_config_web_custom_port():
    assert _load_config({"SERIAL_WEB": "9000"})["web"] == 9000


def test_load_config_web_invalid_falls_back_to_default():
    assert _load_config({"SERIAL_WEB": "abc"})["web"] == 8743
```

**그리고 기존 정확일치 테스트 2개를 수정한다** (`"web"` 키가 추가되므로 깨짐):

`test_load_config_defaults_when_empty`의 기대 dict에 `"web": 8743` 추가:

```python
def test_load_config_defaults_when_empty():
    assert _load_config({}) == {
        "port": "", "baud": 115200, "tee": None, "exclude": None,
        "include": None, "maxlen": 2000, "dedup": True, "web": 8743,
    }
```

`test_load_config_reads_all_vars`의 입력에 `"SERIAL_WEB": "9000"`, 기대에 `"web": 9000` 추가:

```python
def test_load_config_reads_all_vars():
    cfg = _load_config({
        "SERIAL_PORT": "COM4", "SERIAL_BAUD": "9600", "SERIAL_TEE": "log.txt",
        "SERIAL_EXCLUDE": "DEBUG", "SERIAL_INCLUDE": "ERROR",
        "SERIAL_BUFFER_LINES": "500", "SERIAL_DEDUP": "0", "SERIAL_WEB": "9000",
    })
    assert cfg == {
        "port": "COM4", "baud": 9600, "tee": "log.txt", "exclude": "DEBUG",
        "include": "ERROR", "maxlen": 500, "dedup": False, "web": 9000,
    }
```

- [ ] **Step 2: 실행 — 실패 확인**

Run: `uv run pytest tests/test_config.py`
Expected: FAIL — 신규 테스트는 `KeyError: 'web'`, 수정한 2개도 dict 불일치로 실패.

- [ ] **Step 3: 구현**

`server.py`의 `_load_config` **바로 위**에 추가:

```python
def _parse_web(env: Mapping[str, str]) -> Optional[int]:
    """SERIAL_WEB 파싱 — 기본 8743(켜짐). 0/false/no/off → 비활성(None), 정수 → 포트."""
    raw = env.get("SERIAL_WEB", "").strip()
    if raw == "":
        return 8743
    if raw.lower() in ("0", "false", "no", "off"):
        return None
    try:
        return int(raw)
    except ValueError:
        _log(f"환경변수 SERIAL_WEB={raw!r} 해석 실패 → 기본 포트 8743 사용")
        return 8743
```

`_load_config`의 반환 dict에 `"web"` 추가 — **기존:**

```python
    return {
        "port": port, "baud": baud, "tee": tee, "exclude": exclude,
        "include": include, "maxlen": maxlen, "dedup": dedup,
    }
```

**교체 후:**

```python
    return {
        "port": port, "baud": baud, "tee": tee, "exclude": exclude,
        "include": include, "maxlen": maxlen, "dedup": dedup,
        "web": _parse_web(env),
    }
```

- [ ] **Step 4: 실행 — 통과 확인**

Run: `uv run pytest tests/test_config.py`
Expected: `24 passed` (기존 16 + 신규 8케이스[함수 4개, disabled는 5케이스 parametrize])

- [ ] **Step 5: 커밋**

```powershell
git add src/serial_mcp/server.py tests/test_config.py
git commit -m "feat: SERIAL_WEB 환경변수 파싱(기본 8743, 0/off로 비활성)"
```

---

## Task 4: SerialReader → RawFeed 배선 (server.py)

**Files:**
- Modify: `src/serial_mcp/server.py` (import, `SerialReader.__init__`, `_ingest`)
- Test: `tests/test_serial_reader.py` (끝에 추가)

- [ ] **Step 1: 실패 테스트 작성 — `tests/test_serial_reader.py`**

파일 상단 import에 추가:

```python
from serial_mcp.viewer_feed import RawFeed
```

파일 끝에 추가:

```python
def test_ingest_publishes_raw_line_to_feed():
    buf = LineBuffer(maxlen=10, dedup=False)
    feed = RawFeed()
    sub = feed.subscribe()
    r = SerialReader(port="COM_TEST", baud=115200, buffer=buf, feed=feed)
    r._tee = None
    r._ingest(b"boot ok\r\n", BASE)
    assert sub.get(timeout=1.0) == (BASE, "boot ok")


def test_ingest_publishes_blank_lines_to_feed_even_though_buffer_drops():
    # 스트림은 수신 원본 충실도(tee와 동일) — 빈 줄도 발행, 버퍼만 §4.3으로 제외
    buf = LineBuffer(maxlen=10, dedup=True)
    feed = RawFeed()
    sub = feed.subscribe()
    r = SerialReader(port="COM_TEST", baud=115200, buffer=buf, feed=feed)
    r._tee = None
    r._ingest(b"\r\n", BASE)
    assert buf.info()["entries"] == 0
    assert sub.get(timeout=1.0) == (BASE, "")


def test_ingest_without_feed_still_works():
    buf = LineBuffer(maxlen=10, dedup=False)
    r = SerialReader(port="COM_TEST", baud=115200, buffer=buf)   # feed 기본 None
    r._tee = None
    r._ingest(b"x\n", BASE)
    assert buf.info()["entries"] == 1
```

- [ ] **Step 2: 실행 — 실패 확인**

Run: `uv run pytest tests/test_serial_reader.py`
Expected: FAIL — `TypeError: SerialReader.__init__() got an unexpected keyword argument 'feed'`

- [ ] **Step 3: 구현 — server.py 세 곳**

(3-1) import 블록의 `from .ring_buffer import LineBuffer` 아래에 추가:

```python
from .viewer_feed import RawFeed
```

(3-2) `SerialReader.__init__` 시그니처·본문 — **기존:**

```python
    def __init__(
        self,
        port: str,
        baud: int,
        buffer: LineBuffer,
        tee_path: Optional[str] = None,
        reconnect_interval: float = 3.0,
    ) -> None:
        self.port = port
        self.baud = baud
        self.buffer = buffer
        self.tee_path = tee_path
        self.reconnect_interval = reconnect_interval
```

**교체 후** (`feed` 파라미터 추가):

```python
    def __init__(
        self,
        port: str,
        baud: int,
        buffer: LineBuffer,
        tee_path: Optional[str] = None,
        reconnect_interval: float = 3.0,
        feed: Optional[RawFeed] = None,
    ) -> None:
        self.port = port
        self.baud = baud
        self.buffer = buffer
        self.tee_path = tee_path
        self.reconnect_interval = reconnect_interval
        self.feed = feed   # 웹 뷰어 생중계 허브(없으면 발행 생략)
```

(3-3) `_ingest`의 `self.buffer.add(text, ts)` 바로 다음 줄에 추가:

```python
        if self.feed is not None:
            self.feed.publish(ts, text)   # 수신 원본 생중계(빈 줄 포함 — tee와 동일 충실도)
```

- [ ] **Step 4: 실행 — 통과 확인 + 회귀 점검**

Run: `uv run pytest tests/test_serial_reader.py tests/test_tools.py`
Expected: serial_reader `9 passed`(기존 6 + 신규 3), tools 회귀 없음.

- [ ] **Step 5: 커밋**

```powershell
git add src/serial_mcp/server.py tests/test_serial_reader.py
git commit -m "feat: SerialReader에 RawFeed 발행 배선(_ingest → 생중계)"
```

---

## Task 5: ViewerServer + HTML 페이지 (web_viewer.py)

**Files:**
- Create: `src/serial_mcp/web_viewer.py`
- Test: `tests/test_web_viewer.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_web_viewer.py` 전체:

```python
"""ViewerServer — 실제 HTTP 기동 통합 테스트(임시 포트). 외부 네트워크 불필요."""

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
    feed = RawFeed()
    v = ViewerServer(
        feed=feed,
        buffer_info=lambda: {"status": "ok", "entries": [], "capacity": 2000},
        status_info=lambda: {"connected": True, "port": "COM_TEST",
                             "baud": 115200, "last_error": None},
        port=0,   # 테스트는 임시 포트
    )
    v.start()
    assert v.url is not None
    yield v, feed
    v.stop()


def test_root_serves_html(viewer):
    v, _ = viewer
    with urllib.request.urlopen(v.url + "/", timeout=5) as r:
        assert r.status == 200
        assert "text/html" in r.headers["Content-Type"]
        assert "serial-mcp" in r.read().decode("utf-8")


def test_api_status_returns_injected_json(viewer):
    v, _ = viewer
    with urllib.request.urlopen(v.url + "/api/status", timeout=5) as r:
        d = json.loads(r.read())
    assert d["connected"] is True
    assert d["port"] == "COM_TEST"


def test_api_buffer_returns_injected_json(viewer):
    v, _ = viewer
    with urllib.request.urlopen(v.url + "/api/buffer", timeout=5) as r:
        d = json.loads(r.read())
    assert d["status"] == "ok"
    assert d["capacity"] == 2000


def test_unknown_path_returns_404(viewer):
    v, _ = viewer
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(v.url + "/nope", timeout=5)
    assert exc.value.code == 404


def test_stream_sse_delivers_published_line(viewer):
    v, feed = viewer
    resp = urllib.request.urlopen(v.url + "/api/stream", timeout=5)
    try:
        assert "text/event-stream" in resp.headers["Content-Type"]
        feed.publish(BASE, "hello sse")
        line = resp.readline().decode("utf-8")        # "data: {...}\n"
        assert line.startswith("data: ")
        payload = json.loads(line[len("data: "):])
        assert payload == {"ts": "14:00:00.000", "text": "hello sse"}
    finally:
        resp.close()


def test_port_fallback_when_preferred_busy():
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    busy_port = blocker.getsockname()[1]
    try:
        v = ViewerServer(feed=RawFeed(), buffer_info=lambda: {},
                         status_info=lambda: {}, port=busy_port)
        v.start()
        try:
            assert v.url is not None
            assert v.url != f"http://127.0.0.1:{busy_port}"   # 임시 포트로 폴백
        finally:
            v.stop()
    finally:
        blocker.close()
```

- [ ] **Step 2: 실행 — 실패 확인**

Run: `uv run pytest tests/test_web_viewer.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'serial_mcp.web_viewer'`

- [ ] **Step 3: 구현**

`src/serial_mcp/web_viewer.py` 전체 (HTML은 raw 문자열 — JS 정규식의 `\b`·`\s`·`\\[` 같은 백슬래시를 파이썬이 건드리지 않게. ANSI ESC는 소스에 제어문자를 두지 않고 `String.fromCharCode(27)`로 생성한다):

```python
"""웹 로그 뷰어 — localhost 전용 HTTP 서버(stdlib http.server, 데몬 스레드).

사람이 브라우저로 시리얼 로그를 보는 보조 기능. 설계: docs/superpowers/specs/
2026-06-10-web-log-viewer-design.md. MCP 서버 본체와 독립 — 기동 실패해도
본체에 영향을 주지 않는다(url이 None으로 남을 뿐).

- 라우트는 전부 GET 읽기 전용. 서버 상태를 바꾸는 엔드포인트는 없다.
- stdout 금지: 접근 로그는 stderr로만 낸다(log_message 오버라이드).
- 실시간 스트림은 SSE 수동 구현 — RawFeed 구독.
- server.py 전역에 직접 의존하지 않고 필요한 데이터를 콜러블로 주입받는다
  (buffer_info/status_info) — 테스트 용이, 순환 import 방지.
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional

from .ring_buffer import _fmt_ts
from .viewer_feed import RawFeed


def _log(msg: str) -> None:
    """진단 로그 — stderr 전용(server.py의 _log와 동일 형식, 순환 import 회피용 사본)."""
    print(f"[serial-mcp] {msg}", file=sys.stderr, flush=True)


class _ViewerHTTPServer(ThreadingHTTPServer):
    daemon_threads = True        # SSE 핸들러 스레드가 프로세스 종료를 막지 않게
    block_on_close = False       # server_close()가 장수 SSE 핸들러를 기다리지 않게
    allow_reuse_address = False  # Windows에서 점유 포트 중복 바인딩 방지(점유 감지가 정확해야 폴백이 동작)

    feed: RawFeed
    buffer_info: Callable[[], dict]
    status_info: Callable[[], dict]


class _Handler(BaseHTTPRequestHandler):
    server: _ViewerHTTPServer

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - 부모 시그니처
        _log("HTTP " + (fmt % args))   # stdout 금지 — 접근 로그를 stderr로

    def do_GET(self) -> None:  # noqa: N802 - http.server 규약
        if self.path == "/":
            body = _HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/stream":
            self._serve_stream()
        elif self.path == "/api/buffer":
            self._send_json(self.server.buffer_info())
        elif self.path == "/api/status":
            self._send_json(self.server.status_info())
        else:
            self.send_error(404)

    def _send_json(self, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_stream(self) -> None:
        """SSE — RawFeed를 구독해 한 줄당 한 이벤트로 흘려보낸다."""
        # 구독을 헤더 전송보다 먼저: 클라이언트가 응답 헤더를 받은 시점에는
        # 이미 구독이 살아 있어야 발행 누락이 없다(테스트·실사용 레이스 방지).
        sub = self.server.feed.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            idle = 0.0
            while True:
                item = sub.get(timeout=1.0)
                if item is None:
                    idle += 1.0
                    if idle >= 15.0:               # 하트비트(프록시·브라우저 타임아웃 방지)
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        idle = 0.0
                    continue
                idle = 0.0
                ts, text = item
                data = json.dumps({"ts": _fmt_ts(ts), "text": text}, ensure_ascii=False)
                self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionError, OSError):
            pass   # 클라이언트 끊김 — 정상 종료
        finally:
            self.server.feed.unsubscribe(sub)


class ViewerServer:
    """뷰어 HTTP 서버 래퍼 — 기동/포트 폴백/URL 보고. 예외를 밖으로 내지 않는다."""

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
        self._preferred_port = port
        self._httpd: Optional[_ViewerHTTPServer] = None
        self.url: Optional[str] = None   # 기동 성공 시 http://127.0.0.1:{port}, 실패 시 None

    def start(self) -> None:
        for port in (self._preferred_port, 0):   # 선호 포트 점유 시 임시 포트로 폴백
            try:
                self._httpd = _ViewerHTTPServer(("127.0.0.1", port), _Handler)
                break
            except OSError as e:
                _log(f"웹 뷰어 포트 {port} 바인딩 실패: {e}")
        if self._httpd is None:
            _log("웹 뷰어 비활성 — 포트 바인딩 전부 실패")
            return
        self._httpd.feed = self._feed
        self._httpd.buffer_info = self._buffer_info
        self._httpd.status_info = self._status_info
        self.url = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        threading.Thread(
            target=self._httpd.serve_forever, name="serial-web", daemon=True
        ).start()

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()


# ---- 단일 페이지(인라인 CSS/JS, 외부 CDN 없음 → 오프라인 동작) ----
# 컬러 원칙: "색은 장식이 아니라 신호" — 평상시 회색 2~3톤, 이상 상황만 채도.
# 우선순위: ANSI 해석 > 레벨 라인 틴트 > 성공 키워드 > JSON 절제 > 메타 dim.
_HTML = r"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>serial-mcp 로그 뷰어</title>
<style>
  body { margin:0; background:#111418; color:#c9d1d9;
         font:13px/1.45 Consolas,'D2Coding','Courier New',monospace; }
  header { position:sticky; top:0; z-index:1; display:flex; align-items:center;
           gap:10px; padding:8px 12px; background:#1a1f26;
           border-bottom:1px solid #2d333b; }
  header .dot { width:9px; height:9px; border-radius:50%; background:#f85149; }
  header .dot.on { background:#3fb950; }
  header button { background:#21262d; color:#c9d1d9; border:1px solid #2d333b;
                  border-radius:4px; padding:3px 10px; cursor:pointer; font:inherit; }
  header button.active { background:#2d4f7c; border-color:#3b6ea5; }
  header label { display:flex; align-items:center; gap:4px; cursor:pointer;
                 color:#8b949e; }
  #port { color:#8b949e; }
  main { padding:6px 12px 24px; }
  .ln { white-space:pre-wrap; word-break:break-all; }
  .ln.err  { background:rgba(248,81,73,.13); color:#ffa198; }
  .ln.warn { background:rgba(210,153,34,.12); color:#e3b341; }
  .meta { color:#586069; }
  .ok   { color:#3fb950; }
  .jkey { color:#79c0ea; }
  .dim  { color:#586069; }
  .a30{color:#484f58}.a31{color:#ff7b72}.a32{color:#3fb950}.a33{color:#d29922}
  .a34{color:#58a6ff}.a35{color:#bc8cff}.a36{color:#39c5cf}.a37{color:#b1bac4}
  .a90{color:#6e7681}.a91{color:#ffa198}.a92{color:#56d364}.a93{color:#e3b341}
  .a94{color:#79c0ff}.a95{color:#d2a8ff}.a96{color:#56d4dd}.a97{color:#f0f6fc}
  .ab{font-weight:bold}
</style>
</head>
<body>
<header>
  <span class="dot" id="dot"></span>
  <span id="port">…</span>
  <button id="tabStream" class="active">스트림</button>
  <button id="tabBuffer">버퍼</button>
  <button id="pause">⏸ 일시정지</button>
  <label><input type="checkbox" id="follow" checked> 자동스크롤</label>
  <button id="clear">화면 지우기</button>
</header>
<main>
  <div id="stream"></div>
  <div id="buffer" style="display:none"></div>
</main>
<script>
const MAX_STREAM = 5000;
const $ = id => document.getElementById(id);
let paused = false, activeTab = "stream";

function esc(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// ANSI SGR(색·굵기)을 span으로 변환. ANSI가 없으면 null 반환(휴리스틱 컬러로 진행).
const ESC = String.fromCharCode(27);   // 이스케이프 문자를 코드로 생성(소스에 제어문자 없음 — 복사 안전)
function ansiToHtml(s) {
  if (s.indexOf(ESC + "[") === -1) return null;
  let out = "", open = 0;
  for (const p of s.split(new RegExp("(" + ESC + "\\[[0-9;]*m)"))) {
    const m = p.match(new RegExp("^" + ESC + "\\[([0-9;]*)m$"));
    if (!m) {
      out += esc(p.replace(new RegExp(ESC + "\\[[0-9;]*[A-Za-z]", "g"), ""));
      continue;
    }
    out += "</span>".repeat(open); open = 0;
    const cls = [];
    for (const c of (m[1] === "" ? [0] : m[1].split(";").map(Number))) {
      if (c === 1) cls.push("ab");
      else if ((c >= 30 && c <= 37) || (c >= 90 && c <= 97)) cls.push("a" + c);
    }
    if (cls.length) { out += '<span class="' + cls.join(" ") + '">'; open = 1; }
  }
  return out + "</span>".repeat(open);
}

// 휴리스틱 데코: JSON 키 시안, 괄호 dim, 성공 키워드 녹색 (값은 본문색 유지 — 무지개 금지)
function decorate(text) {
  let h = esc(text);   // esc는 &,<,>만 치환 — 따옴표는 그대로 남는다
  h = h.replace(/"([^"]*)"(\s*):/g, '<span class="jkey">"$1"</span>$2:');
  h = h.replace(/([{}\[\]])/g, '<span class="dim">$1</span>');
  h = h.replace(/\b(OK|Success|Done)\b/g, '<span class="ok">$1</span>');
  return h;
}

function lineClass(text) {
  if (/error|fail|exception|rst:/i.test(text)) return " err";
  if (/warn/i.test(text)) return " warn";
  return "";
}

function renderLine(ts, text, metaSuffix) {
  const div = document.createElement("div");
  const ansi = ansiToHtml(text);   // ANSI가 있으면 펌웨어 의도 우선, 휴리스틱 생략
  div.className = "ln" + (ansi === null ? lineClass(text) : "");
  div.innerHTML = '<span class="meta">[' + ts + ']</span> ' +
    (ansi !== null ? ansi : decorate(text)) +
    (metaSuffix ? ' <span class="meta">' + esc(metaSuffix) + '</span>' : "");
  return div;
}

const es = new EventSource("/api/stream");
es.onmessage = ev => {
  if (paused) return;
  const d = JSON.parse(ev.data);
  const box = $("stream");
  box.appendChild(renderLine(d.ts, d.text, ""));
  while (box.childNodes.length > MAX_STREAM) box.removeChild(box.firstChild);
  if ($("follow").checked && activeTab === "stream")
    window.scrollTo(0, document.body.scrollHeight);
};

async function refreshBuffer() {
  if (activeTab !== "buffer" || paused) return;
  const d = await (await fetch("/api/buffer")).json();
  const box = $("buffer");
  box.innerHTML = "";
  for (const e of d.entries || []) {
    const suffix = e.count > 1
      ? "(" + e.count + "회 반복, " + e.first_ts + "~" + e.last_ts + ")" : "";
    box.appendChild(renderLine(e.first_ts, e.text, suffix));
  }
  if ($("follow").checked) window.scrollTo(0, document.body.scrollHeight);
}
setInterval(refreshBuffer, 2000);

async function refreshStatus() {
  try {
    const d = await (await fetch("/api/status")).json();
    $("dot").className = "dot" + (d.connected ? " on" : "");
    $("port").textContent = (d.port || "(포트 미설정)") + " @ " + d.baud +
      (d.last_error ? " — " + d.last_error : "");
  } catch (e) { $("dot").className = "dot"; }
}
setInterval(refreshStatus, 5000);
refreshStatus();

function setTab(name) {
  activeTab = name;
  $("stream").style.display = name === "stream" ? "" : "none";
  $("buffer").style.display = name === "buffer" ? "" : "none";
  $("tabStream").className = name === "stream" ? "active" : "";
  $("tabBuffer").className = name === "buffer" ? "active" : "";
  if (name === "buffer") refreshBuffer();
}
$("tabStream").onclick = () => setTab("stream");
$("tabBuffer").onclick = () => setTab("buffer");
$("pause").onclick = () => {
  paused = !paused;
  $("pause").textContent = paused ? "▶ 재개" : "⏸ 일시정지";
};
$("clear").onclick = () => { $(activeTab).innerHTML = ""; };  // 화면만 — 서버 버퍼 무변경
</script>
</body>
</html>
"""
```

- [ ] **Step 4: 실행 — 통과 확인 + 문법 검증**

```powershell
uv run pytest tests/test_web_viewer.py
py -m compileall -q src
```
Expected: `6 passed`, compileall 무출력.

- [ ] **Step 5: 커밋**

```powershell
git add src/serial_mcp/web_viewer.py tests/test_web_viewer.py
git commit -m "feat: 웹 로그 뷰어 HTTP 서버(SSE 스트림·버퍼 뷰·컬러 하이라이트)"
```

---

## Task 6: server.py 통합 — 뷰어 기동 + viewer_url

**Files:**
- Modify: `src/serial_mcp/server.py` (import, 전역, 도구 2개, main)
- Test: `tests/test_tools.py` (끝에 추가)

- [ ] **Step 1: 실패 테스트 작성 — `tests/test_tools.py` 끝에 추가**

```python
# ---- viewer_url (웹 뷰어 링크 자동 제공) ----

def test_get_serial_status_includes_viewer_url(monkeypatch):
    monkeypatch.setattr(srv, "_reader", None)
    monkeypatch.setattr(srv, "_config", {"port": ""})
    monkeypatch.setattr(srv, "_viewer", SimpleNamespace(url="http://127.0.0.1:8743"))
    assert srv.get_serial_status()["viewer_url"] == "http://127.0.0.1:8743"


def test_viewer_url_is_none_when_viewer_off(monkeypatch, buffer):
    monkeypatch.setattr(srv, "_viewer", None)
    assert srv.get_log_buffer_info()["viewer_url"] is None


def test_get_log_buffer_info_includes_viewer_url(monkeypatch, buffer):
    monkeypatch.setattr(srv, "_viewer", SimpleNamespace(url="http://127.0.0.1:9000"))
    assert srv.get_log_buffer_info()["viewer_url"] == "http://127.0.0.1:9000"
```

- [ ] **Step 2: 실행 — 실패 확인**

Run: `uv run pytest tests/test_tools.py`
Expected: FAIL — `AttributeError: <module 'serial_mcp.server'> has no attribute '_viewer'`

- [ ] **Step 3: 구현 — server.py**

(3-1) import 블록에 추가(`from .viewer_feed import RawFeed` 아래):

```python
from .web_viewer import ViewerServer
```

(3-2) 전역 상태 블록 — **기존:**

```python
mcp = FastMCP("serial-mcp")
_buffer: Optional[LineBuffer] = None
_reader: Optional[SerialReader] = None
_config: dict = {}
```

**교체 후:**

```python
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

(3-3) `get_serial_status`의 반환 dict 2곳에 `viewer_url` 추가 — 리더 없음 분기:

```python
        return {
            "status": "error",
            "message": "리더 미시작 — SERIAL_PORT 가 설정되지 않았다. list_serial_ports 로 포트를 찾아 환경변수를 설정하라.",
            "connected": False,
            "configured_port": _config.get("port") or None,
            "viewer_url": _viewer_url(),
        }
```

정상 분기(`"tee": _config.get("tee") or None,` 다음 줄에 추가):

```python
        "viewer_url": _viewer_url(),
```

docstring의 `[루프 단계] 문제 진단.` 앞에 한 줄 추가:

```python
    사람이 로그를 직접 눈으로 보고 싶어 하면 viewer_url 링크를 안내하라(웹 뷰어).
```

(3-4) `get_log_buffer_info` — **기존:**

```python
    info = _buffer.info()
    info["status"] = "ok"
    info["message"] = f"{info['entries']}/{info['capacity']} 항목"
    return info
```

**교체 후:**

```python
    info = _buffer.info()
    info["status"] = "ok"
    info["message"] = f"{info['entries']}/{info['capacity']} 항목"
    info["viewer_url"] = _viewer_url()
    return info
```

(3-5) 뷰어 데이터 콜백 — `_parse_web` 함수 위에 추가:

```python
def _viewer_buffer_info() -> dict:
    """웹 뷰어 /api/buffer 응답(버퍼 탭) — 구조화 스냅샷 + 카운터."""
    if _buffer is None:
        return {"status": "error", "entries": []}
    info = _buffer.info()
    return {
        "status": "ok",
        "entries": _buffer.snapshot(),
        "capacity": info["capacity"],
        "total_received": info["total_received"],
        "total_stored": info["total_stored"],
        "dedup": info["dedup"],
    }


def _viewer_status_info() -> dict:
    """웹 뷰어 /api/status 응답(헤더 표시) — get_serial_status의 경량판."""
    if _reader is None:
        return {
            "connected": False,
            "port": _config.get("port") or "",
            "baud": _config.get("baud"),
            "last_error": "리더 미시작(SERIAL_PORT 미설정)",
        }
    return {
        "connected": _reader.connected,
        "port": _reader.port,
        "baud": _reader.baud,
        "last_error": _reader.last_error,
    }
```

(3-6) `main()` — **기존:**

```python
    global _buffer, _reader, _config
```

**교체 후:**

```python
    global _buffer, _reader, _config, _feed, _viewer
```

**기존:**

```python
    _buffer = LineBuffer(
        maxlen=cfg["maxlen"], dedup=cfg["dedup"],
        exclude=cfg["exclude"], include=cfg["include"],
    )

    if not cfg["port"]:
```

**교체 후** (`_feed` 생성 추가):

```python
    _buffer = LineBuffer(
        maxlen=cfg["maxlen"], dedup=cfg["dedup"],
        exclude=cfg["exclude"], include=cfg["include"],
    )
    _feed = RawFeed()

    if not cfg["port"]:
```

**기존:**

```python
        _reader = SerialReader(
            port=cfg["port"], baud=cfg["baud"], buffer=_buffer, tee_path=cfg["tee"],
        )
        _reader.start()
```

**교체 후** (feed 전달):

```python
        _reader = SerialReader(
            port=cfg["port"], baud=cfg["baud"], buffer=_buffer,
            tee_path=cfg["tee"], feed=_feed,
        )
        _reader.start()
```

그 블록 바로 다음(시작 로그 `_log(f"시작 …")` **앞**)에 뷰어 기동 추가:

```python
    if cfg["web"] is not None:
        _viewer = ViewerServer(
            feed=_feed,
            buffer_info=_viewer_buffer_info,
            status_info=_viewer_status_info,
            port=cfg["web"],
        )
        _viewer.start()   # 실패해도 예외 없음 — url이 None으로 남을 뿐
        _log(f"웹 뷰어: {_viewer.url or '기동 실패'}")
    else:
        _log("웹 뷰어 꺼짐 (SERIAL_WEB=0)")
```

- [ ] **Step 4: 실행 — 전체 통과 확인 + 문법 검증**

```powershell
uv run pytest
py -m compileall -q src
```
Expected: 전체 🟢(기존 스위트 회귀 없음 — 정확 합계는 출력으로 확인), compileall 무출력.

- [ ] **Step 5: 커밋**

```powershell
git add src/serial_mcp/server.py tests/test_tools.py
git commit -m "feat: 웹 뷰어 기동 통합 + 도구 응답에 viewer_url 자동 포함"
```

---

## Task 7: 문서 동기화 + 전체 검증

**Files:**
- Modify: `SPEC.md` (§2 한 줄·§5·§10 신설·부록), `README.md` (특징·환경변수 표·웹 뷰어 절)

- [ ] **Step 1: SPEC.md §2 보완**

§2의 첫 항목 — **기존:**

```
- 헤드리스로만 동작한다. GUI를 포함하지 않으며, PyQt6 등 GUI 라이브러리를 사용하지 않는다.
```

**교체 후:**

```
- 헤드리스로만 동작한다. GUI를 포함하지 않으며, PyQt6 등 GUI 라이브러리를 사용하지 않는다. (localhost 웹 뷰어(§10)는 GUI 라이브러리가 아니라 사용자의 브라우저를 화면으로 쓰므로 이 제약에 위배되지 않는다.)
```

- [ ] **Step 2: SPEC.md §5에 viewer_url 한 줄 추가**

`get_serial_status` 항목 — **기존:**

```
- `get_serial_status` : 현재 연결 상태, 포트, 보드레이트.
```

**교체 후:**

```
- `get_serial_status` : 현재 연결 상태, 포트, 보드레이트. (웹 뷰어 활성 시 `viewer_url` 포함 — `get_log_buffer_info`도 동일. 사람이 로그를 직접 보고 싶어 하면 AI가 이 링크를 안내한다.)
```

- [ ] **Step 3: SPEC.md §10 신설 — §9 끝(`---` 구분선 앞)에 추가**

```markdown
## 10. 웹 로그 뷰어 (localhost 전용 보조 기능)

서버가 포트를 점유하면 테라텀 등으로 사람이 로그를 볼 수 없으므로, stdlib `http.server` 기반 웹 뷰어를 내장한다(새 의존성 0). 상세 설계: `docs/superpowers/specs/2026-06-10-web-log-viewer-design.md`.

- `SERIAL_WEB` 환경변수: 기본 `8743`(켜짐). `0`/`false`/`no`/`off` → 비활성. 포트 점유 시 임시 포트로 자동 폴백, 실제 URL은 도구 응답 `viewer_url`로 보고.
- `127.0.0.1` 바인딩만(외부 접속 불가). 전 라우트 GET 읽기 전용 — 서버 상태를 바꾸는 엔드포인트 없음.
- 탭 2개: 실시간 스트림(수신 원본 — 빈 줄·필터 제외 줄 포함, tee와 동일 충실도) / 링버퍼(접힘·필터 적용 가공 뷰).
- 컬러: ANSI 해석 > 에러·경고 라인 틴트 > 성공 키워드 > JSON 절제 > 메타 dim ("색은 신호" 원칙).
- 불변식: 뷰어 실패는 MCP 서버에 영향 없음 / 느린 브라우저가 시리얼 경로를 막지 않음(drop-oldest).
```

- [ ] **Step 4: SPEC.md 부록 갱신**

부록의 `- 미완:` 줄 — **기존:**

```
- 미완: silotek-tools 측 플러그인(plugin.json + SKILL.md), GitHub push.
```

**교체 후:**

```
- 웹 로그 뷰어 구현(2026-06-10, §10): RawFeed 허브 + ViewerServer(stdlib HTTP/SSE) + 단일 페이지. 도구 응답 viewer_url 포함.
- 미완: silotek-tools 측 플러그인(plugin.json + SKILL.md), GitHub push.
```

- [ ] **Step 5: README.md 갱신 — 3곳**

(5-1) 6번째 줄의 확장 아이디어 문구 — **기존:**

```
**사람이 로그를 눈으로 보기 위한 모니터가 아니다, 다만 포트를 mcp가 점유해 테라텀으로 스트림 로그를 확인이 불가능하니, local host 웹을 연동해 실제 로그를 볼수있는 분기를 만들 확장 아이디어단계는 있음.**
```

**교체 후:**

```
**사람이 로그를 눈으로 보기 위한 모니터가 아니다.** 다만 포트를 MCP가 점유하면 테라텀으로 볼 수 없으므로, **localhost 웹 뷰어**를 내장한다 — 서버가 떠 있으면 브라우저에서 `http://127.0.0.1:8743` (기본)으로 실시간 스트림·링버퍼를 컬러로 볼 수 있다(도구 응답의 `viewer_url` 참조).
```

(5-2) 환경변수 표에 행 추가(`SERIAL_DEDUP` 행 아래):

```
| `SERIAL_WEB` | `8743` | 웹 뷰어 포트. `0`으로 끔. 점유 시 임시 포트 폴백(실제 URL은 `viewer_url`) |
```

(5-3) "로컬 개발" 절 위에 새 절 추가:

```markdown
## 웹 로그 뷰어

서버가 떠 있는 동안 브라우저로 `http://127.0.0.1:8743` (기본)을 열면:

- **스트림 탭** — 수신 원본 실시간 표시(테라텀 대체). 일시정지·자동스크롤·화면 지우기 지원.
- **버퍼 탭** — AI가 보는 것과 같은 가공 뷰(중복 접힘 `(N회 반복…)` 표기 포함).
- 에러/경고 라인 틴트, ANSI 색 해석, JSON 키 하이라이트.

뷰어는 보조 기능이다 — 실패해도 MCP 도구는 정상 동작하며, `127.0.0.1` 전용이라 외부에서 접속할 수 없다.
```

- [ ] **Step 6: 전체 검증**

```powershell
uv run pytest
py -m compileall -q src
```
Expected: 전체 🟢 (Task 1~6 합산 — 정확 수는 출력으로 확정), compileall 무출력.

- [ ] **Step 7: 커밋**

```powershell
git add SPEC.md README.md
git commit -m "docs: 웹 뷰어 반영 — SPEC §2/§5/§10·부록, README 환경변수·사용법"
```

- [ ] **Step 8: (수동, 계획 범위 밖 후속) 실장비 스모크**

보드 연결 상태에서: `$env:SERIAL_PORT="COM4"; uv run serial-mcp` → 브라우저로 `http://127.0.0.1:8743` → 스트림 탭에 실시간 로그·컬러 확인 → 보드 리셋 → 부팅 로그 확인 → 버퍼 탭에서 접힘 표기 확인. (stdio 직접 실행이라 MCP 클라이언트 없이 뷰어만 확인하는 용도 — 서버는 Ctrl+C로 종료.)

---

## Verification (end-to-end)

1. `uv run pytest` 전체 🟢 + `py -m compileall -q src` 무출력.
2. 불변식 확인: 뷰어 콜백·feed는 전부 주입식 — `server.py` 전역을 web_viewer가 직접 참조하지 않음(순환 import 없음). SSE 구독이 헤더 전송보다 먼저(레이스 없음). `allow_reuse_address=False`(Windows 점유 감지). 모든 라우트 GET 읽기 전용.
3. 실장비 스모크(Task 7 Step 8) — 사람 확인.

## Self-Review

**1. Spec coverage:** 설계문서 §2 표(진입/뷰/컬러/활성화/접근/구현) → Task 6(viewer_url)·5(탭·SSE)·5(_HTML 컬러)·3(SERIAL_WEB)·5(127.0.0.1·GET-only)·전체(stdlib) ✅. §4 컴포넌트 4개 → Task 1/2/5/6 ✅. §6 엣지(기동실패·다중인스턴스 폴백·SSE 끊김·하트비트·빈 줄 스트림) → Task 5 코드 + Task 4 빈 줄 테스트 ✅. §7 테스트 전략의 6개 파일 전부 해당 Task에 존재 ✅. §8 문서 → Task 7 ✅ (plugin.json/SKILL.md는 §8에서 "차후 패키징 시"로 명시 — 본 계획 범위 밖). §9 비범위 준수 ✅.
**2. Placeholder scan:** TBD/TODO/"적절히" 없음. 모든 코드 스텝에 전체 코드 포함 ✅.
**3. Type consistency:** `RawFeed(queue_maxlen)`·`subscribe()/unsubscribe(sub)/publish(ts,text)`·`Subscription.get(timeout)`이 Task 1 정의 = Task 4/5/6 사용처와 일치. `ViewerServer(feed, buffer_info, status_info, port)`·`.url`·`.start()/.stop()`이 Task 5 정의 = Task 6 사용처와 일치. `snapshot()` 키(text/first_ts/last_ts/count)가 Task 2 = Task 6 `_viewer_buffer_info` = _HTML JS(`e.first_ts` 등)와 일치. `cfg["web"]`이 Task 3 = Task 6과 일치 ✅.
**4. 주의:** Task 3 Step 4의 기대 통과 수(24)는 어림 — 실행 출력으로 확정. Task 5 `_HTML`은 raw 문자열(`r"""`)이어야 함(JS 백슬래시 보존). ANSI ESC 문자는 소스 어디에도 제어문자로 두지 말 것 — JS에서 `String.fromCharCode(27)`로 생성한다(계획서 복사 시 보이지 않는 바이트가 유실되는 사고 방지).
