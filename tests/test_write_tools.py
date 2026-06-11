"""쓰기 MCP 도구 계약 테스트.

@mcp.tool()은 원본 함수를 반환하므로 async 도구를 asyncio.run으로 직접 호출한다.
승인 게이트는 Context 덕타이핑으로 검증한다.
"""

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData

import serial_mcp.server as srv
from serial_mcp.ring_buffer import LineBuffer
from serial_mcp.viewer_feed import RawFeed

BASE = datetime(2026, 6, 9, 14, 0, 0, 0)


class FakeSession:
    def __init__(self, capability: bool = True) -> None:
        self.capability = capability
        self.checked = []

    def check_client_capability(self, capability) -> bool:
        self.checked.append(capability)
        return self.capability


class FakeContext:
    def __init__(self, action: str = "accept", *, capability: bool = True, exc: Exception | None = None) -> None:
        self.session = FakeSession(capability)
        self.action = action
        self.exc = exc
        self.elicit_calls = []

    async def elicit(self, message: str, schema):
        self.elicit_calls.append((message, schema))
        if self.exc is not None:
            raise self.exc
        return SimpleNamespace(action=self.action)


class RecordingReader:
    def __init__(
        self,
        *,
        write_exc: Exception | None = None,
        on_write=None,
        write_return: int | None = None,
    ) -> None:
        self.connected = True
        self.port = "COM_A"
        self.baud = 115200
        self.last_error = None
        self.opened_at = None
        self.write_exc = write_exc
        self.on_write = on_write
        self.write_return = write_return
        self.writes = []
        self.pulses = 0

    def write(self, data: bytes, audit: str | None = None) -> int:
        if self.write_exc is not None:
            raise self.write_exc
        self.writes.append((data, audit))
        if self.on_write is not None:
            self.on_write()
        return self.write_return if self.write_return is not None else len(data)

    def pulse_reset(self) -> None:
        self.pulses += 1


def make_monitor(port="COM_A", name="SSM", reader=None) -> srv.PortMonitor:
    return srv.PortMonitor(
        port=port,
        name=name,
        buffer=LineBuffer(maxlen=100, dedup=1),
        feed=RawFeed(),
        reader=reader or RecordingReader(),
    )


@pytest.fixture(autouse=True)
def clean_globals(monkeypatch):
    monkeypatch.setattr(srv, "_config", {"write": True, "write_confirm": True})
    monkeypatch.setattr(srv, "_viewer", None)


@pytest.fixture
def single(monkeypatch):
    mon = make_monitor()
    monkeypatch.setattr(srv, "_monitors", {"COM_A": mon})
    return mon


@pytest.fixture
def dual(monkeypatch):
    a = make_monitor("COM_A", "SSM")
    b = make_monitor("COM_B", "SB")
    monkeypatch.setattr(srv, "_monitors", {"COM_A": a, "COM_B": b})
    return a, b


def run(coro):
    return asyncio.run(coro)


def test_send_requires_approval_and_sends_on_accept(single):
    ctx = FakeContext("accept")

    out = run(srv.send_serial_command("AT+GMR", port="SSM", wait_ms=0, ctx=ctx))

    assert out["status"] == "ok"
    assert out["sent"] == "AT+GMR"
    assert out["bytes"] == 7
    assert single.reader.writes == [(b"AT+GMR\n", "[TX] AT+GMR")]
    assert "AT+GMR" in ctx.elicit_calls[0][0]


def test_send_declined_does_not_write(single):
    out = run(srv.send_serial_command("AT", wait_ms=0, ctx=FakeContext("decline")))

    assert out["status"] == "declined"
    assert out["count"] == 0
    assert single.reader.writes == []


def test_send_cancel_does_not_write(single):
    out = run(srv.send_serial_command("AT", wait_ms=0, ctx=FakeContext("cancel")))

    assert out["status"] == "declined"
    assert out["count"] == 0
    assert single.reader.writes == []


def test_send_without_elicitation_capability_errors_with_guidance(single):
    ctx = FakeContext(capability=False)

    out = run(srv.send_serial_command("AT", wait_ms=0, ctx=ctx))

    assert out["status"] == "error"
    assert "SERIAL_WRITE_CONFIRM" in out["message"]
    assert single.reader.writes == []
    assert ctx.elicit_calls == []


def test_send_elicit_mcperror_falls_back_to_error(single):
    err = McpError(ErrorData(code=-32000, message="closed"))
    ctx = FakeContext(exc=err)

    out = run(srv.send_serial_command("AT", wait_ms=0, ctx=ctx))

    assert out["status"] == "error"
    assert "SERIAL_WRITE_CONFIRM" in out["message"]
    assert single.reader.writes == []


def test_send_skips_elicit_when_confirm_off(monkeypatch, single):
    monkeypatch.setattr(srv, "_config", {"write": True, "write_confirm": False})
    ctx = FakeContext(capability=False)

    out = run(srv.send_serial_command("AT", wait_ms=0, ctx=ctx))

    assert out["status"] == "ok"
    assert single.reader.writes == [(b"AT\n", "[TX] AT")]
    assert ctx.elicit_calls == []


