"""MCP 도구 계약 테스트(다중 포트, SPEC §5 개정).

도구는 모듈 전역 _monitors(dict[str, PortMonitor])를 읽는다 → monkeypatch 주입.
@mcp.tool()은 원본 함수를 반환하므로 직접 호출.
"""

import threading
from datetime import datetime
from types import SimpleNamespace

import pytest

import serial_mcp.server as srv
from serial_mcp.ring_buffer import LineBuffer
from serial_mcp.viewer_feed import RawFeed

BASE = datetime(2026, 6, 9, 14, 0, 0, 0)


def make_monitor(port="COM_T", name=None, connected=True, last_error=None, opened_at=None,
                 first_open_done=True):
    """실제 LineBuffer/RawFeed + 가짜 reader로 PortMonitor 구성.

    first_open_done: 첫 open 결판 여부(기본 True=결판남). False면 '여는 중'(opening) 재현."""
    ev = threading.Event()
    if first_open_done:
        ev.set()
    reader = SimpleNamespace(connected=connected, port=port, baud=115200,
                             last_error=last_error, opened_at=opened_at,
                             _first_open_done=ev)
    return srv.PortMonitor(port=port, name=name,
                           buffer=LineBuffer(maxlen=100, dedup=1),
                           feed=RawFeed(), reader=reader)


@pytest.fixture
def single(monkeypatch):
    """포트 1개(COM_A) 주입 — 미지정 호환 경로 검증용."""
    mon = make_monitor("COM_A")
    monkeypatch.setattr(srv, "_monitors", {"COM_A": mon})
    monkeypatch.setattr(srv, "_viewer", None)
    monkeypatch.setattr(srv, "_owner_active", True, raising=False)
    return mon


@pytest.fixture
def dual(monkeypatch):
    """포트 2개(SSM=COM_A, COM_B) 주입 — 라우팅·별칭·미지정 에러 검증용."""
    a = make_monitor("COM_A", name="SSM")
    b = make_monitor("COM_B", connected=False, last_error="포트 열기 실패(COM_B): busy")
    monkeypatch.setattr(srv, "_monitors", {"COM_A": a, "COM_B": b})
    monkeypatch.setattr(srv, "_viewer", None)
    monkeypatch.setattr(srv, "_owner_active", True, raising=False)
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
    monkeypatch.setattr(srv, "_owner_active", True, raising=False)
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


def test_viewer_status_includes_session_hw_board_without_released(monkeypatch, dual):
    a, b = dual
    a.name = "SB-STM"
    monkeypatch.setattr(srv, "_session_label", "claude-code", raising=False)

    out = srv._viewer_status_info()

    assert out["session"] == "claude-code"
    assert out["ports"][0]["hw"] == "SB"
    assert out["ports"][0]["board"] == "STM"
    assert "released" not in out["ports"][0]
    assert "released" not in out["ports"][1]


def test_session_label_captured_from_first_tool_context(monkeypatch, single):
    def ctx(name):
        return SimpleNamespace(
            session=SimpleNamespace(
                client_params=SimpleNamespace(
                    clientInfo=SimpleNamespace(name=name),
                ),
            ),
        )

    monkeypatch.setattr(srv, "_session_label", None, raising=False)

    srv.get_serial_status(ctx=ctx("claude-code"))
    srv.get_serial_status(ctx=ctx("codex"))

    assert srv._viewer_status_info()["session"] == "claude-code"


def test_viewer_release_port_releases_whole_session(monkeypatch, single):
    calls = []

    monkeypatch.setattr(srv, "_release_owner", lambda reason: calls.append(reason))

    out = srv._viewer_release_port("COM_A")

    assert out == {"status": "ok", "released": True}
    assert "release" in calls[0]


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


# ---- #2: 첫 open self-trigger race 가드 (opening 플래그 + 바운드 대기) ----

