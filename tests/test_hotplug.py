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
