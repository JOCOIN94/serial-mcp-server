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

import os
import re
import sys
import threading
from datetime import datetime
from typing import Mapping, Optional

import serial
from serial.tools import list_ports
from mcp.server.fastmcp import FastMCP

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
    ) -> None:
        self.port = port
        self.baud = baud
        self.buffer = buffer
        self.tee_path = tee_path
        self.reconnect_interval = reconnect_interval
        self.feed = feed   # 웹 뷰어 생중계 허브(없으면 발행 생략)

        self._thread = threading.Thread(target=self._run, name="serial-reader", daemon=True)
        self._stop = threading.Event()
        self._ser: Optional[serial.Serial] = None
        self._tee = None

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
            self._ser = serial.Serial(self.port, self.baud, timeout=1)
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
            try:
                raw = self._ser.readline()
            except Exception as e:  # noqa: BLE001 - 연결 끊김 등 모든 읽기 오류 복구
                self.connected = False
                self.last_error = f"읽기 중 오류: {e}"
                _log(self.last_error)
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
        if self._tee is not None:
            try:
                stamp = ts.strftime("%Y-%m-%d %H:%M:%S.") + f"{ts.microsecond // 1000:03d}"
                self._tee.write(f"[{stamp}] {text}\n")
            except Exception as e:  # noqa: BLE001
                _log(f"tee 기록 실패: {e}")


# ---- 전역 상태 (main 에서 초기화) ----
mcp = FastMCP("serial-mcp")
_buffer: Optional[LineBuffer] = None
_reader: Optional[SerialReader] = None
_config: dict = {}
_feed: Optional[RawFeed] = None
_viewer: Optional[ViewerServer] = None


def _viewer_url() -> Optional[str]:
    """웹 뷰어 URL — 비활성/기동 실패 시 None."""
    return _viewer.url if _viewer is not None else None


@mcp.tool()
def list_serial_ports() -> dict:
    """[언제 호출] 어느 포트가 대상 보드인지 모를 때, 또는 SERIAL_PORT 설정이
    맞는지 확인할 때 호출한다.

    [무엇을 반환] 현재 PC의 시리얼 포트 목록. 각 포트의 device(예: COM4,
    /dev/ttyUSB0), description, vid/pid, manufacturer 를 포함한다. VID/PID 와
    description 으로 어떤 칩(CP210x, CH343, STLink 등)인지 추론하라. 응답의
    configured_port 는 이 서버가 현재 가리키는 포트다.

    [루프 단계] 사전 점검 — 보통 한 번만.
    """
    ports = []
    for p in list_ports.comports():
        ports.append(
            {
                "device": p.device,
                "description": p.description,
                "hwid": p.hwid,
                "vid": p.vid,
                "pid": p.pid,
                "manufacturer": p.manufacturer,
                "serial_number": p.serial_number,
            }
        )
    return {
        "status": "ok",
        "message": f"{len(ports)}개 포트 발견",
        "configured_port": _config.get("port") or None,
        "ports": ports,
    }


@mcp.tool()
def get_serial_status() -> dict:
    """[언제 호출] 로그가 안 들어올 때 '서버가 포트에 연결돼 있는지'부터 확인할
    때. 포트 점유/미연결/미설정 같은 원인을 구분한다.

    [무엇을 반환] connected, 대상 port/baud, last_error, opened_at. connected
    가 false 이고 last_error 에 점유/권한 류 에러가 있으면 사람에게 같은 포트를
    쓰는 다른 프로그램(테라텀 등) 종료를 요청하라. 리더가 아예 없으면(SERIAL_PORT
    미설정) 그 사실을 message 로 알린다.

    사람이 로그를 직접 눈으로 보고 싶어 하면 viewer_url 링크를 안내하라(웹 뷰어).
    [루프 단계] 문제 진단.
    """
    if _reader is None:
        return {
            "status": "error",
            "message": "리더 미시작 — SERIAL_PORT 가 설정되지 않았다. list_serial_ports 로 포트를 찾아 환경변수를 설정하라.",
            "connected": False,
            "configured_port": _config.get("port") or None,
            "viewer_url": _viewer_url(),
        }
    return {
        "status": "ok",
        "message": "연결됨" if _reader.connected else "연결 안 됨",
        "connected": _reader.connected,
        "port": _reader.port,
        "baud": _reader.baud,
        "last_error": _reader.last_error,
        "opened_at": _reader.opened_at.isoformat() if _reader.opened_at else None,
        "tee": _config.get("tee") or None,
        "viewer_url": _viewer_url(),
    }


