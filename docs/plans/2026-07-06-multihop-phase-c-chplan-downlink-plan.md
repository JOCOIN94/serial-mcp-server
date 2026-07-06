# Phase C — 하행 CHPLAN 인식·체인 합류 (topology_events/chains/server 게이트) — 구현 지시서 (Codex 핸드오프)

상태: 구현 대기 (설계 확정 — 재설계 금지, 이 문서대로만 구현)
설계: Claude (2026-07-06). 전제 문서: `2026-07-03-multihop-relay-evidence-masterplan.md`(원칙 D1~D6·D4-1) + `2026-07-03-multihop-phase-a-firmware-findings.md`(펌웨어 사실 대장, 특히 XXX-1/5/6). 이 지시서는 자족적으로 쓰였지만, 판단이 갈리면 위 두 문서가 우선한다.
구현: Codex
검증·리뷰: Claude (구현 완료 후 리뷰. 배포 SSM(SSM260702-002)의 `[Route] CHPLAN to` 실제 문구 실측·실장비 검증은 Claude 몫 — Codex 범위 아님)

---

## 0. 절대 규칙 (보상 해킹 방지 — 위반 시 작업 무효)

1. **기존 테스트 삭제·약화·skip·xfail·수정 금지.** 기존 테스트 수정 허용 목록은 **없다**(0건). 기존 테스트와 이 설계가 충돌한다고 판단되면 구현을 멈추고 보고한다.
2. **수정 파일 범위 고정** (이 목록 외 어떤 파일도 만지지 않는다):
   - `src/serial_mcp/topology_events.py`
   - `src/serial_mcp/topology_chains.py`
   - `src/serial_mcp/server.py` — **오직 `_chain_publishable` 함수 내부만**(§3-S1). 다른 함수·전역 수정 금지.
   - `tests/test_topology_events.py` / `tests/test_topology_chains.py` / `tests/test_topology_engine.py` / `tests/test_tools.py` (전부 **신규 테스트 추가만**)
   - 특히 금지: `web_viewer.py`·`topology_peerlinks.py`·`topology_correlator.py`·`topology_routing.py`·`topology_pairing.py`·`topology.py` 수정, chains `_KINDS` 집합 확장(routetx 는 별도 분기, §3-C2), `pass_refused` kind 에 소비자 배선(Phase B 결정 유지 — 후속 단계 몫), 버전 bump·`SPEC.md`·`README.md` 수정(Phase F 소관).
