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


def test_get_serial_status_with_disconnected_reader(monkeypatch):
    fake_reader = SimpleNamespace(
        connected=False, port="COM4", baud=115200,
        last_error="포트 열기 실패(COM4): Access is denied", opened_at=None,
    )
    monkeypatch.setattr(srv, "_reader", fake_reader)
    monkeypatch.setattr(srv, "_config", {"tee": None})
    out = srv.get_serial_status()
    assert out["status"] == "ok"            # 리더가 존재하므로 error 아님
    assert out["connected"] is False
    assert out["message"] == "연결 안 됨"
    assert "포트 열기 실패" in out["last_error"]   # 점유/권한 진단 근거 전달
    assert out["opened_at"] is None


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
