"""serial-mcp-server — FastMCP 서버 본체.

백그라운드 스레드가 시리얼 포트를 지속적으로 읽어 LineBuffer에 쌓고,
AI(Claude Code)는 6개의 조회 도구로 버퍼를 읽으며, 승인 게이트가 있는
쓰기 도구 2개(send_serial_command/reset_board)로 제한된 능동 시험을 수행한다.

설계 주의:
- stdout 으로 MCP JSON-RPC 가 흐른다. stdout 에 절대 print/로그 금지.
  모든 진단은 stderr 또는 tee 파일로만(_log 헬퍼 사용).
- 버퍼는 LineBuffer 내부 Lock 으로 보호된다(리더 스레드 ↔ 도구 호출 동시 접근).
- 쓰기는 send_serial_command/reset_board 두 도구로만 제공한다. 승인 범위는
  SERIAL_WRITE_CONFIRM=all(기본·모두)/r3(파괴 명령만)/off(클라이언트 위임)로 정하며,
  SERIAL_WRITE=off로 전면 차단할 수 있다.
"""

from __future__ import annotations

import asyncio
import atexit
import json
import math
import os
import re
import socket
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime
from typing import Callable, Mapping, Optional
from urllib.error import URLError
from urllib.request import urlopen

import serial
from serial.tools import list_ports
from mcp.server.fastmcp import Context, FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ClientCapabilities, ElicitationCapability
# pydantic은 mcp[cli]의 하드 의존성이므로 별도 top-level 의존성 추가가 아니다.
from pydantic import BaseModel

from .ports import (
    auto_usb_ports,
    compile_autoname,
    first_autoname_match,
    label,
    name_for,
    parse_autoname,
    parse_names,
    parse_port_list,
)
from .ring_buffer import LineBuffer
from .viewer_feed import RawFeed
from .web_viewer import ViewerServer


def _log(msg: str) -> None:
    """진단 로그 — 반드시 stderr 로만(절대 stdout 금지)."""
    print(f"[serial-mcp] {msg}", file=sys.stderr, flush=True)


class SerialReader:
    """백그라운드에서 시리얼 포트를 줄 단위로 읽어 LineBuffer 에 적재.

    포트가 점유됐거나 열 수 없으면 죽지 않고 last_error 에 남긴 뒤 주기적으로
    재시도한다(블랙박스 루프 중 장비가 늦게 붙거나 잠시 빠져도 복구).
    """

    def __init__(
        self,
        port: str,
        baud: int,
        buffer: LineBuffer,
        tee_path: Optional[str] = None,
        reconnect_interval: float = 3.0,
        feed: Optional[RawFeed] = None,
        on_line: Optional[Callable[[datetime, str], None]] = None,
        char_delay: float = 0.0,
    ) -> None:
        self.port = port
        self.baud = baud
        self.buffer = buffer
        self.tee_path = tee_path
        self.reconnect_interval = reconnect_interval
        self.feed = feed   # 웹 뷰어 생중계 허브(없으면 발행 생략)
        self.on_line = on_line   # 서버측 라인 후킹(보드 자동 식별 등, 없으면 생략)
        self.char_delay = char_delay   # 전송 문자 간 지연(초) — 폴링 수신 펌웨어의 바이트 유실 대응

        self._thread = threading.Thread(target=self._run, name="serial-reader", daemon=True)
        self._stop = threading.Event()
        self._wake = threading.Event()
        # 첫 open 시도가 결판(성공/실패)나면 set — get_serial_status 의 바운드 대기가
        # 이걸 기다려 lazy 기동 직후의 self-trigger race(전 포트 일시 false)를 막는다.
        self._first_open_done = threading.Event()
        self._ser: Optional[serial.Serial] = None
        self._ser_lock = threading.Lock()
        self._tee = None
        self._tee_lock = threading.Lock()

        # 상태(도구가 조회) — 단순 대입만 하므로 별도 Lock 불필요
        self.connected = False
        self.last_error: Optional[str] = None
        self.opened_at: Optional[datetime] = None

    def start(self) -> None:
        if self.tee_path:
            try:
                self._tee = open(self.tee_path, "a", encoding="utf-8", buffering=1)
            except OSError as e:
                self.last_error = f"tee 파일 열기 실패: {e}"
                _log(self.last_error)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        with self._ser_lock:
            if self._ser is not None:
                try:
                    self._ser.close()
                except Exception:
                    pass
        if self._tee is not None:
            try:
                self._tee.close()
            except Exception:
                pass

    def _open(self) -> bool:
        try:
            ser = serial.Serial(self.port, self.baud, timeout=1, write_timeout=2)
            with self._ser_lock:
                self._ser = ser
                self.connected = True
                self.opened_at = datetime.now()
                self.last_error = None
            _log(f"열림: {self.port} @ {self.baud}")
            return True
        except serial.SerialException as e:
            self.connected = False
            self.last_error = f"포트 열기 실패({self.port}): {e}"
            _log(self.last_error)
            return False
        except Exception as e:  # noqa: BLE001 - 어떤 예외든 죽지 않고 상태만 남긴다
            self.connected = False
            self.last_error = f"포트 열기 예외({self.port}): {e!r}"
            _log(self.last_error)
            return False
        finally:
            # 첫 시도가 성공이든 실패든 '결판났음'을 신호한다(status 바운드 대기를 깨움).
            # 멱등 — 재연결 루프의 후속 _open 호출도 무해. 죽은 포트는 즉시 실패→즉시 set.
            self._first_open_done.set()

    def _wait_retry(self) -> None:
        self._wake.wait(self.reconnect_interval)
        self._wake.clear()

    def _run(self) -> None:
        while not self._stop.is_set():
            if self._ser is None or not self.connected:
                if not self._open():
                    self._wait_retry()  # 중단 신호에 즉시 반응
                    continue
            ser = self._ser
            try:
                raw = ser.readline()
            except Exception as e:  # noqa: BLE001 - 연결 끊김 등 모든 읽기 오류 복구
                self.connected = False
                self.last_error = f"읽기 중 오류: {e}"
                _log(self.last_error)
                with self._ser_lock:
                    try:
                        if self._ser is not None:
                            self._ser.close()
                    except Exception:
                        pass
                    self._ser = None
                continue

            if not raw:
                continue  # timeout — 수신 데이터 없음
            self._ingest(raw, datetime.now())

    def force_disconnect(self, reason: str) -> None:
        """외부 감시자가 핸들을 강제 해제한다 — 좀비 핸들 대응(SPEC §3).

        일부 어댑터/드라이버(시리얼넘버 없는 PL2303 클론 등)는 장치가 죽어도
        읽기 예외 없이 타임아웃만 반복해, 리더 루프가 탈락을 영원히 감지하지
        못한다(2026-06-12 실측: 40분간 connected 인 채 수신 0). 핸들을 즉시
        닫아 OS 재열거 충돌을 막고, 재연결은 기존 리더 루프가 맡는다.
        """
        with self._ser_lock:
            ser, self._ser = self._ser, None
            self.connected = False
            self.last_error = reason
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass
        _log(f"강제 해제: {self.port} — {reason}")

    def _serial_failure(self, prefix: str, exc: Exception) -> serial.SerialException:
        """쓰기/리셋 오류를 재연결 루프가 복구할 수 있는 상태로 변환."""
        self.connected = False
        self.last_error = f"{prefix}: {exc}"
        _log(self.last_error)
        try:
            if self._ser is not None:
                self._ser.close()
        except Exception:
            pass
        self._ser = None
        if isinstance(exc, serial.SerialException):
            return exc
        return serial.SerialException(str(exc))

    def write(self, data: bytes, audit: Optional[str] = None) -> int:
        """페이로드를 포트에 기록한다. 성공하면 audit 텍스트를 TX 감사 기록으로 남긴다.

        char_delay(초)가 0보다 크면 1바이트씩 사이에 지연을 두고 기록한다 — 폴링
        수신 펌웨어(SB-STM32 등)가 기계 속도 연속 바이트를 흘리는 문자 유실 대응.
        보드를 가리지 않고 공통 적용한다(즉답형 펌웨어에도 부작용 없음).
        """
        with self._ser_lock:
            ser = self._ser
            if ser is None or not self.connected:
                raise serial.SerialException(f"포트가 연결되어 있지 않음: {self.port}")
            try:
                if self.char_delay > 0 and len(data) > 1:
                    n = 0
                    for i in range(len(data)):
                        n += ser.write(data[i:i + 1])
                        if i < len(data) - 1:
                            time.sleep(self.char_delay)
                else:
                    n = ser.write(data)
            except Exception as e:  # noqa: BLE001 - pyserial/드라이버 예외를 동일 계약으로 변환
                raise self._serial_failure("쓰기 실패", e) from e
        if audit is not None:
            self._audit_tx(audit, datetime.now())
        return n

    def pulse_reset(self, pulse_s: float = 0.1) -> None:
        """DTR/RTS 펄스로 보드를 일반 부팅 리셋한다(CH343 등 자동리셋 회로용)."""
        with self._ser_lock:
            ser = self._ser
            if ser is None or not self.connected:
                raise serial.SerialException(f"포트가 연결되어 있지 않음: {self.port}")
            try:
                ser.dtr = False
                ser.rts = True
                time.sleep(pulse_s)
                ser.rts = False
            except Exception as e:  # noqa: BLE001 - 배선/드라이버 오류도 도구 레이어가 처리하게 한다
                raise self._serial_failure("리셋 실패", e) from e
        self._audit_tx("[RST] DTR/RTS 하드웨어 리셋 펄스", datetime.now())

    def _audit_tx(self, text: str, ts: datetime) -> None:
        """송신 감사 기록을 버퍼·웹 피드·tee에 남긴다."""
        self.buffer.add(text, ts)
        if self.feed is not None:
            self.feed.publish(ts, text)
        if self._tee is not None:
            try:
                stamp = ts.strftime("%Y-%m-%d %H:%M:%S.") + f"{ts.microsecond // 1000:03d}"
                with self._tee_lock:
                    self._tee.write(f"[{stamp}] {text}\n")
            except Exception as e:  # noqa: BLE001
                _log(f"tee 기록 실패: {e}")

    def _ingest(self, raw: bytes, ts: datetime) -> None:
        """수신 바이트 한 줄을 디코드·정리해 버퍼에 적재하고, tee가 열렸으면 함께 기록.

        무한 I/O 루프(_run)에서 분리한 '한 줄 처리' 단위 — 실제 시리얼 없이 단위
        테스트할 수 있다. 디코드(utf-8/replace)·개행 제거·tee 타임스탬프 형식을
        여기에 고정한다(SPEC §3).
        """
        text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        self.buffer.add(text, ts)
        if self.feed is not None:
            self.feed.publish(ts, text)   # 수신 원본 생중계(빈 줄 포함 — tee와 동일 충실도)
        if self.on_line is not None:
            try:
                self.on_line(ts, text)
            except Exception as e:  # noqa: BLE001 - 훅 오류가 리더 스레드를 죽이면 안 됨
                _log(f"on_line 훅 오류({self.port}): {e!r}")
        if self._tee is not None:
            try:
                stamp = ts.strftime("%Y-%m-%d %H:%M:%S.") + f"{ts.microsecond // 1000:03d}"
                with self._tee_lock:
                    self._tee.write(f"[{stamp}] {text}\n")
            except Exception as e:  # noqa: BLE001
                _log(f"tee 기록 실패: {e}")


