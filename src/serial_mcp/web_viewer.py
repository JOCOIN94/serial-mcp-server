"""웹 로그 뷰어 — localhost 전용 HTTP 서버(stdlib http.server, 데몬 스레드).

사람이 브라우저로 시리얼 로그를 보는 보조 기능. 설계: docs/superpowers/specs/
2026-06-10-web-log-viewer-design.md. MCP 서버 본체와 독립 — 기동 실패해도
본체에 영향을 주지 않는다(url이 None으로 남을 뿐).

- 조회 라우트는 GET 읽기 전용. 소유권 제어 `/api/release`만 명시적 상태 변경 예외다.
- stdout 금지: 접근 로그는 stderr로만 낸다(log_message 오버라이드).
- 실시간 스트림은 SSE 수동 구현 — RawFeed 구독.
- server.py 전역에 직접 의존하지 않고 필요한 데이터를 콜러블로 주입받는다
  (buffer_info/status_info) — 테스트 용이, 순환 import 방지.
"""

from __future__ import annotations

import json
import threading
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Callable, Optional
from urllib.parse import parse_qs, urlparse

from .diagnostics import log as _log
from .ring_buffer import _fmt_ts
from .viewer_feed import RawFeed


@lru_cache(maxsize=1)
def _viewer_html_bytes() -> bytes:
    """wheel에 포함된 단일 페이지 자산을 최초 요청 때 한 번만 읽는다."""
    return resources.files("serial_mcp").joinpath("viewer.html").read_bytes()


class _ViewerHTTPServer(ThreadingHTTPServer):
    daemon_threads = True        # SSE 핸들러 스레드가 프로세스 종료를 막지 않게
    block_on_close = False       # server_close()가 장수 SSE 핸들러를 기다리지 않게
    allow_reuse_address = False  # Windows에서도 8743 bind가 whole-session 소유권 잠금으로 동작해야 한다

    ports_info: Callable[[], list]
    feed_for: Callable[[str], Optional[RawFeed]]
    buffer_info: Callable[..., dict]
    status_info: Callable[[], dict]
    topology_info: Callable[[], dict]
    topology_feed: Optional[RawFeed]
    release_port: Callable[[str], dict]


class _Handler(BaseHTTPRequestHandler):
    server: _ViewerHTTPServer

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - 부모 시그니처
        _log("HTTP " + (fmt % args))   # stdout 금지 — 접근 로그를 stderr로

    def do_GET(self) -> None:  # noqa: N802 - http.server 규약
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        port = (query.get("port") or [""])[0]
        if path == "/":
            try:
                body = _viewer_html_bytes()
            except OSError as e:
                _log(f"웹 뷰어 자산 로드 실패: {e}")
                self.send_error(500, "viewer asset unavailable")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/stream":
            self._serve_stream(port)
        elif path == "/api/ports":
            self._send_json({"ports": self.server.ports_info()})
        elif path == "/api/buffer":
            raw_since = (query.get("since") or [None])[0]
            if raw_since is None:
                self._send_json(self.server.buffer_info(port))
            else:
                try:
                    since = int(raw_since)
                except (TypeError, ValueError):
                    since = -1     # 다른 생애/잘못된 revision → 백엔드가 reset 전체 스냅샷
                self._send_json(self.server.buffer_info(port, since))
        elif path == "/api/status":
            self._send_json(self.server.status_info())
        elif path == "/api/topology":
            self._send_json(self.server.topology_info())
        elif path == "/api/topology/stream":
            self._serve_topology_stream()
        elif path == "/api/release":
            out = self.server.release_port(port)
            self._send_json(out, status=404 if out.get("status") == "error" else 200)
        else:
            self.send_error(404)

    def _send_json(self, obj: dict, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_stream(self, port: str) -> None:
        """SSE — 지정 포트의 RawFeed를 구독해 한 줄당 한 이벤트로 흘려보낸다."""
        feed = self.server.feed_for(port)
        if feed is None:
            self.send_error(404, "unknown port")
            return
        self._serve_feed(feed, lambda item: {"ts": _fmt_ts(item[0]), "text": item[1]})

    def _serve_topology_stream(self) -> None:
        """SSE — 토폴로지 Hop dict를 한 이벤트씩 흘려보낸다."""
        feed = self.server.topology_feed
        if feed is None:
            self.send_error(404, "topology stream unavailable")
            return
        self._serve_feed(feed, lambda item: item[1])

    def _serve_feed(self, feed: RawFeed, encode_item) -> None:
        """RawFeed 구독을 SSE data 이벤트로 변환한다."""
        # 구독을 헤더 전송보다 먼저: 클라이언트가 응답 헤더를 받은 시점에는
        # 이미 구독이 살아 있어야 발행 누락이 없다(테스트·실사용 레이스 방지).
        sub = feed.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            idle = 0.0
            while True:
                item = sub.get(timeout=1.0)
                if item is None:
                    idle += 1.0
                    if idle >= 15.0:               # 하트비트(프록시·브라우저 타임아웃 방지)
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        idle = 0.0
                    continue
                idle = 0.0
                data = json.dumps(encode_item(item), ensure_ascii=False)
                self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionError, OSError):
            pass   # 클라이언트 끊김 — 정상 종료
        finally:
            feed.unsubscribe(sub)


class ViewerServer:
    """뷰어 HTTP 서버 래퍼 — localhost 고정 포트 bind/URL 보고. 예외를 밖으로 내지 않는다."""

    def __init__(
        self,
        ports_info: Callable[[], list],
        feed_for: Callable[[str], Optional[RawFeed]],
        buffer_info: Callable[..., dict],
        status_info: Callable[[], dict],
        topology_info: Optional[Callable[[], dict]] = None,
        topology_feed: Optional[RawFeed] = None,
        release_port: Optional[Callable[[str], dict]] = None,
        port: int = 8743,
    ) -> None:
        self._ports_info = ports_info
        self._feed_for = feed_for
        self._buffer_info = buffer_info
        self._status_info = status_info
        self._topology_info = topology_info or (lambda: {"groups": [], "unplaced": []})
        self._topology_feed = topology_feed
        self._release_port = release_port or (
            lambda _port: {"status": "error", "message": "unknown port"}
        )
        self._preferred_port = port
        self._httpd: Optional[_ViewerHTTPServer] = None
        self.url: Optional[str] = None   # 기동 성공 시 http://127.0.0.1:{port}, 실패 시 None

    def start(self) -> bool:
        try:
            self._httpd = _ViewerHTTPServer(("127.0.0.1", self._preferred_port), _Handler)
        except (OSError, OverflowError) as e:   # OverflowError: 0~65535 범위 밖 포트
            _log(f"웹 뷰어 포트 {self._preferred_port} 바인딩 실패: {e}")
        if self._httpd is None:
            _log("웹 뷰어 비활성 — 포트 바인딩 실패")
            return False
        self._httpd.ports_info = self._ports_info
        self._httpd.feed_for = self._feed_for
        self._httpd.buffer_info = self._buffer_info
        self._httpd.status_info = self._status_info
        self._httpd.topology_info = self._topology_info
        self._httpd.topology_feed = self._topology_feed
        self._httpd.release_port = self._release_port
        self.url = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        threading.Thread(
            target=self._httpd.serve_forever, name="serial-web", daemon=True
        ).start()
        return True

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        self.url = None
