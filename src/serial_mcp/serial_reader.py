"""시리얼 포트 읽기·쓰기와 재연결을 담당하는 I/O 경계."""

from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Callable, Optional

import serial

from .diagnostics import log as _log
from .ring_buffer import LineBuffer
from .viewer_feed import RawFeed


class SerialReader:
    """백그라운드에서 시리얼 포트를 줄 단위로 읽어 LineBuffer에 적재."""

    def __init__(
        self,
        port: str,
        baud: int,
        buffer: LineBuffer,
        tee_path: Optional[str] = None,
        reconnect_interval: float = 3.0,
        feed: Optional[RawFeed] = None,
        on_line: Optional[Callable[[datetime, str], None]] = None,
        on_open: Optional[Callable[[], None]] = None,
        char_delay: float = 0.0,
    ) -> None:
        self.port = port
        self.baud = baud
        self.buffer = buffer
        self.tee_path = tee_path
        self.reconnect_interval = reconnect_interval
        self.feed = feed
        self.on_line = on_line
        self.on_open = on_open
        self.char_delay = char_delay

        self._thread = threading.Thread(target=self._run, name="serial-reader", daemon=True)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._first_open_done = threading.Event()
        self._ser: Optional[serial.Serial] = None
        self._ser_lock = threading.Lock()
        self._tee = None
        self._tee_lock = threading.Lock()

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
            if self.on_open is not None:
                try:
                    self.on_open()
                except Exception as e:  # noqa: BLE001
                    _log(f"on_open 훅 오류({self.port}): {e!r}")
            return True
        except serial.SerialException as e:
            self.connected = False
            self.last_error = f"포트 열기 실패({self.port}): {e}"
            _log(self.last_error)
            return False
        except Exception as e:  # noqa: BLE001
            self.connected = False
            self.last_error = f"포트 열기 예외({self.port}): {e!r}"
            _log(self.last_error)
            return False
        finally:
            self._first_open_done.set()

    def _wait_retry(self) -> None:
        self._wake.wait(self.reconnect_interval)
        self._wake.clear()

    def _run(self) -> None:
        while not self._stop.is_set():
            if self._ser is None or not self.connected:
                if not self._open():
                    self._wait_retry()
                    continue
            ser = self._ser
            try:
                raw = ser.readline()
            except Exception as e:  # noqa: BLE001
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
                continue
            self._ingest(raw, datetime.now())

    def force_disconnect(self, reason: str) -> None:
        """외부 감시자가 핸들을 강제 해제한다 — 좀비 핸들 대응(SPEC §3)."""
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
        """페이로드를 포트에 기록하고 성공 시 TX 감사 기록을 남긴다."""
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
            except Exception as e:  # noqa: BLE001
                raise self._serial_failure("쓰기 실패", e) from e
        if audit is not None:
            self._audit_tx(audit, datetime.now())
        return n

    def pulse_reset(self, pulse_s: float = 0.1) -> None:
        """DTR/RTS 펄스로 보드를 일반 부팅 리셋한다."""
        with self._ser_lock:
            ser = self._ser
            if ser is None or not self.connected:
                raise serial.SerialException(f"포트가 연결되어 있지 않음: {self.port}")
            try:
                ser.dtr = False
                ser.rts = True
                time.sleep(pulse_s)
                ser.rts = False
            except Exception as e:  # noqa: BLE001
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
        """수신 바이트 한 줄을 디코드·정리해 버퍼와 보조 출력에 전달한다."""
        text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        self.buffer.add(text, ts)
        if self.feed is not None:
            self.feed.publish(ts, text)
        if self.on_line is not None:
            try:
                self.on_line(ts, text)
            except Exception as e:  # noqa: BLE001
                _log(f"on_line 훅 오류({self.port}): {e!r}")
        if self._tee is not None:
            try:
                stamp = ts.strftime("%Y-%m-%d %H:%M:%S.") + f"{ts.microsecond // 1000:03d}"
                with self._tee_lock:
                    self._tee.write(f"[{stamp}] {text}\n")
            except Exception as e:  # noqa: BLE001
                _log(f"tee 기록 실패: {e}")