# ---- 전역 상태 (main 에서 초기화) ----
mcp = FastMCP("serial-mcp")
# key = 포트명 대문자. ⚠️ 불변식: 핫플러그 스레드 기동 후엔 in-place 변경 금지 —
# 갱신은 _hotplug_scan_once 의 copy-on-write(새 dict 원자 교체)만(단일 작성자 가정.
# 작성자를 추가하려면 — 예: 수동 재스캔 도구 — 교체 구간에 락이 필요해진다).
# 리더 스레드(_autoname_check)·도구가 순회 중인 옛 dict 는 불변이라야 안전하다.
_monitors: dict[str, "PortMonitor"] = {}
_config: dict = {}
_viewer: Optional[ViewerServer] = None
_lock_socket: Optional[socket.socket] = None
_owner_active = False
_owner_lock = threading.RLock()
_autoname_rules: list = []   # SERIAL_AUTONAME 컴파일 결과 [(이름, re.Pattern)]
_autoname_lock = threading.Lock()   # 검사-부여 원자화(동시 리셋 시 중복 이름 방지)
_hotplug_stop = threading.Event()   # 핫플러그 스캔 루프 종료 신호(테스트·향후 정리용)
_hotplug_thread: Optional[threading.Thread] = None
_session_label: Optional[str] = None
_session_lock = threading.Lock()


class _WriteApproval(BaseModel):
    """쓰기 승인 폼 — 빈 스키마라 클라이언트는 수락/거절만 표시한다."""


@dataclass
class PortMonitor:
    """포트 하나의 모니터링 묶음 — 리더·버퍼·생중계 허브(설계 §4)."""

    port: str
    name: Optional[str]               # SERIAL_NAMES 별칭(없으면 None)
    buffer: LineBuffer
    feed: RawFeed
    reader: Optional[SerialReader]    # 테스트에선 SimpleNamespace 주입 가능
    absent_scans: int = 0             # 열거 목록 연속 부재 횟수(핫플러그 스캔 스레드만 갱신)

    @property
    def label(self) -> str:
        return label(self.port, self.name)


def _viewer_url() -> Optional[str]:
    """웹 뷰어 URL — 비활성/기동 실패 시 None."""
    return _viewer.url if _viewer is not None else None


def _get_nested(obj, path: tuple[str, ...]):
    cur = obj
    for part in path:
        if cur is None:
            return None
        if isinstance(cur, Mapping):
            cur = cur.get(part)
        else:
            cur = getattr(cur, part, None)
    return cur


def _client_name_from_ctx(ctx: Optional[Context]) -> Optional[str]:
    """FastMCP Context에서 initialize clientInfo.name을 덕타이핑으로 추출."""
    if ctx is None:
        return None
    paths = (
        ("session", "client_params", "clientInfo", "name"),
        ("session", "client_params", "client_info", "name"),
        ("session", "_client_params", "clientInfo", "name"),
        ("session", "_client_params", "client_info", "name"),
        ("session", "initialize_params", "clientInfo", "name"),
        ("session", "initialize_params", "client_info", "name"),
        ("client_params", "clientInfo", "name"),
        ("client_params", "client_info", "name"),
    )
    for path in paths:
        name = _get_nested(ctx, path)
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None


def _capture_session(ctx: Optional[Context]) -> None:
    """첫 MCP 도구 호출에서 세션 라벨을 1회 캡처해 HTTP 상태에 노출한다."""
    global _session_label
    if _session_label is not None:
        return
    name = _client_name_from_ctx(ctx)
    if name is None:
        return
    with _session_lock:
        if _session_label is None:
            _session_label = name


def _owner_port() -> int:
    raw = _config.get("web", 8743)
    return int(raw) if raw is not None else 8743


def _owner_url(port: Optional[int] = None) -> str:
    return f"http://127.0.0.1:{port if port is not None else _owner_port()}"


def _probe_owner_info(port: int) -> dict:
    """다른 owner의 /api/status를 짧게 조회해 안내에 쓸 세션명을 얻는다."""
    info = {"owner_url": _owner_url(port), "owner_session": None, "reachable": False}
    try:
        with urlopen(info["owner_url"] + "/api/status", timeout=0.3) as response:
            if getattr(response, "status", 200) != 200:
                return info
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, TimeoutError, json.JSONDecodeError):
        return info
    info["reachable"] = True
    session = payload.get("session")
    if isinstance(session, str) and session.strip():
        info["owner_session"] = session.strip()
    return info