def test_send_blocked_when_write_off(monkeypatch, single):
    monkeypatch.setattr(srv, "_config", {"write": False, "write_confirm": True})
    ctx = FakeContext("accept")

    out = run(srv.send_serial_command("AT", wait_ms=0, ctx=ctx))

    assert out["status"] == "error"
    assert "SERIAL_WRITE" in out["message"]
    assert out["lines"] == []
    assert single.reader.writes == []
    assert ctx.elicit_calls == []


def test_send_eol_variants_and_empty_payload_rejected(monkeypatch, single):
    monkeypatch.setattr(srv, "_config", {"write": True, "write_confirm": False})

    assert run(srv.send_serial_command("AT", eol="\r\n", wait_ms=0))["status"] == "ok"
    assert run(srv.send_serial_command("AT", eol="\r", wait_ms=0))["status"] == "ok"
    assert run(srv.send_serial_command("AT", eol="", wait_ms=0))["status"] == "ok"
    assert [call[0] for call in single.reader.writes] == [b"AT\r\n", b"AT\r", b"AT"]

    bad_eol = run(srv.send_serial_command("AT", eol="\n\n", wait_ms=0))
    assert bad_eol["status"] == "error"
    assert "\\r\\n" in bad_eol["message"]

    empty = run(srv.send_serial_command("", eol="", wait_ms=0))
    assert empty["status"] == "error"
    assert empty["lines"] == []


def test_send_multibyte_utf8(monkeypatch, single):
    monkeypatch.setattr(srv, "_config", {"write": True, "write_confirm": False})

    out = run(srv.send_serial_command("안녕", wait_ms=0))

    assert out["status"] == "ok"
    assert single.reader.writes == [("안녕\n".encode("utf-8"), "[TX] 안녕")]


def test_send_routes_by_port_and_errors_on_ambiguous(monkeypatch, dual):
    monkeypatch.setattr(srv, "_config", {"write": True, "write_confirm": False})
    a, b = dual

    ambiguous = run(srv.send_serial_command("AT", wait_ms=0))
    assert ambiguous["status"] == "error"
    assert ambiguous["ports"] == ["SSM (COM_A)", "SB (COM_B)"]
    assert a.reader.writes == []
    assert b.reader.writes == []

    routed = run(srv.send_serial_command("AT", port="sb", wait_ms=0))
    assert routed["status"] == "ok"
    assert b.reader.writes == [(b"AT\n", "[TX] AT")]


def test_send_write_exception_returns_error_dict(monkeypatch):
    monkeypatch.setattr(srv, "_config", {"write": True, "write_confirm": False})
    reader = RecordingReader(write_exc=srv.serial.SerialException("boom"))
    mon = make_monitor(reader=reader)
    monkeypatch.setattr(srv, "_monitors", {"COM_A": mon})

    out = run(srv.send_serial_command("AT", wait_ms=0))

    assert out["status"] == "error"
    assert "전송 실패" in out["message"]
    assert out["lines"] == []


def test_send_reports_payload_length_even_if_writer_returns_short_count(monkeypatch):
    monkeypatch.setattr(srv, "_config", {"write": True, "write_confirm": False})
    reader = RecordingReader(write_return=3)
    mon = make_monitor(reader=reader)
    monkeypatch.setattr(srv, "_monitors", {"COM_A": mon})

    out = run(srv.send_serial_command("AT+GMR", wait_ms=0))

    expected_len = len(b"AT+GMR\n")
    assert out["status"] == "ok"
    assert out["bytes"] == expected_len
    assert f"{expected_len}바이트 전송" in out["message"]


def test_send_harvests_only_lines_after_t0(monkeypatch):
    monkeypatch.setattr(srv, "_config", {"write": True, "write_confirm": False})
    mon = make_monitor()
    mon.buffer.add("old", BASE)
    mon.reader.on_write = lambda: mon.buffer.add("after", datetime.now())
    monkeypatch.setattr(srv, "_monitors", {"COM_A": mon})

    out = run(srv.send_serial_command("AT", wait_ms=0))

    assert out["status"] == "ok"
    assert any(line.endswith("after") for line in out["lines"])
    assert not any(line.endswith("old") for line in out["lines"])


def test_reset_calls_pulse_and_shares_approval_contract(single):
    accepted = run(srv.reset_board(port="SSM", wait_ms=0, ctx=FakeContext("accept")))
    declined = run(srv.reset_board(port="SSM", wait_ms=0, ctx=FakeContext("decline")))

    assert accepted["status"] == "ok"
    assert single.reader.pulses == 1
    assert declined["status"] == "declined"
    assert single.reader.pulses == 1


def test_reset_zero_lines_message_hints_human_fallback(monkeypatch, single):
    monkeypatch.setattr(srv, "_config", {"write": True, "write_confirm": False})

    out = run(srv.reset_board(wait_ms=0))

    assert out["status"] == "ok"
    assert out["count"] == 0
    assert "부팅 로그 없음" in out["message"]
    assert "물리 리셋" in out["message"]


def test_wait_ms_clamped(monkeypatch, single):
    monkeypatch.setattr(srv, "_config", {"write": True, "write_confirm": False})
    sleeps = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(srv.asyncio, "sleep", fake_sleep)

    low = run(srv.send_serial_command("AT", wait_ms=-10))
    high = run(srv.reset_board(wait_ms=31_000))

    assert low["wait_ms"] == 0
    assert high["wait_ms"] == 30_000
    assert sleeps == [0, 30]