3. **의미론 금지 조항** (테스트가 없어도 위반 금지):
   - CHPLAN 토큰(`route_plan`)을 체인 `nodes` 로 그리지 않는다 — CHPLAN 은 경유 순서가 아니라 **relay 후보 우선순위 목록**이다(Phase A 반전 #3). `"00"`(direct)/`"FF"`(empty) 예약 토큰도 당연히 금지.
   - 발행 게이트 완화 금지: §3-S1 의 "관측 src 승격 갱신" 외에 프로브 로직·판정 기준을 바꾸지 않는다. `[Route] CHPLAN to` **부재를 발신 부정으로 해석하지 않는다**(XXX-5 — 구세대 SSM 은 무증거 송신).
4. **TDD 순서 강제**: §4의 신규 테스트를 먼저 작성 → 실패 확인 → §3 구현 → 전체 green. 테스트를 구현에 맞춰 고치는 방향 금지.
5. 검증 명령 (이 PC 전용 주의사항 포함):
   - 문법: `py -m compileall -q src` (`python` 명령은 Windows Store 별칭이라 동작 안 함)
   - 테스트: `uv run python -m pytest -q` (**`uv run pytest`는 이 PC에서 trampoline 오류로 깨짐 — 반드시 `python -m pytest` 형태**)
6. **애매하면 멈추고 보고.** 이 문서가 규정하지 않은 상황(예상 밖 기존 테스트 실패, fixture 라인이 캡처 원문과 불일치, routing/correlator/peerlinks 가 routetx 이벤트에 뜻밖의 부수효과를 보임 등)을 만나면 임의 해석하지 말고 중단 후 보고한다.
7. 커밋은 한국어 + Conventional Commits(§6). stdout 출력 금지 등 AGENTS.md 공통 규칙 준수.

---

## 1. 배경 (자족 요약 — 이번 구현의 유일한 전제)

serial-mcp 는 메시 장비(SSM=게이트웨이, SB=베이, REP=리피터)의 시리얼 콘솔을 `topology_events.py`(줄→Event) → `topology_chains.py`(Event→체인 로그) → `server.py`(발행 게이트→SSE/`get_topology`) 로 가공한다. Phase B(커밋 fc79be7)가 상행 relay 증거(`[BypassJson]`·`[Data_Pass]` A/B형)를 넣었고, Phase C 는 **하행 CHPLAN** 을 다룬다.

- **F-C1. CHPLAN 은 경로가 아니라 'relay 후보 우선순위 목록'(intent)이다.** `[2,["7C","02"],4,30,0]` 의 `["7C","02"]` = "7C 먼저, 안 되면 02"(대안 목록). 소비 주체는 엣지(SB)뿐이고 relay 실행(REGMAC 메커니즘)과 인과가 없다(XXX-1/XXX-6). → 노드로 그리면 **의도를 관측으로 위장**하는 것이라 절대 금지.
- **F-C2. v1/v2 두 물리 포맷이 공존한다.** v1 `[1, planA, planB, ttl, expire_s]`(위치 1·2 = 2글자 토큰 문자열), v2 `[2, [토큰...], ttl, expire_s, pid]`(위치 1 = 배열). 판별은 버전 값이 아니라 **위치 1의 타입**(배열이면 v2 레이아웃) — 수신 펌웨어 `RouteApplyChplan`(SB_ESP32.ino:10740-10802)의 자체 규칙을 미러한다. `"00"`=direct, `"FF"`=empty 예약 토큰.
- **F-C3. SSM 의 CHPLAN 송신은 콘솔 증거가 세대 의존이다.** 구세대(≤260526, 로컬 벤치 실측)는 **아무것도 안 찍는 무증거 송신**. 신세대(배포 260702-002 추정)는 소스 워킹카피 기준 `[Route] CHPLAN to %s A=%s B=%s`(SSM_esp32.ino:6499, fSerial 무게이트) — **실물 문구는 실측 미확인**. → "있으면 쓰는 선택 증거"로만 다룬다(XXX-5).
- **F-C4. 현행 하행 체인 발행 메커니즘 = 추론 src 합성 + needle 프로브 게이트.** SSM 콘솔의 TX 라인엔 Cidx 가 없어(Cidx 는 출력 **후** 스탬프) wifitx 이벤트는 체인 키가 안 잡힌다 — 하행 entry 의 src 는 `_public()` 이 group 포트로 합성하는 **추론(inferred) 노드**뿐이고, `_chain_publishable`(server.py)이 그 포트 버퍼에서 needle(수신 JSON 에서 Rev/Cidx 를 벗긴 원문)을 프로브해 발행을 결정한다. Stat/OK 하행은 `[Proc-WiFiTx] {json}` 라인이 needle 과 일치해 통과하지만, **CHPLAN 은 SSM 이 그 JSON 을 아예 안 찍어 원천 차단**된다(v1.14 의도 동작 — 구세대에선 이게 계속 정답). 게이트 판정은 **체인 id 당 1회 캐시 고정**이다.
- **F-C5. 하행 중계 관측은 Phase B 의 `pass` kind 로 이미 도착한다.** `[Data_Pass]` 12트리거엔 "유니캐스트 not-me" 가 있어, REP 가 하행 CHPLAN 을 중계하면 A형 `[Data_Pass] {json}` 이 찍히고 같은 `("c", ident, Cidx)` 키로 entry 에 합류한다. 단 현행 `_insert_before_dst` 는 role `"dst"` 만 종단으로 알아서, 하행 entry(종단 role `"rx"`)에선 relay 노드가 **수신 노드 뒤에** 붙는다(순서 훼손).
- **F-C6. 하행 overhear 가 실측됐다.** `2_bay...txt:214` — SB2(UnID 2) 콘솔에 **UnID 1 대상** CHPLAN 이 `[WiFi_Rx]` 로 찍힘(무선 특성상 청취). 현행 `_observe_wifirx` 하행 분기는 대상 여부 무관 그 포트를 role `"rx"` 노드로 추가하고 `ok=True` — 대상이 아닌 청취자를 수신자로 그리는 오표현이다(발행이 게이트에 막혀 있어 지금까지 안 보였을 뿐).
- **F-C7. 게이트 캐시와 후행 송신 증거의 경쟁.** 수신 관측(SB 콘솔)이 먼저 처리되면 게이트가 False 를 캐시하고, 이후 `[Route] CHPLAN to`(SSM 콘솔) 관측으로 src 가 승격돼도 캐시 False 가 영구 차단한다. → 캐시는 "프로브 플립"(버퍼 밀림)은 계속 무시하되, **"추론→관측 승격"이라는 새 증거**는 갱신해야 한다(False→True 단조, 깜빡임 없음).

### 실측 fixture 원천 (docs/archive/ + Phase A 문서 §1 XXX-5)

캡처 파일의 줄 앞 `[16:33:35.728] ` 타임스탬프는 캡처 도구가 붙인 것 — 제거하고 콘솔 원문만 쓴다.

- v2 수신(2_bay:214): `[WiFi_Rx] {"CHPLAN":[2,["7C","02"],4,30,0],"Asn":6,"UnID":1,"Cidx":297}`
- v2 bare 에코(2_bay:218, B형 pass 뒤): `{"CHPLAN":[2,["7C","02"],4,30,0],"Asn":6,"UnID":1,"Cidx":297}`
- v1 수신 시퀀스(로컬 벤치 실측 — Phase A §1 XXX-5 fixture 블록에서 채취):
  `[WiFi_Rx] {"CHPLAN":[1,"00","FF",4,120],"Asn":70,"UnID":5,"Cidx":898}` → bare 에코(Cidx 없음) → `[Route] CHPLAN applied A=00 B=FF ttl=4 expiry=120s`
- SB TX측 라우트 로그(2_bay:405): `[Route] Event route Asn=1 pid=1 stage=0 rescue=0 relays=00 len=171`
- **합성 허용 3건**(각각 테스트 주석에 "합성 — 근거"를 명시): ① `[Route] CHPLAN to 80,7D,3A,82,5A,AC A=7C B=02` (SSM_esp32.ino:6499 printf 형식 유래, 배포 세대 문구 실측 미확인) ② `[Route] CHPLAN to Bay_B02 v=2 cnt=2` (가상 문구 — 미지 세대 톨러런스 검증용) ③ `[Data_Pass] {"CHPLAN":[2,["7C","02"],4,30,0],"Asn":6,"UnID":1,"Cidx":297}` (2_bay:218 원문 JSON + A형 태그 — REP 하행 중계 시나리오).

---

## 2. 설계 계약 (이번 작업 후 지켜지는 의미론)

| 관측 | 처리 | 의미(D1 라벨) |
|---|---|---|
| 소비되는 이벤트(json 에 `CHPLAN` 보유 — wifirx/pass/rx 불문) | entry 에 `route_plan` 필드 부착(최초 1회). **nodes 불변** | **intent** — 경로 주장 아님 |
| `[Route] CHPLAN to ...` (SSM 콘솔) | 신규 kind `routetx` → 매칭된 하행 entry 의 src 를 **관측 노드로 승격** + needle 을 이 원문 라인으로 교체 | **observed(TX)** — 송신 콘솔 증거 |
| `[Route] CHPLAN applied ...` / `[Route] Plan update ...` / `[Route] Event route ...` | 비인식(헤더 아님 — 연속 줄/무시) | 범위 밖(§7) |
| 하행 wifirx, 포트 ident ≠ entry ident (둘 다 확인된 경우만) | `heard` 에 추가 + 대상은 **추론 rx 노드**로 표기. `ok` 미설정 | 청취≠수신 — 정직한 미확정(D5) |
| 하행 pass (A형) | relay 노드 — **수신(rx) 노드 앞** 삽입 | observed(relay) |

**D1 라벨링 규칙 확정 (XXX-6 이 Phase C 에 위임한 결정):**
- **intent 는 노드 표면에 올리지 않는다.** 체인의 `nodes`/`ok`/`confidence` 는 계속 "전달 관측"만 서술하고, 계획 정보는 entry 의 신규 필드 `route_plan` 에만 실린다. intent 전용 confidence 값은 **추가하지 않는다** — 노드가 intent 를 안 실으므로 필요가 없고, 이게 D1(관측/의도 구분)을 가장 강하게 지키는 형태다.
- 관측/추론 노드 구분은 기존 `inferred` 필드 재사용(신규 필드 없음).

**발행 게이트와의 정합(F-C3/F-C4의 명시 처리):** SSM TX 마커류는 fSerial 또는 세대 의존이라 "부재"가 흔하다. 게이트는 부재를 **증거 없음 → 미발행**으로만 다루고(사용자 결정 2026-07-03 유지), 발신 부정으로 승격하지 않는다. 구세대 SSM 의 CHPLAN 하행 체인은 이번 변경 후에도 **계속 미발행**(정답)이고, 신세대가 `[Route] CHPLAN to` 를 찍으면 routetx 관측 → src 승격 → 게이트 자연 통과로 발행된다 — 게이트 기준 자체는 불변.

**D3/D4 준수 논거(리뷰어 확인용):** 하행은 별도 렌더러를 만들지 않는다 — 같은 ChainLog entry·같은 `_observe_pass`/`_skeleton_from_tokens`/`_insert_*` 경로에 합류하고, 하행에 Rt 스켈레톤이 없는 것은 펌웨어 사실(Rt 스탬프는 상행 BypassJson 전용)이지 코드 분기가 아니다. 홉 수 하드코딩 없음. D4-1: 이번 변경은 전부 "관측되면 추가" additive — CHPLAN/routetx 가 없는 기존 체인(직결 상·하행)은 노드 수·순서·ordered·발행 전부 불변이어야 한다.

**공개 스키마 변화(Phase F 에서 SPEC §5 동기화 예정):** entry 에 `route_plan` 키 추가(없으면 `None`). 뷰어는 미지 필드를 무시하므로 무해 — `web_viewer.py` 는 건드리지 않는다.

---

## 3. 구현

### 3-E. `src/serial_mcp/topology_events.py`

1. **`_HEADERS` 에 routetx 추가** (route 항목 인접): `("routetx", re.compile(r"\[Route\]\s*CHPLAN to\b"))`. `[Route] CHPLAN applied`(to 없음)·`[Route] Event route`·`[Route] Plan update`·`[Route] Link`(기존 route kind 유지)와 매칭되지 않아야 한다.
2. **routetx 파싱** — `_new_event` 에서 kind=="routetx" 이면 JSON 시도 대신:
   `ev["route_plan_tx"] = {"target": <'to' 다음 첫 토큰 — mac 형(콤마/콜론 16진)이면 _norm_mac, 아니면 원문>, "tokens": [<라인 나머지에서 `\b[AB]=([0-9A-Fa-f]{1,2})\b` 전부 — 2자리 대문자 hex 로 정규화>]}`
   A=/B= 가 없으면 `tokens=[]`(미지 세대 문구 톨러런스 — 이벤트는 방출하되 chains 매칭이 자연 불발되는 안전 무동작).
3. **즉시 방출**: `feed()` 에서 routetx 는 tx/wifirx 와 같은 즉시 방출 목록에 넣는다(한 줄 완결).
4. 모듈 docstring 에 routetx 한 줄 추가. 그 외 로직 무수정.

### 3-C. `src/serial_mcp/topology_chains.py`

1. **`_parse_chplan(value, resolver) -> Optional[dict]`** (모듈 함수): value 가 list 이고 len≥2 일 때만.
   - 레이아웃 판별: `value[1]` 이 list → v2(`tokens=value[1]`, ttl=value[2], expire_s=value[3], pid=value[4] — 각 위치는 존재+int 일 때만, bool 제외), 아니면 v1(`tokens=[value[1]] (+value[2] 존재 시)`, ttl=value[3], expire_s=value[4], pid=None).
   - 토큰 정규화는 기존 `_norm_token`(실패 시 `str(원문)` 보존). `version = value[0]`(int 일 때, 아니면 레이아웃 추정값 1/2).
   - 반환: `{"version", "tokens", "ttl", "expire_s", "pid", "relays"}` — `relays` = tokens 중 `_RESERVED_TOKENS` 제외분을 `_resolve_token(resolver, tok)` 으로 해소한 `[{"token","name","resolved"}]`.
2. **route_plan 부착**: `observe()` 의 `_seen` 통과 후(kind 디스패치 전), `ent.get("route_plan") is None` 이고 `ev["json"]` dict 에 `"CHPLAN"` 이 있으면 `_parse_chplan` 성공 시 `ent["route_plan"] = rp` + **§3-C3 역방향 매칭 시도**. `_new_entry` 에 `"route_plan": None` 초기화. `_public()` 에 `"route_plan": ent.get("route_plan")` 노출. `_correct_direction` 은 건드리지 않는다(route_plan 은 페이로드 사실이라 방향 정정에도 유지).
3. **routetx 소비** — `observe()` 최상단 `_KINDS` 검사 **앞에** 분기: `if kind == "routetx": return self._observe_route_tx(ev, resolver, port_names)`. (`_KINDS` 에 넣지 않는다 — 키 없는 이벤트라 기존 경로와 계약이 다르다.)
   - `_observe_route_tx`: `ev["route_plan_tx"]["tokens"]` 가 비면 버퍼만 적재하지 말고 그대로 무시해도 된다(매칭 불능). 아니면 `self._route_tx` deque(maxlen 32, `{"port","ts","target","tokens","raw"}` — raw 는 `ev["raw_lines"][0]` 의 strip)에 적재 후 활성 entry 매칭 시도. `self._expire(ts)` 는 기존 observe 와 동일하게 먼저 수행하고 changed 에 합류.
   - **매칭 기준(전부 충족)**: ① `ent["dir"]=="down"` 이고 complete 아님 ② `ent["route_plan"]` 존재하고 `ent["route_plan"]["tokens"] == tx["tokens"]`(둘 다 비어있지 않음) ③ `abs(ent["_last_ts"] - tx["ts"]) <= self._window` ④ 그룹 정합: `ent["group"] in (None, tx["port"])` ⑤ ident 정합: entry ident(`_entry_ident`)가 str(mac)이면 `_norm_mac` 비교로 target 과 일치, int(UnID)면 `_resolve_token(resolver, f"{ident & 0xFF:02X}")` 의 `mac` 이 있을 때만 비교 — **비교 불능이면 통과**(차단 아님).
   - **후보가 정확히 1개일 때만 부착**(0개=대기, 2개 이상=모호 → 아무것도 안 함 — 정직한 미확정, D5). 부착 성공 시 그 tx 를 버퍼에서 제거.
   - **부착 동작**: `_ensure_src(ent, port=tx["port"], name=_name_for_port(...))`(기본 inferred=False — 관측 노드), `ent["group"] is None` 이면 `tx["port"]` 로 설정, `ent["_needle"] = tx["raw"]` 로 **교체**(첫 관측 고정 규칙의 명시 예외 — 수신 JSON needle 은 SSM 콘솔에 실재하지 않으므로, '송신측에 실재하는 라인'이라는 needle 계약을 지키려면 교체가 맞다. 주석으로 남길 것), `ent["_seen"].add((tx["port"], "routetx"))`(중복 부착 방지), 변경 public 반환.
   - **역방향(§3-C2 에서 호출)**: entry 가 route_plan 을 얻는 순간 `self._route_tx` 를 같은 기준으로 스캔 — 정확히 1건이면 동일 부착.
4. **하행 wifirx 청취자/대상 구분** — `_observe_wifirx` 하행 분기(dir != "up" 쪽) 재구성:
   - `ent_ident = self._entry_ident(ent)`, `port_ident = (port_idents or {}).get(port)`.
   - **둘 다 None 아님 && 불일치** → `heard` 에 추가(중복 방지) + `_ensure_ident_rx(ent, resolver)` 호출 + **`ok`/`confidence` 를 설정하지 않고** 반환. (대상 장비의 수신은 미관측 — 청취는 도착 증거가 아니다.)
   - 그 외(어느 한쪽이라도 미상)는 **현행 동작 그대로**(rx 노드 추가 + ok=True + confidence="observed") — membership 이 아직 포트 ident 를 모르는 부팅 직후·미등록 장비에서 기존 체인이 변하면 안 된다(D4-1).
   - `_ensure_ident_rx(ent, resolver)`: nodes 에 role "rx"/"dst" 가 하나도 없을 때만, `_ensure_ident_src` 와 대칭으로 — ident 가 str(mac)이면 그 mac, int 면 토큰맵 해소(실패 시 `f"UnID {ident}"`, resolved=False) — `_node(name=..., role="rx", resolved=..., inferred=True)` 를 **append**.
5. **relay 삽입 앵커 일반화** — `_insert_before_dst` 를 "role 이 `dst` **또는** `rx` 인 첫 노드 앞" 으로 확장(이름은 `_insert_before_terminal` 로 바꿔도 좋다 — 호출부 1곳). 상행 entry 엔 rx 노드가 없어 동작 불변.
6. 모듈 docstring 에 D1 규칙(route_plan=intent 전용 필드, nodes=관측+inferred 구분) 요약 추가.

### 3-S1. `src/serial_mcp/server.py` — `_chain_publishable` 캐시 승격만

함수 서두를 다음 의미로 재배열(다른 로직 불변):
```python
cached = _chain_gate.get(cid)
if cached is True:
    return True
srcs = [n for n in chain.get("nodes") or [] if n.get("role") == "src"]
if srcs and all(not n.get("inferred") for n in srcs):
    # 추론→관측 승격(routetx 등 후행 송신 증거) — 프로브 무관 통과 + 캐시 갱신(False→True 단조).
    # 버퍼 밀림에 의한 프로브 플립과 달리 '새 증거'이므로 1회 고정 원칙의 예외가 맞다.
    if cid is not None:
        _chain_gate[cid] = True
    return True
if cached is not None:
    return cached
...(기존 프로브 경로 그대로)...
```
docstring 에 승격 예외 한 줄 추가. **프로브 경로·`_decorate_chain_jumpable`·`_chain_jump_attempts` 는 무수정.**

### 3-X. 무변경 확인 의무 (Codex 가 구현 중 확인만 — 어긋나면 §0-6 발동)

- `topology_engine._drain` 은 모든 이벤트를 routing/peerlinks/correlator 에도 흘린다 — routetx 이벤트(ids 전부 None, json 없음)가 그 셋에서 **무동작**임을 코드로 확인한다(수정 금지 — 부수효과가 보이면 중단·보고).
- 하행 rx 칩의 뷰어 점프는 needle(=routetx 라인, 해당 콘솔엔 없음) 실패 후 `"Cidx":N` 조각 폴백 + ±5s 앵커로 성립한다 — 설계상 허용(코드 확인만, 수정 없음).

---

## 4. 신규 테스트 (먼저 작성 — TDD)

fixture 는 §1 "실측 fixture 원천"의 원문만 쓴다(합성 3건은 명시 주석 필수). 이벤트 dict 를 손으로 만들 땐 각 테스트 파일의 기존 헬퍼(`ev(...)` 등) 형식을 따른다.

### 4.1 `tests/test_topology_events.py`

1. **routetx 인식(합성①)** — `[Route] CHPLAN to 80,7D,3A,82,5A,AC A=7C B=02` 1줄 feed → 즉시 방출 1개, kind=="routetx", `route_plan_tx == {"target":"80:7D:3A:82:5A:AC","tokens":["7C","02"]}`.
2. **미지 문구 톨러런스(합성②)** — `[Route] CHPLAN to Bay_B02 v=2 cnt=2` → kind=="routetx", target=="Bay_B02", tokens==[].
3. **비매칭 가드(실측)** — `[Route] CHPLAN applied A=00 B=FF ttl=4 expiry=120s` 단독 feed → 방출 0, flush 후도 0. `[Route] Event route Asn=1 pid=1 stage=0 rescue=0 relays=00 len=171` 도 동일. 기존 `[Route] Link ...` 라인이 여전히 kind=="route" 임을 함께 단언(회귀 고정).

### 4.2 `tests/test_topology_chains.py`

4. **route_plan v2 파싱·노드 불변(실측 2_bay:214)** — wifirx 로 feed → entry `route_plan` == {version 2, tokens ["7C","02"], ttl 4, expire_s 30, pid 0, relays 2건} 이고, **nodes 에 "7C"/"02" 유래 노드가 없다**(§0-3 금지의 테스트 고정).
5. **route_plan v1·예약 토큰 제외(실측 v1)** — `{"CHPLAN":[1,"00","FF",4,120],...}` → version 1, tokens ["00","FF"], relays == [](00/FF 제외), ttl 4, expire_s 120, pid None.
6. **relays 해소** — resolver(`resolve_token("7C")→{"name":"REPEATOR",...}`) 주입 시 relays[0] == {"token":"7C","name":"REPEATOR","resolved":True}.
7. **routetx 선행 → 수신 후행 합류** — routetx(COM4, 합성① 라인) 먼저, 그 뒤 wifirx(COM12, 실측 2_bay:214, scope={"COM12":"COM4"}) → src 노드: port "COM4", inferred False; needle == 합성① 원문 라인.
8. **수신 선행 → routetx 후행 합류(역순)** — 7번의 반대 순서 → 같은 기대 + needle 이 수신 JSON 니들에서 routetx 라인으로 **교체**됐음을 단언.
9. **모호성 스킵** — 같은 tokens 의 하행 entry 2개(ident 다름, resolver 미제공=ident 비교 불능) 활성 상태에서 routetx 1건 → 어느 entry 에도 src 미부착(둘 다 public src 없음/추론 유지).
10. **그룹 베토** — ent.group=="COM9" 인 entry 에 routetx(port "COM4") → 미부착.
11. **ident 정합 차단** — entry ident 가 mac 문자열(예: "30:AE:A4:4C:94:20")인데 routetx target 이 "80:7D:3A:82:5A:AC" → tokens 가 같아도 미부착.
12. **청취자/대상 구분(F-C6)** — port_idents={"COM13": 2} 로 wifirx(COM13, 실측 2_bay:214 — UnID 1 대상) → heard 에 "COM13", rx 관측 노드 없음, 대신 role "rx"·inferred True·이름 "UnID 1"(resolver 없음) 노드, `ok is None`. **대칭 회귀**: port_idents 미제공이면 현행대로 rx 노드+ok True(신규 테스트로 명시 고정).
13. **relay 가 rx 앞(§3-C5)** — 하행 entry 에 wifirx(수신) 먼저 → pass(합성③, 다른 포트 COMR) 나중 → nodes 의 role 순서에서 relay 가 rx 보다 앞.

### 4.3 `tests/test_topology_engine.py` (통합)

14. **하행 관측 src 승격 파이프라인** — 엔진에 COM4(SSM): 합성① routetx 라인, COM12: 실측 2_bay:214 라인 주입(순서 무관 둘 다) → recent_chains 에 key `["c",1,297]` 항목: dir "down", src 노드 port "COM4"·inferred False, needle == routetx 원문, route_plan.tokens == ["7C","02"]. peer edges 에 routetx 유래 엣지가 없음을 함께 단언(3-X 고정).
15. **D4-1 직결 하행 회귀** — CHPLAN/routetx 없이 기존 하행(예: REQRSSI wifirx)만 주입 → 노드 구성·ok·route_plan is None 이 현행과 동일(공개 노드 2개: 추론 src + rx).

### 4.4 `tests/test_tools.py` (게이트)

16. **승격 갱신·플립 불허(F-C7)** — `_chain_gate` 초기화 후: ①추론 src 체인(id=1) + 프로브 False → False(캐시) ②같은 id 를 프로브 True 로 재판정 → **여전히 False**(프로브 플립 무시 — 기존 1회 고정 유지) ③같은 id 의 nodes 를 관측 src(inferred False)로 바꿔 재판정 → **True**(증거 승격) ④그 뒤 다시 추론 src+프로브 False 로 호출해도 True(캐시 단조).

## 5. 완료 판정

- §4 신규 테스트 전부 green + 기존 테스트 전체 green (`uv run python -m pytest -q`), `py -m compileall -q src` 통과.
- 수정 diff 가 §0-2 파일 범위를 벗어나지 않고, §0-3 금지 조항 위반이 없다.
- 구세대 SSM 시나리오(routetx 부재)에서 CHPLAN 하행 체인이 **계속 미발행**임이 기존 게이트 테스트로 보장된다(완화 없음).
- 실장비 검증(배포 SSM260702-002 의 `[Route] CHPLAN to` 실물 문구 캡처·필요시 정규식 후속 보정, SB↔REP↔SSM 하행 3-node 재현)은 Claude 가 별도 수행 — Codex 범위 아님.

## 6. 커밋

한 커밋으로: `feat: 하행 CHPLAN 인식 — v1/v2 intent 파싱·[Route] CHPLAN to 송신증거 합류·하행 체인 정형화(D1·D3)` (본문에 마스터플랜 Phase C + Phase A findings 참조 한 줄).

## 7. 비범위 (이번에 하지 않음 — 발견 사실 기록)

- **`[Proc-WiFiTx]`(하이픈)·`[Proc-WiFiTx-ACK]`·`[Proc-Alarm]`(하이픈) 이 현행 `_HEADERS` 의 언더스코어 패턴(`\[Proc_WiFiTx\]|\[Proc_Alarm\]`)과 불일치** — 1_ssm 실측에 두 표기가 공존한다(L25 `[Proc_WiFiTx] Ask Info` vs L96 `[Proc-WiFiTx] {json}`). 어차피 SSM TX 콘솔 라인엔 Cidx 가 없어 체인 키가 안 잡히므로 CHPLAN 작업과 무관하지만, wifitx 이벤트 계열이 실질 불활성이라는 뜻이라 별도 후속(자체 TDD)으로 다룬다.
- `pass_refused` 소비(heard 배선) — Phase B 결정대로 후속 단계.
- `[Route] CHPLAN applied`(엣지의 수용 관측)·`[Route] Event route`(상행 시도 스테이징) 파싱 — 세대별 문구 변형이 크고 체인 형상에 기여가 없어 보류.
- 뷰어의 route_plan 표시(후보 목록 UI)·SPEC §5 스키마 동기화 — Phase E/F.
