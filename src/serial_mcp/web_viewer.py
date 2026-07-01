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
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional
from urllib.parse import parse_qs, urlparse

from .ring_buffer import _fmt_ts
from .viewer_feed import RawFeed


def _log(msg: str) -> None:
    """진단 로그 — stderr 전용(server.py의 _log와 동일 형식, 순환 import 회피용 사본)."""
    print(f"[serial-mcp] {msg}", file=sys.stderr, flush=True)


class _ViewerHTTPServer(ThreadingHTTPServer):
    daemon_threads = True        # SSE 핸들러 스레드가 프로세스 종료를 막지 않게
    block_on_close = False       # server_close()가 장수 SSE 핸들러를 기다리지 않게
    allow_reuse_address = False  # Windows에서도 8743 bind가 whole-session 소유권 잠금으로 동작해야 한다

    ports_info: Callable[[], list]
    feed_for: Callable[[str], Optional[RawFeed]]
    buffer_info: Callable[[str], dict]
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
        port = (parse_qs(parsed.query).get("port") or [""])[0]
        if path == "/":
            body = _HTML.encode("utf-8")
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
            self._send_json(self.server.buffer_info(port))
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
        buffer_info: Callable[[str], dict],
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


# ---- 단일 페이지(인라인 CSS/JS, 외부 CDN 없음 → 오프라인 동작) ----
# 렌더 파이프라인은 구조 기반(문자열 암기 아님): parseLine → classifyLine(score) →
# extractTokens → renderBodyHTML, 그리고 ansiToHtmlSafe / normalizeForRepeat / escapeHtml.
# 순수 로직(DOM 비의존)은 VIEWER-PURE sentinel로 감싸 `SViewer`로 노출 → node 단위테스트 대상.
# 컬러 원칙: "색은 장식이 아니라 신호". 기본 neutral, 한 줄 최대 1개 약한 틴트, 의미는
# 좌측 bar·작은 badge 중심. ANSI(본문색)와 semantic(구조 bar/badge)은 채널이 달라 공존한다.
_HTML = r"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>serial-mcp 로그 뷰어</title>
<style>
/* app.css — serial-mcp 로그 뷰어. 다크 터미널, 외부 의존성 0.
   색은 신호다: 평상시 회색 2~3톤, 진짜 신호(에러/경고/노이즈)·ANSI·검색에만 채도. */

:root {
  --fs: 13px;          /* 로그 폰트 크기 (A−/A+ 로 11~18) */
  --row-pad: 2px;      /* 줄 세로 여백 (리듬) */
  --lh: 1.55;          /* 줄 간격 (리듬) */

  --bg:        #0a0d12;
  --bg-raised: #11151c;
  --bg-bar:    #0f1319;
  --bg-input:  #0a0d12;
  --bg-hover:  rgba(255,255,255,.045);
  --border:    #222932;
  --border-2:  #2c333d;

  --fg:        #c5ccd6;   /* 기본 본문 */
  --fg-bright: #e9eef4;   /* 강조 본문 */
  --muted:     #707b88;   /* 라벨·메타 */
  --faint:     #49525d;   /* 타임스탬프 등 저대비 */

  --accent:    #5b9bd8;   /* UI 강조(활성 탭·포커스) — 단일 색조 */
  --accent-bg: #1b2a3d;

  --err:  #f0786f;  --err-bg:  rgba(240,120,111,.13);
  --warn: #d8a23a;  --warn-bg: rgba(216,162,58,.12);
  --boot: #5aa7f0;  --boot-bg: rgba(90,167,240,.11);
  --ok:   #57c98a;

  --mono: ui-monospace, 'Cascadia Code', 'Cascadia Mono', Consolas,
          'SF Mono', Menlo, 'DejaVu Sans Mono', 'D2Coding', monospace;
  --ui:   -apple-system, 'Segoe UI', Roboto, system-ui, sans-serif;
}

* { box-sizing: border-box; }
html { height: 100%; }
body {
  margin: 0; min-height: 100vh;
  background: var(--bg);
  color: var(--fg);
  font: var(--fs)/var(--lh) var(--mono);
  -webkit-font-smoothing: antialiased;
  display: flex; align-items: stretch;
}

/* ===== 좌측 네비게이션 (세션 + 포트 상태) ===== */
#nav {
  flex: 0 0 600px; box-sizing: border-box;
  position: sticky; top: 0; align-self: flex-start; height: 100vh; overflow-y: auto;
  background: var(--bg-raised); border-right: 1px solid var(--border);
  display: flex; flex-direction: column; gap: 11px; padding: 10px;
  font-family: var(--ui); transition: flex-basis .16s ease;
}
#content { flex: 1 1 auto; min-width: 0; }

/* ============================ HEADER ============================ */
header {
  position: sticky; top: 0; z-index: 10;
  background: var(--bg-raised);
  border-bottom: 1px solid var(--border);
  font-family: var(--ui);
  display: flex; flex-wrap: wrap; align-items: center; gap: 12px 22px; padding: 8px 14px;
}
.bar { display: flex; align-items: center; gap: 10px; }
.bar.tools { flex: 1 1 320px; flex-wrap: wrap; gap: 7px; }
.spacer { flex: 1 1 auto; }

