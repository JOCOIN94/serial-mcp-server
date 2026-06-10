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

from .ring_buffer import _fmt_ts
from .viewer_feed import RawFeed


def _log(msg: str) -> None:
    """진단 로그 — stderr 전용(server.py의 _log와 동일 형식, 순환 import 회피용 사본)."""
    print(f"[serial-mcp] {msg}", file=sys.stderr, flush=True)


class _ViewerHTTPServer(ThreadingHTTPServer):
    daemon_threads = True        # SSE 핸들러 스레드가 프로세스 종료를 막지 않게
    block_on_close = False       # server_close()가 장수 SSE 핸들러를 기다리지 않게
    allow_reuse_address = False  # Windows에서 점유 포트 중복 바인딩 방지(점유 감지가 정확해야 폴백이 동작)

    feed: RawFeed
    buffer_info: Callable[[], dict]
    status_info: Callable[[], dict]


class _Handler(BaseHTTPRequestHandler):
    server: _ViewerHTTPServer

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - 부모 시그니처
        _log("HTTP " + (fmt % args))   # stdout 금지 — 접근 로그를 stderr로

    def do_GET(self) -> None:  # noqa: N802 - http.server 규약
        if self.path == "/":
            body = _HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/stream":
            self._serve_stream()
        elif self.path == "/api/buffer":
            self._send_json(self.server.buffer_info())
        elif self.path == "/api/status":
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

    def _serve_stream(self) -> None:
        """SSE — RawFeed를 구독해 한 줄당 한 이벤트로 흘려보낸다."""
        # 구독을 헤더 전송보다 먼저: 클라이언트가 응답 헤더를 받은 시점에는
        # 이미 구독이 살아 있어야 발행 누락이 없다(테스트·실사용 레이스 방지).
        sub = self.server.feed.subscribe()
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
            self.server.feed.unsubscribe(sub)


