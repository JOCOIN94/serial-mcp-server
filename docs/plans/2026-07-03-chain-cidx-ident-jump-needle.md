# 체인 Cidx 키 ident 보강 + 점프 니들 폴백 — 구현 지시서 (Codex 핸드오프)

상태: 구현 대기 (설계 확정 — 재설계 금지, 이 문서대로만 구현)
설계: Claude (2026-07-03), 근거는 펌웨어 소스·실장비 로그로 검증 완료
구현: Codex
검증·리뷰: Claude (구현 완료 후 헤드리스 실장비 검증은 Claude 몫 — Codex 범위 아님)

---

## 0. 절대 규칙 (보상 해킹 방지 — 위반 시 작업 무효)

1. **기존 테스트 삭제·약화·skip·xfail 처리 금지.** 기존 테스트 수정은 §5.1의 허용 목록 3건만 가능하다.
2. **수정 파일 범위 고정**: `src/serial_mcp/topology_chains.py`, `src/serial_mcp/web_viewer.py`, `tests/test_topology_chains.py`, `tests/test_topology_engine.py`, `tests/viewer_logic_harness.cjs` — 이 5개 외 어떤 파일도 만지지 않는다(버전 bump·릴리스·SPEC 수정 금지, 별도 단계임).
3. **TDD 순서 강제**: §5의 신규 테스트를 먼저 작성 → 실패 확인 → §3·§4 구현 → 전체 green. 테스트를 구현에 맞춰 고치는 방향 금지(허용 목록 제외).
4. 검증 명령 (이 PC 전용 주의사항 포함):
   - 문법: `py -m compileall -q src` (`python` 명령은 Windows Store 별칭이라 동작 안 함)
   - 테스트: `uv run python -m pytest -q` (**`uv run pytest`는 이 PC에서 trampoline 오류로 깨짐 — 반드시 `python -m pytest` 형태**)
   - node 하니스는 pytest(test_viewer_logic.py)가 자동 실행하므로 별도 명령 불필요.
5. `web_viewer.py`의 JS는 `_HTML = r"""..."""` 원시 문자열 안에 있다 — 문자열 경계·이스케이프를 깨지 않게 편집하고, f-string 으로 바꾸지 않는다.
6. 커밋은 한국어 + Conventional Commits(§7). stdout 출력 금지 원칙 등 AGENTS.md 공통 규칙 준수.

---

## 1. 배경 — 무엇이 왜 문제인가

### 1.1 대상 시스템 요약

- serial-mcp 서버는 임베디드 메시 장비(SSM=게이트웨이, SB=베이, REP=리피터, APU)의 USB 시리얼 콘솔을 포트별로 읽어, 토폴로지 엔진(`topology_engine.py`)이 줄→이벤트(`topology_events.py`)→체인 로그(`topology_chains.py`)로 가공한다.
- 웹 뷰어(`web_viewer.py` 내 인라인 JS)는 체인 로그 행마다 노드 칩과 ▸ 점프 버튼을 그린다. ▸ 클릭 시 `jumpToPortLog(port, key, ts)`가 그 포트의 버퍼에서 **원문 부분문자열(니들) 검색 + 체인 첫 관측시각(ts, epoch s) 30초 근접 매칭**으로 해당 라인에 스크롤한다. 실패하면 ▸가 회색(miss)이 된다.
- 체인 키 2종: 상행 `("u", ident, unique)` (ident=UnID 또는 Mac), Cidx 계열 `("c", cidx)`.

### 1.2 확정된 펌웨어 계약 (소스 검증 완료 — 이 계약이 설계의 유일한 전제)

4개 펌웨어(SSM_esp32, SB_ESP32, Repeat_esp32, APU_C_SLIM_esp32)가 같은 `sendMessage(String)` 코드를 쓴다:

```cpp
// SB_ESP32.ino sendMessage() 발췌 (REP·APU 동일, SSM은 Rev 없이 Cidx만)
if(fBypass == false)
{
  jsonWiFiTxBuf["Rev"] = true;          // SSM 펌웨어에는 이 줄이 없음
  jsonWiFiTxBuf["Cidx"] = TxInx_Cnt;    // 자기 송신 카운터(부팅 후 0부터 증가)
  TxInx_Cnt++;
  serializeJson(jsonWiFiTxBuf, outgoing);
}
```

