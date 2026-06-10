"""SerialReader._ingest() — 무한 I/O 루프에서 분리한 '한 줄 처리' 단위 테스트.

실제 시리얼 포트 없이 디코드 정책·개행 제거·tee 타임스탬프 형식(SPEC §3)을 고정한다.
"""

import io
from datetime import datetime

from serial_mcp.ring_buffer import LineBuffer
from serial_mcp.server import SerialReader
from serial_mcp.viewer_feed import RawFeed

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


def test_ingest_tees_lines_dropped_by_filter():
    buf = LineBuffer(maxlen=10, dedup=False, exclude=r"DEBUG")
    tee = io.StringIO()
    _make_reader(buf, tee=tee)._ingest(b"DEBUG noise\n", BASE)
    assert buf.info()["entries"] == 0           # 버퍼에서는 걸러지지만
    assert "DEBUG noise" in tee.getvalue()      # tee에는 수신 원본 보존(SPEC §3)


def test_ingest_tees_every_repeat_even_when_deduped():
    buf = LineBuffer(maxlen=10, dedup=True)
    tee = io.StringIO()
    r = _make_reader(buf, tee=tee)
    r._ingest(b"tick\n", BASE)
    r._ingest(b"tick\n", BASE)
    assert buf.info()["entries"] == 1           # 버퍼는 접히지만
    assert tee.getvalue().count("tick") == 2    # tee는 반복 줄도 전부 기록


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
