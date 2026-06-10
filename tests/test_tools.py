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


# ---- SERIAL_AUTONAME (로그 내용 기반 자동 식별, 서버측 1회 확정) ----

def test_autoname_assigns_on_first_match(monkeypatch, dual):
    a, b = dual            # a=SSM(이미 명명), b=COM_B(무명)
    monkeypatch.setattr(srv, "_autoname_rules", srv.compile_autoname([("SB1", r"STM32")]))
    srv._autoname_check(b, "***Send to the STM32 to request the FWVer.")
    assert b.name == "SB1"
    assert b.label == "SB1 (COM_B)"


def test_autoname_respects_existing_name(monkeypatch, dual):
    a, _ = dual
    monkeypatch.setattr(srv, "_autoname_rules", srv.compile_autoname([("WRONG", r".")]))
    srv._autoname_check(a, "anything")
    assert a.name == "SSM"          # 명시 SERIAL_NAMES 우선 — 덮어쓰지 않음


def test_autoname_skips_duplicate_name(monkeypatch, dual):
    a, b = dual
    monkeypatch.setattr(srv, "_autoname_rules", srv.compile_autoname([("SSM", r".")]))
    srv._autoname_check(b, "would match anything")
    assert b.name is None           # 'SSM'은 이미 a의 이름 — 오인 방지 위해 미부여


def test_autoname_noop_without_rules(monkeypatch, dual):
    _, b = dual
    monkeypatch.setattr(srv, "_autoname_rules", [])
    srv._autoname_check(b, "***Send to the STM32")
    assert b.name is None


# ---- 코드리뷰 보강: 라벨 해석·clear 계약 타입·tee 경로 ----

def test_resolve_accepts_label_form(dual):
    # 에러 응답의 ports 목록("SSM (COM_A)")을 그대로 되돌려도 해석돼야 복구 루프가 닫힌다
    a, _ = dual
    a.buffer.add("hello", BASE)
    assert srv.get_recent_logs(port="SSM (COM_A)")["count"] == 1


def test_clear_error_keeps_ports_dict_type(dual):
    # 한 도구 안에서 ports 키 타입은 항상 dict — 후보 목록은 available_ports로
    out = srv.clear_log_buffer(port="NOPE")
    assert out["status"] == "error"
    assert out["ports"] == {}
    assert any("COM_B" in s for s in out["available_ports"])


def test_tee_path_for_inserts_tag_and_sanitizes():
    assert srv._tee_path_for("log.txt", "SSM") == "log.SSM.txt"
    assert srv._tee_path_for("log.txt", "SB1 (COM13)") == "log.SB1__COM13_.txt"
    assert srv._tee_path_for("noext", "A") == "noext.A"
    assert srv._tee_path_for(None, "SSM") is None