여기서 도출되는 사실:

- **F1. 송신자 콘솔의 TX 라인에는 Cidx가 원리적으로 없다.** 호출자는 `sendMessage()` 호출 전 직렬화본을 찍고, Rev/Cidx는 함수 내부에서 전파 직전에 붙는다(함수 내부의 콘솔 출력은 전부 주석 처리됨).
- **F2. Cidx는 장비별 독립 카운터다.** 값 공간이 장비마다 다르고, 동시에 전원 켠 장비들은 카운터 대역이 비슷하게 움직인다.
- **F3. 수신자는 받은 JSON 문자열을 그대로 찍는다.** 즉 수신측 라인 = 송신측 콘솔 라인의 JSON + `,"Rev":true`(비-SSM 송신자만) + `,"Cidx":N` 을 **끝에 append**한 것(ArduinoJson은 새 키를 뒤에 붙이고 공백 없이 직렬화).
- **F4. REP 중계는 bypass다.** `fBypass = true; sendMessage(sWiFiRx)` — Rev/Cidx 재부착 없이 받은 원문 바이트 그대로 재전송하고, 콘솔에 `[Data_Pass] {원문}` 을 찍는다. 따라서 멀티홉이어도 메시지 바이트는 전 구간 동일하다.
- **F5. 예외**: `REGMAC`/`SPECIAL` 키가 있는 메시지는 Cidx 미부착(fexept) — 이 경우 "c" 체인 자체가 안 생기므로 본 작업과 무관.

실장비 검증 예 (2026-07-02, SSM=COM4·SB=COM12):

| 관측 | 라인 |
|---|---|
| SB 송신 콘솔(COM12) | `{"UnID":5,"Stat":"OK","Asn":58}` — Cidx 없음 (F1) |
| SSM 수신 콘솔(COM4) | `[Proc-WiFiRx] {"UnID":5,"Stat":"OK","Asn":58,"Rev":true,"Cidx":4520}` (F3) |
| SSM 송신 콘솔(COM4) | `[Proc_WiFiTx] Ask Info : To. SB1, {"RTC":[38,35,20,2,7,2026],"CHANNEL":"11","INFO":"REQ","UnID":5}` — Cidx 없음 |
| SB 수신 콘솔(COM12) | `[WiFi_Rx] {"UnID":5,"REQRSSI":"REQ","Rng":[0,4],"Unique":57,"Cidx":4703}` — SSM은 Rev 없이 Cidx만 |

### 1.3 고칠 문제 2건

**P1 — "c" 체인의 송신측 ▸ 점프가 항상 회색.**
"c" 체인의 뷰어 점프 니들은 `"Cidx":<값>` 하나인데, F1에 의해 **송신측 포트 버퍼에는 그 문자열이 존재한 적이 없다**. 송신측 src 칩은 42b2dd3 커밋이 membership 역해소로 포트를 부착해 준 추론 노드(inferred)라 ▸ 버튼은 생기지만, 클릭하면 원리적으로 매칭 불가 → 항상 회색. (수신측·중계측 칩은 Cidx가 찍히므로 정상 점프 — "되는 것과 안 되는 것이 섞여 보이는" 증상의 원인.)

**P2 — `("c", cidx)` 키가 발신자를 구분하지 않아 오병합 위험.**
현재 키는 Cidx 값 하나뿐이다. 같은 그룹에서 서로 다른 두 장비의 Cidx가 15초 윈도(`window_s`) 안에 같은 값이면 서로 다른 메시지 두 개가 한 체인으로 병합된다. F2에 의해 장비가 늘수록(특히 동시 전원 인가) 충돌 확률이 실질적으로 오른다. 지금은 장비 2대라 잠복 중.

---

## 2. 설계 결정 (고정 — 변경 금지)

