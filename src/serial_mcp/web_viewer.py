"""웹 로그 뷰어 — localhost 전용 HTTP 서버(stdlib http.server, 데몬 스레드).

사람이 브라우저로 시리얼 로그를 보는 보조 기능. 설계: docs/superpowers/specs/
2026-06-10-web-log-viewer-design.md. MCP 서버 본체와 독립 — 기동 실패해도
본체에 영향을 주지 않는다(url이 None으로 남을 뿐).

- 라우트는 전부 GET 읽기 전용. 서버 상태를 바꾸는 엔드포인트는 없다.
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
    allow_reuse_address = False  # Windows에서 점유 포트 중복 바인딩 방지(점유 감지가 정확해야 폴백이 동작)

    ports_info: Callable[[], list]
    feed_for: Callable[[str], Optional[RawFeed]]
    buffer_info: Callable[[str], dict]
    status_info: Callable[[], dict]


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
        else:
            self.send_error(404)

    def _send_json(self, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
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
                ts, text = item
                data = json.dumps({"ts": _fmt_ts(ts), "text": text}, ensure_ascii=False)
                self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionError, OSError):
            pass   # 클라이언트 끊김 — 정상 종료
        finally:
            feed.unsubscribe(sub)


class ViewerServer:
    """뷰어 HTTP 서버 래퍼 — 기동/포트 폴백/URL 보고. 예외를 밖으로 내지 않는다."""

    def __init__(
        self,
        ports_info: Callable[[], list],
        feed_for: Callable[[str], Optional[RawFeed]],
        buffer_info: Callable[[str], dict],
        status_info: Callable[[], dict],
        port: int = 8743,
    ) -> None:
        self._ports_info = ports_info
        self._feed_for = feed_for
        self._buffer_info = buffer_info
        self._status_info = status_info
        self._preferred_port = port
        self._httpd: Optional[_ViewerHTTPServer] = None
        self.url: Optional[str] = None   # 기동 성공 시 http://127.0.0.1:{port}, 실패 시 None

    def start(self) -> None:
        for port in (self._preferred_port, 0):   # 선호 포트 점유/이상 시 임시 포트로 폴백
            try:
                self._httpd = _ViewerHTTPServer(("127.0.0.1", port), _Handler)
                break
            except (OSError, OverflowError) as e:   # OverflowError: 0~65535 범위 밖 포트
                _log(f"웹 뷰어 포트 {port} 바인딩 실패: {e}")
        if self._httpd is None:
            _log("웹 뷰어 비활성 — 포트 바인딩 전부 실패")
            return
        self._httpd.ports_info = self._ports_info
        self._httpd.feed_for = self._feed_for
        self._httpd.buffer_info = self._buffer_info
        self._httpd.status_info = self._status_info
        self.url = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        threading.Thread(
            target=self._httpd.serve_forever, name="serial-web", daemon=True
        ).start()

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()


# ---- 단일 페이지(인라인 CSS/JS, 외부 CDN 없음 → 오프라인 동작) ----
# Claude Design 핸드오프(claude.ai/design) 기반 — app.css + Serial Viewer.html 마크업 +
# board.js + app.js 를 단일 인라인으로 통합. mock.js·React tweaks 패널 제외, tweaks 기본값 baked-in.
# 좌측 소유권 보드(세션/hw/board/release)는 백엔드 미지원분 degraded — TODO(codex) 표시.
# 컬러 원칙: "색은 장식이 아니라 신호" — 평상시 회색 2~3톤, 이상 상황만 채도.
# 우선순위: ANSI 해석 > 레벨 라인 틴트 > 성공 키워드 > JSON 절제 > 메타 dim.
_HTML = r"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>serial-mcp 로그 뷰어</title>
<style>
/* app.css — serial-mcp 로그 뷰어. 다크 터미널, 외부 의존성 0.
   색은 신호다: 평상시 회색 2~3톤, ANSI/레벨/검색에만 채도. */

:root {
  --fs: 13px;          /* 로그 폰트 크기 (A−/A+ 로 11~18) */
  --row-pad: 2px;      /* 줄 세로 여백 (리듬 tweak) */
  --lh: 1.55;          /* 줄 간격 (리듬 tweak) */

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
  flex: 0 0 256px; box-sizing: border-box;
  position: sticky; top: 0; align-self: flex-start; height: 100vh; overflow-y: auto;
  background: var(--bg-raised); border-right: 1px solid var(--border);
  display: flex; flex-direction: column; gap: 11px; padding: 10px;
  font-family: var(--ui); transition: flex-basis .16s ease;
}
#content { flex: 1 1 auto; min-width: 0; }
.nav-toggle {
  align-self: flex-end; appearance: none; cursor: pointer; width: 26px; height: 26px; flex: none;
  border: 1px solid var(--border-2); border-radius: 6px; background: var(--bg); color: var(--muted);
  display: grid; place-items: center;
}
.nav-toggle:hover { color: var(--fg); border-color: #3a4350; }
.nav-toggle svg { width: 15px; height: 15px; transition: transform .16s; }
body.nav-collapsed #nav { flex: 0 0 90px !important; width: 90px; min-width: 0; padding: 10px 5px; overflow: hidden; }
body.nav-collapsed .nav-toggle { align-self: center; }
body.nav-collapsed .nav-toggle svg { transform: rotate(180deg); }
/* 접으면 간소화: 세션이름·COM 숨김, 라벨·행 가운데 정렬 (❛[해제]·점·보드명은 유지) */
body.nav-collapsed .sess-name { display: none; }
body.nav-collapsed .free-tag { display: none; }
body.nav-collapsed .sess-head { justify-content: center; gap: 6px; }
body.nav-collapsed .btn.release { padding: 4px 7px; }
body.nav-collapsed .pb-com { display: none; }
body.nav-collapsed .hwbox { padding: 2px; }
body.nav-collapsed .hwbox-label { padding: 3px 2px 4px; }
body.nav-collapsed .prow { grid-template-columns: auto auto; justify-content: center; gap: 6px; padding: 5px 3px; }

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
.search .mode {
  appearance: none; border: 0; background: var(--border); color: var(--muted);
  font: 10px/1 var(--ui); font-weight: 700; letter-spacing: .04em;
  border-radius: 4px; padding: 4px 6px; cursor: pointer; flex: none;
}
.search .mode.on { background: var(--accent-bg); color: var(--accent); }

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
  padding: 6px; min-width: 210px; box-shadow: 0 12px 32px rgba(0,0,0,.5);
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
#stream, #buffer { /* containers */ }

.ln {
  display: grid; grid-template-columns: 9ch 1fr; column-gap: 12px;
  padding: var(--row-pad) 6px; border-radius: 4px; position: relative;
}
.ln:not(.err):not(.warn):not(.boot):hover { background: var(--bg-hover); }
.ln .ts { color: var(--faint); user-select: none; font-variant-numeric: tabular-nums; }
.ln .ts.rep { opacity: .28; }
.ln .txt { white-space: pre-wrap; word-break: break-word; overflow-wrap: anywhere; min-width: 0; }
body.nowrap .ln .txt { white-space: pre; overflow-x: hidden; text-overflow: ellipsis; }
body.no-ts .ln { grid-template-columns: 0 1fr; column-gap: 0; }
body.no-ts .ln .ts { display: none; }

.ln.err  { background: var(--err-bg);  box-shadow: inset 3px 0 0 var(--err); }
.ln.err  .txt { color: #ffb0a8; }
.ln.warn { background: var(--warn-bg); box-shadow: inset 3px 0 0 var(--warn); }
.ln.warn .txt { color: #e8bd62; }
.ln.boot { background: var(--boot-bg); box-shadow: inset 3px 0 0 var(--boot); }
.ln.boot .txt { color: #88c0ff; }
.ln.blank { padding: 0; line-height: .5em; }
.ln.blank .ts { opacity: .14; font-size: .8em; }
.ln.hide { display: none; }

/* repeat-count badge (buffer tab) */
.rep-badge {
  display: inline-block; margin-left: 8px; padding: 0 7px; border-radius: 10px;
  background: var(--border); color: var(--muted);
  font: 10.5px/1.6 var(--ui); font-weight: 600; vertical-align: 1px;
  font-variant-numeric: tabular-nums;
}
.meta { color: var(--faint); }

/* gap divider between bursts */
.gap { display: flex; align-items: center; gap: 10px; color: var(--faint);
       font: 10.5px var(--ui); padding: 5px 6px; user-select: none; }
.gap::before, .gap::after { content: ""; height: 1px; background: var(--border); flex: 1; }
.gap.marker { color: var(--accent); }
.gap.marker::before, .gap.marker::after { background: var(--accent-bg); }

/* search highlight */
mark { background: rgba(216,162,58,.32); color: inherit; border-radius: 2px;
       box-shadow: 0 0 0 1px rgba(216,162,58,.4); }
mark.cur { background: var(--warn); color: #1a1205; box-shadow: 0 0 0 1px var(--warn); }

/* decorations */
.ok   { color: var(--ok); }
.jkey { color: #7fc0e0; }
.jstr { color: #b8c98a; }
.jnum { color: #d2a8ff; }
.dim  { color: var(--faint); }
.tag  { font-weight: 600; }

/* ANSI palette */
.a30{color:#5a626d}.a31{color:#ff7b72}.a32{color:#3fb950}.a33{color:#d29922}
.a34{color:#58a6ff}.a35{color:#bc8cff}.a36{color:#39c5cf}.a37{color:#b1bac4}
.a90{color:#6e7681}.a91{color:#ffa198}.a92{color:#56d364}.a93{color:#e3b341}
.a94{color:#79c0ff}.a95{color:#d2a8ff}.a96{color:#56d4dd}.a97{color:#f0f6fc}
.ab{font-weight:bold}

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
  body.nav-collapsed #nav { flex-basis: auto; }
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

/* ===================== TWEAK 모드 (디자인 탐색용) =====================
   분위기(팔레트 온도) · 색은 신호(데코 강도) · 리듬(줄 간격). 전체 느낌을 바꾼다. */

/* ── 분위기: 팔레트 ── */
body.mood-amber {
  --bg:#0d0a05; --bg-raised:#15110a; --bg-bar:#15110a; --bg-input:#0d0a05;
  --border:#2b2312; --border-2:#3c3119;
  --fg:#dac99a; --fg-bright:#f6e8bf; --muted:#9c8c5e; --faint:#5f5430;
  --accent:#e2a73a; --accent-bg:#3c2c0e;
}
body.mood-green {
  --bg:#050a06; --bg-raised:#0a130d; --bg-bar:#0a130d; --bg-input:#050a06;
  --border:#163122; --border-2:#204833;
  --fg:#9ed8a7; --fg-bright:#d6f5d8; --muted:#5d9a6d; --faint:#2e5e40;
  --accent:#3fd07a; --accent-bg:#0c3a1e;
}

/* ── 색은 신호: 데코 강도 ──
   차분 = 회색 지배, 색은 진짜 신호(에러/경고)에만 / 선명 = 색·강조 최대 */
body.sig-calm .tag { color: var(--muted) !important; font-weight: 500; }
body.sig-calm .jkey, body.sig-calm .jstr, body.sig-calm .jnum { color: var(--muted); }
body.sig-calm .ok { color: var(--muted); }
body.sig-calm .ln.boot { background: transparent; box-shadow: inset 3px 0 0 var(--faint); }
body.sig-calm .ln.boot .txt { color: var(--muted); }

body.sig-vivid .tag { font-weight: 700; }
body.sig-vivid .ln.err  { background: rgba(240,120,111,.24); }
body.sig-vivid .ln.warn { background: rgba(216,162,58,.22); }
body.sig-vivid .ln.boot { background: rgba(90,167,240,.2); }
body.sig-vivid .ln.err .txt  { color: #ffc2bb; }
body.sig-vivid .ok { color: #7ee0a6; font-weight: 600; }

/* ── 리듬: 줄 간격 + 세로 여백을 한 번에 ── */
body.rhythm-dense   { --row-pad: 0px; --lh: 1.3; }
body.rhythm-normal  { --row-pad: 2px; --lh: 1.55; }
body.rhythm-relaxed { --row-pad: 6px; --lh: 1.9; }
</style>
</head>
<body class="mood-slate sig-standard rhythm-normal">
<!-- tweaks 기본값 baked-in (패널 미탑재). 셋 다 표준값과 동치 — 나중에 패널 재도입 시
     클래스 표면을 맞추려 고정. 팔레트 mood-amber/green, 데코 sig-calm/vivid, 리듬 rhythm-dense/relaxed. -->

<aside id="nav">
  <button id="navToggle" class="nav-toggle" title="네비게이션 접기/펼치기" aria-label="네비게이션 접기/펼치기">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m15 6-6 6 6 6"/></svg>
  </button>
  <section class="portboard" id="portboard"></section>
</aside>

<div id="content">
  <header>
  <div class="bar tools">
    <div class="tabs">
      <button id="tabStream" class="active" title="이 화면을 연 뒤 수신한 원본 줄 (테라텀 대체)">
        스트림 <span class="count" id="cStream">0/5000</span>
      </button>
      <button id="tabBuffer" title="서버가 보관 중인 가공 로그 — AI가 보는 것과 동일 (접힘·필터 적용)">
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
      <button class="chip boot" id="lvBoot" title="부팅/리셋 줄 표시/숨김">BOOT <span class="n" id="nBoot">0</span></button>
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
        <div class="div"></div>
        <div class="row toggle" id="tgTs"><span>타임스탬프</span><span class="switch"></span></div>
        <div class="row toggle" id="tgWrap"><span>줄바꿈</span><span class="switch"></span></div>
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
/* board.js — 왼쪽 네비게이션의 포트 상태.
   소유권 모델: AI가 MCP를 호출하면 그 MCP 서버(= 단일 AI 세션)가 모든 포트를 통째로 점유.
   레이아웃: [AI 세션 + 해제] 를 맨 위, 그 아래 H.W 유닛별 박스(SSM, SB, SB1 … 늘어날 수 있음).
   SB처럼 한 유닛에 여러 포트가 있으면 한 박스 안에 위아래로 묶인다.
   app.js 가 /api/status 응답으로 renderPortBoard(ports, session, active, onSelect, onRelease) 호출. */
"use strict";

(function () {
  let lastSig = "";

  function el(tag, cls) { const e = document.createElement(tag); if (cls) e.className = cls; return e; }
  function txt(tag, t, cls) { const e = el(tag, cls); e.textContent = t; return e; }

  function sig(ports, session, active) {
    return active + "|" + (session || "") + "|" + ports.map(p =>
      [p.port, p.hw, p.board, p.label, p.connected, p.baud, p.last_error].join(",")
    ).join(";");
  }

  // 보드 이름 담백하게: ESP32-S3 → ESP32, STM32F4 → STM32 (칩 패밀리만)
  function chipFamily(name) {
    const m = String(name || "").match(/^[A-Za-z]+\d+/);
    return m ? m[0] : (name || "");
  }

  // 백엔드가 hw/board 를 안 주는 동안, label(별칭)에서 유닛/칩을 역추론한다.
  //   "SB-STM (COM8)" → 유닛 "SB"·칩 "STM",  "SSM (COM4)" → 유닛 "SSM"·칩 없음,
  //   "COM8"(별칭 미부여) → null(미분류). 별칭은 서버 autoname/SERIAL_NAMES 산출.
  // TODO(codex): /api/status 가 hw/board 를 정식 제공하면 p.hw/p.board 가 우선이라 이 추론은 자동으로 덮인다.
  function aliasOf(p) {
    const m = String(p.label || "").match(/^(.+?)\s*\(/);   // 괄호 앞 = 별칭, 괄호 없으면 미부여
    return m ? m[1].trim() : null;
  }
  function unitOf(p) {
    if (p.hw) return p.hw;
    const a = aliasOf(p);
    if (!a) return null;
    const d = a.indexOf("-");
    return d >= 0 ? a.slice(0, d) : a;       // "SB-STM"→"SB", "SSM"→"SSM"
  }
  function boardOf(p) {
    if (p.board) return p.board;
    const a = aliasOf(p);
    if (!a) return null;
    const d = a.indexOf("-");
    return d >= 0 ? a.slice(d + 1) : null;   // "SB-STM"→"STM", "SSM"→null
  }

  // 포트 한 행:  ● dot   board   COM
  function buildRow(p, active, onSelect) {
    const row = el("div", "prow" + (p.port === active ? " active" : ""));
    row.title = "클릭 — 이 포트 로그 보기";
    row.appendChild(el("span", "dot " + (p.connected ? "on" : "fail")));
    row.appendChild(txt("span", chipFamily(boardOf(p) || p.port), "pb-board"));
    row.appendChild(txt("span", p.port, "pb-com"));
    row.onclick = () => onSelect(p.port);
    return row;
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
      // TODO(codex): /api/status 최상위 session 제공 시 owned(벤더 글리프+해제)/free(미점유) 분기 복원.
      //   코랄 Claude 글리프(spark-claude)는 실제 session 에 'claude'가 있을 때만 — degraded 에선 중립 placeholder.
      card.classList.add("free");
      head.innerHTML = '<span class="vmark free">' + HEX + "</span>";
      head.appendChild(txt("span", "세션 대기 중", "sess-name"));
      const btn = el("button", "btn release");
      btn.textContent = "해제";
      btn.disabled = true;                // 백엔드 소유권(session/release) 연결 전 — 비활성
      btn.title = "백엔드 소유권 연결 전 — 비활성";
      head.appendChild(btn);
      card.appendChild(head);
      return card;
    }
    card.classList.add("owned");
    head.innerHTML = vendorBadge(session);
    head.appendChild(txt("span", session, "sess-name"));
    const btn = el("button", "btn release");
    btn.textContent = "해제";
    btn.title = "이 AI 세션이 점유한 포트를 모두 해제 — 사람·TeraTerm가 쓸 수 있게";
    btn.onclick = (e) => { e.stopPropagation(); onRelease(ports.map(p => p.port), session); };
    head.appendChild(btn);
    card.appendChild(head);
    return card;
  }

  window.renderPortBoard = function (ports, session, active, onSelect, onRelease) {
    const root = document.getElementById("portboard");
    const s = sig(ports, session, active);
    if (s === lastSig) return;
    lastSig = s;
    root.innerHTML = "";
    if (!ports.length) {
      root.appendChild(txt("div", "감지된 시리얼 포트가 없습니다.", "sess-meta"));
      return;
    }
    // 맨 위: AI 세션 + 해제
    root.appendChild(buildSession(session, ports, onRelease));
    // 그 아래: H.W 유닛별 박스 (연속 같은 유닛 묶기 — p.hw 우선, 없으면 label 별칭에서 추론)
    const boxes = el("div", "hwboxes");
    const hasUnit = ports.some(p => unitOf(p));
    let i = 0;
    while (i < ports.length) {
      let j = i;
      while (j < ports.length && unitOf(ports[j]) === unitOf(ports[i])) j++;
      const box = el("div", "hwbox");
      if (hasUnit) box.appendChild(txt("div", unitOf(ports[i]) || "—", "hwbox-label"));
      for (let x = i; x < j; x++) box.appendChild(buildRow(ports[x], active, onSelect));
      boxes.appendChild(box);
      i = j;
    }
    root.appendChild(boxes);
  };

  window.resetPortBoardSig = function () { lastSig = ""; };
})();

/* app.js — serial-mcp 로그 뷰어 클라이언트.
   읽기 전용: /api/stream(SSE) · /api/buffer · /api/status · /api/ports. */
"use strict";

const $ = id => document.getElementById(id);
const MAX_STREAM = 5000;
const GAP_SEC = 2;
const ESC = String.fromCharCode(27);
const ANSI_RE = new RegExp(ESC + "\\[[0-9;]*[A-Za-z]", "g");
const stripAnsi = s => s.replace(ANSI_RE, "");

const state = {
  paused: false,
  follow: true,
  tab: "stream",
  port: null,
  ports: [],
  session: null,
  streamLines: 0,
  streamLastSec: null,
  newCount: 0,
  query: "",
  regex: false,        // 검색 정규식 모드
  matcher: null,       // 현재 컴파일된 RegExp (g)
  levels: { err: true, warn: true, boot: true },  // true = 표시
  matchEls: [],
  matchIdx: -1,
};

/* ============================ 텍스트 데코 ============================ */
function esc(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function ansiToHtml(s) {
  if (s.indexOf(ESC + "[") === -1) return null;
  let out = "", open = 0;
  for (const p of s.split(new RegExp("(" + ESC + "\\[[0-9;]*m)"))) {
    const m = p.match(new RegExp("^" + ESC + "\\[([0-9;]*)m$"));
    if (!m) { out += esc(p.replace(new RegExp(ESC + "\\[[0-9;]*[A-Za-z]", "g"), "")); continue; }
    out += "</span>".repeat(open); open = 0;
    const cls = [];
    for (const c of (m[1] === "" ? [0] : m[1].split(";").map(Number))) {
      if (c === 1) cls.push("ab");
      else if ((c >= 30 && c <= 37) || (c >= 90 && c <= 97)) cls.push("a" + c);
    }
    if (cls.length) { out += '<span class="' + cls.join(" ") + '">'; open = 1; }
  }
  return out + "</span>".repeat(open);
}

const TAG_RE = /(\[[A-Za-z][A-Za-z0-9_ .\-]{1,23}\])/;
function tagColor(name) {
  let h = 0;
  for (const ch of name) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return "hsl(" + (h % 360) + ",42%,68%)";
}
function plainDecorate(s) {
  let h = esc(s);
  h = h.replace(/"([^"]*)"(\s*:)/g, '<span class="jkey">"$1"</span>$2');   // JSON 키
  h = h.replace(/:(\s*)"([^"]*)"/g, ':$1<span class="jstr">"$2"</span>');  // 문자열 값
  h = h.replace(/\b(-?\d+\.?\d*)\b/g, '<span class="jnum">$1</span>');     // 숫자
  h = h.replace(/([{}\[\]])/g, '<span class="dim">$1</span>');
  h = h.replace(/\b(OK|Success|Done|ready|connected)\b/gi, '<span class="ok">$1</span>');
  return h;
}
function decorate(text) {
  let out = "";
  for (const p of text.split(TAG_RE)) {
    const m = p.match(/^\[([A-Za-z][A-Za-z0-9_ .\-]{1,23})\]$/);
    out += m
      ? '<span class="tag" data-tag="' + esc(m[1]) + '" style="color:' + tagColor(m[1]) + '">' + esc(p) + "</span>"
      : plainDecorate(p);
  }
  return out;
}

function lineLevel(text) {
  if (/^(ESP-ROM:|rst:0x|entry 0x|load:0x|Build:)/.test(text)) return "boot";
  if (/^E \(\d/.test(text) || /\b(error|fail|failed|exception|fatal|panic)\b/i.test(text)) return "err";
  if (/^W \(\d/.test(text) || /\bwarn(ing)?\b/i.test(text)) return "warn";
  return "";
}

function toSec(ts) { return +ts.slice(0, 2) * 3600 + +ts.slice(3, 5) * 60 + +ts.slice(6, 8); }

function gapDivider(sec) {
  const g = document.createElement("div");
  g.className = "gap";
  g.textContent = sec >= 60 ? "+" + Math.floor(sec / 60) + "m " + (sec % 60) + "s 정적" : "+" + sec + "s 정적";
  return g;
}
function markerDivider(label) {
  const g = document.createElement("div");
  g.className = "gap marker";
  g.textContent = label;
  return g;
}

function buildLine(ts, text, count, firstTs, lastTs, dimTs) {
  const div = document.createElement("div");
  div.dataset.raw = stripAnsi(text);   // 필터·검색·복사용 — 제어문자 없는 가시 텍스트
  if (text.trim() === "") {
    div.className = "ln blank";
    div.dataset.level = "";
    div.innerHTML = '<span class="ts">' + ts.slice(0, 8) + '</span><span class="txt"></span>';
    return div;
  }
  const ansi = ansiToHtml(text);
  const lvl = ansi === null ? lineLevel(text) : "";
  div.className = "ln" + (lvl ? " " + lvl : "");
  div.dataset.level = lvl;
  let body = ansi !== null ? ansi : decorate(text);
  let meta = "";
  if (count && count > 1) {
    meta = '<span class="rep-badge" title="' + firstTs.slice(0, 8) + " ~ " + lastTs.slice(0, 8) + '">×' + count + "</span>";
  }
  div.innerHTML =
    '<span class="ts' + (dimTs ? " rep" : "") + '" title="' + ts + '">' + ts.slice(0, 8) + "</span>" +
    '<span class="txt">' + body + meta + "</span>";
  return div;
}

/* ============================ 검색 + 필터 ============================ */
function escapeRegExp(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); }

function compileMatcher() {
  const q = state.query;
  if (!q) { state.matcher = null; $("searchWrap").classList.remove("has-q"); return; }
  $("searchWrap").classList.add("has-q");
  // 리터럴 부분일치(대소문자 무시) — 정규식 모드 없이 단순·직관적으로
  try { state.matcher = new RegExp(escapeRegExp(q), "gi"); } catch (e) { state.matcher = null; }
}

// .txt 안의 텍스트 노드만 훑어 매치를 <mark>로 감싼다(데코 span 보존).
function highlightLine(div) {
  const txt = div.querySelector(".txt");
  if (!txt) return;
  txt.querySelectorAll("mark").forEach(m => {   // 기존 하이라이트 해제
    const t = document.createTextNode(m.textContent);
    m.replaceWith(t);
  });
  txt.normalize();
  if (!state.matcher || div.classList.contains("hide")) return;
  const re = state.matcher;
  const walker = document.createTreeWalker(txt, NodeFilter.SHOW_TEXT);
  const targets = [];
  let n;
  while ((n = walker.nextNode())) targets.push(n);
  for (const node of targets) {
    const s = node.nodeValue;
    re.lastIndex = 0;
    if (!re.test(s)) continue;
    re.lastIndex = 0;
    const frag = document.createDocumentFragment();
    let last = 0, m;
    while ((m = re.exec(s))) {
      if (m.index > last) frag.appendChild(document.createTextNode(s.slice(last, m.index)));
      const mk = document.createElement("mark");
      mk.textContent = m[0];
      frag.appendChild(mk);
      last = m.index + m[0].length;
      if (m[0].length === 0) re.lastIndex++;
    }
    if (last < s.length) frag.appendChild(document.createTextNode(s.slice(last)));
    node.replaceWith(frag);
  }
}

function lineVisible(div) {
  const lvl = div.dataset.level;
  if (lvl && !state.levels[lvl]) return false;
  if (state.matcher) { state.matcher.lastIndex = 0; if (!state.matcher.test(div.dataset.raw || "")) return false; }
  return true;
}

function applyVisibility(div) {
  const vis = lineVisible(div);
  div.classList.toggle("hide", !vis);
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
    const lvl = el.dataset.level;
    if (lvl === "err") nErr++; else if (lvl === "warn") nWarn++; else if (lvl === "boot") nBoot++;
    if (!el.classList.contains("hide")) visible++;
  }
  $("nErr").textContent = nErr; $("nWarn").textContent = nWarn; $("nBoot").textContent = nBoot;
  // 매치 목록 (검색 시)
  state.matchEls = state.matcher ? [...pane.querySelectorAll(".ln:not(.hide) mark")] : [];
  if (state.matcher) {
    const tot = state.matchEls.length;
    $("matchn").textContent = tot ? (Math.min(state.matchIdx + 1, tot) || 1) + "/" + tot : "0";
    $("matchn").classList.toggle("none", tot === 0);
    if (state.matchIdx >= tot) state.matchIdx = tot - 1;
  }
  // 빈 상태
  const empty = visible === 0;
  $("empty").classList.toggle("show", empty);
  if (empty) {
    $("empty").querySelector(".big").textContent = state.matcher || hasLevelFilter() ? "표시할 줄 없음" : "로그 없음";
    $("emptyHint").textContent = state.matcher || hasLevelFilter()
      ? "검색어나 레벨 필터를 조정해 보세요" : (state.tab === "stream" ? "실시간 수신 대기 중…" : "버퍼가 비어 있습니다");
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
function connectStream(port) {
  if (es) es.close();
  state.port = port;
  $("stream").innerHTML = ""; $("buffer").innerHTML = "";
  state.streamLines = 0; state.streamLastSec = null; state.newCount = 0;
  $("newpill").classList.remove("show");
  updateStreamCount();
  es = new EventSource("/api/stream?port=" + encodeURIComponent(port));
  es.onopen = () => $("stream").appendChild(markerDivider("실시간 수신 시작 — 이전 기록은 [버퍼] 탭"));
  es.onmessage = ev => {
    if (state.paused) return;
    const d = JSON.parse(ev.data);
    const box = $("stream");
    const sec = toSec(d.ts);
    const dim = state.streamLastSec === sec;
    if (state.streamLastSec !== null && sec - state.streamLastSec >= GAP_SEC) {
      const g = gapDivider(sec - state.streamLastSec);
      if (state.matcher) g.style.display = "none";
      box.appendChild(g);
    }
    state.streamLastSec = sec;
    const div = buildLine(d.ts, d.text, 0, "", "", dim);
    box.appendChild(div);
    applyVisibility(div);
    state.streamLines++;
    while (box.childNodes.length > MAX_STREAM) {
      const removed = box.firstChild;
      if (removed.classList && removed.classList.contains("ln")) state.streamLines--;
      box.removeChild(removed);
    }
    updateStreamCount();
    scheduleRecount();
    if (state.follow && state.tab === "stream") window.scrollTo(0, document.body.scrollHeight);
    else if (state.tab === "stream" && !nearBottom() && !div.classList.contains("hide")) {
      state.newCount++;
      $("newpillText").textContent = "새 로그 " + state.newCount + "건";
      $("newpill").classList.add("show");
    }
  };
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
  let prevSec = null, prevShownSec = null;
  for (const e of d.entries || []) {
    const sec = toSec(e.first_ts);
    if (prevSec !== null && sec - prevSec >= GAP_SEC) {
      const g = gapDivider(sec - prevSec);
      if (state.matcher) g.style.display = "none";
      box.appendChild(g);
    }
    prevSec = toSec(e.last_ts);
    const div = buildLine(e.first_ts, e.text, e.count, e.first_ts, e.last_ts, sec === prevShownSec);
    box.appendChild(div);
    applyVisibility(div);
    prevShownSec = sec;
  }
  $("cBuffer").textContent = (d.entries || []).length + "/" + (d.capacity ?? "?");
  scheduleRecount();
  if (state.follow && state.tab === "buffer") window.scrollTo(0, document.body.scrollHeight);
}
setInterval(refreshBuffer, 2000);

/* ============================ 상태 폴링 ============================ */
async function refreshStatus() {
  let d;
  try { d = await (await fetch("/api/status")).json(); }
  catch (e) { return; }
  const ports = d.ports || [];
  state.ports = ports;
  state.session = d.session || null;
  if (!state.port && ports.length) connectStream(ports[0].port);
  if (window.renderPortBoard) renderPortBoard(ports, state.session, state.port, selectPort, releaseSession);
  const p = ports.find(x => x.port === state.port) || ports[0];
  if (p) $("cBuffer").textContent = (p.buffer_entries ?? 0) + "/" + (p.buffer_capacity ?? "?");
}
setInterval(refreshStatus, 5000);

/* 포트 전환 · 소유권 종료 (상태 보드는 항상 표시) */
function selectPort(port) {
  if (port !== state.port) connectStream(port);
  resetPortBoardSig();
  refreshStatus();
  refreshBuffer();
}
async function releaseSession(ports, session) {
  // TODO(codex): /api/release?port= 는 백엔드 미구현(404). 세션 카드가 session 있을 때만 그려져 평소 미호출.
  if (!confirm((session || "이 세션") + "\n이 AI 세션의 포트 점유를 해제할까요?\n(" + ports.join(", ") + ")")) return;
  for (const p of ports) { try { await fetch("/api/release?port=" + encodeURIComponent(p)); } catch (e) {} }
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
  if (state.tab === "stream") { state.streamLastSec = null; state.streamLines = 0; updateStreamCount(); }
  recount();
};

function visibleLines() {
  const out = [];
  for (const el of $(state.tab).children) {
    if (!el.classList.contains("ln") || el.classList.contains("hide")) continue;
    const ts = el.querySelector(".ts")?.textContent || "";
    out.push((ts ? "[" + ts + "] " : "") + el.dataset.raw);
  }
  return out.join("\n");
}

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

/* tag click → filter */
document.addEventListener("click", ev => {
  const t = ev.target.closest(".tag");
  if (!t) return;
  const lit = "[" + t.dataset.tag + "]";
  const box = $("search");
  box.value = box.value.trim() === lit ? "" : lit;
  setSearch(box.value);
}, true);

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
gear.onclick = e => {
  e.stopPropagation();
  pop.classList.toggle("open");
  if (pop.classList.contains("open")) positionPop();
};
document.addEventListener("click", e => { if (!pop.contains(e.target) && e.target !== gear) pop.classList.remove("open"); });

let fs = +(localStorage.getItem("sv_fs") || 13);
function applyFs() {
  fs = Math.min(18, Math.max(11, fs));
  document.documentElement.style.setProperty("--fs", fs + "px");
  localStorage.setItem("sv_fs", fs);
}
$("fsDown").onclick = () => { fs--; applyFs(); };
$("fsUp").onclick = () => { fs++; applyFs(); };
applyFs();

function bindToggle(rowId, cls, key, def) {
  const row = $(rowId);
  let on = localStorage.getItem(key);
  on = on === null ? def : on === "1";
  const apply = () => {
    row.classList.toggle("on", on);
    if (cls) document.body.classList.toggle(cls, on);
    localStorage.setItem(key, on ? "1" : "0");
  };
  row.onclick = () => { on = !on; apply(); };
  apply();
}
bindToggle("tgTs", null, "sv_ts", true);   // 타임스탬프 표시 (on = 표시)
// 'on'이 표시이므로 body.no-ts 는 반대로 적용
$("tgTs").onclick = () => {
  const on = !$("tgTs").classList.contains("on");
  $("tgTs").classList.toggle("on", on);
  document.body.classList.toggle("no-ts", !on);
  localStorage.setItem("sv_ts", on ? "1" : "0");
};
(function initTs() {
  let on = localStorage.getItem("sv_ts"); on = on === null ? true : on === "1";
  $("tgTs").classList.toggle("on", on);
  document.body.classList.toggle("no-ts", !on);
})();
bindToggle("tgWrap", null, "sv_wrap", true);
$("tgWrap").onclick = () => {
  const on = !$("tgWrap").classList.contains("on");
  $("tgWrap").classList.toggle("on", on);
  document.body.classList.toggle("nowrap", !on);
  localStorage.setItem("sv_wrap", on ? "1" : "0");
};
(function initWrap() {
  let on = localStorage.getItem("sv_wrap"); on = on === null ? true : on === "1";
  $("tgWrap").classList.toggle("on", on);
  document.body.classList.toggle("nowrap", !on);
})();

/* ============================ 단축키 ============================ */
$("tgHelp").onclick = () => $("help").classList.toggle("open");

/* 네비게이션 접기/펼치기 (기억) */
function setNav(collapsed) {
  document.body.classList.toggle("nav-collapsed", collapsed);
  localStorage.setItem("sv_nav", collapsed ? "1" : "0");
}
$("navToggle").onclick = () => setNav(!document.body.classList.contains("nav-collapsed"));
setNav(localStorage.getItem("sv_nav") === "1");

$("help").onclick = e => { if (e.target === $("help")) $("help").classList.remove("open"); };

document.addEventListener("keydown", e => {
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
  if (e.key === "/" && !typing) { e.preventDefault(); $("search").focus(); return; }
  if (e.key === "?" ) { $("help").classList.toggle("open"); return; }
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
  try {
    const d = await (await fetch("/api/ports")).json();
    if (!state.port && (d.ports || []).length) connectStream(d.ports[0].port);
  } catch (e) { /* 포트 목록 조회 실패 — 좌측 portboard 가 빈 상태를 표시(board.js) */ }
  await refreshStatus();
  recount();
}
init();
</script>
</body>
</html>
"""