class ViewerServer:
    """뷰어 HTTP 서버 래퍼 — 기동/포트 폴백/URL 보고. 예외를 밖으로 내지 않는다."""

    def __init__(
        self,
        feed: RawFeed,
        buffer_info: Callable[[], dict],
        status_info: Callable[[], dict],
        port: int = 8743,
    ) -> None:
        self._feed = feed
        self._buffer_info = buffer_info
        self._status_info = status_info
        self._preferred_port = port
        self._httpd: Optional[_ViewerHTTPServer] = None
        self.url: Optional[str] = None   # 기동 성공 시 http://127.0.0.1:{port}, 실패 시 None

    def start(self) -> None:
        for port in (self._preferred_port, 0):   # 선호 포트 점유 시 임시 포트로 폴백
            try:
                self._httpd = _ViewerHTTPServer(("127.0.0.1", port), _Handler)
                break
            except OSError as e:
                _log(f"웹 뷰어 포트 {port} 바인딩 실패: {e}")
        if self._httpd is None:
            _log("웹 뷰어 비활성 — 포트 바인딩 전부 실패")
            return
        self._httpd.feed = self._feed
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
# 컬러 원칙: "색은 장식이 아니라 신호" — 평상시 회색 2~3톤, 이상 상황만 채도.
# 우선순위: ANSI 해석 > 레벨 라인 틴트 > 성공 키워드 > JSON 절제 > 메타 dim.
_HTML = r"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>serial-mcp 로그 뷰어</title>
<style>
  body { margin:0; background:#111418; color:#c9d1d9;
         font:13px/1.45 Consolas,'D2Coding','Courier New',monospace; }
  header { position:sticky; top:0; z-index:1; display:flex; align-items:center;
           gap:10px; padding:8px 12px; background:#1a1f26;
           border-bottom:1px solid #2d333b; }
  header .dot { width:9px; height:9px; border-radius:50%; background:#f85149; }
  header .dot.on { background:#3fb950; }
  header button { background:#21262d; color:#c9d1d9; border:1px solid #2d333b;
                  border-radius:4px; padding:3px 10px; cursor:pointer; font:inherit; }
  header button.active { background:#2d4f7c; border-color:#3b6ea5; }
  header label { display:flex; align-items:center; gap:4px; cursor:pointer;
                 color:#8b949e; }
  #port { color:#8b949e; }
  main { padding:6px 12px 24px; }
  .ln { white-space:pre-wrap; word-break:break-all; }
  .ln.err  { background:rgba(248,81,73,.13); color:#ffa198; }
  .ln.warn { background:rgba(210,153,34,.12); color:#e3b341; }
  .meta { color:#586069; }
  .ok   { color:#3fb950; }
  .jkey { color:#79c0ea; }
  .dim  { color:#586069; }
  .a30{color:#484f58}.a31{color:#ff7b72}.a32{color:#3fb950}.a33{color:#d29922}
  .a34{color:#58a6ff}.a35{color:#bc8cff}.a36{color:#39c5cf}.a37{color:#b1bac4}
  .a90{color:#6e7681}.a91{color:#ffa198}.a92{color:#56d364}.a93{color:#e3b341}
  .a94{color:#79c0ff}.a95{color:#d2a8ff}.a96{color:#56d4dd}.a97{color:#f0f6fc}
  .ab{font-weight:bold}
</style>
</head>
<body>
<header>
  <span class="dot" id="dot"></span>
  <span id="port">…</span>
  <button id="tabStream" class="active">스트림</button>
  <button id="tabBuffer">버퍼</button>
  <button id="pause">⏸ 일시정지</button>
  <label><input type="checkbox" id="follow" checked> 자동스크롤</label>
  <button id="clear">화면 지우기</button>
</header>
<main>
  <div id="stream"></div>
  <div id="buffer" style="display:none"></div>
</main>
<script>
const MAX_STREAM = 5000;
const $ = id => document.getElementById(id);
let paused = false, activeTab = "stream";

function esc(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// ANSI SGR(색·굵기)을 span으로 변환. ANSI가 없으면 null 반환(휴리스틱 컬러로 진행).
const ESC = String.fromCharCode(27);   // 이스케이프 문자를 코드로 생성(소스에 제어문자 없음 — 복사 안전)
function ansiToHtml(s) {
  if (s.indexOf(ESC + "[") === -1) return null;
  let out = "", open = 0;
  for (const p of s.split(new RegExp("(" + ESC + "\\[[0-9;]*m)"))) {
    const m = p.match(new RegExp("^" + ESC + "\\[([0-9;]*)m$"));
    if (!m) {
      out += esc(p.replace(new RegExp(ESC + "\\[[0-9;]*[A-Za-z]", "g"), ""));
      continue;
    }
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

// 휴리스틱 데코: JSON 키 시안, 괄호 dim, 성공 키워드 녹색 (값은 본문색 유지 — 무지개 금지)
function decorate(text) {
  let h = esc(text);   // esc는 &,<,>만 치환 — 따옴표는 그대로 남는다
  h = h.replace(/"([^"]*)"(\s*):/g, '<span class="jkey">"$1"</span>$2:');
  h = h.replace(/([{}\[\]])/g, '<span class="dim">$1</span>');
  h = h.replace(/\b(OK|Success|Done)\b/g, '<span class="ok">$1</span>');
  return h;
}

function lineClass(text) {
  if (/error|fail|exception|rst:/i.test(text)) return " err";
  if (/warn/i.test(text)) return " warn";
  return "";
}

function renderLine(ts, text, metaSuffix) {
  const div = document.createElement("div");
  const ansi = ansiToHtml(text);   // ANSI가 있으면 펌웨어 의도 우선, 휴리스틱 생략
  div.className = "ln" + (ansi === null ? lineClass(text) : "");
  div.innerHTML = '<span class="meta">[' + ts + ']</span> ' +
    (ansi !== null ? ansi : decorate(text)) +
    (metaSuffix ? ' <span class="meta">' + esc(metaSuffix) + '</span>' : "");
  return div;
}

const es = new EventSource("/api/stream");
es.onmessage = ev => {
  if (paused) return;
  const d = JSON.parse(ev.data);
  const box = $("stream");
  box.appendChild(renderLine(d.ts, d.text, ""));
  while (box.childNodes.length > MAX_STREAM) box.removeChild(box.firstChild);
  if ($("follow").checked && activeTab === "stream")
    window.scrollTo(0, document.body.scrollHeight);
};

async function refreshBuffer() {
  if (activeTab !== "buffer" || paused) return;
  const d = await (await fetch("/api/buffer")).json();
  const box = $("buffer");
  box.innerHTML = "";
  for (const e of d.entries || []) {
    const suffix = e.count > 1
      ? "(" + e.count + "회 반복, " + e.first_ts + "~" + e.last_ts + ")" : "";
    box.appendChild(renderLine(e.first_ts, e.text, suffix));
  }
  if ($("follow").checked) window.scrollTo(0, document.body.scrollHeight);
}
setInterval(refreshBuffer, 2000);

async function refreshStatus() {
  try {
    const d = await (await fetch("/api/status")).json();
    $("dot").className = "dot" + (d.connected ? " on" : "");
    $("port").textContent = (d.port || "(포트 미설정)") + " @ " + d.baud +
      (d.last_error ? " — " + d.last_error : "");
  } catch (e) { $("dot").className = "dot"; }
}
setInterval(refreshStatus, 5000);
refreshStatus();

function setTab(name) {
  activeTab = name;
  $("stream").style.display = name === "stream" ? "" : "none";
  $("buffer").style.display = name === "buffer" ? "" : "none";
  $("tabStream").className = name === "stream" ? "active" : "";
  $("tabBuffer").className = name === "buffer" ? "active" : "";
  if (name === "buffer") refreshBuffer();
}
$("tabStream").onclick = () => setTab("stream");
$("tabBuffer").onclick = () => setTab("buffer");
$("pause").onclick = () => {
  paused = !paused;
  $("pause").textContent = paused ? "▶ 재개" : "⏸ 일시정지";
};
$("clear").onclick = () => { $(activeTab).innerHTML = ""; };  // 화면만 — 서버 버퍼 무변경
</script>
</body>
</html>
"""
