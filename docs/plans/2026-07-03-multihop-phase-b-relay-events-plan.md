# Phase B — 상행 relay 증거 통합 인식 (topology_events.py) — 구현 지시서 (Codex 핸드오프)

상태: 구현 대기 (설계 확정 — 재설계 금지, 이 문서대로만 구현)
설계: Claude (2026-07-03). 전제 문서: `2026-07-03-multihop-relay-evidence-masterplan.md`(원칙 D1~D6·D4-1) + `2026-07-03-multihop-phase-a-firmware-findings.md`(펌웨어 사실 대장). 이 지시서는 자족적으로 쓰였지만, 판단이 갈리면 위 두 문서가 우선한다.
구현: Codex
검증·리뷰: Claude (구현 완료 후 리뷰·실장비 검증은 Claude 몫 — Codex 범위 아님)

---

## 0. 절대 규칙 (보상 해킹 방지 — 위반 시 작업 무효)

1. **기존 테스트 삭제·약화·skip·xfail·수정 금지.** 이번 작업의 기존 테스트 수정 허용 목록은 **없다**(0건). 기존 테스트와 신규 설계가 충돌한다고 판단되면 구현을 멈추고 보고한다.
2. **수정 파일 범위 고정** (이 3개 외 어떤 파일도 만지지 않는다):
   - `src/serial_mcp/topology_events.py`
   - `tests/test_topology_events.py` (신규 테스트 추가만)
   - `tests/test_topology_engine.py` (신규 테스트 추가만)
   - 특히 금지: `topology_chains.py`·`topology_peerlinks.py`·`topology_engine.py`·`server.py`·`web_viewer.py` 수정, `_KINDS` 확장, 발행 게이트(`_chain_publishable`) 접근, 버전 bump·SPEC.md·README 수정(별도 단계).
3. **TDD 순서 강제**: §4의 신규 테스트를 먼저 작성 → 실패 확인 → §3 구현 → 전체 green. 테스트를 구현에 맞춰 고치는 방향 금지.
4. 검증 명령 (이 PC 전용 주의사항 포함):
   - 문법: `py -m compileall -q src` (`python` 명령은 Windows Store 별칭이라 동작 안 함)
   - 테스트: `uv run python -m pytest -q` (**`uv run pytest`는 이 PC에서 trampoline 오류로 깨짐 — 반드시 `python -m pytest` 형태**)
5. **애매하면 멈추고 보고.** 이 문서가 규정하지 않은 상황(예: 예상 밖 기존 테스트 실패, fixture 라인이 실제 파일과 불일치)을 만나면 임의 해석하지 말고 중단 후 보고한다.
6. 커밋은 한국어 + Conventional Commits(§6). stdout 출력 금지 등 AGENTS.md 공통 규칙 준수.

---

## 1. 배경 (자족 요약)

serial-mcp 서버는 임베디드 메시 장비(SSM=게이트웨이, SB=베이, REP=리피터)의 USB 시리얼 콘솔을 포트별로 읽어 `topology_events.py`(줄→Event 조립) → `topology_chains.py`(Event→체인 로그) 로 가공한다. 실장비 검증(2026-07-03)에서 SB↔REP↔SSM 구성인데 `get_topology` 체인이 SB→SSM 직결로만 찍히고 리피터 홉이 빠졌다.

Phase A 펌웨어 재검증으로 확정된 사실(이번 구현의 유일한 전제):

