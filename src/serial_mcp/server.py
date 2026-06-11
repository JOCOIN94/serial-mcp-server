"""silotek-serial-mcp — FastMCP 서버 본체.

백그라운드 스레드가 시리얼 포트를 지속적으로 읽어 LineBuffer에 쌓고,
AI(Claude Code)는 6개의 읽기 전용 도구로 그 버퍼를 조회한다.

설계 주의:
- stdout 으로 MCP JSON-RPC 가 흐른다. stdout 에 절대 print/로그 금지.
  모든 진단은 stderr 또는 tee 파일로만(_log 헬퍼 사용).
- 버퍼는 LineBuffer 내부 Lock 으로 보호된다(리더 스레드 ↔ 도구 호출 동시 접근).
- 현재 읽기 전용. 향후 쓰기(명령 전송)는 SerialReader 가 포트 핸들을 들고 있어
  메서드 추가만으로 확장 가능(지금은 추가하지 않는다).
"""

from __future__ import annotations

import math
import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime
from typing import Callable, Mapping, Optional

import serial
from serial.tools import list_ports
from mcp.server.fastmcp import FastMCP

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
    ) -> None:
        self.port = port
        self.baud = baud
        self.buffer = buffer
        self.tee_path = tee_path
        self.reconnect_interval = reconnect_interval
        self.feed = feed   # 웹 뷰어 생중계 허브(없으면 발행 생략)
        self.on_line = on_line   # 서버측 라인 후킹(보드 자동 식별 등, 없으면 생략)

        self._thread = threading.Thread(target=self._run, name="serial-reader", daemon=True)
        self._stop = threading.Event()
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

    def _run(self) -> None:
        while not self._stop.is_set():
            if self._ser is None or not self.connected:
                if not self._open():
                    self._stop.wait(self.reconnect_interval)  # 중단 신호에 즉시 반응
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
        """페이로드를 포트에 기록한다. 성공하면 audit 텍스트를 TX 감사 기록으로 남긴다."""
        with self._ser_lock:
            ser = self._ser
            if ser is None or not self.connected:
                raise serial.SerialException(f"포트가 연결되어 있지 않음: {self.port}")
            try:
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
_autoname_rules: list = []   # SERIAL_AUTONAME 컴파일 결과 [(이름, re.Pattern)]
_autoname_lock = threading.Lock()   # 검사-부여 원자화(동시 리셋 시 중복 이름 방지)
_hotplug_stop = threading.Event()   # 핫플러그 스캔 루프 종료 신호(테스트·향후 정리용)


@dataclass
class PortMonitor:
    """포트 하나의 모니터링 묶음 — 리더·버퍼·생중계 허브(설계 §4)."""

    port: str
    name: Optional[str]               # SERIAL_NAMES 별칭(없으면 None)
    buffer: LineBuffer
    feed: RawFeed
    reader: Optional[SerialReader]    # 테스트에선 SimpleNamespace 주입 가능

    @property
    def label(self) -> str:
        return label(self.port, self.name)


def _viewer_url() -> Optional[str]:
    """웹 뷰어 URL — 비활성/기동 실패 시 None."""
    return _viewer.url if _viewer is not None else None


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
            return next(iter(_monitors.values())), None
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


@mcp.tool()
def list_serial_ports() -> dict:
    """[언제 호출] 어느 포트가 어느 보드인지 확인할 때, 모니터링 대상을 점검할 때.

    [무엇을 반환] 현재 PC의 시리얼 포트 목록. 각 포트의 device/description/vid/pid/
    manufacturer/serial_number 에 더해, 이 서버가 모니터링 중이면 monitored=true 와
    별칭 name 이 붙는다. monitored_ports 는 현재 모니터링 목록(별칭 표기).
    VID/PID·description 으로 칩(CH343, CP210x 등)을 추론하라.

    [루프 단계] 사전 점검 — 보통 한 번만.
    """
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
    return {
        "status": "ok",
        "message": f"{len(ports)}개 포트 발견, {len(_monitors)}개 모니터링 중",
        "monitored_ports": [m.label for m in _monitors.values()],
        "ports": ports,
    }