@mcp.tool()
def get_recent_logs(lines: int = 200) -> dict:
    """[언제 호출] 블랙박스 루프의 '결과 확인' 단계 — 사람이 장비를 동작시킨 뒤,
    그동안 쌓인 로그를 확인할 때. 가장 자주 쓰는 도구.

    [무엇을 반환] 최근 N개 라인(시간 오름차순). 연속 중복은 접혀서 한 줄에
    '(N회 반복, HH:MM:SS~HH:MM:SS)'로 표기된다. 각 줄 앞에 수신 시각이 붙는다.

    [팁] 결과가 많으면 query_serial_logs 로 좁혀라. 비어 있으면 get_serial_status
    로 연결을 확인하고, 그래도 비면 사람에게 장비 동작/리셋을 요청하라 — 이 서버는
    읽기 전용이라 AI가 직접 리셋할 수 없다.

    [루프 단계] 결과 확인.
    """
    if _buffer is None:
        return {"status": "error", "message": "버퍼 미초기화", "count": 0, "lines": []}
    got = _buffer.get_recent(lines)
    return {
        "status": "ok",
        "message": f"{len(got)}줄 반환",
        "count": len(got),
        "lines": got,
    }


@mcp.tool()
def query_serial_logs(pattern: str, max_results: int = 100) -> dict:
    """[언제 호출] 특정 키워드/에러/마커를 버퍼에서 찾을 때. 예: 부팅 완료 문구,
    'ERROR', 특정 상태 출력의 등장 여부 확인.

    [무엇을 반환] 정규식 pattern 에 매칭되는 라인들(최신 우선 max_results개,
    반환은 시간 오름차순). 접힌 묶음 표기 포함.

    [주의] pattern 은 파이썬 정규식이다. 매칭이 0이면 그 문구가 아직 안 나온 것 —
    사람에게 해당 동작을 요청하거나 더 기다린 뒤 다시 조회하라.

    [루프 단계] 결과 확인(표적 검색).
    """
    if _buffer is None:
        return {"status": "error", "message": "버퍼 미초기화", "count": 0, "lines": []}
    try:
        got = _buffer.query(pattern, max_results)
    except re.error as e:
        return {"status": "error", "message": f"정규식 오류: {e}", "count": 0, "lines": []}
    return {
        "status": "ok",
        "message": f"{len(got)}줄 매칭",
        "pattern": pattern,
        "count": len(got),
        "lines": got,
    }


@mcp.tool()
def get_log_buffer_info() -> dict:
    """[언제 호출] 버퍼가 얼마나 찼는지, 가장 최근/오래된 줄이 무엇인지 빠르게 볼
    때. clear_log_buffer 직후 새 로그가 들어오기 시작했는지 폴링할 때 특히 유용.

    [무엇을 반환] entries/capacity, oldest/newest, 누적 total_received/total_stored,
    dedup 여부.

    [루프 단계] 진행 점검(폴링).
    """
    if _buffer is None:
        return {"status": "error", "message": "버퍼 미초기화"}
    info = _buffer.info()
    info["status"] = "ok"
    info["message"] = f"{info['entries']}/{info['capacity']} 항목"
    info["viewer_url"] = _viewer_url()
    return info


@mcp.tool()
def clear_log_buffer() -> dict:
    """[언제 호출] 블랙박스 시험의 '시작' 단계 — 새 시험을 깨끗한 상태에서
    관측하려고 직전 로그를 비울 때. 표준 절차: 이 도구로 비우고 → 사람에게 장비
    동작/리셋을 요청하고 → 잠시 후 get_recent_logs 로 결과를 회수한다.

    [무엇을 반환] 비우기 직전의 항목 수(cleared).

    [루프 단계] 시험 시작.
    """
    if _buffer is None:
        return {"status": "error", "message": "버퍼 미초기화", "cleared": 0}
    n = _buffer.clear()
    return {"status": "ok", "message": f"{n}개 항목 비움", "cleared": n}


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


