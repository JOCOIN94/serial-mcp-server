"""server 토폴로지 bootstrap — 송신 판정(_bootstrap_due) + tick 부수효과(Phase B 모듈6-b).

부트스트랩 INFO 는 안전민감 자동 송신이라(서버-내부 시리얼 write) 판정 조건을 못박는다:
SSM 식별 + 연결됨 + 미송신 + 부팅 window(owner 획득 후 _TOPOLOGY_BOOT_WINDOW_S) 종료.
부팅 setup window 중 송신 절대 금지(serialCmd 오작동 보호, AGENTS.md 안전제약).
"""

from types import SimpleNamespace

import serial_mcp.server as srv
from serial_mcp.server import _bootstrap_due

OWNER = 100.0
WIN = 8.0


def _due(now=200.0, sent=frozenset(), port="COM4", is_ssm=True, connected=True):
    return _bootstrap_due(now, OWNER, WIN, set(sent), port, is_ssm, connected)


def test_due_when_all_conditions_met():
    assert _due() is True


def test_not_due_during_boot_window():
    # owner 획득 후 window(8s) 이내 → 부팅 보호로 송신 금지.
    assert _due(now=OWNER + 1.0) is False
    assert _due(now=OWNER + WIN - 0.01) is False
    assert _due(now=OWNER + WIN) is True            # 경계: window 종료 시점 허용


def test_not_due_when_not_ssm():
    assert _due(is_ssm=False) is False              # 비-SSM 포트엔 절대 송신 금지


def test_not_due_when_disconnected():
    assert _due(connected=False) is False


def test_not_due_when_already_sent():
    assert _due(sent={"COM4"}) is False             # 1회 래치
    assert _due(sent={"COM9"}) is True              # 다른 포트는 무관


# ---- _topology_bootstrap_tick 부수효과(서버-내부 INFO 송신) ----

class _SpyReader:
    """write 를 기록하는 가짜 리더(connected 제어)."""

    def __init__(self, connected=True):
        self.connected = connected
        self.writes = []

    def write(self, data, audit=None):
        self.writes.append((data, audit))
        return len(data)


def _spy_mon(port="COM4", lines=None, connected=True, name=None):
    reader = _SpyReader(connected=connected)
    buffer = SimpleNamespace(get_recent=lambda n=300, _l=lines or []: list(_l))
    return SimpleNamespace(port=port, name=name, reader=reader, buffer=buffer,
                           label=f"{name or port}")


def _run_tick(monkeypatch, monitors, *, write=True, dtype="SSM",
              owner_ts=0.0, now=100.0, sent=None):
    """tick 을 격리 실행 — classify_device·monotonic·전역 상태를 stub."""
    monkeypatch.setattr(srv, "_monitors", {m.port.upper(): m for m in monitors})
    monkeypatch.setattr(srv, "_config", {"write": write})
    monkeypatch.setattr(srv, "_topology_owner_ts", owner_ts)
    monkeypatch.setattr(srv, "_topology_bootstrapped", set(sent or []))
    monkeypatch.setattr(srv, "_TOPOLOGY_BOOT_WINDOW_S", 8.0)
    monkeypatch.setattr(srv.time, "monotonic", lambda: now)
    # classify_device 는 dtype(맵 또는 단일값)으로 stub — 실분류는 topology 테스트가 담당.
    type_map = dtype if isinstance(dtype, dict) else None

    def fake_classify(lines, alias=None):
        t = type_map.get(alias if alias else "", dtype) if type_map else dtype
        return {"type": t, "mcu": None, "number": None, "confidence": 1.0, "source": "manual"}

    monkeypatch.setattr(srv, "classify_device", fake_classify)
    srv._topology_bootstrap_tick()


def test_tick_sends_info_to_ssm_and_latches(monkeypatch):
    mon = _spy_mon(port="COM4")
    _run_tick(monkeypatch, [mon], dtype="SSM", now=100.0)
    assert mon.reader.writes == [(b"INFO\r\n", mon.reader.writes[0][1])]   # INFO 1회
    assert "COM4" in srv._topology_bootstrapped                            # 래치 등록


def test_tick_no_send_to_non_ssm(monkeypatch):
    mon = _spy_mon(port="COM14")
    _run_tick(monkeypatch, [mon], dtype="SB", now=100.0)
    assert mon.reader.writes == []                                         # 비-SSM 송신 금지
    assert "COM14" not in srv._topology_bootstrapped


def test_tick_no_send_when_write_disabled(monkeypatch):
    mon = _spy_mon(port="COM4")
    _run_tick(monkeypatch, [mon], dtype="SSM", write=False, now=100.0)
    assert mon.reader.writes == []                                         # SERIAL_WRITE=off


def test_tick_no_send_during_boot_window(monkeypatch):
    mon = _spy_mon(port="COM4")
    _run_tick(monkeypatch, [mon], dtype="SSM", owner_ts=99.0, now=100.0)   # 1s < 8s window
    assert mon.reader.writes == []                                         # 부팅 window 보호


def test_tick_no_resend_when_already_latched(monkeypatch):
    mon = _spy_mon(port="COM4")
    _run_tick(monkeypatch, [mon], dtype="SSM", now=100.0, sent={"COM4"})
    assert mon.reader.writes == []                                         # 1회 래치 유지


def test_tick_no_send_when_disconnected(monkeypatch):
    mon = _spy_mon(port="COM4", connected=False)
    _run_tick(monkeypatch, [mon], dtype="SSM", now=100.0)
    assert mon.reader.writes == []                                         # 미연결 송신 금지