@mcp.tool()
def get_serial_status(port: str = "") -> dict:
    """[언제 호출] 로그가 안 들어올 때 '어느 보드가 연결돼 있는지'부터 확인할 때.
    포트 점유/미연결/미인식 원인을 구분한다.

    [무엇을 반환] port 미지정 시 모니터링 중인 전 포트의 상태 배열(ports).
    port(별칭 "SSM" 또는 포트명 "COM4") 지정 시 그 포트의 단일 상태.
    connected 가 false 이고 last_error 에 점유/권한 에러가 있으면 사람에게 같은
    포트를 쓰는 다른 프로그램(테라텀 등) 종료를 요청하라.

    사람이 로그를 직접 눈으로 보고 싶어 하면 viewer_url 링크를 안내하라(웹 뷰어).
    [루프 단계] 문제 진단.
    """

    def one(m: PortMonitor) -> dict:
        r = m.reader
        return {
            "name": m.name,
            "label": m.label,
            "port": m.port,
            "connected": bool(r and r.connected),
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
        d["message"] = "연결됨" if d["connected"] else "연결 안 됨"
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
def get_recent_logs(lines: int = 200, port: str = "") -> dict:
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
def query_serial_logs(pattern: str, max_results: int = 100, port: str = "") -> dict:
    """[언제 호출] 특정 키워드/에러/마커를 버퍼에서 찾을 때. 예: 부팅 완료 문구,
    'ERROR', 특정 상태 출력의 등장 여부.

    [port 규약] get_recent_logs 와 동일 — 복수 포트면 지정, 미지정 에러 시 ports
    목록에서 골라 재호출.

    [무엇을 반환] 정규식 pattern 매칭 라인들(최신 우선 max_results개, 반환은 시간
    오름차순, 접힌 묶음 표기 포함). 매칭 0이면 그 문구가 아직 안 나온 것 — 사람에게
    해당 동작을 요청하거나 더 기다린 뒤 재조회하라.

    [루프 단계] 결과 확인(표적 검색).
    """
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
def get_log_buffer_info(port: str = "") -> dict:
    """[언제 호출] 버퍼가 얼마나 찼는지, 최근/최오래 항목이 무엇인지 빠르게 볼 때.
    clear_log_buffer 직후 새 로그 유입을 폴링할 때 특히 유용.

    [port 규약] get_recent_logs 와 동일.

    [무엇을 반환] entries/capacity, oldest/newest, 누적 total_received/total_stored,
    dedup(룩백 윈도 — 0이면 끔).

    [루프 단계] 진행 점검(폴링).
    """
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
def clear_log_buffer(port: str = "") -> dict:
    """[언제 호출] 블랙박스 시험의 '시작' 단계 — 새 시험을 깨끗한 상태에서
    관측하려고 직전 로그를 비울 때. 표준 절차: 비우고 → 사람에게 장비 동작/리셋
    요청 → 잠시 후 get_recent_logs 로 회수.

    [port 규약] 다른 도구와 달리 **미지정 = 전체 포트 비우기**(시험 시작 시 모든
    보드를 함께 리셋 관측하는 게 보통이므로). 특정 보드만 비우려면 port 지정.

    [무엇을 반환] cleared(총 비운 항목 수)와 ports(포트별 내역).

    [루프 단계] 시험 시작.
    """
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
        plist.append({
            "port": m.port,
            "label": m.label,
            "connected": bool(r and r.connected),
            "baud": r.baud if r else None,
            "last_error": r.last_error if r else None,
            "buffer_entries": binfo["entries"],
            "buffer_capacity": binfo["capacity"],
        })
    return {"ports": plist}


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


def _parse_web(env: Mapping[str, str]) -> Optional[int]:
    """SERIAL_WEB 파싱 — 기본 8743(켜짐). 0/false/no/off → 비활성(None), 정수 → 포트.

    1~65535 밖이면 기본값으로 — 범위 밖 포트는 socket.bind에서 OverflowError로
    서버 기동 자체를 죽일 수 있다(뷰어 실패는 본체에 영향 없어야 한다는 불변식).
    """
    raw = env.get("SERIAL_WEB", "").strip()
    if raw == "":
        return 8743
    if raw.lower() in ("0", "false", "no", "off"):
        return None
    try:
        n = int(raw)
        if 1 <= n <= 65535:
            return n
    except ValueError:
        pass
    _log(f"환경변수 SERIAL_WEB={raw!r} 해석 실패(1~65535 필요) → 기본 포트 8743 사용")
    return 8743


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


def _load_config(env: Mapping[str, str]) -> dict:
    """환경변수 매핑에서 서버 설정을 파싱해 dict로 반환(부작용 없음, 순수 함수).

    main()이 이 결과로 LineBuffer/SerialReader를 구성한다. I/O·스레드 시작과
    분리돼 있어 환경변수 계약(SPEC §3/§4)을 단독 테스트할 수 있다.
    """
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
        "web": _parse_web(env),
        "hotplug": _parse_hotplug(env),
        "write": _parse_flag(env, "SERIAL_WRITE"),
        "write_confirm": _parse_flag(env, "SERIAL_WRITE_CONFIRM"),
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
                              feed=feed, on_line=on_line)
    return mon