def _viewer_buffer_info() -> dict:
    """웹 뷰어 /api/buffer 응답(버퍼 탭) — 구조화 스냅샷 + 카운터."""
    if _buffer is None:
        return {"status": "error", "entries": []}
    info = _buffer.info()
    return {
        "status": "ok",
        "entries": _buffer.snapshot(),
        "capacity": info["capacity"],
        "total_received": info["total_received"],
        "total_stored": info["total_stored"],
        "dedup": info["dedup"],
    }


def _viewer_status_info() -> dict:
    """웹 뷰어 /api/status 응답(헤더 표시) — get_serial_status의 경량판."""
    if _reader is None:
        return {
            "connected": False,
            "port": _config.get("port") or "",
            "baud": _config.get("baud"),
            "last_error": "리더 미시작(SERIAL_PORT 미설정)",
        }
    return {
        "connected": _reader.connected,
        "port": _reader.port,
        "baud": _reader.baud,
        "last_error": _reader.last_error,
    }


def _parse_web(env: Mapping[str, str]) -> Optional[int]:
    """SERIAL_WEB 파싱 — 기본 8743(켜짐). 0/false/no/off → 비활성(None), 정수 → 포트."""
    raw = env.get("SERIAL_WEB", "").strip()
    if raw == "":
        return 8743
    if raw.lower() in ("0", "false", "no", "off"):
        return None
    try:
        return int(raw)
    except ValueError:
        _log(f"환경변수 SERIAL_WEB={raw!r} 해석 실패 → 기본 포트 8743 사용")
        return 8743


def _load_config(env: Mapping[str, str]) -> dict:
    """환경변수 매핑에서 서버 설정을 파싱해 dict로 반환(부작용 없음, 순수 함수).

    main()이 이 결과로 LineBuffer/SerialReader를 구성한다. I/O·스레드 시작과
    분리돼 있어 환경변수 계약(SPEC §3/§4)을 단독 테스트할 수 있다.
    """
    port = env.get("SERIAL_PORT", "").strip()
    baud = _env_int(env, "SERIAL_BAUD", 115200)
    tee = env.get("SERIAL_TEE", "").strip() or None
    exclude = env.get("SERIAL_EXCLUDE", "").strip() or None
    include = env.get("SERIAL_INCLUDE", "").strip() or None
    maxlen = _env_int(env, "SERIAL_BUFFER_LINES", 2000)
    dedup = env.get("SERIAL_DEDUP", "1").strip().lower() not in ("0", "false", "no", "off")
    return {
        "port": port, "baud": baud, "tee": tee, "exclude": exclude,
        "include": include, "maxlen": maxlen, "dedup": dedup,
        "web": _parse_web(env),
    }


def main() -> None:
    """엔트리포인트. 환경변수로 설정을 읽고 리더를 띄운 뒤 stdio 로 MCP 서버 구동."""
    global _buffer, _reader, _config, _feed, _viewer

    cfg = _load_config(os.environ)
    _config = {
        "port": cfg["port"], "baud": cfg["baud"], "tee": cfg["tee"],
        "exclude": cfg["exclude"], "include": cfg["include"],
    }
    _buffer = LineBuffer(
        maxlen=cfg["maxlen"], dedup=cfg["dedup"],
        exclude=cfg["exclude"], include=cfg["include"],
    )
    _feed = RawFeed()

    if not cfg["port"]:
        _log("경고: SERIAL_PORT 미설정 — 서버는 뜨지만 리더는 시작하지 않는다. "
             "list_serial_ports 로 포트를 확인하고 환경변수를 설정하라.")
    else:
        _reader = SerialReader(
            port=cfg["port"], baud=cfg["baud"], buffer=_buffer,
            tee_path=cfg["tee"], feed=_feed,
        )
        _reader.start()

    if cfg["web"] is not None:
        _viewer = ViewerServer(
            feed=_feed,
            buffer_info=_viewer_buffer_info,
            status_info=_viewer_status_info,
            port=cfg["web"],
        )
        _viewer.start()   # 실패해도 예외 없음 — url이 None으로 남을 뿐
        _log(f"웹 뷰어: {_viewer.url or '기동 실패'}")
    else:
        _log("웹 뷰어 꺼짐 (SERIAL_WEB=0)")

    _log(f"시작 (port={cfg['port'] or '(미설정)'}, baud={cfg['baud']}, dedup={cfg['dedup']}, "
         f"buffer={cfg['maxlen']}, tee={cfg['tee'] or '없음'})")
    mcp.run()  # stdio transport(기본)


if __name__ == "__main__":
    main()