**D1. 키 확장**: `("c", cidx)` → `("c", ident, cidx)`. ident = 이벤트 ids의 `unid`, 없으면 `mac`, 둘 다 없으면 `None`. F3·F4에 의해 같은 메시지를 관측한 모든 포트의 이벤트는 같은 ids를 가지므로 키 일관성이 보장된다. `("u", ident, unique)`와 동형이 되어 `_entry_ident`도 단일화된다.

**D2. 점프 니들 공개**: "c" 체인 엔트리에, 관측 이벤트의 **raw 라인에서 `,"Rev":true` 와 `,"Cidx":<숫자>` 조각만 제거한 JSON 부분문자열**을 `needle`로 저장·공개한다. F3에 의해 이 문자열은 송신측 콘솔 라인과 (직결·멀티홉 불문) 문자 그대로 일치하거나 부분문자열로 포함된다. 파싱된 dict의 재직렬화가 아니라 **raw 텍스트 조작**이어야 한다(포맷 왜곡 방지).

**D3. 뷰어 폴백 검색**: `jumpToPortLog`는 니들 시도 목록(attempts)을 순서대로 검색한다 — "c" 키는 1차 `"Cidx":<cidx>`, 2차 `needle`. 1차가 0매칭이거나 30초 앵커 밖이면 2차로 넘어간다. 기존 30초 시간앵커 규칙은 각 시도에 동일 적용(오착지 이중 안전망). "u" 키는 기존 1차 니들 유지 + needle이 있으면 폴백에 추가(현재 백엔드는 "c"만 needle을 실으므로 사실상 무영향, 미래 호환).

**D4. 회색 의미 정정**: 그래도 실패하면(송신 장비 `fSerial=false`로 콘솔 미출력, 또는 링버퍼에서 밀림) 회색이 정당하다. 툴팁 문구를 실제 의미로 바꾼다.

**비범위(하지 말 것)**: 하행 브로드캐스트 rx 노드 폭증 정리(별도 과제), 릴리스/버전 동기화, SPEC.md 수정, correlator·engine 로직 변경.

---

## 3. 백엔드 구현 명세 — `src/serial_mcp/topology_chains.py`

줄 번호는 변할 수 있으니 **심볼로 찾아라**. 이 파일은 순수 로직 모듈(시리얼/MCP 비의존)이다 — 그 성질을 유지한다.

### 3.1 `_event_key` — "c" 분기에 ident 추가

현재:

```python
    cidx = ids.get("cidx")
    if cidx is not None:
        return ("c", cidx), "down"
```

변경(“u” 분기의 ident 해소와 동일 패턴, 단 ident 없음 허용):

```python
    cidx = ids.get("cidx")
    if cidx is not None:
        ident = ids.get("unid")
        if ident is None:
            ident = ids.get("mac")
        return ("c", ident, cidx), "down"
```

ident가 `None`이어도 키를 만든다(UnID/Mac 없는 브로드캐스트 — 오늘 동작과 동일한 폴백).

### 3.2 니들 추출 헬퍼 — 모듈 레벨에 신규 추가

```python
_RE_NEEDLE_REV = re.compile(r',"Rev":true(?=[,}])')
_RE_NEEDLE_CIDX = re.compile(r',"Cidx":\d+(?=[,}])')


def _jump_needle(ev: dict) -> Optional[str]:
    """관측 raw 라인에서 sendMessage 부착분(Rev/Cidx)만 벗긴 점프 니들.

    펌웨어 계약: 수신 라인 = 송신 콘솔 JSON + ,"Rev":true(비-SSM) + ,"Cidx":N append
    (공백 없는 serializeJson, REP 중계는 bypass 무변형). 벗긴 결과는 송신측 콘솔
    라인과 원문 일치하므로 뷰어가 송신측 버퍼에서 부분문자열 검색에 쓴다.
    """
    for line in (ev or {}).get("raw_lines") or []:
        if '"Cidx"' not in line:
            continue
        start = line.find("{")
        end = line.rfind("}")
        if start == -1 or end <= start:
            continue
        frag = _RE_NEEDLE_REV.sub("", line[start:end + 1])
        return _RE_NEEDLE_CIDX.sub("", frag)
    return None
```