def _hotplug_scan_once() -> list[str]:
    """핫플러그 1회 스캔 — 새 USB 시리얼 포트를 모니터에 추가하고 포트명 목록 반환.

    _monitors 는 copy-on-write 로만 갱신한다(새 dict 생성 → 전역 참조 원자 교체).
    리더 스레드(_autoname_check)·도구 호출이 옛 dict 를 순회 중이어도 안전하다.
    사라진 포트의 모니터는 제거하지 않는다 — 버퍼·tee 를 보존하고, 재연결은
    SerialReader 의 재시도 루프가 담당한다.
    """
    global _monitors
    com = list(list_ports.comports())
    fresh = [d for d in auto_usb_ports(com) if d.upper() not in _monitors]
    if not fresh:
        return []
    sn_map = {p.device.upper(): getattr(p, "serial_number", None) for p in com}
    added = [_make_monitor(d, None, sn_map, _config) for d in fresh]
    # 등록을 먼저 끝낸 뒤 리더 시작(main 의 1·2패스와 동일한 순서 보장)
    _monitors = {**_monitors, **{m.port.upper(): m for m in added}}
    for m in added:
        m.reader.start()
        _log(f"핫플러그: 모니터 추가 {m.label} @ {m.reader.baud}")
    return [m.port for m in added]


def _hotplug_loop(interval: float, stop: threading.Event) -> None:
    """핫플러그 스캔 루프(데몬 스레드 본체) — 어떤 예외에도 죽지 않는다.

    stop.wait(interval) 가 타이머 겸 종료 신호 수신을 겸한다(즉시 반응).
    """
    while not stop.wait(interval):
        try:
            _hotplug_scan_once()
        except Exception as e:  # noqa: BLE001 - 스캔 실패가 스레드를 죽이면 안 됨
            _log(f"핫플러그 스캔 오류: {e!r}")


def main() -> None:
    """엔트리포인트. USB 자동 스캔(또는 SERIAL_PORT 목록)으로 포트별 모니터를
    띄우고 stdio 로 MCP 서버 구동."""
    global _config, _viewer, _autoname_rules

    cfg = _load_config(os.environ)
    _config = cfg
    _autoname_rules = compile_autoname(cfg["autoname"], log=_log)

    com = list(list_ports.comports())
    specs = cfg["ports"]
    if not specs:
        specs = [(dev, None) for dev in auto_usb_ports(com)]
        _log(f"자동 스캔: USB 시리얼 {len(specs)}개 발견")
    sn_map = {p.device.upper(): getattr(p, "serial_number", None) for p in com}

    # 1패스: 모니터 전부 생성·등록 (리더 시작 전 — 리더 스레드의 _autoname_check가
    # _monitors를 순회하므로, 순회 중 dict 변경이 없도록 등록을 먼저 끝낸다)
    for port, baud_override in specs:
        if port.upper() in _monitors:
            _log(f"중복 포트 무시: {port}")
            continue
        _monitors[port.upper()] = _make_monitor(port, baud_override, sn_map, cfg)

    # 2패스: 등록이 끝난 뒤 리더 일괄 시작
    for mon in _monitors.values():
        mon.reader.start()
        _log(f"모니터 시작: {mon.label} @ {mon.reader.baud}")

    hotplug_on = cfg["hotplug"] is not None and not cfg["ports"]
    if not _monitors:
        if hotplug_on:
            _log("경고: 모니터링할 포트 없음 — USB 장비를 연결하면 핫플러그 스캔이 자동 추가한다.")
        else:
            _log("경고: 모니터링할 포트 없음 — USB 시리얼이 안 보이고 SERIAL_PORT 도 "
                 "비어 있다. 장비 연결 후 서버를 재시작하라(핫플러그 꺼짐).")

    if hotplug_on:
        threading.Thread(target=_hotplug_loop, args=(cfg["hotplug"], _hotplug_stop),
                         name="hotplug-scan", daemon=True).start()
        _log(f"핫플러그 스캔 켜짐 ({cfg['hotplug']:g}초 간격)")
    elif cfg["ports"]:
        _log("핫플러그 스캔 없음 — SERIAL_PORT 고정 목록 모드(늦은 연결은 재연결 루프가 잡음)")
    else:
        _log("핫플러그 스캔 꺼짐 (SERIAL_HOTPLUG=0)")

    if cfg["web"] is not None:
        _viewer = ViewerServer(
            ports_info=_viewer_ports_info,
            feed_for=_viewer_feed_for,
            buffer_info=_viewer_buffer_info,
            status_info=_viewer_status_info,
            port=cfg["web"],
        )
        _viewer.start()   # 실패해도 예외 없음 — url이 None으로 남을 뿐
        _log(f"웹 뷰어: {_viewer.url or '기동 실패'}")
    else:
        _log("웹 뷰어 꺼짐 (SERIAL_WEB=0)")

    _log(f"시작 (포트 {len(_monitors)}개, dedup={cfg['dedup']}, "
         f"buffer={cfg['maxlen']}, tee={cfg['tee'] or '없음'})")
    mcp.run()  # stdio transport(기본)


if __name__ == "__main__":
    main()