/* connection status */
.conn { display: flex; align-items: center; gap: 9px; min-width: 0; }
.dot {
  width: 9px; height: 9px; border-radius: 50%; flex: none;
  background: var(--err); box-shadow: 0 0 0 0 transparent;
}
.dot.on  { background: var(--ok);  animation: pulse 2.4s ease-out infinite; }
.dot.off { background: var(--faint); }
.dot.fail { background: var(--err); }
@keyframes pulse {
  0%   { box-shadow: 0 0 0 0 rgba(87,201,138,.45); }
  70%  { box-shadow: 0 0 0 6px rgba(87,201,138,0); }
  100% { box-shadow: 0 0 0 0 rgba(87,201,138,0); }
}
.dev { color: var(--fg-bright); font-weight: 600; font-size: 13px;
       white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.baud { color: var(--muted); font-size: 12px; font-variant-numeric: tabular-nums; }
.err-pill {
  display: none; align-items: center; gap: 5px;
  background: var(--err-bg); color: var(--err);
  border: 1px solid rgba(240,120,111,.3); border-radius: 5px;
  padding: 2px 8px; font-size: 11.5px; max-width: 46ch;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.err-pill.show { display: inline-flex; }

/* board selector */
select.board {
  background: var(--bg-input); color: var(--fg); font: 12px var(--ui);
  border: 1px solid var(--border-2); border-radius: 6px; padding: 4px 8px;
  cursor: pointer;
}
select.board:focus-visible { outline: none; border-color: var(--accent); }

/* ----- segmented tabs ----- */
.tabs { display: inline-flex; background: var(--bg); border: 1px solid var(--border-2);
        border-radius: 7px; padding: 2px; gap: 2px; }
.tabs button {
  appearance: none; border: 0; background: transparent; cursor: pointer;
  color: var(--muted); font: 12px/1 var(--ui); font-weight: 600; white-space: nowrap;
  padding: 6px 12px; border-radius: 5px; display: flex; align-items: baseline; gap: 6px;
}
.tabs button .count {
  font-size: 10.5px; font-weight: 500; color: var(--faint);
  font-variant-numeric: tabular-nums;
}
.tabs button:hover { color: var(--fg); }
.tabs button.active { background: var(--accent-bg); color: var(--fg-bright); }
.tabs button.active .count { color: var(--accent); }

/* ----- generic toolbar buttons ----- */
.btn {
  appearance: none; cursor: pointer; font: 12px/1 var(--ui); font-weight: 500;
  color: var(--fg); background: var(--bg); border: 1px solid var(--border-2);
  border-radius: 6px; padding: 6px 10px; display: inline-flex; align-items: center; gap: 6px;
  white-space: nowrap;
}
.btn:hover { background: var(--bg-hover); border-color: #3a4350; }
.btn:focus-visible { outline: none; border-color: var(--accent); }
.btn.on { background: var(--accent-bg); border-color: var(--accent); color: var(--fg-bright); }
.btn.icon { padding: 6px 8px; }
.btn .ic { width: 15px; height: 15px; display: block; }
.btn.danger:hover { border-color: rgba(240,120,111,.5); color: var(--err); }

/* ----- search ----- */
.search {
  display: flex; align-items: center; gap: 6px; flex: 1 1 200px; min-width: 130px;
  background: var(--bg-input); border: 1px solid var(--border-2); border-radius: 6px;
  padding: 0 8px; height: 30px;
}
.search:focus-within { border-color: var(--accent); }
.search .ic { width: 14px; height: 14px; color: var(--muted); flex: none; }
.search input {
  flex: 1 1 auto; min-width: 40px; background: transparent; border: 0; outline: none;
  color: var(--fg); font: 13px var(--mono);
}
.search input::placeholder { color: var(--faint); }
.search .nav { display: none; align-items: center; gap: 2px; flex: none; }
.search.has-q .nav { display: flex; }
.search .matchn {
  color: var(--muted); font: 11px var(--ui); font-variant-numeric: tabular-nums;
  white-space: nowrap; padding: 0 2px;
}
.search .matchn.none { color: var(--err); }
.search .navbtn {
  appearance: none; border: 0; background: transparent; color: var(--muted);
  cursor: pointer; border-radius: 4px; width: 20px; height: 20px;
  display: grid; place-items: center; padding: 0;
}
.search .navbtn:hover { background: var(--bg-hover); color: var(--fg); }

/* ----- level filter chips ----- */
.levels { display: inline-flex; gap: 4px; }
.chip {
  appearance: none; cursor: pointer; font: 11px/1 var(--ui); font-weight: 600;
  letter-spacing: .03em; border-radius: 5px; padding: 5px 9px;
  border: 1px solid transparent; display: inline-flex; align-items: center; gap: 5px;
  font-variant-numeric: tabular-nums;
}
.chip .n { font-weight: 500; opacity: .7; }
.chip.err  { color: var(--err);  border-color: rgba(240,120,111,.35); background: var(--err-bg); }
.chip.warn { color: var(--warn); border-color: rgba(216,162,58,.3);   background: var(--warn-bg); }
.chip.boot { color: var(--boot); border-color: rgba(90,167,240,.3);   background: var(--boot-bg); }
.chip.off { color: var(--faint); border-color: var(--border); background: transparent; }
.chip.off .n { opacity: .5; }

/* ----- settings popover ----- */
.pop-wrap { position: relative; }
.pop {
  display: none; position: fixed; z-index: 40;
  background: var(--bg-raised); border: 1px solid var(--border-2); border-radius: 9px;
  padding: 6px; min-width: 248px; max-height: 86vh; overflow-y: auto;
  box-shadow: 0 12px 32px rgba(0,0,0,.5);
}
.pop.open { display: block; }
.pop .row { display: flex; align-items: center; justify-content: space-between;
            gap: 10px; padding: 7px 9px; border-radius: 6px; }
.pop .row:hover { background: var(--bg-hover); }
.pop .row span { font: 12px var(--ui); color: var(--fg); }
.pop .seg { display: inline-flex; background: var(--bg); border: 1px solid var(--border);
            border-radius: 6px; overflow: hidden; }
.pop .seg button {
  appearance: none; border: 0; background: transparent; color: var(--muted);
  font: 11px var(--ui); font-weight: 600; padding: 4px 9px; cursor: pointer;
}
.pop .seg button.on { background: var(--accent-bg); color: var(--accent); }
.pop .div { height: 1px; background: var(--border); margin: 5px 4px; }
.pop .row.toggle { cursor: pointer; }
.switch { width: 30px; height: 17px; border-radius: 9px; background: var(--border-2);
          position: relative; transition: background .15s; flex: none; }
.switch::after { content: ""; position: absolute; top: 2px; left: 2px; width: 13px; height: 13px;
                 border-radius: 50%; background: var(--muted); transition: .15s; }
.row.toggle.on .switch { background: var(--accent); }
.row.toggle.on .switch::after { left: 15px; background: #fff; }

/* ============================ LOG AREA ============================ */
main { padding: 8px 14px 80px; }

.ln {
  display: grid; grid-template-columns: 9ch 1fr; column-gap: 12px;
  padding: var(--row-pad) 6px; border-radius: 4px; position: relative;
}
.ln:not(.err):not(.warn):not(.boot):not(.noise):hover { background: var(--bg-hover); }
.ln .ts { color: var(--faint); user-select: none; font-variant-numeric: tabular-nums; white-space: nowrap; }
.ln .txt { white-space: pre-wrap; word-break: break-word; overflow-wrap: anywhere; min-width: 0; }
body.nowrap .ln .txt { white-space: pre; overflow-x: hidden; text-overflow: ellipsis; }
body.no-ts .ln { grid-template-columns: 0 1fr; column-gap: 0; }
body.no-ts .ln .ts { display: none; }

/* 카테고리: 좌측 상태 bar 중심. 배경 틴트는 색 강도 normal/vivid 에서만 약하게. */
.ln.err   { box-shadow: inset 3px 0 0 var(--err); }
.ln.warn  { box-shadow: inset 3px 0 0 var(--warn); }
.ln.boot  { box-shadow: inset 2px 0 0 var(--boot); }
.ln.noise { box-shadow: inset 2px 0 0 var(--faint); }
.ln.ok    { box-shadow: inset 2px 0 0 var(--ok); }
.ln.noise .txt { color: var(--faint); }     /* 깨진/바이너리: 저대비, 강조 금지 */

.ln.blank { padding: 0; line-height: .5em; }
.ln.blank .ts { opacity: .14; font-size: .8em; }
.ln.hide { display: none; }

/* repeat-count badge */
.rep-badge {
  display: inline-block; margin-left: 8px; padding: 0 7px; border-radius: 10px;
  background: var(--border); color: var(--muted); cursor: pointer;
  font: 10.5px/1.6 var(--ui); font-weight: 600; vertical-align: 1px;
  font-variant-numeric: tabular-nums;
}
.rep-badge:hover { color: var(--fg-bright); }

/* 보조 신호 badge (json/net/time/ok/correlation) — 색이 아니라 작은 칩으로 */
.badge {
  display: inline-block; margin-left: 6px; padding: 0 6px; border-radius: 9px;
  font: 10px/1.5 var(--ui); font-weight: 600; vertical-align: 1px;
  border: 1px solid var(--border-2); color: var(--muted);
}
.badge.net  { color: #8fb6d8; border-color: rgba(120,170,210,.26); }
.badge.time { color: #c2a98c; border-color: rgba(190,160,120,.24); }
.badge.ok   { color: var(--ok); border-color: rgba(87,201,138,.3); }
.badge.corr { color: #9fb0c0; background: var(--bg); font-variant-numeric: tabular-nums; }

/* 접힌 유사 반복의 펼친 목록 */
.variants { display: none; margin: 3px 0 2px; padding-left: 4px; border-left: 1px dashed var(--border-2); }
.variants.open { display: block; }
.variants .v { color: var(--muted); white-space: pre-wrap; word-break: break-word; }
.variants .vts { color: var(--faint); margin-right: 8px; font-variant-numeric: tabular-nums; }

/* gap / section divider */
.gap { display: flex; align-items: center; gap: 10px; color: var(--faint);
       font: 10.5px var(--ui); padding: 5px 6px; user-select: none; }
.gap::before, .gap::after { content: ""; height: 1px; background: var(--border); flex: 1; }
.gap.marker { color: var(--accent); }
.gap.marker::before, .gap.marker::after { background: var(--accent-bg); }
.gap.section { color: var(--muted); }
.gap.section::before, .gap.section::after { background: var(--border-2); }

/* search highlight */
mark { background: rgba(216,162,58,.32); color: inherit; border-radius: 2px;
       box-shadow: 0 0 0 1px rgba(216,162,58,.4); }
mark.cur { background: var(--warn); color: #1a1205; box-shadow: 0 0 0 1px var(--warn); }

/* decorations — 절제: key는 약하게, 값만 약간 선명. true/false/null/number 과장 금지. */
.ok   { color: var(--ok); }
.tag  { font-weight: 600; }
.jk   { color: var(--muted); }                 /* JSON key — 약하게 */
.js   { color: #b8c98a; }                       /* string value — 본문보다 약간 선명 */
.jn   { color: var(--fg); }                      /* number — 거의 본문색 */
.jb   { color: var(--muted); }                   /* true/false/null — 약하게 */
.jp   { color: var(--faint); }                   /* 구두점 { } [ ] : , — dim */

/* payload 접기(긴 JSON / 장문) */
.fold .full { display: none; }
.fold.open .full { display: block; margin: 2px 0; }
.fold.open .preview { display: none; }
.pfold {
  appearance: none; border: 0; background: var(--border); color: var(--muted); cursor: pointer;
  border-radius: 4px; font: 10px var(--ui); padding: 0 5px; margin-right: 6px; vertical-align: 1px;
}
.pfold:hover { color: var(--fg-bright); }
.fold pre { margin: 0; font: inherit; white-space: pre-wrap; word-break: break-word; }

/* ANSI: 펌웨어가 보낸 색은 inline style 로 run마다 적용(중첩/미닫힘 불가). */

/* ============================ OVERLAYS ============================ */
#newpill {
  position: fixed; left: 50%; bottom: 18px; transform: translateX(-50%);
  z-index: 30; display: none; align-items: center; gap: 8px;
  background: var(--accent); color: #06131f; font: 12px var(--ui); font-weight: 700;
  border: 0; border-radius: 18px; padding: 8px 16px; cursor: pointer;
  box-shadow: 0 8px 24px rgba(0,0,0,.5);
}
#newpill.show { display: inline-flex; }

#empty {
  display: none; text-align: center; color: var(--faint);
  font: 13px var(--ui); padding: 80px 20px;
}
#empty.show { display: block; }
#empty .big { font-size: 15px; color: var(--muted); margin-bottom: 6px; }

/* keyboard help */
#help {
  display: none; position: fixed; inset: 0; z-index: 50;
  background: rgba(5,8,12,.7); backdrop-filter: blur(2px);
  align-items: center; justify-content: center;
}
#help.open { display: flex; }
#help .card {
  background: var(--bg-raised); border: 1px solid var(--border-2); border-radius: 12px;
  padding: 22px 26px; min-width: 340px; box-shadow: 0 20px 60px rgba(0,0,0,.6);
  font-family: var(--ui);
}
#help h2 { margin: 0 0 14px; font-size: 14px; color: var(--fg-bright); }
#help dl { display: grid; grid-template-columns: auto 1fr; gap: 9px 18px; margin: 0; }
#help dt { text-align: right; }
#help dd { margin: 0; color: var(--muted); font-size: 12.5px; }
kbd {
  font: 11px var(--mono); background: var(--bg); border: 1px solid var(--border-2);
  border-bottom-width: 2px; border-radius: 4px; padding: 2px 6px; color: var(--fg-bright);
}

@media (max-width: 720px) {
  .baud, .err-pill { display: none !important; }
  .bar.tools { gap: 6px; flex-wrap: wrap; }
  .search { flex-basis: 100%; order: 5; }
  body { flex-direction: column; }
  #nav { flex: 0 0 auto; height: auto; position: static; max-height: 42vh;
         border-right: 0; border-bottom: 1px solid var(--border); }
}

/* ============================ 포트 상태 보드 ============================ */
.btn .pill-n { background: var(--border); color: var(--muted); border-radius: 8px;
               padding: 0 6px; font-size: 11px; font-weight: 700; font-variant-numeric: tabular-nums; }
.btn .chev { transition: transform .15s; width: 13px; height: 13px; }
#boardToggle.open { background: var(--accent-bg); border-color: var(--accent); color: var(--fg-bright); }
#boardToggle.open .pill-n { background: var(--accent); color: #06131f; }
#boardToggle.open .chev { transform: rotate(180deg); }

.portboard { font-family: var(--ui); display: flex; flex-direction: column; gap: 10px; }
.portboard[hidden] { display: none; }

/* H.W 유닛 박스 — 평상시에도 살짝 보이는 테두리, 작은 라운드, 박스 간 간격 */
.hwboxes { display: flex; flex-direction: column; gap: 8px; }
.hwbox { border: 1px solid var(--border-2); border-radius: 5px; padding: 3px; background: var(--bg);
         transition: border-color .12s; }
.hwbox:hover { border-color: #3a4350; }
.hwbox-label { font: 700 10px/1 var(--ui); letter-spacing: .07em; color: var(--accent);
               padding: 4px 6px 5px; text-align: center; }

/* 포트 행 */
.prow { display: grid; grid-template-columns: 9px 1fr auto; gap: 9px; align-items: center;
        padding: 5px 7px; border-radius: 4px; cursor: pointer; }
.prow:hover { background: var(--bg-hover); }
.prow.active { background: var(--accent-bg); }
.prow .dot { width: 9px; height: 9px; animation: none; }
.pb-board { font: 13px var(--mono); color: var(--fg-bright);
            white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.prow.active .pb-board { color: var(--accent); font-weight: 600; }
.pb-com { font: 11.5px var(--mono); color: var(--muted); white-space: nowrap; }

/* AI 세션 카드 */
.sess-card {
  display: flex; flex-direction: column; gap: 9px; justify-content: center;
  flex: none; align-self: flex-start;
  background: transparent; border: 0; border-radius: 0;
  padding: 0;
}
.sess-card.owned { border-color: rgba(91,155,216,.4); }
.sess-card.free { border-style: dashed; border-color: var(--border-2); }
.sess-head { display: flex; align-items: center; gap: 9px; }
.sess-card .sess-name { flex: 1 1 auto; }
.vmark { width: 22px; height: 22px; border-radius: 7px; flex: none;
         display: grid; place-items: center; background: var(--accent-bg); color: var(--accent); }
.vmark svg { width: 13px; height: 13px; }
.vmark.claude { background: #D97757; color: #fff; }
.spark-claude { color: #D97757; font-size: 20px; line-height: 1; width: 22px; flex: none;
                display: inline-flex; align-items: center; justify-content: center; }
.vmark.openai { background: #0e1714; color: #19c39a; border: 1px solid #1d3b33; }
.vmark.free   { background: transparent; color: var(--muted); border: 1px dashed var(--border-2); }
.sess-name { font: 12.5px var(--ui); font-weight: 700; color: var(--fg-bright);
             white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.sess-meta { font: 11px var(--ui); color: var(--muted); }
.btn.release {
  flex: none; padding: 4px 10px; font-size: 11.5px; font-weight: 600; gap: 5px;
  color: var(--err); border-color: rgba(240,120,111,.35); background: var(--err-bg);
}
.btn.release:hover { border-color: var(--err); background: rgba(240,120,111,.2); }
.sess-card.free .free-tag { font: 11.5px var(--ui); color: var(--ok); }

/* ===================== 토폴로지 그래프(좌측 통합 사이드바) =====================
   멀티홉 메시 시각화. 노드 = 직접 연결된 포트(SB 만 ESP+STM 2포트 → [ESP|STM] 분할).
   노드/칩 클릭 = 그 포트 로그로 뷰 전환(기존 좌측 슬라이드 동작 흡수). 배치(row/col)는
   백엔드 /api/topology 가 주고, 프론트는 절대배치로 그린다(추론 안 함). */
.topo { display: flex; flex-direction: column; gap: 13px; }

.tgroup { background: var(--bg); border: 1px solid var(--border-2); border-radius: 10px; padding: 12px 12px 14px; }
.tgroup-num { font: 700 10px var(--ui); letter-spacing: .08em; color: var(--muted); margin: 0 0 6px 2px; }
.tcanvas { position: relative; }   /* width/height 인라인 — 절대배치 노드 컨테이너 */
.tedges { position: absolute; top: 0; left: 0; pointer-events: none; overflow: visible; }   /* 링크선 — 노드 뒤·클릭 비간섭 */

/* 노드 = 라벨(밖·위) + 박스(색). 단일 MCU(SSM/REPEAT/APU/APU_C)는 박스에 ESP 한 칸,
   SB 는 박스를 좌우로 나눠 ESP|STM 두 칸(각 칸이 포트 클릭 타깃·자체 상태점). */
.tnode { position: absolute; box-sizing: border-box; display: flex; flex-direction: column;
         align-items: stretch; user-select: none; }
.tn-name { font: 700 11px var(--ui); color: var(--fg-bright); letter-spacing: .3px;
           text-align: center; margin-bottom: 3px; white-space: nowrap;
           overflow: hidden; text-overflow: ellipsis; }
.tn-box { flex: 1; display: flex; border: 1.5px solid var(--border-2); border-radius: 8px;
          overflow: hidden; transition: box-shadow .12s; }
.tn-box.active { box-shadow: 0 0 0 1px var(--accent), 0 0 8px rgba(91,155,216,.35); }
.tn-cell { position: relative; flex: 1; display: flex; align-items: center; justify-content: center;
           cursor: pointer; font: 600 12px var(--ui); color: var(--fg); }
.tn-cell:hover { background: var(--bg-hover); }
.tn-cell.active { background: var(--accent-bg); color: var(--accent); }
.tn-cell + .tn-cell { border-left: 1px solid var(--border-2); }   /* SB 좌/우 칸 구분선 */
.tn-stat { position: absolute; top: 5px; right: 6px; width: 6px; height: 6px; border-radius: 50%; }

/* 미분류 존 — 자동발견 안 된 포트를 COMx 뱃지만 담백하게 나열(라벨·테두리 박스 없음). */
.tunclassified { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; padding: 0 2px; }
.uport { font: 600 10.5px var(--mono); padding: 2px 8px; border: 1px solid var(--border-2);
         border-radius: 6px; color: var(--fg-bright); background: var(--bg); cursor: pointer; }
.uport:hover { border-color: #3a4350; }
.uport.active { border-color: var(--accent); color: var(--accent); background: var(--accent-bg); }

/* ===================== 색 강도(Off/Min/Normal/Vivid) =====================
   "색은 신호" 의 강약. Off=거의 원문, Normal=기본(약한 틴트), Vivid=강조 강화. */

/* Normal/Vivid 에서만 카테고리 배경 틴트(약하게). Min/Off 는 bar 만 또는 무채색. */
body.int-normal .ln.err,  body.int-vivid .ln.err  { background: var(--err-bg); }
body.int-normal .ln.warn, body.int-vivid .ln.warn { background: var(--warn-bg); }
body.int-vivid .ln.boot { background: var(--boot-bg); }
body.int-vivid .ln.err .txt { color: #ffc2bb; }
body.int-vivid .ln.warn .txt { color: #e8bd62; }
body.int-vivid .ok { color: #7ee0a6; font-weight: 600; }
body.int-vivid .tag { font-weight: 700; }

/* Min: tag·payload 채도를 거의 죽이고 bar/badge 로만 구조 표현 */
body.int-min .tag { color: var(--muted) !important; font-weight: 500; }
body.int-min .js, body.int-min .jn { color: var(--fg); }
body.int-min .ok { color: var(--muted); }

/* Off: 데코·badge·bar 전부 제거 → 거의 원문 보기 (renderBodyHTML 도 평문 반환) */
body.int-off .ln { box-shadow: none !important; background: transparent !important; }
body.int-off .ln .txt { color: var(--fg) !important; }
body.int-off .tag, body.int-off .ok,
body.int-off .jk, body.int-off .js, body.int-off .jn, body.int-off .jb, body.int-off .jp { color: inherit !important; font-weight: 400 !important; }
body.int-off .badge { display: none !important; }

/* ── 리듬: 줄 간격 + 세로 여백 ── */
body.rhythm-dense   { --row-pad: 0px; --lh: 1.3; }
body.rhythm-normal  { --row-pad: 2px; --lh: 1.55; }
body.rhythm-relaxed { --row-pad: 6px; --lh: 1.9; }
</style>
</head>
<body class="int-normal rhythm-normal">

<aside id="nav">
  <section class="portboard" id="portboard"></section>
</aside>

<div id="content">
  <header>
  <div class="bar tools">
    <div class="tabs">
      <button id="tabStream" class="active" title="실시간 원본 — 수신한 그대로. 접기 토글로 반복 압축 가능(테라텀 대체)">
        스트림 <span class="count" id="cStream">0/5000</span>
      </button>
      <button id="tabBuffer" title="AI가 보는 것 — 서버 버퍼 그대로(클라 재가공 없음). 교차검증용">
        버퍼 <span class="count" id="cBuffer">0/2000</span>
      </button>
    </div>

    <div class="search" id="searchWrap">
      <svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg>
      <input id="search" placeholder="" spellcheck="false" autocomplete="off">
      <div class="nav">
        <span class="matchn" id="matchn"></span>
        <button class="navbtn" id="prev" title="이전 매치 (Shift+Enter)">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><path d="m18 15-6-6-6 6"/></svg>
        </button>
        <button class="navbtn" id="next" title="다음 매치 (Enter)">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><path d="m6 9 6 6 6-6"/></svg>
        </button>
      </div>
    </div>

    <div class="levels">
      <button class="chip err"  id="lvErr"  title="에러 줄 표시/숨김">ERR <span class="n" id="nErr">0</span></button>
      <button class="chip warn" id="lvWarn" title="경고 줄 표시/숨김">WARN <span class="n" id="nWarn">0</span></button>
      <button class="chip boot" id="lvBoot" title="부팅/리셋/설정 줄 표시/숨김">BOOT <span class="n" id="nBoot">0</span></button>
    </div>

    <button class="btn icon on" id="follow" title="자동 스크롤 (F)">
      <svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 5v14M19 12l-7 7-7-7"/></svg>
    </button>
    <button class="btn icon danger" id="clear" title="화면 지우기 (서버 버퍼는 그대로)">
      <svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 7h16M9 7V5a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v2m-9 0 1 13a1 1 0 0 0 1 1h6a1 1 0 0 0 1-1l1-13"/></svg>
    </button>
    <div class="pop-wrap">
      <button class="btn icon" id="gear" title="표시 설정">
        <svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.6 1.6 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.6 1.6 0 0 0-1.8-.3 1.6 1.6 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.6 1.6 0 0 0-1-1.5 1.6 1.6 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.6 1.6 0 0 0 .3-1.8 1.6 1.6 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.6 1.6 0 0 0 1.5-1 1.6 1.6 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.6 1.6 0 0 0 1.8.3H9a1.6 1.6 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.6 1.6 0 0 0 1 1.5 1.6 1.6 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.6 1.6 0 0 0-.3 1.8V9a1.6 1.6 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.6 1.6 0 0 0-1.5 1Z"/></svg>
      </button>
      <div class="pop" id="pop">
        <div class="row"><span>글자 크기</span>
          <div class="seg"><button id="fsDown">A−</button><button id="fsUp">A+</button></div>
        </div>
        <div class="row"><span>색 강도</span>
          <div class="seg" id="segIntensity">
            <button data-v="off">Off</button><button data-v="min">Min</button>
            <button data-v="normal">Norm</button><button data-v="vivid">Vivid</button>
          </div>
        </div>
        <div class="row"><span>줄 간격</span>
          <div class="seg" id="segRhythm">
            <button data-v="dense">조밀</button><button data-v="normal">보통</button><button data-v="relaxed">여유</button>
          </div>
        </div>
        <div class="row"><span>JSON</span>
          <div class="seg" id="segJson">
            <button data-v="inline">한 줄</button><button data-v="compact">접기</button><button data-v="pretty">펼침</button>
          </div>
        </div>
        <div class="row"><span>간격선(초)</span>
          <div class="seg" id="segGap">
            <button data-v="0">끔</button><button data-v="2">2</button><button data-v="5">5</button><button data-v="10">10</button>
          </div>
        </div>
        <div class="div"></div>
        <div class="row toggle" id="tgTs"><span>타임스탬프</span><span class="switch"></span></div>
        <div class="row toggle" id="tgWrap"><span>줄바꿈</span><span class="switch"></span></div>
        <div class="row toggle" id="tgFold"><span>반복 줄 접기</span><span class="switch"></span></div>
        <div class="row"><span>반복 판정</span>
          <div class="seg" id="segFoldmode"><button data-v="norm">값 무시</button><button data-v="exact">정확</button></div>
        </div>
        <div class="row toggle" id="tgAnsi"><span>ANSI 색</span><span class="switch"></span></div>
        <div class="row toggle" id="tgSemantic"><span>의미 색(분류)</span><span class="switch"></span></div>
        <div class="row toggle" id="tgFocus"><span>Focus (에러·경고·성공)</span><span class="switch"></span></div>
        <div class="div"></div>
        <div class="row toggle" id="tgHelp"><span>단축키 도움말</span><kbd>?</kbd></div>
      </div>
    </div>
  </div>
</header>

<main>
  <div id="stream"></div>
  <div id="buffer" style="display:none"></div>
  <div id="empty"><div class="big">로그 없음</div><span id="emptyHint">수신 대기 중…</span></div>
</main>
</div>

<button id="newpill">
  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><path d="M12 5v14M19 12l-7 7-7-7"/></svg>
  <span id="newpillText">새 로그 0건</span>
</button>

<div id="help">
  <div class="card">
    <h2>키보드 단축키</h2>
    <dl>
      <dt><kbd>/</kbd></dt><dd>검색에 포커스</dd>
      <dt><kbd>Enter</kbd> / <kbd>⇧Enter</kbd></dt><dd>다음 / 이전 매치</dd>
      <dt><kbd>Esc</kbd></dt><dd>검색 지우기 · 닫기</dd>
      <dt><kbd>F</kbd></dt><dd>자동 스크롤 토글</dd>
      <dt><kbd>1</kbd> / <kbd>2</kbd></dt><dd>스트림 / 버퍼 탭</dd>
      <dt><kbd>G</kbd></dt><dd>맨 아래로 이동</dd>
      <dt><kbd>?</kbd></dt><dd>이 도움말</dd>
    </dl>
  </div>
</div>
<script>
/* ============================================================================
   VIEWER-PURE-START — DOM 비의존 순수 로직. node 단위테스트가 이 블록만 추출해 검증한다.
   책임: parseLine / classifyLine / extractTokens / findPayload / correlationBadges /
         normalizeForRepeat / ansiToHtmlSafe / renderBodyHTML / renderPayloadHTML / escapeHtml.
   여기서는 document/window 를 절대 참조하지 않는다(맨 끝에서만 export).
   ============================================================================ */
(function () {
"use strict";
var ESC = String.fromCharCode(27);

/* ---- 안전 출력 ---- */
function escapeHtml(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
// 표시용 제어문자 정리: 탭은 보존, CR·기타 C0/C1 제어문자 제거(원문은 raw 로 보존).
function cleanCtrl(s) {
  return String(s).replace(/[\x00-\x08\x0b-\x1f\x7f-\x9f]/g, "");
}
// ANSI/이스케이프 시퀀스 제거: CSI(ESC[ … 종결자) + 2바이트 이스케이프.
function stripAnsi(s) {
  return String(s).replace(new RegExp(ESC + "\\[[0-9;:?<>=]*[ -/]*[@-~]|" + ESC + "[@-_]", "g"), "");
}

/* ---- ANSI → HTML (run 마다 self-contained span: 중첩/미닫힘 구조적으로 불가) ---- */
var STD16 = [
  "#5a626d","#ff7b72","#3fb950","#d29922","#58a6ff","#bc8cff","#39c5cf","#b1bac4",
  "#6e7681","#ffa198","#56d364","#e3b341","#79c0ff","#d2a8ff","#56d4dd","#f0f6fc"
];
function hex2(x) { x = x & 255; return (x < 16 ? "0" : "") + x.toString(16); }
function rgbHex(r, g, b) { return "#" + hex2(r) + hex2(g) + hex2(b); }
function xterm256(n) {
  n = n | 0;
  if (n < 16) return STD16[n] || STD16[7];
  if (n >= 232) { var g = 8 + (n - 232) * 10; return rgbHex(g, g, g); }
  n -= 16;
  var conv = function (x) { return x === 0 ? 0 : 55 + x * 40; };
  return rgbHex(conv(Math.floor(n / 36)), conv(Math.floor((n % 36) / 6)), conv(n % 6));
}
function blankStyle() { return { fg: null, bg: null, bold: false, dim: false, italic: false, underline: false, inverse: false }; }
function applySgr(st, params) {
  var codes = (params === "" ? "0" : params).split(/[;:]/).map(function (x) { return x === "" ? 0 : (+x | 0); });
  for (var i = 0; i < codes.length; i++) {
    var c = codes[i];
    if (c === 0) { var b = blankStyle(); for (var k in b) st[k] = b[k]; }
    else if (c === 1) st.bold = true;
    else if (c === 2) st.dim = true;
    else if (c === 3) st.italic = true;
    else if (c === 4) st.underline = true;
    else if (c === 7) st.inverse = true;
    else if (c === 22) { st.bold = false; st.dim = false; }
    else if (c === 23) st.italic = false;
    else if (c === 24) st.underline = false;
    else if (c === 27) st.inverse = false;
    else if (c >= 30 && c <= 37) st.fg = STD16[c - 30];
    else if (c === 39) st.fg = null;
    else if (c >= 90 && c <= 97) st.fg = STD16[c - 90 + 8];
    else if (c >= 40 && c <= 47) st.bg = STD16[c - 40];
    else if (c === 49) st.bg = null;
    else if (c >= 100 && c <= 107) st.bg = STD16[c - 100 + 8];
    else if (c === 38 || c === 48) {
      var tgt = c === 38 ? "fg" : "bg";
      // 잘린 38;5/38;2 시퀀스(인자 부족)는 가짜 색을 만들지 않게 안전 중단 — codes 끝 초과 읽기 방지
      if (codes[i + 1] === 5 && i + 2 < codes.length) { st[tgt] = xterm256(codes[i + 2]); i += 2; }
      else if (codes[i + 1] === 2 && i + 4 < codes.length) { st[tgt] = rgbHex(codes[i + 2], codes[i + 3], codes[i + 4]); i += 4; }
      else break;
    }
    /* 그 외 SGR 코드는 무시(안전) */
  }
}
function styleToCss(st) {
  var fg = st.fg, bg = st.bg;
  if (st.inverse) { var t = fg; fg = bg || "#0a0d12"; bg = t || "#c5ccd6"; }
  var css = "";
  if (fg) css += "color:" + fg + ";";
  if (bg) css += "background:" + bg + ";";
  if (st.bold) css += "font-weight:bold;";
  if (st.dim) css += "opacity:.6;";
  if (st.italic) css += "font-style:italic;";
  if (st.underline) css += "text-decoration:underline;";
  return css;
}
function ansiRun(st, text) {
  if (text === "") return "";
  var safe = escapeHtml(cleanCtrl(text));
  var css = styleToCss(st);
  return css ? '<span style="' + css + '">' + safe + "</span>" : safe;
}
// enable=false → ANSI 제거 후 평문. 어떤 입력에도 try/catch 로 HTML 무결성 보장.
function ansiToHtmlSafe(text, opts) {
  text = String(text == null ? "" : text);
  if (!opts || !opts.enable) return escapeHtml(cleanCtrl(stripAnsi(text)));
  try {
    var re = new RegExp(ESC + "\\[([0-9;:?]*)m|" + ESC + "\\[[0-9;:?<>=]*[ -/]*[@-~]|" + ESC + "[@-_]", "g");
    var out = "", last = 0, m, st = blankStyle();
    while ((m = re.exec(text))) {
      out += ansiRun(st, text.slice(last, m.index));
      last = re.lastIndex;
      if (m[1] !== undefined) applySgr(st, m[1]);   // SGR 만 스타일 반영(커서·지움 등은 무시·제거)
      if (re.lastIndex === m.index) re.lastIndex++; // 0폭 매치 방지
    }
    out += ansiRun(st, text.slice(last));
    return out;
  } catch (e) {
    return escapeHtml(cleanCtrl(stripAnsi(text)));  // 실패해도 원문(평문)으로 안전 fallback
  }
}

/* ---- 문자 통계(noise 감지용) ---- */
function charStats(raw) {
  var s = String(raw), total = s.length, ctrl = 0, print = 0, repl = 0, run = 0, maxRun = 0;
  for (var i = 0; i < total; i++) {
    var c = s.charCodeAt(i);
    if (c === 0xFFFD) repl++;
    if (c === 0x1b) continue;                                   // ESC 는 ANSI 로 별도 처리
    if ((c < 0x20 && c !== 0x09) || (c >= 0x7f && c <= 0x9f)) ctrl++; else print++;
    var sym = (c >= 0x21 && c <= 0x2f) || (c >= 0x3a && c <= 0x40) || (c >= 0x5b && c <= 0x60) || (c >= 0x7b && c <= 0x7e);
    if (sym) { run++; if (run > maxRun) maxRun = run; } else run = 0;
  }
  var denom = ctrl + print || 1;
  return { total: total, ctrlRatio: ctrl / denom, printRatio: print / denom, replacement: repl, symbolRun: maxRun };
}

/* ---- 토큰 패턴(구조적 특징; 특정 문자열 암기 아님) ---- */
var RE_TAG   = /\[[^\[\]\r\n]{1,40}\]/;
var RE_IP    = /\b(?:\d{1,3}\.){3}\d{1,3}\b/;
var RE_MAC   = /\b(?:[0-9A-Fa-f]{2}[:\-]){5}[0-9A-Fa-f]{2}\b/;
var RE_URL   = /\b[a-z][a-z0-9+.\-]*:\/\/[^\s'"]+/i;
var RE_UUID  = /\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b/i;
var RE_KV    = /[\w.\-]+\s*[:=]\s*\S/;
var RE_DUR   = /\b\d+(?:\.\d+)?\s*(?:ns|us|µs|ms|sec|secs|seconds|min|h)\b/i;

function countWords(s, re) { var m = s.match(re); return m ? m.length : 0; }

/* ---- 라인 모델 ---- */
function parseLine(raw, meta) {
  meta = meta || {};
  raw = raw == null ? "" : String(raw);
  var visible = cleanCtrl(stripAnsi(raw));
  return {
    raw: raw,
    visible: visible,
    ts: meta.ts || "",
    source: meta.source || "",
    count: meta.count || 1,
    firstTs: meta.firstTs || meta.ts || "",
    lastTs: meta.lastTs || meta.ts || "",
    hasAnsi: raw.indexOf(ESC) !== -1,
    blank: visible.trim() === ""
  };
}

/* ---- score 기반 분류 ----
   구조 축(bar/틴트 대상): noise/error/warning/boot/success. 신뢰도 낮으면 neutral.
   보조 축(badge 만): network/json/timing/duplicate. */
function classifyLine(model) {
  var s = model.visible, low = s.toLowerCase();
  var sc = { error: 0, warning: 0, success: 0, boot: 0, network: 0, json: 0, timing: 0, duplicate: 0, noise: 0 };

  // noise — 문자 통계
  var stat = charStats(model.raw);
  if (stat.replacement > 0) sc.noise += 2 + Math.min(3, stat.replacement);
  if (stat.total >= 4) {
    if (stat.ctrlRatio > 0.18) sc.noise += 3;
    if (stat.printRatio < 0.62) sc.noise += 3;
    if (stat.symbolRun >= 8) sc.noise += 2;
  }

  // error
  if (/^\s*E\s*\(\s*\d+\)/.test(s)) sc.error += 3;                 // ESP-IDF: E (123)
  if (/(^|[\s\[(<])E[\]\)>\/:]/.test(s)) sc.error += 1;
  sc.error += 2 * countWords(low, /\b(error|errors|fail|failed|failure|fatal|panic|exception|abort|aborted|crash|crashed|denied|refused|unreachable|invalid|corrupt|corrupted|timeout)\b/g);
  if (/[✗✘❌]/.test(s)) sc.error += 2;
  if (/\b(no|0|zero)\s+(error|errors|fail|failure)/i.test(s)) sc.error = Math.max(0, sc.error - 3);

  // warning
  if (/^\s*W\s*\(\s*\d+\)/.test(s)) sc.warning += 3;
  sc.warning += 2 * countWords(low, /\b(warn|warning|warnings|deprecated|retry|retrying|unstable|degraded|dropped|discard|discarded|skipped)\b/g);
  if (/[⚠]/.test(s)) sc.warning += 2;

  // success
  sc.success += 2 * countWords(low, /\b(ok|success|succeeded|succeed|done|ready|complete|completed|connected|mounted|pass|passed|online|established|saved|loaded|enabled)\b/g);
  if (/[✓✔✅]/.test(s)) sc.success += 2;

  // boot/setup
  if (/^\s*(esp-rom:|rst:0x|entry 0x|load:0x|boot:|build:|configsip:|mode:[a-z]|clk_drv:|ets )/i.test(s)) sc.boot += 4;
  sc.boot += 2 * countWords(low, /\b(boot|booting|reboot|reset|resetting|setup|init|initialize|initialized|initializing|mount|mounting|firmware|bootloader|startup|starting|configuring)\b/g);

  // network/io
  if (RE_IP.test(s)) sc.network += 1;
  if (RE_MAC.test(s)) sc.network += 1;
  if (RE_URL.test(s)) sc.network += 1;
  sc.network += Math.min(3, countWords(low, /\b(wifi|wlan|tcp|udp|http|https|mqtt|socket|sock|dns|ssid|rssi|dhcp|gateway|ping|connect|connecting|disconnect|reconnect|sta|ap|ethernet|server|client|request|response|recv|receive|packet)\b/g));

  // timing
  if (RE_DUR.test(s)) sc.timing += 1;
  sc.timing += Math.min(2, countWords(low, /\b(elapsed|took|latency|duration|timeout|interval|uptime|heap|stack|free|mem|memory)\b/g));

  // json/payload
  var pay = findPayload(s);
  if (pay) sc.json += 3;
  else if (RE_KV.test(s)) sc.json += 1;

  // duplicate
  if (model.count > 1) sc.duplicate += 3;

  // 구조 축 argmax
  var order = ["noise", "error", "warning", "boot", "success"];
  var primary = "", top = 0, second = 0;
  for (var i = 0; i < order.length; i++) {
    var v = sc[order[i]];
    if (v > top) { second = top; top = v; primary = order[i]; }
    else if (v > second) second = v;
  }
  var confidence = 0;
  if (top >= 2) confidence = Math.min(1, (top - 1) / 5) * (top - second >= 1 ? 1 : 0.5);
  if (top < 2) primary = "";   // 단일 명시신호(score>=2)는 신뢰; 동점은 argmax 심각도순(error>warning>boot>success)으로 결정
  // noise 는 다른 구조 신호보다 약하지 않을 때만 지배 — 쓰레기에 묻힌 명확한 에러/경고는 덮지 않는다
  if (sc.noise >= 3 && sc.noise >= sc.error && sc.noise >= sc.warning) { primary = "noise"; confidence = Math.max(confidence, 0.6); }

  var badges = [];
  if (sc.duplicate > 0) badges.push("dup");
  if (pay) badges.push("json");
  if (sc.network >= 2) badges.push("net");
  if (sc.timing >= 1 && primary !== "noise") badges.push("time");
  if (sc.success >= 2) badges.push("ok");

  return { scores: sc, primary: primary, confidence: confidence, badges: badges, payload: pay };
}

/* ---- 관용 JSON 추출(파싱 성공분만; 잘린 JSON 은 null → 평문, error 과장 금지) ---- */
function findPayload(s) {
  s = String(s);
  if (s.length > 4000) return null;          // 초장문은 평문 처리(안전 fallback; JSON 구조화만 생략, 원문은 보존)
  // 각 '{'/'[' 후보를 순서대로 시도 — 앞쪽 태그(예: "[Mod-Rx]")가 JSON 이 아니면
  // 균형/파싱에 실패하므로 자연히 건너뛰고 뒤의 진짜 payload 를 찾는다.
  var re = /[{\[]/g, m, tries = 0;
  while ((m = re.exec(s)) && tries < 24) {    // opener 후보 과다(깨진/바이너리 줄)일 때 O(n^2) 폭주 방어
    tries++;
    var i = m.index, depth = 0, inStr = false, esc = false, end = -1;
    for (var k = i; k < s.length; k++) {
      var ch = s[k];
      if (inStr) { if (esc) esc = false; else if (ch === "\\") esc = true; else if (ch === '"') inStr = false; continue; }
      if (ch === '"') { inStr = true; continue; }
      if (ch === "{" || ch === "[") depth++;
      else if (ch === "}" || ch === "]") { depth--; if (depth === 0) { end = k; break; } }
    }
    if (end < 0) continue;                          // 이 시작점은 균형 안 맞음(잘림) → 다음 후보
    var cand = s.slice(i, end + 1);
    try {
      var val = JSON.parse(cand);
      if (val !== null && typeof val === "object") {   // 스칼라는 payload 취급 안 함
        return { pre: s.slice(0, i), raw: cand, value: val, post: s.slice(end + 1) };
      }
    } catch (e) { /* 파싱 실패 → 다음 후보로 */ }
  }
  return null;
}

/* ---- correlation key 감지(이름 패턴 기반; 특정 키 고정 아님, 대소문자·변형 허용) ---- */
var RE_CORR = /^(?:.*?[_.])?(id|uid|guid|cid|cidx|idx|seq|seqno|asn|sn|no|num|ref|rid|reqid|requestid|trace|traceid|txn|tx|transactionid|msgid|messageid|sessionid|session|token|correlationid|correlation|uniqueid|unique|key)$/i;
function correlationBadges(obj) {
  var out = [];
  if (!obj || typeof obj !== "object" || Array.isArray(obj)) return out;
  var keys = Object.keys(obj);
  for (var j = 0; j < keys.length && out.length < 2; j++) {
    var k = keys[j], v = obj[k], t = typeof v;
    if (v == null) continue;
    if ((t === "string" && v.length <= 48) || t === "number") {
      if (RE_CORR.test(k)) out.push({ key: k, val: String(v) });
    }
  }
  return out;
}

/* ---- 반복 signature ---- */
var RE_MAC_G = new RegExp(RE_MAC.source, "g");
var RE_IP_G  = new RegExp(RE_IP.source, "g");
var RE_UUID_G = new RegExp(RE_UUID.source, "gi");
function normalizeForRepeat(model, mode) {
  var s = model.visible;
  if (mode === "exact") return s;
  return s
    .replace(RE_UUID_G, "<u>")
    .replace(RE_MAC_G, "<m>")
    .replace(RE_IP_G, "<ip>")
    .replace(/\b0x[0-9a-f]+\b/gi, "<x>")
    .replace(/\b\d{1,2}:\d{2}:\d{2}(?:\.\d+)?\b/g, "<t>")
    .replace(/-?\d+(?:\.\d+)?/g, "<n>")
    .replace(/\s+/g, " ").trim();
}

/* ---- 타임스탬프 → ms (gap 계산) ---- */
function tsToMs(ts) {
  var m = /^(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,3}))?/.exec(ts || "");
  if (!m) return null;
  var frac = m[4] ? +(m[4] + "00").slice(0, 3) : 0;
  return ((+m[1]) * 3600 + (+m[2]) * 60 + (+m[3])) * 1000 + frac;
}

/* ---- 태그·텍스트 데코(절제) ---- */
function tagHue(name) { var h = 0; for (var i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0; return h % 360; }
function tagSpan(name, raw, view) {
  var color = "";
  if (view.intensity === "vivid") color = "hsl(" + tagHue(name) + ",48%,70%)";
  else if (view.intensity === "normal") color = "hsl(" + tagHue(name) + ",30%,64%)";  // 저채도 고정색
  var style = color ? ' style="color:' + color + '"' : "";
  return '<span class="tag" data-tag="' + escapeHtml(name) + '"' + style + '>' + escapeHtml(raw) + "</span>";
}
function decoText(str, cls, view) {
  var parts = String(str).split(new RegExp("(" + RE_TAG.source + ")"));
  var out = "";
  for (var i = 0; i < parts.length; i++) {
    var p = parts[i];
    if (p && p.charAt(0) === "[" && RE_TAG.test(p)) out += tagSpan(p.slice(1, -1), p, view);
    else out += accentWords(escapeHtml(p), cls, view);
  }
  return out;
}
// 성공어에만 약한 accent — 분류가 success 라고 본 줄에서만(맹목 whitelist 아님).
function accentWords(escaped, cls, view) {
  if (!view.semantic || view.intensity === "off") return escaped;
  if (!cls || cls.badges.indexOf("ok") < 0) return escaped;
  return escaped.replace(/\b(OK|Success|Succeeded|Done|Ready|Connected|Mounted|PASS|Passed|Online|Established)\b/g, '<span class="ok">$&</span>');
}

/* ---- JSON 값 → HTML(절제 컬러) ---- */
function jvInline(v) {
  if (v === null) return '<span class="jb">null</span>';
  var t = typeof v;
  if (t === "number") return '<span class="jn">' + escapeHtml(String(v)) + "</span>";
  if (t === "boolean") return '<span class="jb">' + v + "</span>";
  if (t === "string") return '<span class="js">"' + escapeHtml(v) + '"</span>';
  if (Array.isArray(v)) return '<span class="jp">[</span>' + v.map(jvInline).join('<span class="jp">, </span>') + '<span class="jp">]</span>';
  if (t === "object") {
    var parts = Object.keys(v).map(function (k) {
      return '<span class="jk">' + escapeHtml(k) + '</span><span class="jp">: </span>' + jvInline(v[k]);
    });
    return '<span class="jp">{</span>' + parts.join('<span class="jp">, </span>') + '<span class="jp">}</span>';
  }
  return escapeHtml(String(v));
}
function jvPretty(v, indent) {
  indent = indent || 0;
  var pad = new Array(indent + 2).join("  "), padEnd = new Array(indent + 1).join("  ");
  if (Array.isArray(v)) {
    if (!v.length) return '<span class="jp">[]</span>';
    return '<span class="jp">[</span>\n' + v.map(function (x) { return pad + jvPretty(x, indent + 1); }).join('<span class="jp">,</span>\n') + "\n" + padEnd + '<span class="jp">]</span>';
  }
  if (v && typeof v === "object") {
    var keys = Object.keys(v);
    if (!keys.length) return '<span class="jp">{}</span>';
    return '<span class="jp">{</span>\n' + keys.map(function (k) {
      return pad + '<span class="jk">' + escapeHtml(k) + '</span><span class="jp">: </span>' + jvPretty(v[k], indent + 1);
    }).join('<span class="jp">,</span>\n') + "\n" + padEnd + '<span class="jp">}</span>';
  }
  return jvInline(v);
}
// 긴 JSON/장문은 preview + 펼치기(fold). mode: inline(항상 한 줄)/compact(길면 접기)/pretty(항상 펼침).
var LONG = 140;
function renderPayloadHTML(value, opts) {
  opts = opts || {};
  var mode = opts.mode || "compact";
  var inline = jvInline(value);
  var plain = stripTags(inline);
  if (mode === "inline") return inline;
  if (mode === "pretty" || (mode === "compact" && plain.length > LONG)) {
    var pretty = jvPretty(value, 0);
    var preview = plain.length > LONG ? escapeHtml(plain.slice(0, LONG)) + "…" : inline;
    return '<span class="fold' + (mode === "pretty" ? " open" : "") + '">' +
      '<button class="pfold" title="펼치기/접기">⤢</button>' +
      '<span class="preview">' + preview + "</span>" +
      '<pre class="full">' + pretty + "</pre></span>";
  }
  return inline;
}
function stripTags(html) { return String(html).replace(/<[^>]*>/g, "").replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&"); }

/* ---- 본문 HTML 조립 ---- */
function renderBodyHTML(model, cls, view) {
  if (model.blank) return "";
  // 색 강도 Off → 거의 원문(평문). ANSI 도 제거하여 깔끔히.
  if (view.intensity === "off") return escapeHtml(model.visible);
  // ANSI 가 있고 사용 on → 펌웨어 색을 본문에 적용(semantic 구조 bar/badge 는 호출부가 따로 얹음).
  if (model.hasAnsi && view.ansi) return ansiToHtmlSafe(model.raw, { enable: true });

  var s = model.visible;
  var pay = cls ? cls.payload : findPayload(s);   // classifyLine 이 이미 찾아둔 결과 재사용(같은 줄 재파싱 제거)
  if (pay) {
    var head = decoText(pay.pre, cls, view);
    var body = renderPayloadHTML(pay.value, { mode: view.json });
    var tail = pay.post ? decoText(pay.post, cls, view) : "";
    return head + body + tail;
  }
  // payload 없음: 태그·성공어 정도만 데코, 평문 숫자/문자열은 본문색 유지(과한 토큰 컬러 금지).
  if (s.length > 600) {           // 장문(JSON 아님)도 접을 수 있게
    return '<span class="fold"><button class="pfold" title="펼치기/접기">⤢</button>' +
      '<span class="preview">' + decoText(s.slice(0, 300), cls, view) + "…</span>" +
      '<pre class="full">' + decoText(s, cls, view) + "</pre></span>";
  }
  return decoText(s, cls, view);
}

/* ---- 토폴로지 그래프 순수로직(모듈8 ① edges 링크선) — DOM 비의존 계산만 ---- */

/* rssi(dBm) → 링크강도 색. 강(-30)=초록 → 약(-90)=빨강, 미상(null/NaN/공백)=중립 회색.
   REPRSSI/[Route] Link 간선의 농도 표현에 쓴다. 범위 밖은 클램프(throw 없음). */
function rssiColor(rssi) {
  var r = Number(rssi);
  if (rssi == null || rssi === "" || isNaN(r)) return "#707b88";   // 미상 = 중립(stale 계열)
  if (r < -90) r = -90; else if (r > -30) r = -30;                 // 강약 범위 클램프
  var hue = Math.round(((r + 90) / 60) * 120);                     // 0(약)=빨강 → 120(강)=초록
  return "hsl(" + hue + ",62%,55%)";
}

/* 로스터 edges({from,to = mac}) + 배치된 노드(placed:[{n:{mac},x,y,w,h}]) → 선분 목록.
   노드 mac 으로 양 끝을 찾아 중심좌표로 선분화한다. 배치에 없는 mac(원격 mesh 노드 등)이나
   자기루프는 그릴 대상이 없어 skip. 좌표 계산만 하고 DOM 은 board.js 가 그린다. */
function edgeSegments(placed, edges) {
  var byMac = {}, ps = placed || [];
  for (var i = 0; i < ps.length; i++) {
    var p = ps[i], n = p && p.n, mac = n && n.mac;
    if (mac) byMac[mac] = { x: p.x + (p.w || 0) / 2, y: p.y + (p.h || 0) / 2 };
  }
  var out = [], es = edges || [];
  for (var j = 0; j < es.length; j++) {
    var e = es[j] || {}, a = byMac[e.from], b = byMac[e.to];
    if (!a || !b || e.from === e.to) continue;
    out.push({ x1: a.x, y1: a.y, x2: b.x, y2: b.y, rssi: e.rssi, fresh: e.fresh });
  }
  return out;
}

/* ---- export (browser global + node require 양쪽) ---- */
var SViewer = {
  escapeHtml: escapeHtml, cleanCtrl: cleanCtrl, stripAnsi: stripAnsi,
  ansiToHtmlSafe: ansiToHtmlSafe, xterm256: xterm256, charStats: charStats,
  parseLine: parseLine, classifyLine: classifyLine, findPayload: findPayload,
  correlationBadges: correlationBadges, normalizeForRepeat: normalizeForRepeat,
  tsToMs: tsToMs, renderBodyHTML: renderBodyHTML, renderPayloadHTML: renderPayloadHTML,
  decoText: decoText,
  rssiColor: rssiColor, edgeSegments: edgeSegments
};
if (typeof module !== "undefined" && module.exports) module.exports = SViewer;
if (typeof window !== "undefined") window.SViewer = SViewer;
})();
/* ============================================================================
   VIEWER-PURE-END
   ============================================================================ */

/* board.js — 왼쪽 네비게이션의 포트 상태.
   소유권 모델: AI가 MCP를 호출하면 그 MCP 서버(= 단일 AI 세션)가 모든 포트를 통째로 점유.
   레이아웃: [AI 세션 + 해제] 를 맨 위, 그 아래 H.W 유닛별 박스(SSM, SB, SB1 … 늘어날 수 있음).
   SB처럼 한 유닛에 여러 포트가 있으면 한 박스 안에 위아래로 묶인다.
   app.js 가 /api/status 응답으로 renderPortBoard(ports, session, active, onSelect, onRelease) 호출. */
"use strict";

(function () {
  let lastSig = "";
  const SV = window.SViewer;                       // 순수 로직(edgeSegments·rssiColor) — VIEWER-PURE 에서 export
  const SVGNS = "http://www.w3.org/2000/svg";

  function el(tag, cls) { const e = document.createElement(tag); if (cls) e.className = cls; return e; }
  function txt(tag, t, cls) { const e = el(tag, cls); e.textContent = t; return e; }
  function svgEl(tag, attrs) {                      // SVG 는 createElementNS + setAttribute 라야 그려진다
    const e = document.createElementNS(SVGNS, tag);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    return e;
  }

  // 장비 타입 색·배지·상태 색 (디자인 토큰)
  var TYPE_COLOR = { SSM: "#56d4dd", REPEAT: "#e3b341", APU: "#3fb950", APU_C: "#2ea043", SB: "#6ab7ff" };
  var STATUS_COLOR = { good: "#3fb950", live: "#3fb950", checking: "#e3b341", bad: "#f0786f", stale: "#707b88" };

  function tint(hex, a) {
    const n = parseInt(hex.slice(1), 16);
    return "rgba(" + ((n >> 16) & 255) + "," + ((n >> 8) & 255) + "," + (n & 255) + "," + a + ")";
  }

  // 그룹 노드 배치 — 디자인 layout() 포팅. 타입 행을 band 로 묶어 빈 행은 접는다.
  // 반환 { w, h, placed:[{n,x,y,w,h}] }. UI 는 백엔드가 준 row/col 만 받아 절대배치(추론 안 함).
  function layoutGroup(nodes) {
    const SW = 108, SH = 66, GX = 16, RG = 14, BG = 26;
    const bands = [[0], [1], [2, 3], [4, 5]];
    const rows = new Set(nodes.map(n => n.row));
    const rowY = {}; let y = 0, prev = false;
    for (const band of bands) {
      const vis = band.filter(r => rows.has(r));
      if (!vis.length) continue;
      if (prev) y += BG;
      vis.forEach((r, i) => { rowY[r] = y; y += SH; if (i < vis.length - 1) y += RG; });
      prev = true;
    }
    let maxCol = 0;
    for (const n of nodes) maxCol = Math.max(maxCol, n.col || 0);
    const w = (maxCol + 1) * (SW + GX) - GX, h = y;
    const placed = nodes.map(n => ({ n: n, x: (n.col || 0) * (SW + GX), y: rowY[n.row] || 0, w: SW, h: SH }));
    return { w: w, h: h, placed: placed };
  }

  // 노드: 라벨(밖·위) + 박스. 단일 MCU=박스에 한 칸, SB=좌우 두 칸(ESP|STM, 각 칸 클릭·상태점).
  function renderNode(p, active, onSelect) {
    const n = p.n, tc = TYPE_COLOR[n.type] || "#8b949e";
    const wrap = el("div", "tnode");
    wrap.style.left = p.x + "px"; wrap.style.top = p.y + "px";
    wrap.style.width = p.w + "px"; wrap.style.height = p.h + "px";
    wrap.appendChild(txt("div", n.label, "tn-name"));        // 타입/식별 라벨은 노드 밖(위)
    const box = el("div", "tn-box");
    box.style.borderColor = tint(tc, .55);
    box.style.background = "linear-gradient(0deg," + tint(tc, .1) + "," + tint(tc, .1) + "),var(--bg-raised)";
    const ports = n.ports || [];
    if (ports.some(pt => pt.port === active)) box.classList.add("active");
    for (const pt of ports) {                                // 단일=1칸, SB=2칸(ESP|STM)
      const cell = el("div", "tn-cell" + (pt.port === active ? " active" : ""));
      cell.title = "클릭 — " + pt.port + " 로그 보기";
      const stat = el("span", "tn-stat");
      stat.style.background = pt.connected ? STATUS_COLOR.good : STATUS_COLOR.stale;
      cell.appendChild(stat);
      cell.appendChild(txt("span", pt.mcu || "?"));          // 박스 안 = MCU(ESP/STM)
      cell.onclick = () => onSelect(pt.port);
      box.appendChild(cell);
    }
    wrap.appendChild(box);
    return wrap;
  }

  // 링크선(REPRSSI/[Route] Link) SVG 오버레이 — 노드 박스 뒤에 깔린다. edges 없으면 null.
  // 좌표·매칭은 SViewer.edgeSegments(순수, 테스트됨), 간선 색은 rssiColor(강=초록·약=빨강).
  // fresh=false(오래된 링크)는 옅게. path 실제경로가 아니라 '가능한 링크' 그래프임에 유의(plan §4).
  function renderEdges(lay, edges) {
    const segs = SV.edgeSegments(lay.placed, edges);
    if (!segs.length) return null;
    const svg = svgEl("svg", { "class": "tedges", width: lay.w, height: lay.h });
    for (const s of segs) {
      svg.appendChild(svgEl("line", {
        x1: s.x1, y1: s.y1, x2: s.x2, y2: s.y2,
        stroke: SV.rssiColor(s.rssi), "stroke-width": "2",
        "stroke-opacity": s.fresh === false ? "0.3" : "0.85", "stroke-linecap": "round",
      }));
    }
    return svg;
  }

  // 그룹 = "그룹 N" 순번(밖·위) + 박스(노드 절대배치). 그룹↔SSM 1:1.
  function renderGroup(g, idx, active, onSelect) {
    const wrap = el("div");
    wrap.appendChild(txt("div", "그룹 " + (idx + 1), "tgroup-num"));
    const box = el("div", "tgroup");
    const lay = layoutGroup(g.nodes || []);
    box.style.width = (lay.w + 26) + "px";   // 콘텐츠 크기로 좌측 정렬
    const canvas = el("div", "tcanvas");
    canvas.style.width = lay.w + "px"; canvas.style.height = lay.h + "px";
    const edges = renderEdges(lay, g.edges);            // 노드 아래 링크선(있을 때만)
    if (edges) canvas.appendChild(edges);
    for (const pl of lay.placed) canvas.appendChild(renderNode(pl, active, onSelect));
    box.appendChild(canvas);
    wrap.appendChild(box);
    return wrap;
  }

  // 미분류 존 — 자동발견 안 된 포트를 COMx 뱃지만 담백하게. 클릭=그 포트 로그.
  function renderUnclassified(unplaced, active, onSelect) {
    const box = el("div", "tunclassified");
    for (const port of unplaced) {
      const b = txt("span", port, "uport" + (port === active ? " active" : ""));
      b.title = "클릭 — " + port + " 로그 보기";
      b.onclick = () => onSelect(port);
      box.appendChild(b);
    }
    return box;
  }

  // [해제] 카드 onRelease 라벨용 — 로스터의 모든 포트를 {port} 객체로.
  function allPorts(roster) {
    const out = [];
    for (const g of (roster.groups || []))
      for (const n of (g.nodes || []))
        for (const pt of (n.ports || [])) out.push({ port: pt.port });
    for (const port of (roster.unplaced || [])) out.push({ port: port });
    return out;
  }

  // AI 벤더 시그니처 배지 — 상표 로고 대신 식별용 색상+글리프(세션 문자열로 판별)
  const SPARK = '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2c.5 4.6 2.4 6.5 7 7-4.6.5-6.5 2.4-7 7-.5-4.6-2.4-6.5-7-7 4.6-.5 6.5-2.4 7-7Z"/></svg>';
  const HEX = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 3l7 4v8l-7 4-7-4V7z"/></svg>';
  function vendorBadge(session) {
    const s = (session || "").toLowerCase();
    if (s.includes("claude")) return '<span class="spark-claude" aria-hidden="true">·</span>';
    if (s.includes("codex") || s.includes("openai") || s.includes("gpt"))
      return '<span class="vmark openai">' + HEX + "</span>";
    return '<span class="vmark">' + SPARK + "</span>";
  }

  // Claude 시그니처 — 코랄 색 글리프가 번갈아 깜빡이며 살아있는 느낌
  const CLAUDE_GLYPHS = ["·", "∗", "⁕", "✶", "❋", "❊", "❋", "✶", "⁕", "∗"];
  let _cg = 0;
  setInterval(() => {
    const e = document.querySelector(".spark-claude");
    if (e) e.textContent = CLAUDE_GLYPHS[_cg++ % CLAUDE_GLYPHS.length];
  }, 160);

  function buildSession(session, ports, onRelease) {
    const card = el("div", "sess-card");
    const head = el("div", "sess-head");
    if (!session) {                       // 백엔드 session 미제공(degraded) — 미점유로 단정하지 않는다
      card.classList.add("free");
      head.innerHTML = '<span class="vmark free">' + HEX + "</span>";
      head.appendChild(txt("span", "세션 대기 중", "sess-name"));
      const btn = el("button", "btn release");
      btn.textContent = "해제";
      btn.disabled = true;
      btn.title = "MCP clientInfo 캡처 전 — 비활성";
      head.appendChild(btn);
      card.appendChild(head);
      return card;
    }
    card.classList.add("owned");
    head.innerHTML = vendorBadge(session);
    head.appendChild(txt("span", session, "sess-name"));
    const btn = el("button", "btn release");
    btn.textContent = "해제";
    btn.title = "이 AI 세션의 전체 소유권 해제 — 사람·TeraTerm가 쓸 수 있게";
    btn.onclick = (e) => { e.stopPropagation(); onRelease(ports.map(p => p.port), session); };
    head.appendChild(btn);
    card.appendChild(head);
    return card;
  }

  // 좌측 통합 사이드바: [AI 세션 + 해제] 카드 위, 그 아래 SSM 그룹별 토폴로지 그래프.
  // app.js 가 /api/topology 로스터로 호출: renderTopology(roster, session, active, onSelect, onRelease).
  window.renderTopology = function (roster, session, active, onSelect, onRelease) {
    roster = roster || { groups: [], unplaced: [] };
    const root = document.getElementById("portboard");
    const s = JSON.stringify(roster) + "|" + (session || "") + "|" + (active || "");
    if (s === lastSig) return;
    lastSig = s;
    root.innerHTML = "";
    root.appendChild(buildSession(session, allPorts(roster), onRelease));
    const groups = roster.groups || [], unplaced = roster.unplaced || [];
    const wrap = el("div", "topo");
    if (unplaced.length) wrap.appendChild(renderUnclassified(unplaced, active, onSelect));   // 있을 때만
    if (!groups.length) {
      wrap.appendChild(txt("div", "감지된 그룹 없음", "sess-meta"));
    } else {
      groups.forEach((g, i) => wrap.appendChild(renderGroup(g, i, active, onSelect)));
    }
    root.appendChild(wrap);
  };

  window.resetPortBoardSig = function () { lastSig = ""; };
})();

/* app.js — serial-mcp 로그 뷰어 클라이언트.
   조회: /api/stream(SSE) · /api/buffer · /api/status · /api/ports. 소유권 제어 예외: /api/release.
   렌더는 SViewer(순수 파이프라인) + 얇은 DOM glue. 설정은 localStorage 영속. */
"use strict";

const $ = id => document.getElementById(id);
const SV = window.SViewer;
const MAX_STREAM = 5000;
const GAP_SEC = 2;

const state = {
  paused: false,
  follow: true,
  tab: "stream",
  port: null,
  ports: [],
  session: null,
  multiSource: false,
  topology: { groups: [], unplaced: [] },   // /api/topology 로스터 캐시(그래프 렌더원)
  streamLines: 0,
  streamItems: [],         // 재렌더용 원본 보관(설정 변경 시 다시 그림)
  newCount: 0,
  query: "",
  matcher: null,
  levels: { err: true, warn: true, boot: true },
  matchEls: [],
  matchIdx: -1,
  // 표시 설정(아래 loadSettings 가 localStorage 에서 채움; 신규 키는 기본값 fallback = migration)
  intensity: "normal", rhythm: "normal", json: "compact",
  fold: true, foldmode: "norm", ansi: true, semantic: true, gap: GAP_SEC, focus: false,
};

function currentView() {
  return { intensity: state.intensity, json: state.json, fold: state.fold,
           foldmode: state.foldmode, ansi: state.ansi, semantic: state.semantic,
           gap: state.gap, focus: state.focus };
}

/* ============================ 라인 노드 빌드 ============================ */
const CAT_CLASS = { error: "err", warning: "warn", boot: "boot", noise: "noise", success: "ok" };

// correlation 추적 칩 — 본문 복제가 아니라 행동 도구. 클릭 시 같은 값 줄로 검색(아래 클릭 위임 참고).
function corrChip(key, val) {
  const v = String(val);
  return '<span class="badge corr" data-corr="' + SV.escapeHtml(v) +
    '" title="클릭 — 같은 ' + SV.escapeHtml(String(key)) + ' 줄만 검색">' + SV.escapeHtml(key + " " + v) + "</span>";
}

// entry: {ts, text, count, firstTs, lastTs}. → {node, model, cls, sig}
function buildLineNode(entry) {
  const view = currentView();
  const model = SV.parseLine(entry.text, {
    ts: entry.ts, source: entry.source || state.port, count: entry.count || 1,
    firstTs: entry.firstTs || entry.ts, lastTs: entry.lastTs || entry.ts,
  });
  const div = document.createElement("div");
  div.dataset.raw = model.visible;
  div.dataset.cat = "";
  if (model.blank) {
    div.className = "ln blank";
    div.innerHTML = '<span class="ts">' + SV.escapeHtml((model.ts || "").slice(0, 8)) + '</span><span class="txt"></span>';
    return { node: div, model: model, cls: { primary: "", badges: [], payload: null }, sig: null };
  }
  const cls = SV.classifyLine(model);
  const primary = cls.primary;                        // 분류는 항상 — 레벨칩·필터·Focus 는 색 설정과 독립(색 꺼도 동작)
  const showColor = view.semantic && view.intensity !== "off";
  div.className = "ln" + (showColor && CAT_CLASS[primary] ? " " + CAT_CLASS[primary] : "");  // 시각 색만 설정에 종속
  div.dataset.cat = primary;                          // 필터·카운트의 단일 근거(항상 유효)

  let body = SV.renderBodyHTML(model, cls, view);

  // 보조 badge: 본문에 이미 보이는 정보(JSON·net·time·success·source)는 칩으로 복제하지 않는다.
  // correlation(추적 ID)만 남기되 — 클릭하면 같은 값 줄로 검색되는 '추적 도구'로 노출한다.
  let badges = "";
  if (view.intensity !== "off" && view.semantic && cls.payload) {
    const corr = SV.correlationBadges(cls.payload.value);
    for (const c of corr) badges += corrChip(c.key, c.val);
  }

  // 반복 배지(서버 count 또는 클라 접기). 클릭 시 variants 펼침.
  let rep = "";
  if ((entry.count || 1) > 1) {
    div.dataset.repeat = entry.count;
    div.dataset.firstts = model.firstTs; div.dataset.lastts = model.lastTs;
    rep = '<span class="rep-badge" title="' + SV.escapeHtml(model.firstTs + " ~ " + model.lastTs) + '">×' + entry.count + "</span>";
  }
  div.innerHTML =
    '<span class="ts" title="' + SV.escapeHtml(model.ts) + '">' + SV.escapeHtml((model.ts || "").slice(0, 8)) + "</span>" +
    '<span class="txt">' + body + badges + rep + "</span>";
  const sig = view.fold ? SV.normalizeForRepeat(model, view.foldmode) : null;
  return { node: div, model: model, cls: cls, sig: sig };
}

// 접기: 기존 노드에 ×N 증가 + variant 저장(펼치기용).
function foldInto(node, model) {
  const n = (+node.dataset.repeat || 1) + 1;
  node.dataset.repeat = n;
  node.dataset.lastts = model.lastTs;
  if (!node.dataset.firstts) node.dataset.firstts = model.firstTs;
  const txt = node.querySelector(".txt");
  let badge = node.querySelector(".rep-badge");
  if (!badge) { badge = document.createElement("span"); badge.className = "rep-badge"; txt.appendChild(badge); }
  badge.textContent = "×" + n;
  badge.title = (node.dataset.firstts || "") + " ~ " + model.lastTs;
  const store = node._variants || (node._variants = []);
  if (store.length < 50) store.push({ ts: model.lastTs, text: model.visible });
}

function gapDivider(sec) {
  const g = document.createElement("div");
  g.className = "gap";
  sec = Math.round(sec);
  g.textContent = sec >= 60 ? "+" + Math.floor(sec / 60) + "m " + (sec % 60) + "s 정적" : "+" + sec + "s 정적";
  return g;
}
function markerDivider(label) { const g = document.createElement("div"); g.className = "gap marker"; g.textContent = label; return g; }
function sectionDivider(text) {
  const g = document.createElement("div");
  g.className = "gap section";
  g.textContent = "▸ " + (text.length > 52 ? text.slice(0, 52) + "…" : text);
  return g;
}

/* 한 entry 를 box 에 추가. ctx 는 이전 줄 상태(fold·divider 판정용)를 들고 다닌다. */
function appendEntry(box, entry, ctx, opts) {
  const view = currentView();
  const built = buildLineNode(entry);
  const model = built.model;
  const ms = SV.tsToMs(model.firstTs);

  if (!model.blank) {
    // gap divider
    if (view.gap > 0 && ctx.prevMs != null && ms != null && ms - ctx.prevMs >= view.gap * 1000) {
      const g = gapDivider((ms - ctx.prevMs) / 1000);
      if (state.matcher) g.style.display = "none";
      box.appendChild(g); ctx.prevSig = null; ctx.prevNode = null;
    }
    // section divider (boot 전환; score 기반, 단일 문자열 아님)
    if (view.semantic && built.cls.primary === "boot" && built.cls.confidence >= 0.5 && ctx.prevNode) {
      const sd = sectionDivider(model.visible);
      if (state.matcher) sd.style.display = "none";
      box.appendChild(sd); ctx.prevSig = null; ctx.prevNode = null;
    }
    // fold (연속 같은 signature) — 버퍼(서버 가공 완료분)에서는 클라 재접기 금지(AI parity·이중 접기 방지)
    if (!(opts && opts.noFold) && view.fold && built.sig != null && built.sig === ctx.prevSig && ctx.prevNode) {
      foldInto(ctx.prevNode, model);
      if (ms != null) ctx.prevMs = SV.tsToMs(model.lastTs) || ms;
      return null;
    }
  }
  box.appendChild(built.node);
  applyVisibility(built.node);
  ctx.prevSig = built.sig; ctx.prevNode = model.blank ? ctx.prevNode : built.node;
  if (ms != null) ctx.prevMs = ms;
  return built.node;
}

/* ============================ 검색 + 필터 ============================ */
function escapeRegExp(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); }
function compileMatcher() {
  const q = state.query;
  if (!q) { state.matcher = null; $("searchWrap").classList.remove("has-q"); return; }
  $("searchWrap").classList.add("has-q");
  try { state.matcher = new RegExp(escapeRegExp(q), "gi"); } catch (e) { state.matcher = null; }
}

function highlightLine(div) {
  const txt = div.querySelector(".txt");
  if (!txt) return;
  txt.querySelectorAll("mark").forEach(m => { const t = document.createTextNode(m.textContent); m.replaceWith(t); });
  txt.normalize();
  if (!state.matcher || div.classList.contains("hide")) return;
  const re = state.matcher;
  const walker = document.createTreeWalker(txt, NodeFilter.SHOW_TEXT);
  const targets = []; let n;
  while ((n = walker.nextNode())) targets.push(n);
  for (const node of targets) {
    const s = node.nodeValue; re.lastIndex = 0;
    if (!re.test(s)) continue;
    re.lastIndex = 0;
    const frag = document.createDocumentFragment();
    let last = 0, m;
    while ((m = re.exec(s))) {
      if (m.index > last) frag.appendChild(document.createTextNode(s.slice(last, m.index)));
      const mk = document.createElement("mark"); mk.textContent = m[0]; frag.appendChild(mk);
      last = m.index + m[0].length;
      if (m[0].length === 0) re.lastIndex++;
    }
    if (last < s.length) frag.appendChild(document.createTextNode(s.slice(last)));
    node.replaceWith(frag);
  }
}

function lineVisible(div) {
  const cat = div.dataset.cat;
  if (cat === "error" && !state.levels.err) return false;
  if (cat === "warning" && !state.levels.warn) return false;
  if (cat === "boot" && !state.levels.boot) return false;
  if (state.focus && !(cat === "error" || cat === "warning" || cat === "success")) return false;
  if (state.matcher) { state.matcher.lastIndex = 0; if (!state.matcher.test(div.dataset.raw || "")) return false; }
  return true;
}
function applyVisibility(div) {
  if (!div.classList || !div.classList.contains("ln")) return;
  div.classList.toggle("hide", !lineVisible(div));
  highlightLine(div);
}
function applyAll() {
  for (const pane of [$("stream"), $("buffer")]) {
    for (const el of pane.children) {
      if (el.classList.contains("gap")) { el.style.display = state.matcher ? "none" : ""; continue; }
      applyVisibility(el);
    }
  }
  scheduleRecount();
}

let recountQueued = false;
function scheduleRecount() {
  if (recountQueued) return;
  recountQueued = true;
  requestAnimationFrame(() => { recountQueued = false; recount(); });
}
function recount() {
  const pane = $(state.tab);
  let nErr = 0, nWarn = 0, nBoot = 0, visible = 0;
  for (const el of pane.children) {
    if (!el.classList.contains("ln")) continue;
    const cat = el.dataset.cat;
    if (cat === "error") nErr++; else if (cat === "warning") nWarn++; else if (cat === "boot") nBoot++;
    if (!el.classList.contains("hide")) visible++;
  }
  $("nErr").textContent = nErr; $("nWarn").textContent = nWarn; $("nBoot").textContent = nBoot;
  state.matchEls = state.matcher ? [...pane.querySelectorAll(".ln:not(.hide) mark")] : [];
  if (state.matcher) {
    const tot = state.matchEls.length;
    $("matchn").textContent = tot ? (Math.min(state.matchIdx + 1, tot) || 1) + "/" + tot : "0";
    $("matchn").classList.toggle("none", tot === 0);
    if (state.matchIdx >= tot) state.matchIdx = tot - 1;
  }
  const empty = visible === 0;
  $("empty").classList.toggle("show", empty);
  if (empty) {
    const filtered = state.matcher || hasLevelFilter() || state.focus;
    $("empty").querySelector(".big").textContent = filtered ? "표시할 줄 없음" : "로그 없음";
    $("emptyHint").textContent = filtered ? "검색어·레벨·Focus 를 조정해 보세요"
      : (state.tab === "stream" ? "실시간 수신 대기 중…" : "버퍼가 비어 있습니다");
  }
}
function hasLevelFilter() { return !state.levels.err || !state.levels.warn || !state.levels.boot; }

function setSearch(v) {
  state.query = v.trim();
  state.matchIdx = -1;
  compileMatcher();
  applyAll();
  if (state.matchEls.length) setCurrentMatch(0);
}
function setCurrentMatch(i) {
  state.matchEls.forEach(m => m.classList.remove("cur"));
  if (!state.matchEls.length) return;
  state.matchIdx = (i + state.matchEls.length) % state.matchEls.length;
  const el = state.matchEls[state.matchIdx];
  el.classList.add("cur");
  const r = el.getBoundingClientRect();
  window.scrollTo({ top: window.scrollY + r.top - window.innerHeight / 2, behavior: "smooth" });
  $("matchn").textContent = (state.matchIdx + 1) + "/" + state.matchEls.length;
}
function nextMatch(dir) {
  if (!state.matcher) return;
  recount();
  if (!state.matchEls.length) return;
  setCurrentMatch(state.matchIdx + (dir || 1));
}

/* ============================ 스트림 (SSE) ============================ */
let es = null;
let streamCtx = {};
function connectStream(port) {
  if (es) es.close();
  state.port = port;
  state.streamItems = [];
  $("stream").innerHTML = ""; $("buffer").innerHTML = "";
  state.streamLines = 0; state.newCount = 0;
  streamCtx = {};
  $("newpill").classList.remove("show");
  updateStreamCount();
  es = new EventSource("/api/stream?port=" + encodeURIComponent(port));
  es.onopen = () => $("stream").appendChild(markerDivider("실시간 수신 시작 — 이전 기록은 [버퍼] 탭"));
  es.onmessage = ev => {
    if (state.paused) return;
    let d; try { d = JSON.parse(ev.data); } catch (e) { return; }
    state.streamItems.push(d);
    const node = appendEntry($("stream"), d, streamCtx);
    if (node) state.streamLines++;
    // 상한 초과 시 위에서부터 제거(원본 보관 배열도 함께 정리)
    const box = $("stream");
    while (box.childNodes.length > MAX_STREAM) {
      const removed = box.firstChild;
      if (removed.classList && removed.classList.contains("ln")) state.streamLines--;
      box.removeChild(removed);
    }
    if (state.streamItems.length > MAX_STREAM) state.streamItems.splice(0, state.streamItems.length - MAX_STREAM);
    updateStreamCount();
    scheduleRecount();
    if (state.follow && state.tab === "stream") window.scrollTo(0, document.body.scrollHeight);
    else if (state.tab === "stream" && node && !nearBottom() && !node.classList.contains("hide")) {
      state.newCount++;
      $("newpillText").textContent = "새 로그 " + state.newCount + "건";
      $("newpill").classList.add("show");
    }
  };
}
// 설정 변경 시 스트림을 보관된 원본에서 다시 그린다(접기·데코·간격선 재적용).
function renderStreamAll() {
  const box = $("stream");
  box.innerHTML = "";
  streamCtx = {};
  state.streamLines = 0;
  box.appendChild(markerDivider("실시간 수신 — 이전 기록은 [버퍼] 탭"));
  for (const d of state.streamItems) { if (appendEntry(box, d, streamCtx)) state.streamLines++; }
  updateStreamCount();
  scheduleRecount();
  if (state.follow && state.tab === "stream") window.scrollTo(0, document.body.scrollHeight);
}
function updateStreamCount() { $("cStream").textContent = state.streamLines + "/" + MAX_STREAM; }
function nearBottom() { return window.innerHeight + window.scrollY >= document.body.scrollHeight - 60; }

/* ============================ 버퍼 (폴링) ============================ */
async function refreshBuffer() {
  if (state.tab !== "buffer" || state.paused || !state.port) return;
  let d;
  try { d = await (await fetch("/api/buffer?port=" + encodeURIComponent(state.port))).json(); }
  catch (e) { return; }
  const box = $("buffer");
  box.innerHTML = "";
  const ctx = {};
  for (const e of d.entries || []) {
    appendEntry(box, { ts: e.first_ts, text: e.text, count: e.count, firstTs: e.first_ts, lastTs: e.last_ts }, ctx, { noFold: true });
  }
  $("cBuffer").textContent = (d.entries || []).length + "/" + (d.capacity != null ? d.capacity : "?");
  scheduleRecount();
  if (state.follow && state.tab === "buffer") window.scrollTo(0, document.body.scrollHeight);
}
setInterval(refreshBuffer, 2000);

/* ============================ 상태 폴링 ============================ */
function pickDefaultPort(ports) {
  const saved = localStorage.getItem("sv_port");
  if (saved && ports.some(p => p.port === saved)) return saved;
  const withData = ports.find(p => (p.buffer_entries || 0) > 0);
  if (withData) return withData.port;
  const conn = ports.find(p => p.connected);
  if (conn) return conn.port;
  return ports[0].port;
}
async function refreshStatus() {
  let d;
  try { d = await (await fetch("/api/status")).json(); }
  catch (e) { return; }
  const ports = d.ports || [];
  state.ports = ports;
  state.multiSource = ports.length > 1;
  state.session = d.session || null;
  if (!state.port && ports.length) connectStream(pickDefaultPort(ports));
  renderTopologyNow();   // 세션·active 갱신 반영(로스터는 캐시 사용)
  const p = ports.find(x => x.port === state.port) || ports[0];
  if (p) $("cBuffer").textContent = (p.buffer_entries != null ? p.buffer_entries : 0) + "/" + (p.buffer_capacity != null ? p.buffer_capacity : "?");
}
setInterval(refreshStatus, 5000);

/* 토폴로지 그래프 — 로스터(/api/topology)는 천천히 변하므로 별도 폴링, 렌더는 캐시+현재 active. */
function renderTopologyNow() {
  if (window.renderTopology)
    renderTopology(state.topology, state.session, state.port, selectPort, releaseSession);
}
async function refreshTopology() {
  let d;
  try { d = await (await fetch("/api/topology")).json(); }
  catch (e) { return; }
  state.topology = d || { groups: [], unplaced: [] };
  renderTopologyNow();
}
setInterval(refreshTopology, 5000);

function selectPort(port) {
  if (port !== state.port) connectStream(port);
  localStorage.setItem("sv_port", port);
  resetPortBoardSig();
  renderTopologyNow();     // 즉시 active 하이라이트(폴링 대기 없이)
  refreshStatus();
  refreshBuffer();
}
async function releaseSession(ports, session) {
  const label = ports.length ? ports.join(", ") : "전체 세션";
  if (!confirm((session || "이 세션") + "\n이 AI 세션의 소유권을 해제할까요?\n(" + label + ")")) return;
  try { await fetch("/api/release"); } catch (e) {}
  resetPortBoardSig();
  refreshStatus();
}

/* ============================ 탭 / 컨트롤 ============================ */
function setTab(name) {
  state.tab = name;
  $("stream").style.display = name === "stream" ? "" : "none";
  $("buffer").style.display = name === "buffer" ? "" : "none";
  $("tabStream").classList.toggle("active", name === "stream");
  $("tabBuffer").classList.toggle("active", name === "buffer");
  if (name === "buffer") refreshBuffer();
  state.matchIdx = -1;
  applyAll();
  if (state.follow) window.scrollTo(0, document.body.scrollHeight);
}
$("tabStream").onclick = () => setTab("stream");
$("tabBuffer").onclick = () => setTab("buffer");

function setFollow(v) {
  state.follow = v;
  $("follow").classList.toggle("on", v);
  if (v) { window.scrollTo(0, document.body.scrollHeight); state.newCount = 0; $("newpill").classList.remove("show"); }
}
$("follow").onclick = () => setFollow(!state.follow);

$("clear").onclick = () => {
  $(state.tab).innerHTML = "";
  if (state.tab === "stream") { state.streamItems = []; streamCtx = {}; state.streamLines = 0; updateStreamCount(); }
  recount();
};

/* level chips */
function bindLevel(btn, key) {
  $(btn).onclick = () => {
    state.levels[key] = !state.levels[key];
    $(btn).classList.toggle("off", !state.levels[key]);
    applyAll();
  };
}
bindLevel("lvErr", "err"); bindLevel("lvWarn", "warn"); bindLevel("lvBoot", "boot");

/* search wiring */
$("search").addEventListener("input", e => setSearch(e.target.value));
$("search").addEventListener("keydown", e => {
  if (e.key === "Enter") { e.preventDefault(); nextMatch(e.shiftKey ? -1 : 1); }
  else if (e.key === "Escape") { e.target.value = ""; setSearch(""); e.target.blur(); }
});
$("prev").onclick = () => nextMatch(-1);
$("next").onclick = () => nextMatch(1);

/* 위임 클릭: 태그 → 필터, repeat 배지 → variants 펼침, payload fold 토글 */
document.addEventListener("click", ev => {
  const pf = ev.target.closest(".pfold");
  if (pf) { const fold = pf.closest(".fold"); if (fold) fold.classList.toggle("open"); ev.preventDefault(); return; }
  const rb = ev.target.closest(".rep-badge");
  if (rb) { toggleVariants(rb.closest(".ln")); return; }
  const t = ev.target.closest(".tag");
  if (t) {
    const lit = "[" + t.dataset.tag + "]";
    const box = $("search");
    box.value = box.value.trim() === lit ? "" : lit;
    setSearch(box.value);
    return;
  }
  const cc = ev.target.closest(".badge.corr");   // correlation 칩 클릭 → 같은 값 줄 추적
  if (cc) {
    const v = cc.dataset.corr;
    const box = $("search");
    box.value = box.value.trim() === v ? "" : v;
    setSearch(box.value);
  }
}, true);

function toggleVariants(ln) {
  if (!ln) return;
  let v = ln.querySelector(".variants");
  if (v) { v.classList.toggle("open"); return; }
  const store = ln._variants || [];
  if (!store.length) return;
  v = document.createElement("div");
  v.className = "variants open";
  for (const it of store) {
    const row = document.createElement("div");
    row.className = "v";
    row.innerHTML = '<span class="vts">' + SV.escapeHtml((it.ts || "").slice(0, 12)) + "</span>" + SV.escapeHtml(it.text);
    v.appendChild(row);
  }
  ln.querySelector(".txt").appendChild(v);
}

$("newpill").onclick = () => { setTab("stream"); setFollow(true); };
window.addEventListener("scroll", () => {
  if (nearBottom()) { state.newCount = 0; $("newpill").classList.remove("show"); }
});

/* ============================ 설정 popover ============================ */
const gear = $("gear"), pop = $("pop");
function positionPop() {
  const r = gear.getBoundingClientRect();
  pop.style.top = (r.bottom + 6) + "px";
  pop.style.left = Math.max(8, Math.min(r.right - pop.offsetWidth, window.innerWidth - pop.offsetWidth - 8)) + "px";
}
gear.onclick = e => { e.stopPropagation(); pop.classList.toggle("open"); if (pop.classList.contains("open")) positionPop(); };
document.addEventListener("click", e => { if (!pop.contains(e.target) && e.target !== gear && !gear.contains(e.target)) pop.classList.remove("open"); });

/* 글자 크기 (기존 키 sv_fs 유지) */
let fs = +(localStorage.getItem("sv_fs") || 13);
function applyFs() {
  fs = Math.min(18, Math.max(11, fs));
  document.documentElement.style.setProperty("--fs", fs + "px");
  localStorage.setItem("sv_fs", fs);
}
$("fsDown").onclick = () => { fs--; applyFs(); };
$("fsUp").onclick = () => { fs++; applyFs(); };
applyFs();

/* body 클래스(색 강도·리듬·줄바꿈·타임스탬프·focus) 반영 */
function applyViewClasses() {
  const b = document.body;
  b.classList.remove("int-off", "int-min", "int-normal", "int-vivid");
  b.classList.add("int-" + state.intensity);
  b.classList.remove("rhythm-dense", "rhythm-normal", "rhythm-relaxed");
  b.classList.add("rhythm-" + state.rhythm);
  b.classList.toggle("nowrap", !state.wrap);
  b.classList.toggle("no-ts", !state.ts);
  b.classList.toggle("focus", state.focus);
}
// 렌더에 영향을 주는 설정이 바뀌면 보관 원본에서 다시 그린다.
function rerenderAll() { renderStreamAll(); if (state.tab === "buffer") refreshBuffer(); }

/* localStorage 로드(기존 키 보존 + 신규 키 기본값 fallback = migration) */
function lsGet(key, def) { const v = localStorage.getItem(key); return v === null ? def : v; }
function lsBool(key, def) { const v = localStorage.getItem(key); return v === null ? def : v === "1"; }
function loadSettings() {
  state.intensity = lsGet("sv_intensity", "normal");
  state.rhythm = lsGet("sv_rhythm", "normal");
  state.json = lsGet("sv_json", "compact");
  state.gap = +lsGet("sv_gap", String(GAP_SEC));
  state.foldmode = lsGet("sv_foldmode", "norm");
  state.fold = lsBool("sv_fold", true);
  state.ansi = lsBool("sv_ansi", true);
  state.semantic = lsBool("sv_semantic", true);
  state.focus = lsBool("sv_focus", false);
  state.ts = lsBool("sv_ts", true);
  state.wrap = lsBool("sv_wrap", true);
}

/* 세그먼트(택1) 위젯 일반 배선 */
function wireSeg(segId, key, getter, setter, onChange) {
  const seg = $(segId);
  function paint() { const cur = String(getter()); for (const b of seg.children) b.classList.toggle("on", b.dataset.v === cur); }
  for (const b of seg.children) {
    b.onclick = () => {
      const v = b.dataset.v;
      setter(v); localStorage.setItem(key, v);
      paint(); applyViewClasses(); if (onChange) onChange();
    };
  }
  paint();
}
/* 토글 위젯 일반 배선 */
function wireToggle(rowId, key, getter, setter, onChange) {
  const row = $(rowId);
  function paint() { row.classList.toggle("on", !!getter()); }
  row.onclick = () => {
    const v = !getter();
    setter(v); localStorage.setItem(key, v ? "1" : "0");
    paint(); applyViewClasses(); if (onChange) onChange();
  };
  paint();
}

loadSettings();
applyViewClasses();

wireSeg("segIntensity", "sv_intensity", () => state.intensity, v => state.intensity = v, rerenderAll);
wireSeg("segRhythm", "sv_rhythm", () => state.rhythm, v => state.rhythm = v, null);
wireSeg("segJson", "sv_json", () => state.json, v => state.json = v, rerenderAll);
wireSeg("segGap", "sv_gap", () => state.gap, v => state.gap = +v, rerenderAll);
wireSeg("segFoldmode", "sv_foldmode", () => state.foldmode, v => state.foldmode = v, rerenderAll);
wireToggle("tgTs", "sv_ts", () => state.ts, v => state.ts = v, null);
wireToggle("tgWrap", "sv_wrap", () => state.wrap, v => state.wrap = v, null);
wireToggle("tgFold", "sv_fold", () => state.fold, v => state.fold = v, rerenderAll);
wireToggle("tgAnsi", "sv_ansi", () => state.ansi, v => state.ansi = v, rerenderAll);
wireToggle("tgSemantic", "sv_semantic", () => state.semantic, v => state.semantic = v, rerenderAll);
wireToggle("tgFocus", "sv_focus", () => state.focus, v => state.focus = v, () => applyAll());

/* 단축키 도움말 */
$("tgHelp").onclick = () => $("help").classList.toggle("open");

$("help").onclick = e => { if (e.target === $("help")) $("help").classList.remove("open"); };

document.addEventListener("keydown", e => {
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
  if (e.key === "/" && !typing) { e.preventDefault(); $("search").focus(); return; }
  if (e.key === "?") { $("help").classList.toggle("open"); return; }
  if (e.key === "Escape") { $("help").classList.remove("open"); pop.classList.remove("open"); return; }
  if (typing) return;
  switch (e.key.toLowerCase()) {
    case "f": setFollow(!state.follow); break;
    case "1": setTab("stream"); break;
    case "2": setTab("buffer"); break;
    case "g": window.scrollTo(0, document.body.scrollHeight); break;
    case "n": nextMatch(e.shiftKey ? -1 : 1); break;
  }
});

/* ============================ 부팅 ============================ */
async function init() {
  await refreshStatus();
  await refreshTopology();
  recount();
}
init();
</script>
</body>
</html>
"""