- **F-B1. `[BypassJson] {json}`** — relay 장비(REP/SB/APU)가 상행 패킷(발신 장비가 `Rev:true`+`Cidx` 를 찍은 것)을 재브로드캐스트할 때 콘솔에 남기는 **relay 실행 증거**. 출력 JSON 은 relay 가 자기 라우트 토큰을 `Rt` 배열에 스탬프한 **뒤**의 것이라 `"Rt":["7C"]` 처럼 경유 토큰을 싣는다. 게이트 없음(fSerial 무관). 실측: REP 캡처 500줄 중 94건. **현행 파서는 이 태그를 아예 모른다 — 이번 작업의 핵심.**
- **F-B2. `[Data_Pass]` 물리 A/B형은 단일 코드의 런타임 분기다.** 펌웨어 49개 지점 전부 `print("[Data_Pass] ") → sendMessage(원문) → println(원문JSON)` 한 형태이고, sendMessage 내부 보호 분기가 갈림길이다:
  - **A형 `[Data_Pass] {json}`** (같은 줄 JSON) = 보호 분기를 안 탐 = **실제 브로드캐스트 완료** → relay 실행 증거.
  - **B형 `[Data_Pass] Protected to bypass.`** (태그 줄이 문구로 닫히고 **다음 줄에 태그 없는 JSON**) = 송신 차단됨 = **relay 아님**. "그 장비가 그 패킷을 들었다"는 수신 관측일 뿐이다. → **경유 홉으로 그리면 안 된다**(Phase A 최우선 반전 #1).
- **F-B3. 물리 변형**: ① Alive 재전파는 태그 앞에 `.` 이 붙어 물리 줄이 `.[Data_Pass] …` ② SSM 콘솔은 `[Data_Pass]  To. <이름>, {json}` 처럼 `To.` 가 끼며, SSM 은 항상 A형(보호 분기 없음) ③ 태그와 JSON 사이에 딴 출력이 끼어 태그 줄에 JSON 이 없는 오류 경로 변형 존재.
- **F-B4. relay 콘솔에는 `[BypassJson]` 직전에 태그 없는 bare JSON 줄**(스탬프 전 수신 원문 echo)이 함께 찍힌다 — 이걸 이벤트로 오인하면 유령 이벤트가 생긴다(현행도 무시하고 있고, 계속 무시가 정답).
- **F-B5. 소비측은 준비돼 있다.** `topology_chains.py` 의 `_observe_pass`(kind `"pass"` 소비)는 이미 그 포트를 relay 노드로 삽입하고, 이벤트 `ids.rt_tokens` 로 스켈레톤 매칭까지 한다. `_dir_hint` 도 `Rev:true`→up, `INFO:"REQ"`/`CHPLAN`→down 을 이미 처리한다. **즉 chains 는 무수정** — events 가 옳은 kind·ids 로 방출만 하면 3-node 체인이 형성된다.
- **F-B6. B형의 현행 처리는 '우연히 옳은 결과'다.** B형 태그 줄엔 JSON 이 없어 ids 전부 None → chains `_event_key` 가 (None,None) → 조용히 드롭. 결과(경유로 안 그림)는 맞지만 의미('거부'였다는 사실)를 버리고 있고, 침묵 드롭이라 의도가 코드에 없다. 이번에 **명시적 의미(kind)** 로 바꾼다.

---

## 2. 설계 계약 (이번 작업 후 events 가 지키는 의미론)

| 관측 | 방출 kind | 의미 | 소비자 |
|---|---|---|---|
| `[Data_Pass] {json}` (같은 줄 JSON — `To.`/`.` 변형 포함) | `"pass"` | relay 실행 관측 | chains(relay 노드)·peerlinks(현행 유지) |
| `[BypassJson] {json}` | `"pass"` | relay 실행 관측(+Rt 스탬프 포함) | 동일 |
| `[Data_Pass]` 뒤 같은 줄에 JSON 없음 (`Protected to bypass.` 등) | `"pass_refused"` | relay **거부**(송신 없음) — 수신 관측만. 다음 줄 JSON 을 부착해 ids 를 채우되 **이번 단계에선 소비자 없음**(chains `_KINDS` 밖 → 전 표면 불활성). heard 배선은 후속 단계 몫(Phase C 와 순서 무관 — C 가 먼저 구현돼도 이 kind 는 C 범위 밖) | 없음 (의도적) |
| 태그 없는 bare JSON 줄 (블록 미진행 중) | 방출 없음 | F-B4 echo — 유령 방지 | — |

D4-1(additive) 준수: 직결(경유 0) 체인은 pass 계열 이벤트가 아예 발생하지 않으므로 동작 불변이어야 한다.

## 3. 구현 (`src/serial_mcp/topology_events.py` 만)

1. **`_HEADERS` 의 `pass` 패턴 확장**: `("pass", re.compile(r"\[Data_Pass\]|\[BypassJson\]"))`. (`search` 사용이라 `.` 접두·타임스탬프 접두는 이미 허용된다 — 앵커 추가 금지.)
2. **`EventAssembler.feed()` 의 pass 분기 변경** — 현행은 `kind in ("tx","pass","wifirx")` 무조건 즉시 방출. 변경:
   - `kind == "pass"` 이고 `ev["json"] is not None` → 즉시 방출(현행 동일).
   - `kind == "pass"` 이고 `ev["json"] is None` → `ev["kind"] = "pass_refused"` 로 바꾸고 **버퍼링**(`self._cur = ev`) — 다음 헤더/flush 에서 방출된다(기존 rx 블록과 같은 수명).
3. **`_attach()` 에 pass_refused 연속 줄 처리 추가**: 현재 블록이 `pass_refused` 이고 `json is None` 이면 `extract_json(text)` 시도 — 성공 시 `ev["json"] = obj; _fill_from_json(ev, obj)` (첫 JSON 한 번만). 실패 줄(`[Bin] …` 노이즈 등)은 raw_lines 보존만(기존 동작). 기존 추출(takentime 등)은 그대로 둔다.
4. **주석 정정(코드 변경 아님)**: L153-158 의 `[Proc-Raw Packet]` 주석 — "Rt 제거 전 원시 패킷이라 실제 경로 토큰 보유"는 **조건부**다(SSM 은 중복수신 검사 통과분만, 기본 `fprintAllReceivedPackets=false` 모드에서 출력 — relay 사본이 직결 사본보다 늦으면 안 찍힘). 전제가 아니라 "찍히면 Rt 를 여기서 얻는다"로 고쳐 쓴다. 모듈 docstring 의 관측 흐름 설명에도 `[BypassJson]`/`pass_refused` 한 줄씩 추가.
5. 그 외 로직( `_fill_from_json` 의 Rt→rt_tokens, `_ID_KEYS` 등)은 **무수정** — `[BypassJson]` JSON 은 기존 코드로 ids 가 전부 채워진다.

## 4. 신규 테스트 (먼저 작성 — TDD)

fixture 는 **실측 캡처 원문**(`docs/archive/`)에서 채취한다. 캡처 파일의 줄 앞 `[16:33:35.709] ` 타임스탬프는 **캡처 도구가 붙인 것**이므로 제거하고 콘솔 원문만 쓴다. `(N회 반복 …)` 주석이 붙은 줄은 1회분 원문만 취한다. 합성 fixture 는 아래에 "합성"으로 명시된 2건만 허용.

### 4.1 `tests/test_topology_events.py` 추가분

1. **BypassJson 즉시 방출** — `[BypassJson] {"UnID":1,"INFO":["4","SB260702-001",-57,false,false,"0",false],"EQ":[0,0,0,0,0,0,1,[],0],"Unique":5,"Rev":true,"Cidx":996,"Rt":["7C"]}` (3_repeater_last500.txt:4) → 이벤트 1개, kind=="pass", ids: unid=1, unique=5, cidx=996, rt_tokens==["7C"], json.Rev is True.
2. **BypassJson Cidx 계열** — `[BypassJson] {"UnID":1,"Stat":"OK","Asn":31,"Rev":true,"Cidx":998,"Rt":["7C"]}` (3_repeater:9) → kind=="pass", asn=31, cidx=998, rt_tokens==["7C"].
3. **Data_Pass A형(현행 회귀 고정)** — `[Data_Pass] {"REGMAC":[["10,06,1C,16,97,AC",0],["94,A9,90,1D,FF,74",0]],"reqId":"k90xdhjy","Mac":"80,7D,3A,82,5A,AC","Cidx":261}` (3_repeater:176) → kind=="pass" 즉시 방출, mac 정규화 "80:7D:3A:82:5A:AC", cidx=261.
4. **B형 → pass_refused + 다음 줄 JSON 부착** — 순서대로 feed: `[Data_Pass] Protected to bypass.` → `{"RTC":[26,33,16,3,7,2026],"CHANNEL":"11","INFO":"REQ","UnID":1,"Cidx":296}` → `[Bin] recorded_data:<쓰레기>` → 다음 헤더 아무거나(예: `[WiFi_Rx] {"UnID":1,"Stat":"OK","Asn":6,"Cidx":298}`) (2_bay_b01_1chi-bay_last500.txt:210-212,219). 기대: 방출 2개 — 첫째 kind=="pass_refused"(unid=1, cidx=296, json.CHANNEL=="11"), 둘째 kind=="wifirx". [Bin] 줄은 raw_lines 에만 남고 json 을 덮지 않는다.
5. **B형 CHPLAN 시퀀스** — `[Data_Pass] Protected to bypass.` → `{"CHPLAN":[2,["7C","02"],4,30,0],"Asn":6,"UnID":1,"Cidx":297}` → flush() (2_bay:217-218). 기대: pass_refused 1개, asn=6, cidx=297, json 에 "CHPLAN" 존재. (방향 힌트 `_dir_hint` 는 chains 몫 — events 는 kind/ids 만 검증.)
6. **bare JSON 단독 줄 무시** — 블록 미진행 상태에서 `{"UnID":2,"INFO":["4","SB260630-002",-47,false,false,"0",false],"EQ":[0,0,0,0,0,0,1,[],0],"Unique":75,"Rev":true,"Cidx":932}` (3_repeater 말미) 1줄만 feed → 방출 0, flush() 후에도 0 (F-B4).
7. **변형 톨러런스(합성 — 펌웨어 소스 유래, 실측 라인 아님을 주석 표기)**:
   - `.[Data_Pass] {"Alive":"SSM","Cidx":900}` → kind=="pass" (선행 `.` 허용).
   - `[Data_Pass]  To. Bay_B02, {"SPECIAL":"CHKRSSI","Unique":9,"UnID":1}` → kind=="pass", unique=9 (`To.` 삽입 허용 — JSON 은 첫 `{` 부터).
8. **A형 뒤 pass_refused 미발생 회귀** — 기존 테스트(115행·163행)가 A형 즉시 방출을 이미 고정한다 — 건드리지 않는다.

### 4.2 `tests/test_topology_engine.py` 추가분 (통합)

9. **상행 3-node 체인 형성** — 엔진에 두 포트 주입:
   - REP 포트("COMR"): 위 1번 BypassJson 라인.
   - SSM 포트("COMS"): `[Proc-WiFiRx] {"UnID":1,"INFO":["4","SB260702-001",-57,false,false,"0",false],"EQ":[0,0,0,0,0,0,1,[],0],"Unique":5,"Rev":true,"Cidx":996}` (합성 — 실측 규칙 "SSM 은 [Proc-WiFiRx] 출력 전 Rt 를 remove" 를 적용해 1번 fixture 에서 Rt 만 뺀 것임을 주석 표기).
   - 기대: recent chains 에 key `("u",1,5)` 항목 — nodes 에 role=="relay" 이고 port=="COMR" 인 노드가 존재하고, dst 는 COMS, `ordered is True`. (src 는 콘솔 미연결 장비라 추론(inferred) 노드 — 정확한 이름 형식은 단언하지 말 것. 발행 게이트는 server.py 소관이라 이 테스트 범위 밖.)
10. **pass_refused 전 표면 불활성** — 엔진에 4번 B형 시퀀스만 주입 → 체인 recent 에 **새 항목 0**, peer edges 에 그 포트 유래 엣지 0. (침묵 드롭이 아니라 kind 차단에 의한 불활성임은 events 단위 테스트 1·4번이 담보.)
11. **D4-1 직결 회귀** — pass 계열 라인 없이 기존 tx/rx 페어만 주입(기존 엔진 테스트의 시나리오 형식을 재사용)했을 때 체인 노드 수가 2(src/dst)로 불변임을 명시 단언.

## 5. 완료 판정

- §4 신규 테스트 전부 green + 기존 테스트 전체 green (`uv run python -m pytest -q`).
- `py -m compileall -q src` 통과.
- 수정 diff 가 §0-2 파일 범위를 벗어나지 않음.
- 실장비 3-node 검증(SB↔REP↔SSM 재현)은 리피터가 있는 테스트베드에서 Claude 가 별도 수행 — Codex 범위 아님.

## 6. 커밋

한 커밋으로: `feat: relay 실행 증거 통합 인식 — [BypassJson]·[Data_Pass] A/B형 런타임 분기(B형=거부, 경유 홉 금지)` (본문에 Phase A findings 문서 참조 한 줄).
