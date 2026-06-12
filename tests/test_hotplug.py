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
        self.force_disconnect_calls = []

    def start(self):
        self.started = True

    def force_disconnect(self, reason):
        self.force_disconnect_calls.append(reason)
        self.connected = False
        self.last_error = reason


BASE_CFG = {
    "ports": [], "names": {}, "autoname": [], "baud": 115200, "tee": None,
    "exclude": None, "include": None, "maxlen": 2000, "dedup": 5, "web": None,
    "hotplug": 5.0, "char_delay": 0.0,
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
    monkeypatch.setattr(srv, "_hotplug_pending", set())
    return existing


def seen_last_scan(monkeypatch, *ports):
    """직전 스캔에서 이미 목격된 상태를 주입 — 정착 유예(연속 2회)를 통과시킨다."""
    monkeypatch.setattr(srv, "_hotplug_pending", {p.upper() for p in ports})


def test_scan_adds_new_usb_port_and_starts_reader(monkeypatch, scan_env):
    seen_last_scan(monkeypatch, "COM_B")
    monkeypatch.setattr(srv.list_ports, "comports",
                        lambda: [usb("COM_A"), usb("COM_B")])
    added = srv._hotplug_scan_once()
    assert added == ["COM_B"]
    assert "COM_B" in srv._monitors
    assert srv._monitors["COM_B"].reader.started is True


def test_scan_add_requires_two_consecutive_scans(monkeypatch, scan_env):
    """새 포트는 연속 2회 스캔에서 확인된 뒤에야 추가된다(드라이버 정착 유예)."""
    monkeypatch.setattr(srv.list_ports, "comports",
                        lambda: [usb("COM_A"), usb("COM_B")])
    assert srv._hotplug_scan_once() == []            # 1회차: 보류
    assert "COM_B" not in srv._monitors
    assert srv._hotplug_scan_once() == ["COM_B"]     # 2회차: 추가
    assert srv._monitors["COM_B"].reader.started is True


def test_scan_pending_cleared_when_port_vanishes(monkeypatch, scan_env):
    """한 번 보였다 사라진 포트는 보류가 풀린다 — 재등장 시 다시 2회 연속 필요."""
    ports = {"now": [usb("COM_A"), usb("COM_B")]}
    monkeypatch.setattr(srv.list_ports, "comports", lambda: ports["now"])
    assert srv._hotplug_scan_once() == []            # 목격 1회
    ports["now"] = [usb("COM_A")]
    assert srv._hotplug_scan_once() == []            # 사라짐 → 보류 해제
    ports["now"] = [usb("COM_A"), usb("COM_B")]
    assert srv._hotplug_scan_once() == []            # 재등장 1회차 — 아직 보류
    assert "COM_B" not in srv._monitors


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
    seen_last_scan(monkeypatch, "COM_C")
    monkeypatch.setattr(srv.list_ports, "comports",
                        lambda: [usb("COM_A"), usb("COM_C", sn="SN777")])
    srv._hotplug_scan_once()
    assert srv._monitors["COM_C"].name == "SB2"


def test_scan_replaces_dict_copy_on_write(monkeypatch, scan_env):
    """리더 스레드가 순회 중인 옛 dict 객체는 불변 — 전역 참조만 교체돼야 한다."""
    before = srv._monitors
    seen_last_scan(monkeypatch, "COM_B")
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


def test_scan_hooks_autoname_on_new_unnamed_port(monkeypatch, scan_env):
    """핫플러그로 추가된 무명 포트도 기동 경로와 동일하게 autoname 훅을 단다."""
    monkeypatch.setattr(srv, "_autoname_rules", srv.compile_autoname([("SB1", r"STM32")]))
    seen_last_scan(monkeypatch, "COM_B")
    monkeypatch.setattr(srv.list_ports, "comports",
                        lambda: [usb("COM_A"), usb("COM_B")])
    srv._hotplug_scan_once()
    assert srv._monitors["COM_B"].reader.on_line is not None


# ---- 좀비 핸들 해제 (열거 교차 확인 — 2026-06-12 실측 대응) ----

def test_scan_releases_zombie_after_two_absent_scans(monkeypatch, scan_env):
    """connected 인데 열거 목록에서 연속 2회 사라진 포트는 핸들을 강제 해제한다."""
    scan_env.reader.connected = True
    monkeypatch.setattr(srv.list_ports, "comports", lambda: [])
    srv._hotplug_scan_once()                          # 부재 1회 — 유예
    assert scan_env.reader.force_disconnect_calls == []
    srv._hotplug_scan_once()                          # 부재 2회 — 해제
    assert len(scan_env.reader.force_disconnect_calls) == 1
    assert scan_env.reader.connected is False


def test_scan_zombie_counter_resets_on_reappearance(monkeypatch, scan_env):
    """한 번 사라졌다 다시 보이면 부재 카운터가 리셋된다(플래핑 방지)."""
    scan_env.reader.connected = True
    ports = {"now": []}
    monkeypatch.setattr(srv.list_ports, "comports", lambda: ports["now"])
    srv._hotplug_scan_once()                          # 부재 1회
    ports["now"] = [usb("COM_A")]
    srv._hotplug_scan_once()                          # 재등장 — 리셋
    ports["now"] = []
    srv._hotplug_scan_once()                          # 부재 1회(다시)
    assert scan_env.reader.force_disconnect_calls == []


def test_scan_ignores_absent_but_disconnected_monitors(monkeypatch, scan_env):
    """이미 끊긴 모니터는 건드리지 않는다 — 재연결은 리더 루프 소관."""
    scan_env.reader.connected = False
    monkeypatch.setattr(srv.list_ports, "comports", lambda: [])
    srv._hotplug_scan_once()
    srv._hotplug_scan_once()
    assert scan_env.reader.force_disconnect_calls == []


def test_scan_allow_add_false_watches_but_never_adds(monkeypatch, scan_env):
    """고정 포트 모드: 좀비 해제는 수행하되 새 포트 추가는 하지 않는다."""
    scan_env.reader.connected = True
    monkeypatch.setattr(srv.list_ports, "comports", lambda: [usb("COM_B")])
    assert srv._hotplug_scan_once(allow_add=False) == []
    assert srv._hotplug_scan_once(allow_add=False) == []
    assert "COM_B" not in srv._monitors               # 2회 연속 목격에도 미추가
    assert len(scan_env.reader.force_disconnect_calls) == 1   # COM_A 부재 2회 → 해제


def test_reader_force_disconnect_closes_handle_and_marks_state():
    """SerialReader.force_disconnect — 핸들 close + connected/last_error 갱신."""
    from serial_mcp.ring_buffer import LineBuffer
    r = srv.SerialReader(port="COMZ", baud=115200, buffer=LineBuffer(maxlen=10))
    closed = []
    r._ser = SimpleNamespace(close=lambda: closed.append(1))
    r.connected = True
    r.force_disconnect("열거 목록에서 사라짐")
    assert closed == [1]
    assert r._ser is None
    assert r.connected is False
    assert "열거 목록에서 사라짐" in r.last_error


# ---- _hotplug_loop (주기 호출·예외 생존) ----

def test_hotplug_loop_survives_scan_exceptions(monkeypatch):
    """스캔이 예외를 던져도 루프는 죽지 않고 다음 주기를 돈다(서버 생존 우선)."""
    stop = threading.Event()
    calls = []

    def boom(allow_add=True):
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
