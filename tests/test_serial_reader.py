"""SerialReader._ingest() — 무한 I/O 루프에서 분리한 '한 줄 처리' 단위 테스트.

실제 시리얼 포트 없이 디코드 정책·개행 제거·tee 타임스탬프 형식(SPEC §3)을 고정한다.
"""

import io
from datetime import datetime

import pytest

import serial_mcp.server as srv
from serial_mcp.ring_buffer import LineBuffer
from serial_mcp.server import SerialReader
from serial_mcp.viewer_feed import RawFeed

BASE = datetime(2026, 6, 9, 14, 0, 0, 0)


def _make_reader(buf: LineBuffer, tee=None) -> SerialReader:
    r = SerialReader(port="COM_TEST", baud=115200, buffer=buf)
    r._tee = tee   # 실제 파일 대신 StringIO 주입(또는 None)
    return r


class FakeSerial:
    """SerialReader 쓰기/리셋 경로 검증용 최소 시리얼 대역."""

    def __init__(self, *, fail_write: bool = False) -> None:
        self.fail_write = fail_write
        self.writes: list[bytes] = []
        self.closed = False
        self.pin_events: list[tuple[str, bool]] = []
        self._dtr = True
        self._rts = False

    @property
    def dtr(self) -> bool:
        return self._dtr

    @dtr.setter
    def dtr(self, value: bool) -> None:
        self._dtr = value
        self.pin_events.append(("dtr", value))

    @property
    def rts(self) -> bool:
        return self._rts

    @rts.setter
    def rts(self, value: bool) -> None:
        self._rts = value
        self.pin_events.append(("rts", value))

    def write(self, data: bytes) -> int:
        if self.fail_write:
            raise srv.serial.SerialException("write boom")
        self.writes.append(data)
        return len(data)

    def close(self) -> None:
        self.closed = True


def _connected_reader(
    buf: LineBuffer,
    fake: FakeSerial,
    *,
    tee=None,
    feed: RawFeed | None = None,
) -> SerialReader:
    r = SerialReader(port="COM_TEST", baud=115200, buffer=buf, feed=feed)
    r._tee = tee
    r._ser = fake
    r.connected = True
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


def test_ingest_calls_on_line_hook():
    # 자동 식별 등 서버측 후킹용 — (ts, text)로 호출
    buf = LineBuffer(maxlen=10, dedup=0)
    seen = []
    r = SerialReader(port="COM_TEST", baud=115200, buffer=buf,
                     on_line=lambda ts, text: seen.append((ts, text)))
    r._tee = None
    r._ingest(b"hello\r\n", BASE)
    assert seen == [(BASE, "hello")]


def test_ingest_without_on_line_still_works():
    buf = LineBuffer(maxlen=10, dedup=0)
    r = SerialReader(port="COM_TEST", baud=115200, buffer=buf)   # on_line 기본 None
    r._tee = None
    r._ingest(b"x\n", BASE)
    assert buf.info()["entries"] == 1


def test_ingest_isolates_on_line_exception():
    # 훅(자동 식별 등) 오류가 리더 스레드를 죽이면 안 된다(무음 수집 중단 방지)
    buf = LineBuffer(maxlen=10, dedup=0)

    def boom(ts, text):
        raise RuntimeError("훅 폭발")

    r = SerialReader(port="COM_TEST", baud=115200, buffer=buf, on_line=boom)
    r._tee = None
    r._ingest(b"x\n", BASE)          # 예외가 새어 나오면 테스트 실패
    assert buf.info()["entries"] == 1


# ---- 쓰기/리셋 경로 ----

def test_write_sends_payload_and_audits_tx():
    buf = LineBuffer(maxlen=10, dedup=False)
    feed = RawFeed()
    sub = feed.subscribe()
    tee = io.StringIO()
    fake = FakeSerial()
    r = _connected_reader(buf, fake, tee=tee, feed=feed)

    assert r.write(b"AT+GMR\n", audit="[TX] AT+GMR") == 7

    assert fake.writes == [b"AT+GMR\n"]
    assert any("[TX] AT+GMR" in line for line in buf.get_recent(10))
    assert sub.get(timeout=1.0)[1] == "[TX] AT+GMR"
    assert "[TX] AT+GMR" in tee.getvalue()


def test_write_raises_when_disconnected():
    r = SerialReader(port="COM_TEST", baud=115200, buffer=LineBuffer())

    with pytest.raises(srv.serial.SerialException):
        r.write(b"AT\n")


def test_write_failure_marks_disconnected_for_reconnect():
    fake = FakeSerial(fail_write=True)
    r = _connected_reader(LineBuffer(), fake)

    with pytest.raises(srv.serial.SerialException):
        r.write(b"AT\n")

    assert r.connected is False
    assert r._ser is None
    assert fake.closed is True
    assert "쓰기 실패" in (r.last_error or "")


def test_write_audit_skipped_in_buffer_by_filter_but_teed():
    buf = LineBuffer(maxlen=10, dedup=False, exclude=r"\[TX\]")
    feed = RawFeed()
    sub = feed.subscribe()
    tee = io.StringIO()
    r = _connected_reader(buf, FakeSerial(), tee=tee, feed=feed)

    r.write(b"AT\n", audit="[TX] AT")

    assert buf.get_recent(10) == []
    assert sub.get(timeout=1.0)[1] == "[TX] AT"
    assert "[TX] AT" in tee.getvalue()


def test_pulse_reset_sequence_order():
    buf = LineBuffer(maxlen=10, dedup=False)
    fake = FakeSerial()
    r = _connected_reader(buf, fake)

    r.pulse_reset(pulse_s=0)

    assert fake.pin_events == [("dtr", False), ("rts", True), ("rts", False)]
    assert any("[RST] DTR/RTS 하드웨어 리셋 펄스" in line for line in buf.get_recent(10))


def test_pulse_reset_raises_when_disconnected():
    r = SerialReader(port="COM_TEST", baud=115200, buffer=LineBuffer())

    with pytest.raises(srv.serial.SerialException):
        r.pulse_reset()


def test_open_sets_write_timeout(monkeypatch):
    captured = {}

    class OpenSerial(FakeSerial):
        def __init__(self, port, baud, timeout, write_timeout):
            super().__init__()
            captured.update({
                "port": port,
                "baud": baud,
                "timeout": timeout,
                "write_timeout": write_timeout,
            })

    monkeypatch.setattr(srv.serial, "Serial", OpenSerial)
    r = SerialReader(port="COM_TEST", baud=115200, buffer=LineBuffer())

    assert r._open() is True
    assert captured == {
        "port": "COM_TEST",
        "baud": 115200,
        "timeout": 1,
        "write_timeout": 2,
    }