`re`·`Optional`은 이미 임포트돼 있는지 확인하고 없으면 추가.

### 3.3 `observe()` — `_ident` 저장 블록을 니들 저장으로 교체

현재 (`ent["_seen"].add(seen_key)` 다음):

```python
        if ent.get("_ident") is None:
            ent["_ident"] = self._event_ident(ev)   # "c" 키(Cidx)도 이벤트 ids 로 발신자 ident 를 안다
```

변경 — ident는 D1로 키에 들어가므로 `_ident` 저장은 폐기하고, "c" 키일 때 니들을 1회 저장(첫 성공 관측 고정):

```python
        if key[0] == "c" and ent.get("_needle") is None:
            ent["_needle"] = _jump_needle(ev)   # 송신측 점프 폴백 니들(첫 관측 고정)
```

### 3.4 `_new_entry` — 내부 필드 교체

`"_ident": None,` → `"_needle": None,`

### 3.5 `_entry_ident` — 키 단일화

현재:

```python
    @staticmethod
    def _entry_ident(ent: dict):
        """항목의 발신자 ident — "u" 키는 키 자체, "c" 키(Cidx)는 관측 이벤트 ids 에서 저장한 값."""
        key = ent.get("key") or (None,)
        if key[0] == "u":
            return key[1]
        return ent.get("_ident")
```

변경:

```python
    @staticmethod
    def _entry_ident(ent: dict):
        """항목의 발신자 ident — "u"/"c" 모두 key[1] (D1: 두 키가 동형)."""
        key = ent.get("key") or (None,)
        if key[0] in ("u", "c"):
            return key[1]
        return None
```

`_event_ident`는 `_observe_wifirx`가 계속 쓰므로 **삭제하지 않는다**.

### 3.6 `_public` — needle 공개

반환 dict에 한 줄 추가(“ts” 항목 근처):

```python
            "needle": ent.get("_needle"),   # "c" 체인 송신측 점프 폴백(§D2) — "u" 는 None
```

`recent()`·`sweep()`·SSE 발행·`get_topology` recent_chains 는 전부 `_public` 단일 원천이라 자동 전파된다 — 다른 곳 수정 금지.

---

## 4. 뷰어 구현 명세 — `src/serial_mcp/web_viewer.py` (인라인 JS)

### 4.1 `chainRow` (VIEWER-PURE 구역, `function chainRow(entry, labels)`)

반환 객체에 passthrough 한 줄 추가 (`ts:` 항목 옆):

```js
    needle: e.needle || null,
```

### 4.2 `jumpFromChip` (체인 패널 IIFE 내부)

`jumpToPortLog` 호출에 needle 전달 + miss 툴팁 문구 정정:

```js
  async function jumpFromChip(btn, row, chip) {
    const found = window.jumpToPortLog ? await window.jumpToPortLog(chip.port, row.key, row.ts, row.needle) : false;
    if (!found) {
      _jumpMiss.add(row.id + "|" + chip.port);
      btn.classList.add("miss");
      btn.title = chip.port + " 로그에서 못 찾음(콘솔 미출력 또는 버퍼에서 밀림)";
    }
  }
```

### 4.3 `window.jumpToPortLog` — 시도 목록 구조로 재편

현재 시그니처 `(port, key, ts)` → `(port, key, ts, needle)`. 니들 구성과 검색 루프를 아래 구조로 바꾼다. **주의: "c" 키의 cidx는 이제 `key[2]`다(D1). 기존 `key[1]` 참조를 남기면 안 된다.**

