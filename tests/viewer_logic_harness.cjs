/* 웹 뷰어 순수 로직(SViewer) 동작 검증 하니스.
   test_viewer_logic.py 가 web_viewer.py 의 VIEWER-PURE 블록을 임시 .cjs 로 추출해
   `node viewer_logic_harness.cjs <module.cjs>` 로 실행한다. 특정 샘플이 아니라
   '일반 패턴'을 검증한다 — 새 펌웨어·태그·키가 와도 깨지지 않게. */
"use strict";
const SV = require(process.argv[2]);
const ESC = String.fromCharCode(27);
const V = { intensity: "normal", json: "compact", fold: true, foldmode: "norm", ansi: true, semantic: true, gap: 2, focus: false };

const fails = [];
function ok(cond, msg) { if (!cond) fails.push(msg); }
function eq(a, b, msg) { if (a !== b) fails.push(msg + " — got " + JSON.stringify(a) + " want " + JSON.stringify(b)); }
function cls(s, meta) { return SV.classifyLine(SV.parseLine(s, meta)); }
function balanced(h) { return (h.match(/<span/g) || []).length === (h.match(/<\/span>/g) || []).length; }

/* 1. ANSI 없는 일반 로그 — 원문 그대로(+escape) */
eq(SV.ansiToHtmlSafe("plain text", { enable: true }), "plain text", "ansi-absent");
ok(SV.ansiToHtmlSafe("a<b>&'\"", { enable: true }).indexOf("<b>") < 0, "ansi-absent-escapes-html");

/* 2. ANSI 있는 로그 — 색 span + 본문 보존 */
{
  const h = SV.ansiToHtmlSafe(ESC + "[31mred" + ESC + "[0m", { enable: true });
  ok(/<span style="[^"]*color:#/.test(h), "ansi-color-span");
  ok(h.indexOf("red") >= 0, "ansi-text-kept");
  ok(balanced(h), "ansi-balanced");
  eq(SV.stripAnsi(ESC + "[31mred" + ESC + "[0m"), "red", "ansi-strip");
  // 256 / truecolor / bg / underline 도 깨지지 않음
  ok(balanced(SV.ansiToHtmlSafe(ESC + "[38;5;208m256" + ESC + "[48;2;10;20;30mtc" + ESC + "[4mU" + ESC + "[0m", { enable: true })), "ansi-256-truecolor");
}

/* 3. reset 누락된 ANSI — span 균형 유지(미닫힘 없음) */
ok(balanced(SV.ansiToHtmlSafe(ESC + "[1m" + ESC + "[32mhi (no reset)", { enable: true })), "ansi-missing-reset-balanced");

/* 4. 잘못된 ANSI 시퀀스 — throw 없이 문자열 반환, 본문 보존 */
{
  const h = SV.ansiToHtmlSafe(ESC + "[999;;;mX" + ESC + "[", { enable: true });
  ok(typeof h === "string" && h.indexOf("X") >= 0 && balanced(h), "ansi-bad-seq");
  ok(typeof SV.ansiToHtmlSafe(ESC + "[", { enable: true }) === "string", "ansi-lone-csi");
  ok(SV.ansiToHtmlSafe(ESC + "[31m<x>", { enable: true }).indexOf("<x>") < 0, "ansi-escapes-between-codes");
}

/* 5. 한 줄 JSON */
{
  const p = SV.findPayload('{"a":1,"b":"x"}');
  ok(p && p.value.a === 1 && p.value.b === "x", "json-oneline");
}

/* 6. prefix + JSON (로그 prefix 가 붙은 JSON) */
{
  const p = SV.findPayload('[Mod-Rx] recv {"id":7,"ok":true}');
  ok(p && p.value.id === 7 && p.value.ok === true, "json-prefixed");
  ok(p && p.pre.indexOf("[Mod-Rx]") >= 0, "json-prefixed-pre");
}

