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
# 컬러 원칙: "색은 장식이 아니라 신호" — 평상시 회색 2~3톤, 이상 상황만 채도.
# 우선순위: ANSI 해석 > 레벨 라인 틴트 > 성공 키워드 > JSON 절제 > 메타 dim.
_HTML = r"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>serial-mcp 로그 뷰어</title>
<style>
  :root { --fs: 13px; }
  body { margin:0; background:#111418; color:#c9d1d9;
         font:var(--fs)/1.5 Consolas,'D2Coding','Courier New',monospace; }
  header { position:sticky; top:0; z-index:1; display:flex; align-items:center;
           flex-wrap:wrap; gap:6px 10px; padding:8px 12px; background:#1a1f26;
           border-bottom:1px solid #2d333b; font-size:13px; }
  header .dot { width:9px; height:9px; border-radius:50%; background:#f85149; }
  header .dot.on { background:#3fb950; }
  header button { background:#21262d; color:#c9d1d9; border:1px solid #2d333b;
                  border-radius:4px; padding:3px 10px; cursor:pointer; font:inherit; }
  header button.active { background:#2d4f7c; border-color:#3b6ea5; }
  header label { display:flex; align-items:center; gap:4px; cursor:pointer;
                 color:#8b949e; white-space:nowrap; flex:0 0 auto; }
  #filter { background:#0d1117; border:1px solid #2d333b; color:#c9d1d9;
            border-radius:4px; padding:3px 8px; font:inherit; width:14ch;
            flex:0 1 auto; min-width:8ch; }
  #filter:focus { outline:none; border-color:#3b6ea5; }
  #port { color:#8b949e; }
  #psel { background:#21262d; color:#c9d1d9; border:1px solid #2d333b;
          border-radius:4px; padding:3px 6px; font:inherit; }
  #fcount { color:#8b949e; }
  #newpill { position:fixed; right:16px; bottom:14px; z-index:2; display:none;
             background:#2d4f7c; border:1px solid #3b6ea5; color:#e6edf3;
             border-radius:14px; padding:4px 12px; cursor:pointer; font-size:12px; }
  main { padding:6px 12px 24px; }
  /* 타임스탬프는 고정폭 거터 칼럼 — 래핑된 본문이 침범하지 못한다 */
  .ln { display:grid; grid-template-columns:8ch 1fr; column-gap:10px;
        padding:1px 0; border-radius:3px; }
  .ln:not(.err):not(.warn):not(.boot):hover { background:rgba(255,255,255,.045); }
  .ln .ts { color:#586069; user-select:none; }
  .ln .ts.rep { opacity:.3; }   /* 같은 초 반복 — 초가 바뀌는 줄만 또렷하게 */
  .ln .txt { white-space:pre-wrap; word-break:break-word; overflow-wrap:anywhere;
             min-width:0; }
  .ln.err  { background:rgba(248,81,73,.13); box-shadow:inset 3px 0 0 #f85149; }
  .ln.err .txt { color:#ffa198; }
  .ln.warn { background:rgba(210,153,34,.12); box-shadow:inset 3px 0 0 #d29922; }
  .ln.warn .txt { color:#e3b341; }
  .ln.boot { background:rgba(88,166,255,.10); box-shadow:inset 3px 0 0 #58a6ff; }
  .ln.boot .txt { color:#79c0ff; }
  .ln.blank { padding:0; line-height:.45em; }   /* 빈 줄은 얇은 스페이서로 압축 */
  .ln.blank .ts { opacity:.18; font-size:.8em; }
  .gap { display:flex; align-items:center; gap:8px; color:#6e7681;
         font-size:.85em; padding:3px 0; }
  .gap::before, .gap::after { content:""; height:1px; background:#262c33; flex:1; }
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
  <select id="psel" title="보드 선택"></select>
  <button id="tabStream" class="active"
          title="이 화면을 연 뒤 수신한 원본 줄 수 — 새로고침하면 0부터">스트림</button>
  <button id="tabBuffer"
          title="서버가 보관 중인 가공 로그(접힘·필터 적용) — AI가 보는 것과 동일">버퍼</button>
  <button id="pause">⏸ 일시정지</button>
  <label><input type="checkbox" id="follow" checked> 자동스크롤</label>
  <button id="clear">화면 지우기</button>
  <input id="filter" placeholder="필터(정규식)" spellcheck="false"
         title="정규식 필터. 로그의 태그를 클릭해도 그 태그로 필터됩니다">
  <span id="fcount"></span>
  <button id="fsMinus" title="글자 작게">A−</button>
  <button id="fsPlus" title="글자 크게">A+</button>
</header>
<main>
  <div id="stream"></div>
  <div id="buffer" style="display:none"></div>
</main>
<div id="newpill">↓ 새 로그 0건</div>
<script>
const MAX_STREAM = 5000;
const GAP_SEC = 2;             // 이 이상 수신 공백이면 간격 구분선 표시
const $ = id => document.getElementById(id);
let paused = false, activeTab = "stream", streamLastSec = null;
let filterRe = null, newCount = 0, streamLines = 0;

// 탭 버튼에 적재 현황 표시 — "지금 몇 줄 / 한도"가 항상 보이게
function updateStreamTab() {
  $("tabStream").textContent = "스트림 " + streamLines + "/" + MAX_STREAM;
}
function setBufferTab(n, cap) {
  $("tabBuffer").textContent = "버퍼 " + n + "/" + cap;
}

// ---- 라이브 필터(정규식, 실패 시 리터럴) + 태그 클릭 필터 ----
function escapeRegExp(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); }

function applyFilterTo(div) {
  div.style.display = (!filterRe || filterRe.test(div.dataset.raw || "")) ? "" : "none";
}

function applyFilterAll() {
  for (const pane of [$("stream"), $("buffer")]) {
    for (const el of pane.children) {
      if (el.classList.contains("gap")) el.style.display = filterRe ? "none" : "";
      else applyFilterTo(el);
    }
  }
  updateFcount();
}

function updateFcount() {
  if (!filterRe) { $("fcount").textContent = ""; return; }
  const pane = $(activeTab);
  let vis = 0, tot = 0;
  for (const el of pane.children) {
    if (el.classList.contains("gap")) continue;
    tot++;
    if (el.style.display !== "none") vis++;
  }
  $("fcount").textContent = vis + "/" + tot;
}

function setFilter(v) {
  v = v.trim();
  if (v === "") filterRe = null;
  else if (/^\[[A-Za-z][A-Za-z0-9_ .\-]*\]$/.test(v)) {
    // "[IOc]" 같은 태그 리터럴 — 정규식 문자클래스로 오해되지 않게 이스케이프
    filterRe = new RegExp(escapeRegExp(v), "i");
  } else {
    try { filterRe = new RegExp(v, "i"); }
    catch (e) { filterRe = new RegExp(escapeRegExp(v), "i"); }
  }
  applyFilterAll();
}

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

// 태그 자동 컬러: [Proc-WiFiRx] 같은 토큰을 해시 기반 고정색으로(같은 태그는 항상 같은 색)
const TAG_RE = /(\[[A-Za-z][A-Za-z0-9_ .\-]{1,23}\])/;
function tagColor(name) {
  let h = 0;
  for (const ch of name) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return "hsl(" + (h % 360) + ",45%,68%)";   // 채도 낮게 — 어지럽지 않게
}

// 휴리스틱 데코(태그 제외 구간): JSON 키 시안, 괄호 dim, 성공 키워드 녹색
function plainDecorate(s) {
  let h = esc(s);   // esc는 &,<,>만 치환 — 따옴표는 그대로 남는다
  h = h.replace(/"([^"]*)"(\s*):/g, '<span class="jkey">"$1"</span>$2:');
  h = h.replace(/([{}\[\]])/g, '<span class="dim">$1</span>');
  h = h.replace(/\b(OK|Success|Done)\b/g, '<span class="ok">$1</span>');
  return h;
}

function decorate(text) {
  let out = "";
  for (const p of text.split(TAG_RE)) {
    const m = p.match(/^\[([A-Za-z][A-Za-z0-9_ .\-]{1,23})\]$/);
    out += m ? '<span class="tag" data-tag="' + esc(m[1]) + '" style="color:' +
               tagColor(m[1]) + '">' + esc(p) + "</span>"
             : plainDecorate(p);
  }
  return out;
}

function lineClass(text) {
  // 부팅/리셋 마커는 에러가 아니라 별도 신호(파랑) — POWERON 정상 부팅 오인 방지
  if (/^(ESP-ROM:|rst:0x|entry 0x)/.test(text)) return " boot";
  // ESP-IDF 표준 프리픽스 E (..)/W (..)도 인식 + 키워드 휴리스틱
  if (/^E \(\d/.test(text) || /error|fail|exception/i.test(text)) return " err";
  if (/^W \(\d/.test(text) || /warn/i.test(text)) return " warn";
  return "";
}

function toSec(ts) {   // "HH:MM:SS(.mmm)" → 하루 내 초(간격 계산용)
  return +ts.slice(0, 2) * 3600 + +ts.slice(3, 5) * 60 + +ts.slice(6, 8);
}

function gapDivider(sec) {
  const g = document.createElement("div");
  g.className = "gap";
  g.textContent = "+" + sec + "s";
  return g;
}

// 첫 태그 끝까지의 폭(ch) — 래핑된 줄이 태그 아래로 밀리지 않고 태그 뒤에 정렬되게
// (타임스탬프 거터와 같은 원리의 내어쓰기. 태그가 너무 깊으면 미적용)
function hangIndent(text) {
  const m = text.match(TAG_RE);
  if (!m) return 0;
  const end = m.index + m[0].length + 1;
  return end <= 32 ? end : 0;
}

function renderLine(ts, text, metaSuffix, dimTs) {
  const div = document.createElement("div");
  div.dataset.raw = text;   // 필터 매칭용 원문 보관
  if (text.trim() === "") {   // 빈 줄 — 얇은 스페이서(원본 존재는 유지)
    div.className = "ln blank";
    div.innerHTML = '<span class="ts" title="' + ts + '">' + ts.slice(0, 8) +
      '</span><span class="txt"></span>';
    applyFilterTo(div);
    return div;
  }
  const ansi = ansiToHtml(text);   // ANSI가 있으면 펌웨어 의도 우선, 휴리스틱 생략
  div.className = "ln" + (ansi === null ? lineClass(text) : "");
  const ind = ansi === null ? hangIndent(text) : 0;
  const indStyle = ind
    ? ' style="padding-left:' + ind + 'ch;text-indent:-' + ind + 'ch"' : "";
  let html = '<span class="ts' + (dimTs ? " rep" : "") + '" title="' + ts + '">' +
    ts.slice(0, 8) + '</span><span class="txt"' +
    indStyle + ">" + (ansi !== null ? ansi : decorate(text));
  if (metaSuffix) html += ' <span class="meta">' + esc(metaSuffix) + "</span>";
  div.innerHTML = html + "</span>";
  applyFilterTo(div);
  return div;
}

let es = null, currentPort = null;
function connectStream(port) {   // 포트 전환 = 스트림 화면 리셋 + 새 SSE 구독
  if (es) es.close();
  currentPort = port;
  $("stream").innerHTML = "";
  $("buffer").innerHTML = "";   // 이전 포트의 버퍼 잔류 방지(다음 폴링까지 빈 화면)
  streamLines = 0; streamLastSec = null; newCount = 0;
  $("newpill").style.display = "none";
  updateStreamTab();
  es = new EventSource("/api/stream?port=" + encodeURIComponent(port));
  es.onopen = () => {   // 스트림은 "지금부터" 생중계 — 시작 지점 명시(이전 기록은 버퍼 탭)
    const g = document.createElement("div");
    g.className = "gap";
    g.textContent = "실시간 수신 시작 — 이전 기록은 [버퍼] 탭";
    $("stream").appendChild(g);
  };
  es.onmessage = ev => {
    if (paused) return;
    const d = JSON.parse(ev.data);
    const box = $("stream");
    const sec = toSec(d.ts);
    const dim = streamLastSec === sec;   // 같은 초 반복 → 거터 흐리게
    if (streamLastSec !== null && sec - streamLastSec >= GAP_SEC) {
      const g = gapDivider(sec - streamLastSec);
      if (filterRe) g.style.display = "none";
      box.appendChild(g);
    }
    streamLastSec = sec;
    box.appendChild(renderLine(d.ts, d.text, "", dim));
    streamLines++;
    while (box.childNodes.length > MAX_STREAM) {   // 한도 초과분은 위에서 제거
      const removed = box.firstChild;
      if (removed.classList && removed.classList.contains("ln")) streamLines--;
      box.removeChild(removed);
    }
    updateStreamTab();
    if ($("follow").checked && activeTab === "stream") {
      window.scrollTo(0, document.body.scrollHeight);
    } else if (!nearBottom()) {
      newCount++;   // 바닥을 안 보고 있을 때만 새 로그 배지
      $("newpill").textContent = "↓ 새 로그 " + newCount + "건";
      $("newpill").style.display = "block";
    }
  };
}

function nearBottom() {
  return window.innerHeight + window.scrollY >= document.body.scrollHeight - 60;
}

$("newpill").onclick = () => {
  setTab("stream");
  $("follow").checked = true;
  window.scrollTo(0, document.body.scrollHeight);
  newCount = 0;
  $("newpill").style.display = "none";
};
window.addEventListener("scroll", () => {
  if (nearBottom()) {
    newCount = 0;
    $("newpill").style.display = "none";
  }
});

async function refreshBuffer() {
  if (activeTab !== "buffer" || paused || !currentPort) return;
  const d = await (await fetch("/api/buffer?port=" + encodeURIComponent(currentPort))).json();
  const box = $("buffer");
  box.innerHTML = "";
  let prevSec = null, prevShownSec = null;
  for (const e of d.entries || []) {
    const sec = toSec(e.first_ts);
    if (prevSec !== null && sec - prevSec >= GAP_SEC) {
      const g = gapDivider(sec - prevSec);
      if (filterRe) g.style.display = "none";
      box.appendChild(g);
    }
    prevSec = toSec(e.last_ts);   // 접힌 묶음은 끝 시각부터 간격 계산
    const suffix = e.count > 1
      ? "(" + e.count + "회 반복, " + e.first_ts.slice(0, 8) + "~" + e.last_ts.slice(0, 8) + ")" : "";
    box.appendChild(renderLine(e.first_ts, e.text, suffix, sec === prevShownSec));
    prevShownSec = sec;
  }
  setBufferTab((d.entries || []).length, d.capacity ?? "?");
  updateFcount();
  if ($("follow").checked) window.scrollTo(0, document.body.scrollHeight);
}
setInterval(refreshBuffer, 2000);

async function refreshStatus() {
  try {
    const d = await (await fetch("/api/status")).json();
    const sel = $("psel");
    for (const o of sel.options) {   // 자동 식별(SERIAL_AUTONAME)로 별칭이 늦게 확정되면 라벨 동기화
      const m = (d.ports || []).find(x => x.port === o.value);
      if (m && o.textContent !== m.label) o.textContent = m.label;
    }
    for (const m of d.ports || []) {   // 핫플러그로 늘어난 포트를 셀렉터에 추가(서버는 런타임에 모니터를 늘린다)
      if (![...sel.options].some(o => o.value === m.port)) {
        const o = document.createElement("option");
        o.value = m.port;
        o.textContent = m.label;
        sel.appendChild(o);
      }
    }
    if (sel.options.length > 1) sel.style.display = "";   // 2개째부터 셀렉터 노출(initPorts가 숨겼어도)
    if (!currentPort && (d.ports || []).length)
      connectStream(d.ports[0].port);   // 포트 0개로 기동 후 첫 보드가 꽂힌 경우 — 스트림 자동 연결
    const p = (d.ports || []).find(x => x.port === currentPort) || (d.ports || [])[0];
    if (!p) { $("dot").className = "dot"; $("port").textContent = "(모니터링 포트 없음)"; return; }
    $("dot").className = "dot" + (p.connected ? " on" : "");
    $("port").textContent = p.label + " @ " + p.baud +
      (p.last_error ? " — " + p.last_error : "");
    setBufferTab(p.buffer_entries, p.buffer_capacity);
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
  updateFcount();
}
$("tabStream").onclick = () => setTab("stream");
$("tabBuffer").onclick = () => setTab("buffer");
$("pause").onclick = () => {
  paused = !paused;
  $("pause").textContent = paused ? "▶ 재개" : "⏸ 일시정지";
};
$("clear").onclick = () => {
  $(activeTab).innerHTML = "";   // 화면만 — 서버 버퍼 무변경
  if (activeTab === "stream") { streamLastSec = null; streamLines = 0; updateStreamTab(); }
  updateFcount();
};

// 필터 입력 + 태그 클릭으로 필터 토글(같은 태그 다시 클릭하면 해제)
$("filter").addEventListener("input", () => setFilter($("filter").value));
document.addEventListener("click", ev => {
  const t = ev.target.closest(".tag");
  if (!t) return;
  const lit = "[" + t.dataset.tag + "]";
  const cur = $("filter").value.trim();
  $("filter").value = cur === lit ? "" : lit;
  setFilter($("filter").value);
}, true);

// 글자 크기 A−/A+ (11~18px, localStorage에 기억)
let fs = +(localStorage.getItem("fs") || 13);
function applyFs() {
  fs = Math.min(18, Math.max(11, fs));
  document.documentElement.style.setProperty("--fs", fs + "px");
  localStorage.setItem("fs", fs);
}
applyFs();
$("fsMinus").onclick = () => { fs--; applyFs(); };
$("fsPlus").onclick = () => { fs++; applyFs(); };
updateStreamTab();   // 초기 표시 "스트림 0/5000"
async function initPorts() {   // 포트 목록 → 셀렉터 구성 → 첫 포트 스트림 연결
  try {
    const d = await (await fetch("/api/ports")).json();
    const sel = $("psel");
    for (const p of d.ports || []) {   // refreshStatus(동시 기동 폴링)가 먼저 채웠을 수 있다 — 중복 추가 금지
      if ([...sel.options].some(o => o.value === p.port)) continue;
      const o = document.createElement("option");
      o.value = p.port;
      o.textContent = p.label;
      sel.appendChild(o);
    }
    if ((d.ports || []).length <= 1) sel.style.display = "none";   // 1개면 셀렉터 불필요
    sel.onchange = () => { connectStream(sel.value); refreshStatus(); refreshBuffer(); };
    if (!currentPort && (d.ports || []).length) connectStream(d.ports[0].port);   // refreshStatus가 이미 연결했으면 재연결(화면 리셋) 생략
    refreshStatus();
  } catch (e) { $("port").textContent = "(포트 목록 조회 실패)"; }
}
initPorts();
</script>
</body>
</html>
"""