```js
/* 체인 칩 점프 — 니들 시도 목록을 순서대로 버퍼 검색. "c" 1차=Cidx 원문, 2차=needle
   (송신측 콘솔엔 Cidx 가 안 찍히는 펌웨어 계약 — sendMessage 가 전파 직전 부착).
   각 시도에 첫 관측시각(ts) 30s 근접 매칭 동일 적용(Unique 1..99 롤링 오착지 방지). */
window.jumpToPortLog = async function (port, key, ts, needle) {
  if (!port || !key || !key.length) return false;
  const attempts = [];
  if (key[0] === "u") {
    const ns = [];
    if (key[2] != null) ns.push('"Unique":' + key[2]);
    if (typeof key[1] === "string") ns.push(key[1]);          // mac ident 는 원문 그대로
    else if (key[1] != null) ns.push('"UnID":' + key[1]);
    if (ns.length) attempts.push(ns);
  } else if (key[0] === "c") {
    if (key[2] != null) attempts.push(['"Cidx":' + key[2]]);
  }
  if (needle) attempts.push([needle]);                        // 송신측 폴백(백엔드 needle 공개분)
  if (!attempts.length) return false;
  let anchorMs = null;                                        // 체인 관측 시각(epoch s) → 하루-내-ms
  if (ts != null) {
    const d = new Date(ts * 1000);
    anchorMs = ((d.getHours() * 60 + d.getMinutes()) * 60 + d.getSeconds()) * 1000 + d.getMilliseconds();
  }
  setFollow(false, { noScroll: true });                       // 점프 = 과거 조회 — 팔로우 해제 후 이동
  if (state.port !== port) selectPort(port);
  setTab("buffer");
  await refreshBuffer();
  const box = $("buffer");
  const rows = box.querySelectorAll("[data-raw]");
  function scan(needles) {                                    // 30s 밖 매칭은 실패로 쳐 다음 시도로
    let best = null, bestDiff = Infinity;
    for (let i = rows.length - 1; i >= 0; i--) {
      const raw = rows[i].dataset.raw || "";
      if (!needles.every(n => raw.indexOf(n) !== -1)) continue;
      if (anchorMs == null) return rows[i];                   // 앵커 없음(구버전 행) — 최신 매칭
      const rowMs = SV.tsToMs(rows[i].dataset.ts);
      const diff = rowMs == null ? Infinity : Math.abs(rowMs - anchorMs);
      if (diff < bestDiff) { bestDiff = diff; best = rows[i]; }
    }
    return bestDiff > 30000 ? null : best;
  }
  let best = null;
  for (const needles of attempts) {
    best = scan(needles);
    if (best) break;
  }
  if (!best) return false;
  SVScroll.cancelPin(box);
  const bb = box.getBoundingClientRect(), rb = best.getBoundingClientRect();
  box.scrollTop += rb.top - bb.top - box.clientHeight / 2 + rb.height / 2;
  best.classList.remove("ln-jump");
  void best.offsetWidth;                                      // 강조 애니메이션 재트리거
  best.classList.add("ln-jump");
  return true;
};
```

의미 변화 주의점(의도된 것): 기존 코드는 1차 니들이 30초 밖 매칭이면 그대로 실패였지만, 이제 그 경우 **다음 시도(needle)로 넘어간다**. `best`가 없으면(`bestDiff===Infinity`) `bestDiff > 30000`이 참이라 null 반환 — 별도 분기 불필요.

---

## 5. 테스트 명세 (구현 전에 먼저 작성 — 실패 확인 필수)

### 5.1 기존 테스트 수정 허용 목록 (이 3건 외 기존 테스트 변경 금지)

1. `tests/test_topology_chains.py`의 `ev()` 헬퍼에 `raw_lines=None` 파라미터 추가 → dict의 `"raw_lines": list(raw_lines or [])`.
2. 키 2-튜플 가정 갱신: `rg '\("c",|\["c",' tests/` 로 전수 검색해 `("c", <cidx>)` 형태 assertion을 `("c", <ident>, <cidx>)`로 갱신한다. 이 문서 작성 시점에 알려진 곳: `tests/viewer_logic_harness.cjs`의 `SV.chainRow({ id: 9, key: ["c", 3028], ... })` → `key: ["c", null, 3028]`, `eq(jr.key.join(","), "c,,3028", ...)`. 파이썬 테스트 쪽도 검색 결과에 따라 동일 원칙으로 갱신(값 의미는 바꾸지 말고 ident 자리만 추가 — 해당 테스트의 ev()가 넣는 unid 값을 그대로 쓴다).
3. `tests/viewer_logic_harness.cjs`에 needle passthrough 검증 추가(§5.3).