/* 7. 잘린 JSON — null(평문), error 로 과장 안 함 */
eq(SV.findPayload('{"a":1, "b":'), null, "json-truncated-null");
ok(cls('{"a":1, "b":').primary !== "error", "json-truncated-not-error");

/* 8. key=value / key:value */
ok(cls("temp=25 hum=40").scores.json >= 1, "kv-equals");
ok(cls("status: pending level:3").scores.json >= 1, "kv-colon");

/* 9. [TAG] 있는 로그 / 태그 없는 평문 */
{
  const t = SV.renderBodyHTML(SV.parseLine("[SENSOR] reading"), cls("[SENSOR] reading"), V);
  ok(t.indexOf('class="tag"') >= 0 && t.indexOf("SENSOR") >= 0, "tag-rendered");
  const plain = SV.renderBodyHTML(SV.parseLine("just a plain message 12345"), { primary: "", badges: [], payload: null }, V);
  ok(plain.indexOf('class="tag"') < 0, "plain-no-tag");
}

/* 10. error / warning / success / neutral 분류(score 기반) */
eq(cls("E (1234) wifi: connection failed").primary, "error", "cls-error");
eq(cls("W (200) heap getting low, warning").primary, "warning", "cls-warning");
eq(cls("filesystem mount OK, system ready").primary, "success", "cls-success");
eq(cls("the quick brown fox jumps over").primary, "", "cls-neutral");
eq(cls("0 errors found").primary, "", "cls-zero-errors-not-error");

/* 11. 긴 payload — 접기(preview + 펼치기) */
{
  const longJson = '{"data":"' + "x".repeat(400) + '"}';
  const p = SV.findPayload(longJson);
  const h = SV.renderPayloadHTML(p.value, { mode: "compact" });
  ok(h.indexOf("fold") >= 0 && h.indexOf("pfold") >= 0, "long-json-foldable");
  const h2 = SV.renderBodyHTML(SV.parseLine("Z".repeat(700)), { primary: "", badges: [], payload: null }, V);
  ok(h2.indexOf("fold") >= 0, "long-text-foldable");
}

/* 12. exact 반복 / 숫자만 바뀌는 유사 반복 */
eq(SV.normalizeForRepeat(SV.parseLine("same line"), "exact"),
   SV.normalizeForRepeat(SV.parseLine("same line"), "exact"), "repeat-exact");
eq(SV.normalizeForRepeat(SV.parseLine("RSSI=-40 heap=10000 t=00:00:01.000"), "norm"),
   SV.normalizeForRepeat(SV.parseLine("RSSI=-55 heap=20480 t=00:00:09.500"), "norm"), "repeat-normalized");
ok(SV.normalizeForRepeat(SV.parseLine("RSSI=-40"), "norm") !==
   SV.normalizeForRepeat(SV.parseLine("HEAP=40"), "norm"), "repeat-skeleton-differs");

/* 13. timestamp gap 계산 */
eq(SV.tsToMs("00:00:02.000") - SV.tsToMs("00:00:00.000"), 2000, "gap-ms");
eq(SV.tsToMs("not-a-ts"), null, "gap-bad-null");

/* 14. source/port 보존 */
eq(SV.parseLine("x", { source: "COM7" }).source, "COM7", "source-kept");

/* 15. 깨진 문자 / 제어문자 — noise, 빨강 강조 아님 */
{
  const noisy = "��" + String.fromCharCode(1, 2, 3, 4, 5, 6) + "@#$%^&*";
  eq(cls(noisy).primary, "noise", "cls-noise");
  ok(SV.cleanCtrl("a" + String.fromCharCode(1) + "b") === "ab", "cleanCtrl-strips");
  ok(SV.escapeHtml("<script>alert(1)</script>").indexOf("<script") < 0, "escape-injection");
}

/* 16. boot/reset/setup/init 계열 */
eq(cls("rst:0x1 (POWERON_RESET),boot:0x13").primary, "boot", "cls-boot-rst");
eq(cls("WiFi init, starting setup, configuring").primary, "boot", "cls-boot-init");