def test_status_reports_opening_when_first_open_unresolved(monkeypatch):
    """첫 open 미결판(Event 미set, connected=false) → opening=true, message '응답 없음'.
    '꺼짐'이 아니라 '여는 중'으로 읽혀야 한다."""
    mon = make_monitor("COM_O", connected=False, first_open_done=False)
    monkeypatch.setattr(srv, "_monitors", {"COM_O": mon})
    monkeypatch.setattr(srv, "_owner_active", True, raising=False)
    monkeypatch.setattr(srv, "_STATUS_FIRST_OPEN_WAIT_S", 0)   # 대기 건너뜀(테스트 속도)

    out = srv.get_serial_status(port="COM_O")
    assert out["connected"] is False
    assert out["opening"] is True
    assert "응답 없음" in out["message"]


def test_status_dead_port_reads_as_failed_not_opening(monkeypatch):
    """죽은 포트(첫 open 실패로 결판: Event set + last_error) → opening=false,
    message '안 됨: ...'. 사용자 합의 규칙: '연결 중'이 아니라 '안 됨'."""
    mon = make_monitor("COM_D", connected=False, last_error="포트 열기 실패(COM_D): busy",
                        first_open_done=True)
    monkeypatch.setattr(srv, "_monitors", {"COM_D": mon})
    monkeypatch.setattr(srv, "_owner_active", True, raising=False)

    out = srv.get_serial_status(port="COM_D")
    assert out["connected"] is False
    assert out["opening"] is False
    assert out["message"].startswith("안 됨")
    assert "busy" in out["message"]


def test_status_waits_for_first_open_then_reports_connected(monkeypatch):
    """바운드 대기: 호출 시점엔 미결판이어도 대기 창 안에 첫 open 이 끝나면
    (Event set + connected=true) status 는 connected=true 를 본다(거짓 false 안 뱉음)."""
    mon = make_monitor("COM_W", connected=False, first_open_done=False)
    monkeypatch.setattr(srv, "_monitors", {"COM_W": mon})
    monkeypatch.setattr(srv, "_owner_active", True, raising=False)
    monkeypatch.setattr(srv, "_STATUS_FIRST_OPEN_WAIT_S", 1.0)

    def finish_open():
        mon.reader.connected = True
        mon.reader._first_open_done.set()
    threading.Timer(0.05, finish_open).start()

    out = srv.get_serial_status(port="COM_W")
    assert out["connected"] is True
    assert out["opening"] is False
    assert out["message"] == "연결됨"


def test_status_open_wait_returns_immediately_when_resolved(monkeypatch):
    """이미 결판난(Event set) 포트들만이면 대기는 0 — 둘째 호출부터 지연 없음."""
    import time as _time
    a = make_monitor("COM_A", name="SSM")                       # 기본 first_open_done=True
    b = make_monitor("COM_B", connected=False, last_error="x")  # 결판된 실패
    monkeypatch.setattr(srv, "_monitors", {"COM_A": a, "COM_B": b})
    monkeypatch.setattr(srv, "_owner_active", True, raising=False)
    monkeypatch.setattr(srv, "_STATUS_FIRST_OPEN_WAIT_S", 5.0)  # 크게 잡아도 안 기다려야 함

    t0 = _time.monotonic()
    out = srv.get_serial_status()
    assert _time.monotonic() - t0 < 0.5
    assert out["status"] == "ok"


def test_status_includes_opening_field_in_aggregate(monkeypatch):
    """집계 경로도 포트별 opening 필드를 싣는다."""
    a = make_monitor("COM_A", name="SSM")
    monkeypatch.setattr(srv, "_monitors", {"COM_A": a})
    monkeypatch.setattr(srv, "_owner_active", True, raising=False)
    out = srv.get_serial_status()
    assert "opening" in out["ports"][0]
    assert out["ports"][0]["opening"] is False


# ---- #1: 뷰어 기본포트 선택이 의존하는 백엔드 계약 가드 ----

def test_viewer_status_ports_carry_connected_and_buffer_entries(dual):
    """뷰어 JS pickDefaultPort 가 의존하는 계약: 각 포트가 connected·buffer_entries 를
    싣는다. 이게 조용히 사라지면 뷰어 기본포트 선택이 깨진다."""
    out = srv._viewer_status_info()
    for p in out["ports"]:
        assert "connected" in p
        assert "buffer_entries" in p