참고: 기존 `test_uplink_cidx_ack_attaches_src_port_via_event_ident`(42b2dd3)는 **무수정으로 계속 통과해야 한다** — D1 이후 src 포트 부착이 `_ident` 대신 key[1]로 동작함을 증명하는 회귀 신호다.

### 5.2 신규 — `tests/test_topology_chains.py`

```python
def test_cidx_key_carries_ident_and_separates_same_cidx_senders():
    # F2: Cidx 는 장비별 카운터라 값 충돌이 가능 — ident 를 키에 넣어 다른 장비의
    # 같은 Cidx 가 한 체인으로 오병합되지 않아야 한다(P2).
    log = ChainLog(window_s=10)
    e1 = log.observe(ev("wifirx", "COM12", ts=1.0, unid=5, unique=None, cidx=100,
                        json_obj={"UnID": 5, "Asn": 1, "Cidx": 100}))[0]
    e2 = log.observe(ev("wifirx", "COM13", ts=1.5, unid=7, unique=None, cidx=100,
                        json_obj={"UnID": 7, "Asn": 2, "Cidx": 100}))[0]
    assert e1["key"] == ["c", 5, 100]
    assert e2["key"] == ["c", 7, 100]
    assert e1["id"] != e2["id"]


def test_cidx_key_without_ident_still_chains():
    # UnID/Mac 없는 브로드캐스트 — ident=None 폴백으로 기존 동작 유지.
    log = ChainLog(window_s=10)
    entry = log.observe(ev("wifirx", "COM12", ts=1.0, unid=None, unique=None, cidx=200))[0]
    assert entry["key"] == ["c", None, 200]


def test_public_needle_strips_rev_and_cidx():
    # D2: 수신 raw 에서 sendMessage 부착분만 벗기면 송신측 콘솔 라인과 원문 일치(F3).
    raw = '[WiFi_Rx] {"UnID":5,"Stat":"OK","Asn":58,"Rev":true,"Cidx":4520}'
    log = ChainLog(window_s=10)
    entry = log.observe(ev("wifirx", "COM4", ts=1.0, unid=5, unique=None, cidx=4520,
                           raw_lines=[raw]))[0]
    assert entry["needle"] == '{"UnID":5,"Stat":"OK","Asn":58}'


def test_public_needle_without_rev_ssm_sender():
    # SSM 펌웨어는 Rev 없이 Cidx 만 부착 — Rev 제거가 선택적이어야 한다.
    raw = '[Proc-WiFiRx] {"CHPLAN":[1,"00"],"Asn":58,"UnID":5,"Cidx":4704}'
    log = ChainLog(window_s=10)
    entry = log.observe(ev("rx", "COM12", ts=1.0, unid=5, unique=None, cidx=4704,
                           raw_lines=[raw]))[0]
    assert entry["needle"] == '{"CHPLAN":[1,"00"],"Asn":58,"UnID":5}'


def test_needle_fixed_on_first_observation():
    # 니들은 첫 성공 관측으로 고정 — 이후 관측(Rev 포함본)이 와도 안 바뀐다(안정 앵커).
    log = ChainLog(window_s=10)
    log.observe(ev("wifirx", "COM4", ts=1.0, unid=5, unique=None, cidx=300,
                   raw_lines=['[WiFi_Rx] {"UnID":5,"A":1,"Cidx":300}']))
    log.observe(ev("rx", "COM13", ts=1.2, unid=5, unique=None, cidx=300,
                   raw_lines=['[Proc-WiFiRx] {"UnID":5,"A":1,"Rev":true,"Cidx":300}']))
    assert log.recent(5)[-1]["needle"] == '{"UnID":5,"A":1}'


def test_u_key_entry_has_no_needle():
    # "u" 체인은 양측 콘솔에 키가 찍혀 폴백이 불필요 — needle 미추출(None) 확인.
    log = ChainLog(window_s=10)
    entry = log.observe(ev("tx", "COM12", ts=1.0), port_names={"COM12": "SB5"})[0]
    assert entry["needle"] is None
```

### 5.3 신규 — `tests/test_topology_engine.py` (통합 경계 테스트 — 필수)

