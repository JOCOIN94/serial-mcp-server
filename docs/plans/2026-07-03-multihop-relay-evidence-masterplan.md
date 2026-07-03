# 멀티홉 relay 증거 인식 재설계 — 마스터플랜(설계 개요서)

상태: **설계 개요 초안 — 세부 구현계획서 아님.** 미해결(XXX) 항목이 남아 있고, 그걸 다 풀기 전엔 코드를 짜지 않는다.
> 상태 갱신(2026-07-03): **Phase A 완료** — §3 XXX-1~7 전부 판정됨. 해소 결과·마커 카탈로그·마스터플랜 보정 3건(특히 원인 #4 전제 뒤집힘)은 `2026-07-03-multihop-phase-a-firmware-findings.md` 참조. 이후 세부 계획서는 두 문서를 함께 전제로 한다.
> 상태 갱신 2(2026-07-03): **Phase B 완료**(커밋 fc79be7, 리뷰 통과). **Phase D 범위 조정(사용자 결정)** — 리피터 2대 확보 불가로 2홉 실측은 보류. D는 ①합성 N홉 fixture 테스트(구조 검증) ②1홉 실장비(SB↔REP↔SSM 별도 PC, 릴리스→마켓플레이스 업데이트 경로) **육안 확인**의 느슨한 2단 구조로 진행하고, "구조상 가능"과 "실측 확인됨"의 구분 표기는 유지한다(XXX-7 원칙).
작성: Claude(2026-07-03), 근거는 실측 캡처 로그(`docs/archive/`) + firmware-src(codebase-memory-mcp 조회) — 세부 근거는 §7 부록.
이 문서의 역할: 다음에 올 **세부 구현계획서(들)**가 지켜야 할 원칙·범위·미해결 질문을 고정한다. 세부 구현계획서는 **별도 세션에서, Phase 단위로 따로** 작성한다(한 번에 다 만들면 실수 유입 확률이 올라간다는 판단, §4). 세부 구현계획서 작성 후 실제 구현은 Codex에게 맡길 예정이다.

---

## 0. Codex 핸드오프 전제 (다음 세션 작성자에게)

Codex는 **큰 그림을 안 보고, 시킨 걸 정확히 구현하는 데는 강하다.** 지시가 모호하면 "일단 테스트를 통과시키는" 방향으로 보상 해킹할 위험이 있다(예: 테스트 완화, 실패 케이스를 조용히 스킵, 애매한 조건을 자기 편한 쪽으로 해석). 그래서 이 마스터플랜에서 갈라져 나올 **각 Phase의 세부 구현계획서는 반드시**:
- `docs/plans/2026-07-03-chain-cidx-ident-jump-needle.md`의 §0("절대 규칙 — 보상 해킹 방지")과 같은 형식의 규칙 섹션을 갖는다(수정 파일 범위 고정, 기존 테스트 변경 금지 목록, TDD 순서 강제).
- 이 마스터플랜의 §2(설계 원칙)·§3(미해결 항목 해소 결과)을 전제로 삼고, 재설계하지 않는다.
- "애매하면 멈추고 보고" 조항을 반드시 포함한다.

---

## 1. 문제 정의 — 실측으로 확인된 것

**증상**: SB(엣지) ↔ 리피터(경유) ↔ SSM(게이트웨이)으로 실제 구성한 PC에서 테스트했더니, `get_topology`의 `recent_chains`가 **SB→SSM 직결로만 찍히고 리피터 홉이 빠진다.** 하행(SSM→SB) 체인은 아예 안 보인다.

**근거**: 2026-07-03 실측 캡처 4개(`docs/archive/1_ssm_last500.txt`, `2_bay_b01_1chi-bay_last500.txt`, `3_repeater_last500.txt`, `4_chain_log.txt`)와 firmware-src(cbm 조회)를 대조해 원인 5가지를 확정했다. 상세 근거는 §7. 요약:

| # | 원인 | 방향 | 실증 상태 |
|---|---|---|---|
| 1 | `[BypassJson]`(SB/REP/APU 공유 `BypassJsonPacket()`) — 상행 relay 시 가장 우세한 실제 마커인데 파서가 아예 모름 | 상행 | 확정(REP 캡처 500줄 중 30줄+ 관측, `[Data_Pass]`는 1건뿐) |
| 2 | `[Passed Device]`(기존 코드가 의존하던 권위 소스) — 이 실측에선 **0건** | 상행 요약 | 확정(SSM 캡처 500줄 전수 검색 0건) |
| 3 | `[Proc-Raw Packet]`에 Rt가 남아있다는 기존 코드 주석의 전제 — 이 실측에선 **거짓** | 상행 보조경로 | 확정(SSM 캡처 `"Rt":` 전수 검색 0건) |
| 4 | `[Data_Pass]` 출력 포맷이 장비마다 다름(REP=같은 줄 JSON, SB=설명문+다음 줄 JSON) — 후자는 `_event_key`가 `(None,None)`을 반환해 조용히 드롭됨 | 상행 | 확정(양쪽 실측 라인 직접 대조) |
| 5 | 하행 권위 소스 후보 `[Route] CHPLAN to ... A=... B=...`(SSM 자체 발신) — 파서가 `[Route] Link`만 인식해서 미인식 | 하행 | 부분 확정(로그·펌웨어 함수 존재는 확정, 정확한 의미·신뢰성은 §3 XXX) |

**프론트엔드**: 우려와 달리 `web_viewer.py`의 `chainRow()`/칩 렌더링은 이미 노드 개수 무관 루프로 짜여 있어(§7.4), N홉 자체는 받을 수 있는 구조다. 데이터가 안 들어와서 안 보인 것이지 렌더러가 못 받는 구조는 아니다. 단, 칩이 많아질 때 시각 폭 처리는 별도 확인 필요(Phase E, 우선순위 낮음).

---

## 2. 설계 원칙 (고정 — 세부 구현계획서가 지켜야 함)

**D1. 관측/의도 신뢰도 구분.** "실제로 그렇게 갔다"는 관측 증거(BypassJson 등, 콘솔에 찍힌 시점엔 이미 벌어진 일)와 "이렇게 가려 한다"는 사전 계획(CHPLAN 같은 의도 증거, 실제 각 홉이 그대로 따랐는지는 별개)은 신뢰도가 다르다. 체인 항목에 이 둘을 구분 표시하는 필드를 유지·확장한다(기존 `confidence`/`inferred` 필드 재사용 우선 검토). CHPLAN이 실제로 인과적으로 relay를 발생시키는지 자체가 XXX-6이므로, 정확한 라벨링 규칙은 그거 확인 후 확정.

**D2. 마커 포맷 다양성을 정면으로 처리.** relay 증거 마커는 최소 2가지 물리 포맷(같은 줄 JSON / 설명문+다음 줄 JSON-태그없음)이 실증됐다. 단일 정규식으로 퉁치지 않고, 포맷별로 JSON을 어디서 찾을지 명시적으로 분기하는 구조로 만든다.

**D3. 양방향이 같은 재구성 메커니즘에 합류.** 상행 relay 인식과 하행 route-plan 인식은 입력 소스는 다르지만(하나는 relay 자신의 발신, 하나는 SSM의 사전 발표), 최종적으로는 **같은 `ChainLog`/`_rebuild_with_skeleton` 스켈레톤 재구성 로직 하나로 합류**시킨다. 방향별로 별도 렌더링 로직을 만들지 않는다.

**D4. 홉 수 하드코딩 금지.** 1홉이든 N홉이든 같은 코드 경로다. "리피터 1개 전용" 같은 특수 케이스를 만들지 않는다.

**D4-1. 경유 0(직결) 케이스는 이 재설계의 적용 대상이 아니다 — 그리고 그게 맞다.** BypassJson류 relay 증거는 **체인 총 H.W 갯수 ≥ 3(= 경유 장비 ≥ 1)일 때만** 실제로 발생한다(경유가 relay 행위를 해야 그 콘솔에 마커가 찍히므로). 엣지↔게이트웨이 직결(H.W 2개, 1<->1)에서는 애초에 이 마커 자체가 찍히지 않는다 — "이 경우엔 BypassJson이 쓸모없다"가 정상이지 결함이 아니다. 세부 구현계획서는 이걸 **특수 케이스로 분기 처리하지 않는다** — relay 증거 파서는 그냥 "그 마커가 보이면 relay 노드를 추가"하는 식으로만 동작해야 하고, 직결 체인은 애초에 그 마커가 안 보이니 자연스럽게 기존 tx/rx 2-node 경로 그대로 남는다. 즉 이번 재설계가 다루는 로직은 전부 **"관측되면 추가"의 additive 성격**이어야 하며, 기존 2-node 경로의 판정·렌더링 자체를 건드리는 변경은 범위 밖이다.

**D5. 기존 안전장치 보존.** `ordered=False` 폴백(경로 불확실 시 정직하게 미확정 표시), 그룹 베토(교차 그룹 오염 방지), dedup — 전부 그대로 유지한다. 이번 재설계가 이걸 약화시키면 안 된다.

**D6. 회귀 금지.** 기존 테스트 전부 green 유지. `get_topology` 응답에 새 필드가 늘면 `SPEC.md` §5 갱신, 도구 시그니처 자체는 안 바뀔 가능성이 높음(내부 파서 확장 위주).

---

## 3. 미해결 — 펌웨어 재검증 필요 (XXX, 다음 세션 최우선 과제)

**이 항목들을 다 풀기 전엔 세부 구현계획서를 쓰지 않는다.** 절반만 확인하고 짜면 이번에 발견한 것과 같은 "부분적으로 죽은 코드"가 또 생긴다.

- **XXX-1. CHPLAN 배열의 실제 구조.** `SSM_esp32.ino:6475`의 `RouteSendChplan(int targetPos, const char *planA, const char *planB)`는 `planA`/`planB`를 **별개 인자 2개**로 받아 `chplan.add(planA); chplan.add(planB)`로 각각 넣는다(→ 위치 1, 2에 별개 값 예상). 그런데 실측 로그(`2_bay_b01...txt:214`)엔 `"CHPLAN":[2,["7C","02"],4,30,0]` — **위치 1에 배열 하나**만 있다. `RouteSendChplan`의 **호출부**(caller)에서 `planA`로 뭘 넘기는지(콤마결합 문자열? 실제로는 다른 오버로드?) 추적 확인 필요.
- **XXX-2. `[Data_Pass]` 포맷 분기 규칙.** 전체 61개 호출부(APU 다수 + SB 12개 + REP + SSM) 각각의 출력 포맷(같은 줄 vs 설명문+다음줄)을 전수 분류해야 한다. 지금까진 "REP=같은 줄, SB=다음 줄"로 딱 1개씩만 표본 확인했다 — 장비별 규칙인지, 메시지 타입별 규칙인지, 예외가 있는지 불명.
- **XXX-3. 상행 relay 경로가 `BypassJsonPacket()` 하나뿐인지.** `Data_Pass`가 61곳에 흩어져 있다는 건 다른 relay성 코드 경로가 더 있을 수 있다는 뜻이다. 전수까진 아니어도 "relay 의미를 갖는 코드 경로 목록"을 확정해야 한다.
- **XXX-4. REP/SB가 복수 SSM 그룹에 걸쳐 relay할 수 있는지.** `topology_chains.py`의 그룹 베토 로직(관측 사실만 그린다는 원칙상 안전한 실패지만) 때문에, 교차 그룹 relay 설정이 실제로 가능하면 그 경우의 증거가 조용히 버려진다. REGMAC/SETBAYCONFIG 실제 운용 규칙 확인 필요(지금 테스트베드는 SSM 1대라 이 케이스가 아직 한 번도 안 나타났다).
- **XXX-5. `[Route] CHPLAN` 로그가 항상 찍히는지.** `[Data_Pass]`처럼 `fSerial` 같은 조건부 게이트가 있는지 `RouteSendChplan` 주변 확인 필요 — 게이트가 있으면 이것도 "가끔만 보이는 증거"가 되어 D1의 신뢰도 라벨링에 영향을 준다.
- **XXX-6. CHPLAN이 실제 relay를 인과적으로 발생시키는지, 아니면 정보성 로그일 뿐인지.** 즉 리피터가 실제로 이 CHPLAN 값을 보고 "나는 이 경로의 1번 홉이다"라고 판단해서 relay를 실행하는지, 아니면 relay 자체는 별개의 REGMAC 기반 판단(BypassJsonPacket 트리거 조건)으로 하고 CHPLAN은 그냥 "SSM이 의도했다"는 부가 정보인지. 이건 D1의 신뢰도 구분과 직결된다.
- **XXX-7. 2홉 이상(엣지↔경유 2개↔게이트웨이) 실측.** 지금 확보된 캡처는 1홉(SB↔REP↔SSM)뿐이다. `_rebuild_with_skeleton`은 구조적으로 N개를 지원하도록 짜여 있지만(§7.3), **실측으로 검증된 적은 없다.** 가능하면 리피터 2대 직렬 구성으로 재현 캡처를 하나 더 뜬다. 물리적으로 불가능하면 최소한 합성(fixture) 멀티홉 테스트로 구조 검증을 대체하되, 이 경우 "구조상 가능"과 "실측 확인됨"을 구분해 문서에 남긴다.

---

## 4. 작업 분해 (향후 세부 구현계획서 단위 — 각각 별도 세션)

한 번에 다 계획하면 실수가 쌓인다고 판단해 Phase로 쪼갠다. 각 Phase는 별도 세션에서 이 마스터플랜을 전제로 세부 구현계획서를 새로 쓴다.

| Phase | 내용 | 선행 조건 |
|---|---|---|
| A | §3의 XXX-1~7 전부 해소(펌웨어 정밀 재검증) | 없음 — 다음 세션 최우선 |
| B | `topology_events.py` relay 증거 통합 인식(포맷별 분기, D2) | Phase A |
| C | 하행 CHPLAN 파싱 + `topology_chains.py` 스켈레톤 반영(D1, D3) | Phase A |
| D | N홉 일반화 검증(실측 2홉 있으면 실측, 없으면 합성 멀티홉 테스트) **+ 경유 0(직결) 회귀 검증(D4-1)** | Phase A(XXX-7), B, C |
| E | 프론트 시각 폭 대응 점검(칩 다수 케이스) — 우선순위 낮음 | Phase B, C 완료 후 |
| F | SPEC.md/README 동기화, 버전 릴리스 | 전 Phase |

---

## 5. 완료 판정 기준 (전체 재설계 기준)

- 이번 캡처와 동일한 토폴로지(SB↔REP↔SSM)를 재현 시 `recent_chains`가 3-node(`SB → REP → SSM`) 순서대로, `ordered=true`로 나온다.
- 같은 토폴로지에서 하행(SSM→REP→SB) 체인도 상응하는 구조로 나온다.
- **경유 없는 엣지↔게이트웨이 직결 체인(H.W 2개, 1<->1)은 이번 변경 전후로 완전히 동일하게 동작한다** — BypassJson 등 새로 인식되는 relay 마커가 전혀 발생하지 않으므로 노드 수·순서·`ordered` 값 전부 불변이어야 한다(D4-1의 구체 검증 항목).
- 기존 테스트 전체 green, 회귀 없음.
- §3 XXX 전부 "확정" 또는 "확인 불가로 설계에 명시적 반영" 상태로 종결(방치 금지).
- 체인 로그에 회색으로 막힌 점프 버튼이 발생하지 않아야 한다.
- 완료 판정 기준을 통과 하기 위한 우회 방식은 허용 되지 않는다.
---

## 6. 비범위 (이번 마스터플랜에서 다루지 않음)

- `docs/plans/2026-07-03-chain-cidx-ident-jump-needle.md`(니들 점프 폴백)는 별개 작업이다 — 겹치는 파일(`topology_chains.py`)은 있지만 다루는 버그가 다르다. 그 작업 상태와 무관하게 진행한다.
- 웹 뷰어 시각 디자인 전면 개편(색상/레이아웃)은 범위 밖 — Phase E는 "N홉이 깨지지 않고 보이는가"만 확인한다.

---

## 7. 부록 — 원본 근거(재검증 시작점)

### 7.1 상행: `[BypassJson]` 압도적 우세, `[Data_Pass]` 희소
`docs/archive/3_repeater_last500.txt` 전체에서 `[BypassJson]` 30줄+ 반복 관측(예: L4, L7, L9, L12...), `[Data_Pass]`는 L176 단 1건(`{"REGMAC":...,"reqId":"k90xdhjy",...}`, Rev 필드 없음 — REGMAC 조회성 메시지).
펌웨어: `BypassJsonPacket()` — SB_ESP32.ino:11105, Repeat_esp32.ino, APU_SLIM_esp32.ino, APU_C_SLIM_esp32.ino에 구조적으로 동일 존재(cbm `search_graph` fp 지문 동일 확인). 트리거 조건(호출부 주석, SB_ESP32.ino:18025): `{ ~,"Rev": ~ } && (config.RegMacBuff[pos].UpDn == 1)`.

### 7.2 SSM 측 권위 소스 부재
`docs/archive/1_ssm_last500.txt`(500줄) 전수 검색: `Passed Device` 0건, `"Rt":` 0건. `[Proc-Raw Packet]`은 다수 존재하나(예: L56, L76, L107...) Rt 필드 없음 — `topology_events.py:153` 주석의 전제("Rt 제거 전 원시 패킷이라 실제 경로 토큰 보유")가 이 실측과 불일치.

### 7.3 `[Data_Pass]` 포맷 불일치
- REP(`3_repeater_last500.txt:176`): `[Data_Pass] {"REGMAC":[...],...}` — 태그+JSON 한 줄.
- SB2(`2_bay_b01_1chi-bay_last500.txt:210-211`): `[Data_Pass] Protected to bypass.` 다음 줄에 태그 없이 `{"RTC":...}` — 현재 `extract_json`이 헤더 줄에서 JSON을 못 찾아 `ids` 전부 None → `_event_key`가 `(None,None)` → `ChainLog.observe()`가 조용히 드롭(`topology_chains.py` observe 시작부 `if key is None or not port: return []`).

### 7.4 하행 CHPLAN 소스
펌웨어(`SSM_esp32.ino:6475-6499`): `RouteSendChplan(targetPos, planA, planB)`가 `CHPLAN` JSON 배열(`[ROUTE_PROTO_VERSION, planA, planB, TTL, expire_s]`)을 구성해 발신하고, 자기 콘솔에 `Serial.printf("[Route] CHPLAN to %s A=%s B=%s\n", ...)`을 찍는다(L6499). 실측(`2_bay_b01...txt:214`): `"CHPLAN":[2,["7C","02"],4,30,0]` — `"7C"`는 리피터 토큰(`3_repeater_last500.txt` 전역에서 `"Rt":["7C"]`로 반복 확인). `topology_events.py:32`의 `_HEADERS` 정규식 `\[Route\]\s*Link\b`는 "CHPLAN"을 매칭하지 않는다(다른 `[Route]` 하위 태그만 인식).

### 7.5 프론트 렌더링 — N노드 이미 일반화
`web_viewer.py:1364` `chainRow()`: `for (var i = 0; i < nodes.length; i++)` — 노드 수 무관 루프. `web_viewer.py:1826`: `row.chips.forEach((chip, i) => {...})` — 하드코딩 인덱스 없음. `chips[0]`/`chips[1]` 같은 고정 인덱스 참조 grep 결과 0건.

### 7.6 관련 코드 위치(다음 세션 시작점)
- `src/serial_mcp/topology_events.py` — `_HEADERS`(L23-33), `_fill_from_json`(L112), `_attach`(L138, `[Proc-Raw Packet]` 특례 L155-158)
- `src/serial_mcp/topology_chains.py` — `_event_key`(방향 판정), `_observe_pass`(L314), `_rebuild_with_skeleton`(스켈레톤 재구성), `observe()`의 그룹 베토
- firmware-src: `BypassJsonPacket`(SB_ESP32.ino:11105 외 3파일), `RouteSendChplan`(SSM_esp32.ino:6475), `[Data_Pass]` 61개소(cbm `search_code pattern="Data_Pass"`로 파일별 분포 확인 가능)