def _owner_busy_result(*, for_status: bool = False) -> dict:
    port = _owner_port()
    info = _probe_owner_info(port)
    who = f"({info['owner_session']})" if info["owner_session"] else ""
    prefix = f"다른 serial-mcp 세션{who}이 점유 중입니다. 먼저 해제하세요."
    if info["reachable"]:
        message = f"{prefix} 웹뷰어: {info['owner_url']}"
    else:
        message = f"{prefix} owner 세션을 종료하거나 그 세션 뷰어에서 해제하세요."
    out = {
        "status": "busy" if for_status else "error",
        "message": message,
        "owner_session": info["owner_session"],
        "owner_url": info["owner_url"],
        "viewer_url": None,
    }
    if for_status:
        out.update({"connected": False, "ports": []})
    return out


def _bind_lock_socket(port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        sock.bind(("127.0.0.1", port))
        sock.listen(1)
        return sock
    except Exception:
        sock.close()
        raise


def _start_monitors_locked(cfg: dict) -> None:
    """현재 프로세스가 owner가 된 뒤에만 포트 모니터를 생성·시작한다."""
    global _monitors

    com = list(list_ports.comports())
    specs = cfg["ports"]
    if not specs:
        specs = [(dev, None) for dev in auto_usb_ports(com)]
        _log(f"자동 스캔: USB 시리얼 {len(specs)}개 발견")
    sn_map = {p.device.upper(): getattr(p, "serial_number", None) for p in com}

    monitors: dict[str, PortMonitor] = {}
    for port, baud_override in specs:
        key = port.upper()
        if key in monitors:
            _log(f"중복 포트 무시: {port}")
            continue
        monitors[key] = _make_monitor(port, baud_override, sn_map, cfg)
    _monitors = monitors
    for mon in _monitors.values():
        if mon.reader is not None:
            mon.reader.start()
            _log(f"모니터 시작: {mon.label} @ {mon.reader.baud}")


def _start_hotplug_locked(cfg: dict) -> None:
    global _hotplug_stop, _hotplug_thread

    watch_on = cfg["hotplug"] is not None
    allow_add = not cfg["ports"]
    if not _monitors:
        if watch_on and allow_add:
            _log("경고: 모니터링할 포트 없음 — USB 장비를 연결하면 핫플러그 스캔이 자동 추가한다.")
        else:
            _log("경고: 모니터링할 포트 없음 — USB 시리얼이 안 보이고 SERIAL_PORT 도 "
                 "비어 있다. 장비 연결 후 서버를 재시작하라(핫플러그 꺼짐).")
    if not watch_on:
        _log("포트 감시 꺼짐 (SERIAL_HOTPLUG=0)")
        return

    _hotplug_stop = threading.Event()
    _hotplug_thread = threading.Thread(
        target=_hotplug_loop,
        args=(cfg["hotplug"], _hotplug_stop, allow_add),
        name="hotplug-scan",
        daemon=True,
    )
    _hotplug_thread.start()
    if allow_add:
        _log(f"포트 감시 켜짐 ({cfg['hotplug']:g}초 간격 — 새 포트 추가 + 좀비 핸들 해제)")
    else:
        _log(f"포트 감시 켜짐 ({cfg['hotplug']:g}초 간격 — SERIAL_PORT 고정 모드, "
             "좀비 핸들 해제만 수행(늦은 연결은 재연결 루프가 잡음))")


def _acquire_owner_locked() -> Optional[dict]:
    """8743/SERIAL_WEB bind를 시도해 성공 시 whole-session owner 리소스를 시작한다."""
    global _viewer, _lock_socket, _owner_active

    if _owner_active or _monitors:
        return None

    cfg = _config or _load_config(os.environ)
    port = _owner_port()
    web_ui = bool(cfg.get("web_ui", True))

    try:
        if web_ui:
            viewer = ViewerServer(
                ports_info=_viewer_ports_info,
                feed_for=_viewer_feed_for,
                buffer_info=_viewer_buffer_info,
                status_info=_viewer_status_info,
                release_port=_viewer_release_port,
                port=port,
            )
            if not viewer.start():
                return _owner_busy_result()
            _viewer = viewer
            _log(f"웹 뷰어: {_viewer.url}")
        else:
            try:
                _lock_socket = _bind_lock_socket(port)
            except (OSError, OverflowError) as e:
                _log(f"소유권 잠금 포트 {port} 바인딩 실패: {e}")
                return _owner_busy_result()
            _viewer = None
            _log(f"소유권 잠금: {_owner_url(port)} bind 성공(UI 미서빙)")
        _owner_active = True
        _start_monitors_locked(cfg)
        _start_hotplug_locked(cfg)
        _log(f"소유권 획득 (포트 {len(_monitors)}개, dedup={cfg['dedup']}, "
             f"buffer={cfg['maxlen']}, tee={cfg['tee'] or '없음'})")
        return None
    except Exception:
        _release_owner_locked("소유권 획득 실패 정리")
        raise


def _ensure_owner(ctx: Optional[Context] = None, *, for_status: bool = False) -> Optional[dict]:
    _capture_session(ctx)
    with _owner_lock:
        if _owner_active or _monitors:
            return None
        err = _acquire_owner_locked()
        if err is None:
            return None
        if for_status:
            err["status"] = "busy"
            err.update({"connected": False, "ports": []})
        return err


def _release_owner_locked(reason: str) -> None:
    """COM 핸들 → 뷰어/8743 순서로 whole-session 소유권을 반납한다."""
    global _monitors, _viewer, _lock_socket, _owner_active, _hotplug_thread

    had_owner = _owner_active or bool(_monitors) or _viewer is not None or _lock_socket is not None
    hotplug_thread = _hotplug_thread
    _hotplug_stop.set()
    if hotplug_thread is not None and hotplug_thread is not threading.current_thread():
        try:
            hotplug_thread.join(timeout=1.0)
        except RuntimeError:
            pass

    for mon in list(_monitors.values()):
        reader = mon.reader
        if reader is not None:
            stop = getattr(reader, "stop", None)
            if callable(stop):
                stop()
    _monitors = {}

    viewer, _viewer = _viewer, None
    lock_socket, _lock_socket = _lock_socket, None
    _owner_active = False
    _hotplug_thread = None

    if viewer is not None:
        viewer.stop()
    if lock_socket is not None:
        try:
            lock_socket.close()
        except OSError:
            pass
    if had_owner:
        _log(f"소유권 반납: {reason}")


def _release_owner(reason: str) -> None:
    with _owner_lock:
        _release_owner_locked(reason)


atexit.register(lambda: _release_owner("프로세스 종료"))


def _hw_board_from_name(name: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    if not name:
        return None, None
    alias = name.strip()
    if not alias:
        return None, None
    if "-" not in alias:
        return alias, None
    hw, board = alias.split("-", 1)
    return (hw.strip() or None), (board.strip() or None)


def _autoname_check(mon: PortMonitor, text: str) -> None:
    """로그 내용으로 보드 자동 식별(SERIAL_AUTONAME) — 이름 없는 모니터만, 첫 매칭에서 1회 확정.

    명시 별칭(SERIAL_NAMES)이 항상 우선이고, 이미 다른 포트가 가진 이름은
    부여하지 않는다(오인 방지). 확정 후엔 더 이상 검사하지 않는다(mon.name 세팅).
    """
    if mon.name is not None or not _autoname_rules:
        return
    name = first_autoname_match(text, _autoname_rules)
    if name is None:
        return
    with _autoname_lock:   # 리더 스레드 N개의 동시 첫-매칭(동시 리셋) TOCTOU 방지
        if mon.name is not None:
            return
        if any(m.name == name for m in _monitors.values()):
            return   # 같은 이름이 이미 존재 — 중복 부여 금지
        mon.name = name
    _log(f"자동 식별: {mon.port} → {name} (SERIAL_AUTONAME 패턴 매칭)")


def _resolve_port(port: str) -> tuple[Optional[PortMonitor], Optional[dict]]:
    """도구의 port 인자(별칭/포트명/빈값)를 PortMonitor로 해석.

    반환: (monitor, None) 또는 (None, 에러 dict — 도구가 그대로 반환).
    미지정: 포트 1개면 그 포트(단일 장비 호환), 복수면 목록과 함께 지정 요구.
    """
    if not _monitors:
        return None, {
            "status": "error",
            "message": "모니터링 중인 포트 없음 — USB 연결 또는 SERIAL_PORT 를 확인하라.",
            "ports": [],
        }
    key = (port or "").strip().upper()
    if not key:
        if len(_monitors) == 1:
            mon = next(iter(_monitors.values()))
            return mon, None
        return None, {
            "status": "error",
            "message": "포트가 여러 개다 — port 인자로 지정하라(별칭/포트명 모두 가능).",
            "ports": [m.label for m in _monitors.values()],
        }
    for m in _monitors.values():
        # 라벨 형태("SSM (COM4)")도 허용 — 에러 응답의 ports 목록을 그대로 되돌려도 해석
        if m.port.upper() == key or (m.name and m.name.upper() == key) or m.label.upper() == key:
            return m, None
    return None, {
        "status": "error",
        "message": f"포트 '{port}' 를 모르겠다 — ports 목록에서 골라 다시 호출하라.",
        "ports": [m.label for m in _monitors.values()],
    }


def _write_disabled_result() -> dict:
    return {
        "status": "error",
        "message": "쓰기 비활성(SERIAL_WRITE=off) — 활성화하려면 SERIAL_WRITE 환경변수를 지우거나 1로",
        "count": 0,
        "lines": [],
    }


def _approval_unavailable_result() -> dict:
    return {
        "status": "error",
        "message": "클라이언트가 elicitation(승인 팝업) 미지원 — 승인을 클라이언트 권한 게이트에 위임하려면 SERIAL_WRITE_CONFIRM=off 설정",
    }


# R3(파괴·임의 주입) 명령 — 3개 보드(SSM·SB-ESP·SB-STM) command-surface 합집합.
# reflash/format/firmware-download/파일삭제/임의 JSON 주입. 어느 보드서든 파괴적이라 보드-무관 union.
# SERIAL_WRITE_CONFIRM="r3"일 때 이 목록의 명령만 승인 팝업을 띄운다.
# 단일 진실원은 플러그인 command-surface/atlas다 — 거기가 바뀌면 여기도 동기화하라.
_R3_COMMANDS = frozenset({
    "REFLASH", "REFLASHESP", "REFLASHSTM", "ALLREFLASH",
    "DOWNBIN", "CDOWNBIN", "DOWNBINWEB", "REMFILE",
    "FORMAT", "KSFFORMAT", "WFORMAT",
    "VIRWEBCMD", "BYPASSCMD",
})


def _is_r3(command: str) -> bool:
    """전송 명령이 R3(파괴적)인지 판정. confirm 모드 "r3"에서 이것만 승인 팝업.

    첫 공백구분 토큰을 대문자화해 _R3_COMMANDS와 대조한다("REMFILE foo.txt"도
    첫 토큰으로 잡힘). boot-menu 단독키 'D'(다운로드/리플래시)도 R3로 취급.
    """
    cmd = command.strip()
    if cmd.upper() == "D":
        return True
    parts = cmd.split()
    token = parts[0].upper() if parts else ""
    return token in _R3_COMMANDS


async def _confirm_write(
    ctx: Optional[Context], summary: str, is_risky: bool = True
) -> Optional[dict]:
    """매 호출 사용자 승인 게이트. 통과하면 None, 차단하면 도구 반환 dict.

    승인 범위는 _config["write_confirm"](SERIAL_WRITE_CONFIRM) 3-state로 정한다:
      - "all"(기본): 모든 쓰기에 elicitation 승인.
      - "r3": R3(파괴) 명령만 승인 — is_risky=False면 elicitation 생략·통과.
      - "off": 승인 생략 후 클라이언트 권한 게이트에 위임.
    """
    mode = _config.get("write_confirm", "all")
    if mode == "off":
        return None
    if mode == "r3" and not is_risky:
        return None
    capability = ClientCapabilities(elicitation=ElicitationCapability())
    if ctx is None or not ctx.session.check_client_capability(capability):
        return _approval_unavailable_result()
    try:
        result = await ctx.elicit(message=summary, schema=_WriteApproval)
    except McpError:
        return _approval_unavailable_result()
    if result.action == "accept":
        return None
    return {
        "status": "declined",
        "message": "사용자가 전송을 거부했다 — 같은 명령을 재시도하지 말고, 사람에게 이유를 묻고 다음 행동을 합의하라.",
    }


def _clamp_wait_ms(wait_ms: int) -> int:
    return max(0, min(int(wait_ms), 30000))


@mcp.tool()
def list_serial_ports(ctx: Optional[Context] = None) -> dict:
    """[언제 호출] 어느 포트가 어느 보드인지 확인할 때, 모니터링 대상을 점검할 때.

    [무엇을 반환] 현재 PC의 시리얼 포트 목록. 각 포트의 device/description/vid/pid/
    manufacturer/serial_number 에 더해, 이 서버가 모니터링 중이면 monitored=true 와
    별칭 name 이 붙는다. monitored_ports 는 현재 모니터링 목록(별칭 표기).
    VID/PID·description 은 어댑터 칩(CH343, CP210x 등) 식별용일 뿐 보드 종류를
    단정하지 마라(칩≠보드). 보드 식별은 별칭 name 이며, 별칭이 없으면 로그 내용으로
    식별하거나 사람에게 포트↔보드 매핑을 물어라.

    [주의] 이 도구는 OS 포트 나열만 한다 — owner 획득/모니터링을 시작하지 않는다.
    monitored 가 전부 false(0개 모니터링)인데 USB 포트가 보이면 '모니터 대상이 없다'가
    아니라 아직 owner 미획득일 뿐이다. get_serial_status 를 호출하면 owner 를 잡고 USB
    포트를 자동으로 모니터링한다 — 모니터 대상은 서버가 자동 결정하므로 **어느 포트를
    잡을지 사람에게 묻지 마라**(SSM 등 별칭도 AI가 아니라 SERIAL_NAMES/AUTONAME 이 만든다).
    응답의 hint 도 같은 안내다.

    [루프 단계] 사전 점검 — 보통 한 번만.
    """
    _capture_session(ctx)
    monitored = {m.port.upper(): m for m in _monitors.values()}
    ports = []
    for p in list_ports.comports():
        mon = monitored.get(p.device.upper())
        ports.append(
            {
                "device": p.device,
                "description": p.description,
                "hwid": p.hwid,
                "vid": p.vid,
                "pid": p.pid,
                "manufacturer": p.manufacturer,
                "serial_number": p.serial_number,
                "monitored": mon is not None,
                "name": mon.name if mon else None,
            }
        )
    result = {
        "status": "ok",
        "message": f"{len(ports)}개 포트 발견, {len(_monitors)}개 모니터링 중",
        "monitored_ports": [m.label for m in _monitors.values()],
        "ports": ports,
    }
    if not _monitors and any(p["vid"] is not None for p in ports):
        # owner 미획득 + USB 포트 존재 → 모니터링이 아직 안 시작된 것일 뿐.
        # AI가 'list 의 0개'를 보고 사람에게 포트 선택을 묻는 오작동을 막는 런타임 안내.
        result["hint"] = (
            "0개 모니터링 중 — 아직 owner 미획득 상태다. get_serial_status 를 호출하면 owner 를 "
            "잡고 USB 포트를 자동 모니터링한다(모니터 대상은 서버가 자동 결정 — 어느 포트를 잡을지 "
            "사람에게 묻지 마라). 0개가 계속되면 다른 세션이 점유 중일 수 있으니 웹 뷰어 release 를 안내하라."
        )
    return result


# get_serial_status 의 '첫 open' 동기화 -----------------------------------------
# 서버는 첫 시리얼 도구 호출 때 owner/reader 를 lazy 기동하고, reader.start() 는
# 스레드만 띄운 뒤 즉시 반환한다(_open 은 백그라운드). 동기화 없이 곧장 스냅샷하면,
# 그 첫 호출 스스로가 띄운 reader 가 포트를 열기 전이라 전 포트가 connected=false 로
# 보이는 self-trigger race 가 난다(과거 'AI가 보드 꺼짐 오판'의 코드 원인).
# → status 는 '첫 open 결판'을 최대 _STATUS_FIRST_OPEN_WAIT_S 초 기다린 뒤 스냅샷한다.
_STATUS_FIRST_OPEN_WAIT_S = 1.5   # 상한(보통 1초 미만 결판). 테스트는 0 으로 낮춘다.


def _await_first_open(budget_s: float) -> None:
    """모니터들의 '첫 open 시도'가 결판날 때까지 최대 budget_s 동안 기다린다.

    공유 예산(여러 포트가 함께 그 안에 결판). _first_open_done 신호가 오면 즉시 깨고,
    이미 결판난 포트는 대기 0 — 둘째 호출부터는 사실상 지연이 없다. 죽은 포트는 첫
    _open 이 즉시 실패하며 Event 를 set 하므로 여기 매달리지 않는다.
    """
    if budget_s <= 0:
        return
    deadline = time.monotonic() + budget_s
    for m in list(_monitors.values()):
        ev = getattr(m.reader, "_first_open_done", None)
        if ev is None:
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        ev.wait(remaining)


def _status_message(d: dict) -> str:
    """단일 포트 status 의 사람용 message. 과도기 '연결 중'은 노출하지 않는다 —
    연결 안 된 모든 경우는 '안 됨'으로 읽히되, 첫 open 이 대기 후에도 안 끝난 지연/먹통은
    '응답 없음', 실패 이유(last_error)가 있으면 그 이유를 붙인다."""
    if d["connected"]:
        return "연결됨"
    if d.get("opening"):
        return "안 됨(응답 없음 — 여는 중)"
    if d.get("last_error"):
        return f"안 됨: {d['last_error']}"
    return "안 됨"


@mcp.tool()
def get_serial_status(port: str = "", ctx: Optional[Context] = None) -> dict:
    """[언제 호출] 로그가 안 들어올 때 '어느 보드가 연결돼 있는지'부터 확인할 때.
    포트 점유/미연결/미인식 원인을 구분한다.

    [무엇을 반환] port 미지정 시 모니터링 중인 전 포트의 상태 배열(ports).
    port(별칭 "SSM" 또는 포트명 "COM4") 지정 시 그 포트의 단일 상태.
    connected 가 false 이고 last_error 에 점유/권한 에러가 있으면 사람에게 같은
    포트를 쓰는 다른 프로그램(테라텀 등) 종료를 요청하라.

    [필수] 세션에서 이 도구를 처음 호출하면, 사용자가 요청하지 않아도 응답의
    viewer_url(웹 뷰어) 링크를 사용자에게 안내하라 — 사용자가 같은 실시간 스트림을
    눈으로 교차검증하게 하기 위함. URL 포트는 기본 8743 고정이며 SERIAL_WEB 으로만
    바뀐다(재시작으로는 안 바뀜) — 하드코딩 말고 매번 응답에서 읽되 값은 안정적이다
    (없으면 SERIAL_WEB=0 — 링크 없이 진행).

    [상태 해석] connected=false 라도 opening=true(message '응답 없음')면 '꺼짐'이
    아니라 첫 연결 시도 지연이다. 실제 미연결/실패는 last_error 로 판단하라 —
    last_error 없이 connected=false 면 보통 막 lazy 기동해 여는 중이니 잠시 후 재확인.
    [루프 단계] 문제 진단.
    """
    busy = _ensure_owner(ctx, for_status=True)
    if busy:
        return busy

    # 첫 open 결판을 잠깐 기다려 self-trigger race 를 없앤다(둘째 호출부터는 지연 0).
    _await_first_open(_STATUS_FIRST_OPEN_WAIT_S)

    def one(m: PortMonitor) -> dict:
        r = m.reader
        ev = getattr(r, "_first_open_done", None)
        return {
            "name": m.name,
            "label": m.label,
            "port": m.port,
            "connected": bool(r and r.connected),
            "opening": bool(r) and ev is not None and not ev.is_set(),
            "baud": r.baud if r else None,
            "last_error": r.last_error if r else None,
            "opened_at": r.opened_at.isoformat() if r and r.opened_at else None,
        }

    if (port or "").strip():
        mon, err = _resolve_port(port)
        if err:
            return {**err, "connected": False, "viewer_url": _viewer_url()}
        d = one(mon)
        d["status"] = "ok"
        d["message"] = _status_message(d)
        d["viewer_url"] = _viewer_url()
        return d
    if not _monitors:
        return {
            "status": "error",
            "message": "모니터링 중인 포트 없음 — USB 연결 또는 SERIAL_PORT 를 확인하라.",
            "connected": False,
            "ports": [],
            "viewer_url": _viewer_url(),
        }
    plist = [one(m) for m in _monitors.values()]
    n_on = sum(1 for x in plist if x["connected"])
    return {
        "status": "ok",
        "message": f"{n_on}/{len(plist)} 포트 연결됨",
        "ports": plist,
        "viewer_url": _viewer_url(),
    }


@mcp.tool()
def get_recent_logs(lines: int = 200, port: str = "", ctx: Optional[Context] = None) -> dict:
    """[언제 호출] 블랙박스 루프의 '결과 확인' 단계 — 사람이 장비를 동작시킨 뒤
    쌓인 로그를 확인할 때. 가장 자주 쓰는 도구.

    [port 규약] 보드가 여러 개면 port 를 지정하라(별칭 "SSM" 또는 "COM4", 대소문자
    무관). 미지정: 포트 1개면 그 포트, 복수면 에러와 함께 ports 목록을 돌려준다 —
    목록에서 골라 즉시 재호출하면 된다.

    [무엇을 반환] 최근 N개 라인(시간 오름차순). 근접 중복은 룩백으로 접혀
    '(N회 반복, HH:MM:SS~HH:MM:SS)' 표기 — 접힘은 요약이라 반복 줄들의 정밀한
    교차 순서는 뭉개진다. 정밀 순서가 필요하면 SERIAL_DEDUP=1 또는 0 으로 낮춰
    재시험하라(tee 파일엔 원본 보존).

    [팁] 결과가 많으면 query_serial_logs 로 좁혀라. 비어 있으면 get_serial_status
    로 연결을 확인하고, 그래도 비면 사람에게 장비 동작/리셋을 요청하라.

    [루프 단계] 결과 확인.
    """
    busy = _ensure_owner(ctx)
    if busy:
        return {**busy, "count": 0, "lines": []}
    mon, err = _resolve_port(port)
    if err:
        return {**err, "count": 0, "lines": []}
    got = mon.buffer.get_recent(lines)
    return {
        "status": "ok",
        "message": f"{mon.label}: {len(got)}줄 반환",
        "port": mon.port,
        "name": mon.name,
        "count": len(got),
        "lines": got,
    }


@mcp.tool()
def query_serial_logs(
    pattern: str,
    max_results: int = 100,
    port: str = "",
    ctx: Optional[Context] = None,
) -> dict:
    """[언제 호출] 특정 키워드/에러/마커를 버퍼에서 찾을 때. 예: 부팅 완료 문구,
    'ERROR', 특정 상태 출력의 등장 여부.

    [port 규약] get_recent_logs 와 동일 — 복수 포트면 지정, 미지정 에러 시 ports
    목록에서 골라 재호출.

    [무엇을 반환] 정규식 pattern 매칭 라인들(최신 우선 max_results개, 반환은 시간
    오름차순, 접힌 묶음 표기 포함). 매칭 0이면 그 문구가 아직 안 나온 것 — 사람에게
    해당 동작을 요청하거나 더 기다린 뒤 재조회하라.

    [루프 단계] 결과 확인(표적 검색).
    """
    busy = _ensure_owner(ctx)
    if busy:
        return {**busy, "count": 0, "lines": []}
    mon, err = _resolve_port(port)
    if err:
        return {**err, "count": 0, "lines": []}
    try:
        got = mon.buffer.query(pattern, max_results)
    except re.error as e:
        return {"status": "error", "message": f"정규식 오류: {e}", "count": 0, "lines": []}
    return {
        "status": "ok",
        "message": f"{mon.label}: {len(got)}줄 매칭",
        "port": mon.port,
        "name": mon.name,
        "pattern": pattern,
        "count": len(got),
        "lines": got,
    }


@mcp.tool()
def get_log_buffer_info(port: str = "", ctx: Optional[Context] = None) -> dict:
    """[언제 호출] 버퍼가 얼마나 찼는지, 최근/최오래 항목이 무엇인지 빠르게 볼 때.
    clear_log_buffer 직후 새 로그 유입을 폴링할 때 특히 유용.

    [port 규약] get_recent_logs 와 동일.

    [무엇을 반환] entries/capacity, oldest/newest, 누적 total_received/total_stored,
    dedup(룩백 윈도 — 0이면 끔).

    [루프 단계] 진행 점검(폴링).
    """
    busy = _ensure_owner(ctx)
    if busy:
        return busy
    mon, err = _resolve_port(port)
    if err:
        return err
    info = mon.buffer.info()
    info["status"] = "ok"
    info["message"] = f"{mon.label}: {info['entries']}/{info['capacity']} 항목"
    info["port"] = mon.port
    info["name"] = mon.name
    info["viewer_url"] = _viewer_url()
    return info


@mcp.tool()
def clear_log_buffer(port: str = "", ctx: Optional[Context] = None) -> dict:
    """[언제 호출] 블랙박스 시험의 '시작' 단계 — 새 시험을 깨끗한 상태에서
    관측하려고 직전 로그를 비울 때. 표준 절차: 비우고 → 사람에게 장비 동작/리셋
    요청 → 잠시 후 get_recent_logs 로 회수.

    [port 규약] 다른 도구와 달리 **미지정 = 전체 포트 비우기**(시험 시작 시 모든
    보드를 함께 리셋 관측하는 게 보통이므로). 특정 보드만 비우려면 port 지정.
    단 기존 문제의 원인을 분석 중이면 비우기 전에 먼저 get_recent_logs/query_serial_logs
    로 snapshot 하라 — 원인 로그가 지워진다. 다른 보드의 맥락 로그를 보존하려면
    반드시 port 를 지정해 선택 비우기.

    [무엇을 반환] cleared(총 비운 항목 수)와 ports(포트별 내역).

    [루프 단계] 시험 시작.
    """
    busy = _ensure_owner(ctx)
    if busy:
        return {"status": "error", "message": busy["message"], "cleared": 0,
                "ports": {}, "owner_session": busy.get("owner_session"),
                "owner_url": busy.get("owner_url"), "viewer_url": None}
    if not _monitors:
        return {"status": "error", "message": "모니터링 중인 포트 없음", "cleared": 0, "ports": {}}
    if (port or "").strip():
        mon, err = _resolve_port(port)
        if err:
            # 이 도구의 ports 키는 항상 dict(포트별 내역) — 후보 목록은 available_ports로
            e = dict(err)
            e["available_ports"] = e.pop("ports", [])
            return {**e, "cleared": 0, "ports": {}}
        n = mon.buffer.clear()
        return {"status": "ok", "message": f"{mon.label}: {n}개 항목 비움",
                "cleared": n, "ports": {mon.port: n}}
    detail = {m.port: m.buffer.clear() for m in _monitors.values()}
    total = sum(detail.values())
    return {"status": "ok", "message": f"전체 {len(detail)}개 포트에서 {total}개 비움",
            "cleared": total, "ports": detail}


@mcp.tool()
async def send_serial_command(
    command: str,
    port: str = "",
    eol: str = "\n",
    wait_ms: int = 500,
    ctx: Optional[Context] = None,
) -> dict:
    """[언제 호출] 보드 CLI/AT 명령을 전송하고 직후 응답 로그를 한 번에 회수할 때.

    승인이 필요한 호출(SERIAL_WRITE_CONFIRM=all, 또는 r3 모드의 R3 파괴 명령)이면
    사용자 승인 팝업이 뜬다. 거부되면 status="declined"를 반환하므로 같은 명령을
    반복 재시도하지 말고 사람과 다음 행동을 합의하라.

    [port 규약] get_recent_logs 와 동일 — 별칭("SSM")/포트명("COM4")/라벨("SSM (COM4)")
    모두 허용. 복수 포트에서 미지정이면 ports 목록과 함께 에러.

    [무엇을 반환] sent/eol/bytes/wait_ms/count/lines. 전송 감사 마커 [TX]가 lines에
    함께 포함될 수 있다.

    [루프 단계] 능동 시험(쓰기).
    """
    _capture_session(ctx)
    if not _config.get("write", True):
        return _write_disabled_result()
    allowed_eol = {"\n", "\r\n", "\r", ""}
    if eol not in allowed_eol:
        return {
            "status": "error",
            "message": "eol은 '\\n', '\\r\\n', '\\r', '' 중 하나여야 한다.",
            "count": 0,
            "lines": [],
        }
    if command == "" and eol == "":
        return {"status": "error", "message": "빈 페이로드는 전송하지 않는다.", "count": 0, "lines": []}
    wait_ms = _clamp_wait_ms(wait_ms)
    was_owner = _owner_active or bool(_monitors)
    busy = _ensure_owner(ctx)
    if busy:
        return {**busy, "count": 0, "lines": []}
    mon, err = _resolve_port(port)
    if err:
        return {**err, "count": 0, "lines": []}
    payload = (command + eol).encode("utf-8")
    block = await _confirm_write(
        ctx,
        f"{mon.label} 대상으로 시리얼 명령 전송 승인 요청\n명령: {command!r} (eol={eol!r}, {len(payload)}바이트)",
        is_risky=_is_r3(command),
    )
    if block:
        if not was_owner:
            _release_owner("쓰기 승인 차단 — 새 owner 반납")
        return {**block, "count": 0, "lines": []}
    t0 = datetime.now()
    try:
        mon.reader.write(payload, audit=f"[TX] {command}")
    except serial.SerialException as e:
        return {"status": "error", "message": f"{mon.label}: 전송 실패 — {e}", "count": 0, "lines": []}
    # stdio 단일 클라이언트 환경에서는 짧은 write/pulse 직접 호출과 asyncio.sleep으로 충분하다.
    await asyncio.sleep(wait_ms / 1000)
    lines = mon.buffer.entries_since(t0)
    return {
        "status": "ok",
        "message": f"{mon.label}: {len(payload)}바이트 전송, {wait_ms}ms 대기 후 {len(lines)}줄 회수",
        "port": mon.port,
        "name": mon.name,
        "sent": command,
        "eol": eol,
        "bytes": len(payload),
        "wait_ms": wait_ms,
        "count": len(lines),
        "lines": lines,
    }


@mcp.tool()
async def reset_board(port: str = "", wait_ms: int = 2000, ctx: Optional[Context] = None) -> dict:
    """[언제 호출] 블랙박스 루프 시작 시 사람이 누르던 물리 리셋을 AI가 직접 수행할 때.

    DTR/RTS 자동리셋 회로가 있는 보드에서만 동작한다. native-USB 또는 미배선 보드는
    예외 없이 0줄 회수로 끝날 수 있으므로, 이때는 사람에게 물리 리셋을 요청하라.

    [port 규약] get_recent_logs 와 동일.

    [무엇을 반환] wait_ms/count/lines. 성공 시 [RST] 감사 마커가 버퍼/웹 피드/tee에
    기록될 수 있다.

    [루프 단계] 시험 시작(리셋).
    """
    _capture_session(ctx)
    if not _config.get("write", True):
        return _write_disabled_result()
    wait_ms = _clamp_wait_ms(wait_ms)
    was_owner = _owner_active or bool(_monitors)
    busy = _ensure_owner(ctx)
    if busy:
        return {**busy, "count": 0, "lines": []}
    mon, err = _resolve_port(port)
    if err:
        return {**err, "count": 0, "lines": []}
    block = await _confirm_write(
        ctx,
        f"{mon.label} 보드를 DTR/RTS 펄스로 하드웨어 리셋합니다. 승인하시겠습니까?",
        is_risky=False,  # 리셋은 R2 — "r3" 모드에선 승인 없이 통과
    )
    if block:
        if not was_owner:
            _release_owner("리셋 승인 차단 — 새 owner 반납")
        return {**block, "count": 0, "lines": []}
    t0 = datetime.now()
    try:
        mon.reader.pulse_reset()
    except serial.SerialException as e:
        return {"status": "error", "message": f"{mon.label}: 리셋 실패 — {e}", "count": 0, "lines": []}
    await asyncio.sleep(wait_ms / 1000)
    lines = mon.buffer.entries_since(t0)
    msg = f"{mon.label}: 리셋 펄스 전송, {wait_ms}ms 대기 후 {len(lines)}줄 회수"
    if not lines:
        msg += " — 부팅 로그 없음 — native-USB 보드이거나 자동리셋 미배선일 수 있다. 사람에게 물리 리셋을 요청하라"
    return {
        "status": "ok",
        "message": msg,
        "port": mon.port,
        "name": mon.name,
        "wait_ms": wait_ms,
        "count": len(lines),
        "lines": lines,
    }


def _env_int(env: Mapping[str, str], name: str, default: int) -> int:
    """정수 환경변수 파싱 — 미설정/빈값/오류 시 기본값. env 주입형(테스트 용이)."""
    v = env.get(name)
    if v is None or v.strip() == "":
        return default
    try:
        return int(v)
    except ValueError:
        _log(f"환경변수 {name}={v!r} 정수 변환 실패 → 기본값 {default} 사용")
        return default


def _viewer_ports_info() -> list[dict]:
    """웹 뷰어 /api/ports — 셀렉터 구성용 [{port, label}] 목록."""
    return [{"port": m.port, "label": m.label} for m in _monitors.values()]


def _viewer_feed_for(port: str) -> Optional[RawFeed]:
    """웹 뷰어 /api/stream?port= — 해당 포트의 RawFeed(없으면 None→404)."""
    m = _monitors.get((port or "").strip().upper())
    return m.feed if m else None


def _viewer_buffer_info(port: str) -> dict:
    """웹 뷰어 /api/buffer?port= — 해당 포트의 구조화 스냅샷 + 카운터."""
    m = _monitors.get((port or "").strip().upper())
    if m is None:
        return {"status": "error", "entries": [], "capacity": 0}
    info = m.buffer.info()
    return {
        "status": "ok",
        "port": m.port,
        "entries": m.buffer.snapshot(),
        "capacity": info["capacity"],
        "total_received": info["total_received"],
        "total_stored": info["total_stored"],
        "dedup": info["dedup"],
    }


def _viewer_status_info() -> dict:
    """웹 뷰어 /api/status — 전 포트 상태 배열(+버퍼 적재 현황, 탭 카운터용)."""
    plist = []
    for m in _monitors.values():
        r = m.reader
        binfo = m.buffer.info()
        hw, board = _hw_board_from_name(m.name)
        plist.append({
            "port": m.port,
            "label": m.label,
            "connected": bool(r and r.connected),
            "baud": r.baud if r else None,
            "last_error": r.last_error if r else None,
            "buffer_entries": binfo["entries"],
            "buffer_capacity": binfo["capacity"],
            "hw": hw,
            "board": board,
        })
    return {"session": _session_label, "ports": plist}


def _viewer_release_port(port: str) -> dict:
    """웹 뷰어 /api/release — 포트 인자와 무관하게 세션 전체 소유권을 반납."""
    _release_owner("뷰어 release — 사용자 양보")
    return {"status": "ok", "released": True}


def _parse_dedup(env: Mapping[str, str]) -> int:
    """SERIAL_DEDUP 파싱 — 룩백 윈도. 기본 5, 0/false=끔, 1/true=직전 줄만(구버전)."""
    raw = env.get("SERIAL_DEDUP", "").strip().lower()
    if raw == "":
        return 5
    if raw in ("0", "false", "no", "off"):
        return 0
    if raw in ("1", "true", "yes", "on"):
        return 1
    try:
        n = int(raw)
        if n >= 0:
            return n
    except ValueError:
        pass
    _log(f"환경변수 SERIAL_DEDUP={raw!r} 해석 실패 → 기본 룩백 5 사용")
    return 5


def _parse_maxlen(env: Mapping[str, str]) -> int:
    """SERIAL_BUFFER_LINES 파싱 — 0 이하면 기본 2000(deque(maxlen<0)은 기동 실패)."""
    n = _env_int(env, "SERIAL_BUFFER_LINES", 2000)
    if n <= 0:
        _log(f"환경변수 SERIAL_BUFFER_LINES={n} 은 1 이상이어야 함 → 기본 2000 사용")
        return 2000
    return n


def _parse_web(env: Mapping[str, str]) -> tuple[int, bool]:
    """SERIAL_WEB 파싱 — 기본 8743(켜짐). 0/false/no/off → 8743 잠금만, UI 미서빙.

    1~65535 밖이면 기본값으로 — 범위 밖 포트는 socket.bind에서 OverflowError로
    서버 기동 자체를 죽일 수 있다(뷰어 실패는 본체에 영향 없어야 한다는 불변식).
    """
    raw = env.get("SERIAL_WEB", "").strip()
    if raw == "":
        return 8743, True
    if raw.lower() in ("0", "false", "no", "off"):
        return 8743, False
    try:
        n = int(raw)
        if 1 <= n <= 65535:
            return n, True
    except ValueError:
        pass
    _log(f"환경변수 SERIAL_WEB={raw!r} 해석 실패(1~65535 필요) → 기본 포트 8743 사용")
    return 8743, True


def _parse_hotplug(env: Mapping[str, str]) -> Optional[float]:
    """SERIAL_HOTPLUG 파싱 — 핫플러그 스캔 간격(초). 기본 5(켜짐).

    0(0.0 포함)/false/no/off → 끔(None), 유한 양수(소수 허용) → 간격. 자동 스캔
    모드에서만 의미가 있다(SERIAL_PORT 고정 목록 모드는 스캔 스레드를 띄우지 않음).
    """
    raw = env.get("SERIAL_HOTPLUG", "").strip().lower()
    if raw == "":
        return 5.0
    if raw in ("false", "no", "off"):
        return None
    try:
        n = float(raw)
        if n == 0:
            return None   # "0"·"0.0" 모두 끔 — 표기 차이로 켜짐/꺼짐이 갈리면 안 됨
        if n > 0 and math.isfinite(n):
            return n      # inf 는 사실상 무음 비활성이라 해석 실패로 취급
    except ValueError:
        pass
    _log(f"환경변수 SERIAL_HOTPLUG={raw!r} 해석 실패(양수 초 필요) → 기본 5초 사용")
    return 5.0


def _parse_char_delay(env: Mapping[str, str]) -> float:
    """SERIAL_CHAR_DELAY 파싱 — 전송 문자 간 지연(ms). 기본 10(켜짐), 반환은 초.

    폴링 수신 펌웨어(SB-STM32 등)가 기계 속도 연속 바이트를 흘리는 문자 유실 대응
    (실측 근거 2026-06-12: 'HELP' 송신 → 보드 수신 'HLP'). 0/false/no/off → 끔(0.0),
    유한 양수(소수 허용, 상한 100ms) → 지연. 해석 실패는 기본값.
    """
    raw = env.get("SERIAL_CHAR_DELAY", "").strip().lower()
    if raw == "":
        return 0.010
    if raw in ("false", "no", "off"):
        return 0.0
    try:
        n = float(raw)
        if n == 0:
            return 0.0   # "0"·"0.0" 모두 끔 — 표기 차이로 켜짐/꺼짐이 갈리면 안 됨
        if n > 0 and math.isfinite(n):
            return min(n, 100.0) / 1000.0   # 과도한 값이 이벤트 루프를 오래 막지 않게 상한
    except ValueError:
        pass
    _log(f"환경변수 SERIAL_CHAR_DELAY={raw!r} 해석 실패 → 기본 10ms 사용")
    return 0.010


def _parse_flag(env: Mapping[str, str], name: str, default: bool = True) -> bool:
    """불리언 환경변수 파싱 — 미설정/빈값은 기본값, 해석 실패도 _log 후 기본값."""
    raw = env.get(name, "").strip().lower()
    if raw == "":
        return default
    if raw in ("0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    _log(f"환경변수 {name}={raw!r} 해석 실패 → 기본값 {default} 사용")
    return default


def _parse_confirm_mode(env: Mapping[str, str], name: str) -> str:
    """SERIAL_WRITE_CONFIRM 3-state 파싱 → "all" | "r3" | "off".

    off/false/0/no → "off"(확인 생략·클라이언트 게이트 위임),
    r3/risky/risk  → "r3"(R3 파괴 명령만 확인),
    그 외(true/1/on/yes/미설정/해석불가) → "all"(모든 쓰기 확인 — 안전 기본).
    """
    raw = env.get(name, "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return "off"
    if raw in ("r3", "risky", "risk"):
        return "r3"
    if raw not in ("", "1", "true", "yes", "on"):
        _log(f"환경변수 {name}={raw!r} 해석 실패 → 기본값 'all' 사용")
    return "all"


def _load_config(env: Mapping[str, str]) -> dict:
    """환경변수 매핑에서 서버 설정을 파싱해 dict로 반환(부작용 없음, 순수 함수).

    main()이 이 결과로 LineBuffer/SerialReader를 구성한다. I/O·스레드 시작과
    분리돼 있어 환경변수 계약(SPEC §3/§4)을 단독 테스트할 수 있다.
    """
    web_port, web_ui = _parse_web(env)
    return {
        "ports": parse_port_list(env.get("SERIAL_PORT", "")),   # [] = USB 자동 스캔
        "names": parse_names(env.get("SERIAL_NAMES", "")),
        "autoname": parse_autoname(env.get("SERIAL_AUTONAME", "")),
        "baud": _env_int(env, "SERIAL_BAUD", 115200),
        "tee": env.get("SERIAL_TEE", "").strip() or None,
        "exclude": env.get("SERIAL_EXCLUDE", "").strip() or None,
        "include": env.get("SERIAL_INCLUDE", "").strip() or None,
        "maxlen": _parse_maxlen(env),
        "dedup": _parse_dedup(env),
        "web": web_port,
        "web_ui": web_ui,
        "hotplug": _parse_hotplug(env),
        "write": _parse_flag(env, "SERIAL_WRITE"),
        "write_confirm": _parse_confirm_mode(env, "SERIAL_WRITE_CONFIRM"),
        "char_delay": _parse_char_delay(env),
    }


def _tee_path_for(base: Optional[str], tag: str) -> Optional[str]:
    """포트별 tee 파일 경로 — 'log.txt' + 'SSM' → 'log.SSM.txt'(파일명 안전화)."""
    if not base:
        return None
    safe = re.sub(r"[^\w\-]", "_", tag)
    p = Path(base)
    return str(p.with_name(f"{p.stem}.{safe}{p.suffix}"))


def _make_monitor(
    port: str,
    baud_override: Optional[int],
    sn_map: Mapping[str, Optional[str]],
    cfg: dict,
) -> PortMonitor:
    """포트 하나의 모니터 조립 — main() 기동과 핫플러그 추가가 공유하는 단일 규칙.

    버퍼·피드 생성, 별칭 해석(SERIAL_NAMES — 포트명/시리얼넘버 키), 무명 포트의
    autoname 훅 장착, tee 경로 산출까지. 리더 start()는 호출자가 등록을 끝낸 뒤
    수행한다(등록 전 시작 금지 — _autoname_check의 _monitors 순회와 race 방지).
    """
    baud = baud_override or cfg["baud"]
    name = name_for(port, sn_map.get(port.upper()), cfg["names"])
    buf = LineBuffer(maxlen=cfg["maxlen"], dedup=cfg["dedup"],
                     exclude=cfg["exclude"], include=cfg["include"])
    feed = RawFeed()
    mon = PortMonitor(port=port, name=name, buffer=buf, feed=feed, reader=None)
    on_line = None
    if _autoname_rules and name is None:   # 명시 별칭 없을 때만 자동 식별 후킹
        on_line = (lambda ts, text, m=mon: _autoname_check(m, text))
    mon.reader = SerialReader(port=port, baud=baud, buffer=buf,
                              tee_path=_tee_path_for(cfg["tee"], name or port),
                              feed=feed, on_line=on_line,
                              char_delay=cfg["char_delay"])
    return mon


_hotplug_pending: set[str] = set()   # 직전 스캔에서 처음 목격된 새 포트(정착 유예 대기)


def _hotplug_scan_once(allow_add: bool = True) -> list[str]:
    """포트 감시 1회 스캔 — 좀비 핸들 해제 + (allow_add 시) 새 USB 포트 추가.

    좀비 해제: connected 라고 믿는 포트가 열거 목록에서 **연속 2회** 사라지면
    핸들을 강제로 닫는다. 일부 드라이버는 장치가 죽어도 읽기 예외를 던지지
    않아 리더 루프가 탈락을 감지하지 못하고, 그 사이 좀비 핸들이 재열거를
    방해한다(2026-06-12 실측). 2회 유예는 일시적 열거 누락 플래핑 방지.

    추가 디바운스: 새 포트는 **연속 2회** 스캔에서 보여야 추가한다 — 막
    열거된 장치를 드라이버 초기화 중에 여는 것을 피한다(정착 유예).

    _monitors 는 copy-on-write 로만 갱신한다(새 dict 생성 → 전역 참조 원자 교체).
    리더 스레드(_autoname_check)·도구 호출이 옛 dict 를 순회 중이어도 안전하다.
    사라진 포트의 모니터는 제거하지 않는다 — 버퍼·tee 를 보존하고, 재연결은
    SerialReader 의 재시도 루프가 담당한다.
    """
    global _monitors, _hotplug_pending
    com = list(list_ports.comports())

    # 1) 좀비 해제 — 부재 카운터는 이 스레드만 갱신한다
    present = {p.device.upper() for p in com}
    for mon in _monitors.values():
        if mon.reader.connected and mon.port.upper() not in present:
            mon.absent_scans += 1
            if mon.absent_scans >= 2:
                mon.absent_scans = 0
                mon.reader.force_disconnect(
                    "장치가 열거 목록에서 사라짐(연속 2회) — 좀비 핸들 해제")
        else:
            mon.absent_scans = 0

    # 2) 새 포트 추가 (자동 스캔 모드 전용)
    if not allow_add:
        return []
    fresh = [d for d in auto_usb_ports(com) if d.upper() not in _monitors]
    confirmed = [d for d in fresh if d.upper() in _hotplug_pending]
    _hotplug_pending = {d.upper() for d in fresh} - {d.upper() for d in confirmed}
    if not confirmed:
        return []
    sn_map = {p.device.upper(): getattr(p, "serial_number", None) for p in com}
    added = [_make_monitor(d, None, sn_map, _config) for d in confirmed]
    # 등록을 먼저 끝낸 뒤 리더 시작(main 의 1·2패스와 동일한 순서 보장)
    _monitors = {**_monitors, **{m.port.upper(): m for m in added}}
    for m in added:
        m.reader.start()
        _log(f"핫플러그: 모니터 추가 {m.label} @ {m.reader.baud}")
    return [m.port for m in added]


def _hotplug_loop(interval: float, stop: threading.Event, allow_add: bool = True) -> None:
    """포트 감시 루프(데몬 스레드 본체) — 어떤 예외에도 죽지 않는다.

    stop.wait(interval) 가 타이머 겸 종료 신호 수신을 겸한다(즉시 반응).
    """
    while not stop.wait(interval):
        try:
            _hotplug_scan_once(allow_add=allow_add)
        except Exception as e:  # noqa: BLE001 - 스캔 실패가 스레드를 죽이면 안 됨
            _log(f"포트 감시 스캔 오류: {e!r}")


def main() -> None:
    """엔트리포인트. 기동 시엔 휴면, 첫 시리얼 도구 호출 때 owner를 획득한다."""
    global _config, _autoname_rules

    cfg = _load_config(os.environ)
    _config = cfg
    _autoname_rules = compile_autoname(cfg["autoname"], log=_log)

    if cfg["web_ui"]:
        _log(f"휴면 시작 — 첫 시리얼 도구 호출 때 {_owner_url(cfg['web'])} 소유권을 획득한다")
    else:
        _log(f"휴면 시작 — SERIAL_WEB=0: 첫 시리얼 도구 호출 때 {_owner_url(cfg['web'])} 잠금만 획득한다")
    try:
        mcp.run()  # stdio transport(기본)
    finally:
        _release_owner("stdio 종료")


if __name__ == "__main__":
    main()