/* 17. MAC / IP / URL / UUID / hex */
{
  const c = cls("conn ip 192.168.0.1 mac AA:BB:CC:DD:EE:FF");
  ok(c.scores.network >= 2, "net-mac-ip-score");
  ok(c.badges.indexOf("net") >= 0, "net-badge");
  ok(cls("GET https://example.com/api").scores.network >= 1, "net-url");
  ok(cls("uuid 550e8400-e29b-41d4-a716-446655440000 reg").scores.network >= 0, "uuid-no-crash");
}

/* 18. JSON 값의 HTML injection 차단 + correlation key 감지(이름 패턴) */
{
  const p = SV.findPayload('{"requestId":"abc-9","msg":"<img src=x onerror=alert(1)>"}');
  ok(p && SV.renderPayloadHTML(p.value, { mode: "inline" }).indexOf("<img") < 0, "json-value-escaped");
  const corr = SV.correlationBadges(p.value);
  ok(corr.length >= 1 && corr[0].key === "requestId", "correlation-key-detected");
  eq(SV.correlationBadges({ temperature: 25, name: "x" }).length, 0, "correlation-none-when-absent");
}

/* ===== 회귀(가독성 리팩토링 버그 수정) — 샘플 암기 아닌 일반 패턴 가드 ===== */

/* R-A. 접두사(E (/W () 없는 평문 단일 키워드도 분류 — score>=2 신뢰, 동점은 심각도 우선.
   옛 lineLevel 은 키워드 1회로 결정적 분류했으나 confidence 게이트가 이를 neutral 로 떨궜다. */
eq(cls("mount failed").primary, "error", "rg-error-plain-single");        // error(failed) vs boot(mount) 동점 → 심각도상 error
eq(cls("connection error").primary, "error", "rg-error-plain-single2");
eq(cls("battery low warning").primary, "warning", "rg-warn-plain-single");
/* 가드: 무신호·상쇄(0 errors)는 여전히 neutral — 과분류로 본문 가독성 해치지 않게 */
eq(cls("the quick brown fox jumps over").primary, "", "rg-neutral-kept");
eq(cls("0 errors found").primary, "", "rg-zero-errors-kept");

/* R-B. noise 가 있어도 명확한 error 가 더 크면 error 우선(쓰레기에 묻힌 에러를 안 놓침).
   단 순수 쓰레기(error 신호 없음)는 그대로 noise. */
eq(cls("� error fail failure").primary, "error", "rg-error-survives-noise");
eq(cls("��" + String.fromCharCode(1, 2, 3, 4, 5, 6) + "@#$%^&*").primary, "noise", "rg-pure-noise-kept");

/* R-C. 잘린 ANSI truecolor/256 — 오색·깨짐 없이 안전(본문 보존·span 균형·가짜 색 없음). */
{
  const t1 = SV.ansiToHtmlSafe(ESC + "[38;2;200mTRUNC" + ESC + "[0m", { enable: true });
  ok(t1.indexOf("TRUNC") >= 0 && balanced(t1), "rg-ansi-trunc-tc-safe");
  ok(t1.indexOf("color:") < 0, "rg-ansi-trunc-tc-no-bogus-color");        // #c80000 같은 가짜 색 금지
  const t2 = SV.ansiToHtmlSafe(ESC + "[38;5mNOIDX" + ESC + "[0m", { enable: true });
  ok(t2.indexOf("NOIDX") >= 0 && balanced(t2) && t2.indexOf("color:") < 0, "rg-ansi-trunc-256-safe");
}

/* R-D. 비정상 opener 폭주/초장문 — neutral(null) 안전 fallback(행 폭주·O(n^2) 방어). */
eq(SV.findPayload("{".repeat(5000)), null, "rg-payload-pathological-null");
ok(SV.findPayload("[".repeat(100)) === null, "rg-payload-many-openers-null");

/* ===== 토폴로지 그래프 순수로직(모듈8 ① edges 링크선) — DOM 비의존 계산만 ===== */