이전 ts 버그(단조시각 vs epoch)는 양측 단위 테스트가 각자 green인데 경계가 안 맞아 생겼다. 같은 실수 방지용으로 **raw 텍스트 → 엔진 → 공개 체인**을 관통 검증한다:

```python
def test_engine_publishes_cidx_ident_key_and_needle_from_raw_line():
    # 실장비 원문(2026-07-02 COM4 실측) 그대로 관통 — assembler 가 raw_lines 를 이벤트에
    # 싣고 ChainLog 가 needle/ident 키를 공개하는 경계를 검증한다.
    eng = TopologyEngine()
    eng.observe("COM4", 1.0, '[Proc-WiFiRx] {"UnID":5,"Stat":"OK","Asn":58,"Rev":true,"Cidx":4520}')
    eng.flush()
    updates = eng.drain_chain_updates()
    chain = next(c for c in updates if c["key"] == ["c", 5, 4520])
    assert chain["needle"] == '{"UnID":5,"Stat":"OK","Asn":58}'
```

만약 이 테스트가 "needle이 None"으로 실패하면 원인은 assembler가 이벤트에 `raw_lines`를 안 싣는 경우다 — 그때는 `topology_events.py`를 고치는 게 아니라 **작업을 멈추고 실패 내용을 보고**하라(수정 범위 밖, 설계 재검토 필요).

### 5.4 신규 — `tests/viewer_logic_harness.cjs`

기존 chainRow 테스트 블록 옆에 추가:

```js
  eq(SV.chainRow({ id: 12, needle: '{"UnID":5,"Asn":58}', nodes: [] }, {}).needle,
     '{"UnID":5,"Asn":58}', "chain-row-needle-passthrough");
  eq(SV.chainRow({ id: 13, nodes: [] }, {}).needle, null, "chain-row-needle-default-null");
```

`jumpToPortLog` 자체는 DOM 의존이라 하니스 범위 밖 — 실장비 헤드리스 검증은 Claude가 수행한다(§0).

---

## 6. 완료 기준 체크리스트

- [ ] §5 신규 테스트가 구현 전 실패, 구현 후 통과
- [ ] `py -m compileall -q src` 무출력 통과
- [ ] `uv run python -m pytest -q` 전체 green (node 하니스 포함)
- [ ] `rg '_ident' src/serial_mcp/topology_chains.py` 결과 없음 (필드 완전 제거; `_event_ident`·`_entry_ident` 함수명은 남는 게 정상)
- [ ] `rg 'key\[1\]' src/serial_mcp/web_viewer.py` 의 "c" 분기 잔존 참조 없음 (cidx는 key[2])
- [ ] 수정 파일이 §0.2의 5개뿐 (`git status`로 확인)

## 7. 커밋

전부 한 커밋으로(키 확장과 니들이 같은 엔트리 구조 변경을 공유):

```
fix: 체인 Cidx 키 ident 보강 + 점프 니들 폴백 — 송신측 ▸ 회색 해소

"c" 체인의 송신측 점프는 원리적으로 불가능했다 — 펌웨어 sendMessage 가
Rev/Cidx 를 전파 직전 부착하므로(4개 펌웨어 공통, SSM 은 Cidx 만) 송신자
콘솔 TX 라인에는 Cidx 가 없다. 수신 raw 에서 그 부착분만 벗긴 needle 을
공개해 뷰어가 1차 Cidx 니들 실패 시 폴백 검색한다(REP 중계는 bypass
무변형이라 멀티홉에서도 원문 일치).

("c", cidx) 키는 발신자 무구분이라 장비 증설 시 같은 그룹 내 Cidx 값
충돌이 오병합을 낳는다 — ("c", ident, cidx) 로 확장해 "u" 키와 동형화
(_ident 내부 필드 폐기, _entry_ident 단일화).

설계·근거: docs/plans/2026-07-03-chain-cidx-ident-jump-needle.md
```

(뒤에 Claude Code 서명 트레일러는 붙이지 않는다 — 구현 주체가 Codex임을 감안해 트레일러는 핸드오프 운영 규칙에 따름.)