/* T-1. rssiColor: 강(-30)~약(-90) 그라디언트 색, null/NaN=중립, 범위 밖은 클램프(throw 없음). */
{
  const isColor = s => typeof s === "string" && /^(#|rgb|hsl)/.test(s);
  ok(isColor(SV.rssiColor(-40)), "rssi-color-strong-is-color");
  ok(isColor(SV.rssiColor(-85)), "rssi-color-weak-is-color");
  eq(SV.rssiColor(null), SV.rssiColor(undefined), "rssi-color-null-undefined-same");
  ok(SV.rssiColor(null) !== SV.rssiColor(-40), "rssi-color-null-differs-from-signal");
  ok(SV.rssiColor(-35) !== SV.rssiColor(-88), "rssi-color-strong-vs-weak-differ");
  ok(isColor(SV.rssiColor(-5)) && isColor(SV.rssiColor(-120)), "rssi-color-clamps-out-of-range");
  ok(isColor(SV.rssiColor("-40")), "rssi-color-numeric-string");   // JSON 이 문자열로 줄 수도
}

/* T-2. edgeSegments: 노드 mac ↔ edge from/to 매칭 → 노드 중심좌표 선분. 미매칭 mesh 엣지는 skip. */
{
  const placed = [
    { n: { mac: "AA", id: "mac:AA" }, x: 0, y: 0, w: 100, h: 60 },
    { n: { mac: "BB", id: "mac:BB" }, x: 200, y: 0, w: 100, h: 60 },
  ];
  const edges = [
    { from: "AA", to: "BB", rssi: -40, fresh: true },
    { from: "AA", to: "ZZ", rssi: -50, fresh: false },   // ZZ 노드 배치에 없음 → 그릴 수 없어 skip
  ];
  const segs = SV.edgeSegments(placed, edges);
  eq(segs.length, 1, "edge-seg-skips-unmatched");
  eq(segs[0].x1, 50, "edge-seg-x1-center");      // 0 + 100/2
  eq(segs[0].y1, 30, "edge-seg-y1-center");      // 0 + 60/2
  eq(segs[0].x2, 250, "edge-seg-x2-center");     // 200 + 100/2
  eq(segs[0].y2, 30, "edge-seg-y2-center");
  eq(segs[0].rssi, -40, "edge-seg-rssi-kept");
  eq(segs[0].fresh, true, "edge-seg-fresh-kept");
}

/* T-3. edgeSegments 방어: null/빈 입력·mac 없는 노드·자기루프 안전(throw 없이 skip). */
{
  eq(SV.edgeSegments([], []).length, 0, "edge-seg-empty");
  eq(SV.edgeSegments(null, null).length, 0, "edge-seg-null-safe");
  const noMac = [{ n: { id: "port:COM4" }, x: 0, y: 0, w: 100, h: 60 }];
  eq(SV.edgeSegments(noMac, [{ from: "AA", to: "BB" }]).length, 0, "edge-seg-node-without-mac-skipped");
  const p2 = [{ n: { mac: "AA" }, x: 0, y: 0, w: 100, h: 60 }];
  eq(SV.edgeSegments(p2, [{ from: "AA", to: "AA" }]).length, 0, "edge-seg-self-loop-skipped");
}

/* ===== 토폴로지 홉 애니메이션 순수로직(모듈8 ② hop) — DOM 비의존 ===== */

/* T-4. hopWaypoints: hop.path(노드명 시퀀스) ↔ 배치노드 label 매칭 → 중심좌표 waypoint.
   미매칭 이름은 건너뛴다(부분 경로라도 그린다). path 는 이름 기반(edges 의 mac 과 다른 축). */
{
  const placed = [
    { n: { label: "SB5", mac: "AA" }, x: 0, y: 0, w: 100, h: 60 },
    { n: { label: "REP1", mac: "BB" }, x: 200, y: 0, w: 100, h: 60 },
    { n: { label: "SSM", mac: "CC" }, x: 400, y: 0, w: 100, h: 60 },
  ];
  const wps = SV.hopWaypoints(placed, ["SB5", "REP1", "SSM"]);
  eq(wps.length, 3, "hop-wp-full-path");
  eq(wps[0].x, 50, "hop-wp-x-center");            // 0 + 100/2
  eq(wps[0].y, 30, "hop-wp-y-center");            // 0 + 60/2
  eq(wps[0].name, "SB5", "hop-wp-name-kept");
  eq(wps[2].x, 450, "hop-wp-last");               // 400 + 100/2
  const partial = SV.hopWaypoints(placed, ["SB5", "GHOST", "SSM"]);
  eq(partial.length, 2, "hop-wp-skips-unmatched");    // GHOST 는 배치에 없음 → skip
  eq(partial[1].name, "SSM", "hop-wp-partial-order");
  eq(SV.hopWaypoints(null, null).length, 0, "hop-wp-null-safe");
  eq(SV.hopWaypoints(placed, []).length, 0, "hop-wp-empty-path");
}

/* T-5. hopColor: ok=성공(초록), ok:false+timeout=실패, 그 외 confidence=미확정. 서로 다른 색.
   ⚠️ 시각(ts) 없음 — 순서/시간 추론 금지, ok·confidence 로만 상태 판단(#1 제약). */
{
  const isColor = s => typeof s === "string" && /^(#|rgb|hsl)/.test(s);
  ok(isColor(SV.hopColor({ ok: true, confidence: "observed" })), "hop-color-ok-is-color");
  ok(SV.hopColor({ ok: true }) !== SV.hopColor({ ok: false, confidence: "timeout" }), "hop-color-ok-vs-fail-differ");
  ok(SV.hopColor({ ok: false, confidence: "timeout" }) !== SV.hopColor({ ok: false, confidence: "unconfirmed" }),
     "hop-color-fail-vs-unconfirmed-differ");
  ok(isColor(SV.hopColor(null)), "hop-color-null-safe");
}

/* ===== 토폴로지 홉 디테일 패널 순수로직(모듈8 ③) — DOM 비의존 ===== */

/* T-6. hopDetail: 홉 → 패널 모델(경로 칩·성패·confidence·RTT). path 비면 src_name 폴백.
   ⚠️ 시각 없음(#1 제약) — 순서/시간차 표현 금지, ok·confidence 로만 상태 판단. */
{
  const d = SV.hopDetail({ ok: true, confidence: "observed", path: ["SB5", "SSM"], rtt_ms: 61, device_type: "SB" });
  eq(d.chips.length, 2, "hop-detail-chips");
  eq(d.chips[0], "SB5", "hop-detail-chip-order");
  eq(d.status, "ok", "hop-detail-status-ok");
  eq(d.rtt, "61ms", "hop-detail-rtt");
  eq(d.deviceType, "SB", "hop-detail-devtype");
  eq(SV.hopDetail({ ok: false, confidence: "timeout", path: ["SB1"] }).status, "fail", "hop-detail-fail");
  eq(SV.hopDetail({ ok: false, confidence: "unconfirmed", path: [], src_name: "SB1" }).status, "pending", "hop-detail-pending");
  eq(SV.hopDetail({ ok: false, confidence: "unconfirmed", path: [], src_name: "SB1" }).chips[0], "SB1", "hop-detail-srcname-fallback");
  eq(SV.hopDetail({ ok: true, path: ["A"], rtt_ms: null }).rtt, null, "hop-detail-no-rtt");
  eq(SV.hopDetail(null), null, "hop-detail-null-safe");
  // status 별 색이 서로 달라야(성공·실패·미확정 구분)
  ok(SV.hopDetail({ ok: true, path: ["A"] }).color !== SV.hopDetail({ ok: false, confidence: "timeout", path: ["A"] }).color, "hop-detail-color-differs");
}

if (fails.length) { console.error("FAILURES (" + fails.length + "):\n" + fails.map(f => " - " + f).join("\n")); process.exit(1); }
console.log("all viewer-logic assertions passed");
