# ChainLog v2 — 표기 통일·방향 수정·뷰어 폴리시 구현 핸드오프 (2026-07-02)

> **▶ 새 세션/에이전트 진입점 (자족적 맥락 캡슐).** 이 문서만 읽고 대화 맥락 없이 구현할 수
> 있게 쓴다. 선행 맥락: `docs/plans/2026-07-02-hop-chain-log.md`(ChainLog v1 — 항목 shape·
> 조립 알고리즘·대원칙 §8), `docs/plans/2026-07-02-peerlinks-universal-correlation.md`(키 파생·
> 펌웨어 태그). 레포 공통 지침은 `AGENTS.md`(빌드·검증, 커밋 규칙, 안전 제약) — 반드시 따른다.

---

## 0. 한 줄 요약

v1.8.0 체인 로그의 실장비 검증 피드백 9건을 반영한다: 체인 노드 표기를 로스터 라벨로 통일,
하달 방향([SSM]→[SB]) 표현, 방향 오분류 수정, UI 슬림화(배지 제거·칩 통일·화살표 단일),
링크선 idle 회색+활동 점등, 로그 오토스크롤 수리+스무스 밀림, 체인 로그 200줄+오토스크롤.

## 1. 실장비 진단 확정 사실 (2026-07-02, COM4=SSM-ESP·COM12=SB-ESP·COM13=SB-STM 별칭)

이 절의 로그는 전부 실측이다 — 설계의 근거이니 임의로 재해석하지 말 것.

1. **이름 3중 표기**: 같은 SB가 그래프에선 "SB5"(로스터 라벨, BayID 5 병합), 체인 src 에선
   "SB1"(펌웨어 unitName — SSM 로그 `<<< From SB1.`), 하달 수신에선 "COM12"(포트 폴백).
   → **로스터 라벨이 정답**(그래프와 일치해야 사용자가 같은 장비로 인식).
2. **하달 src 미관측**: SSM 하달 송신 로그 `[Proc_WiFiTx] Ask Info : To. SB1,
   {"RTC":[...],"CHANNEL":"11","INFO":"REQ","UnID":5}` — **Cidx 가 없다**(전송 직전 부착 추정).
   SB 수신 `[WiFi_Rx] {...,"INFO":"REQ","UnID":5,"Cidx":225}` 에만 Cidx 존재. → down 체인은
   수신 관측만으로 만들어져 칩이 하나뿐이었다.
3. **방향 오분류**: SSM 의 REQRSSI **요청**이 Unique 를 싣는다 —
   `[WiFi_Rx] {"UnID":5,"REQRSSI":"REQ","Rng":[0,4],"Unique":23,"Cidx":226}`. 현행 키 규칙
   (Unique 있으면 up)이 이를 "보고"로 오분류 → "보고 미확정 / 들림: COM12" 빈 항목의 정체.
   리프 **응답**은 `Rev:true` 마커를 싣는다 — `{"UnID":5,...,"Unique":80,"Rev":true,"Cidx":925}`.
   CHPLAN 하달도 관측: `{"CHPLAN":[...],"Asn":24,"UnID":5,"Cidx":227}` (Unique 없음).
4. **UnID 의미 이중성**: 리프 발신 페이로드의 UnID=발신자, SSM 요청 페이로드의 UnID=**대상**.
   방향을 모르면 UnID 만으로 발신자를 단정할 수 없다(§4 방향 판정이 선행돼야 하는 이유).

## 2. 뷰어 현행 구조 확정 사실 (코드 탐색, v1.8.0 시점 라인)

- 로그 페인은 **자체 스크롤 컨테이너가 아니라 window/body 스크롤**: `#stream`/`#buffer` 는
  `main` 안의 평범한 div(web_viewer.py:777-778, main 패딩 :407). follow 는
  `window.scrollTo(0, body.scrollHeight)`/`window.scrollY` 기반(:2023, :2044).
- `MAX_STREAM = 5000`(:1724). 초과 시 상단 행 removeChild 루프(:2015-2019)에 **scrollTop 보정
  없음** → 문서 높이가 줄며 화면 점프. **follow off 여도 발생** — "풀어도 막 움직인다"의 원인.
  scroll 핸들러(:2252-2254)는 follow 자동 해제도 안 함(new pill 숨김만).
- 행 추가/제거 트랜지션 없음(순간이동). `.ln` = grid(9ch | 1fr) 행(:409-430).
- `renderEdges`(:1484-1510): stroke=`rssiColor(rssi)`(강한 RSSI=초록, :1211), width 2 고정,
  opacity 0.85/heard 0.55/stale 0.3. **정적** — 활동 연출은 홉 펄스(.thop, :1652-1676)만.
- 체인 패널 상한: 그룹당 표시 8(:1681) / 클라 `state.chains` 100(:2122) / 서버 viewer
  `chains_n=30`(server.py:1507) / MCP get_topology `chains_n=20`(server.py:1124).
- 체인 정렬: chainGroups 가 그룹 내 id **내림차순**(최신 위)(:1379 부근).
- VIEWER-PURE 블록(ES5·DOM 비의존, 줄 ~796-1390): chainRow/chainGroups/hopColor/rssiColor/
  edgeSegments — `tests/viewer_logic_harness.cjs` 가 pytest(test_viewer_logic.py) 경유 검증.

## 3. 요구사항 (사용자 확정, 2026-07-02)

R1 체인 노드 이름을 로스터 라벨로 통일("포트↔H.W를 이미 아는데 왜 COM12?").
R2 하달/보고 배지 제거, 칩 체인을 그 자리로 올려 행을 얇게.
R3 화살표 →/⇒ 혼용 제거 — `→` 하나로.
R4 칩(박스) 폭 통일 — 라벨 길이 무관, 깔끔하게.
R5 [SSM]→[SB] 하달 방향이 보여야 함(현재 [SB]→[SSM]류만 나옴).
R6 링크선 초록 고정 해제 — idle 얇은 회색, 통신 관측 시마다 점등 후 **0.5s** 내 빠른 복귀.
R7 오토스크롤 off 인데 화면이 움직이는 버그 수리(+위로 스크롤 시 자동 off).
R8 로그 밀림 스무스(순간이동 금지) — 스트림·버퍼(포트 로그) 탭 모두.
R9 체인 로그 보관/표시 200줄(현행 그룹당 8은 너무 적음) + 체인 로그에도 오토스크롤 +
   체인 로그에도 스무스 밀림.

## 4. 설계 — 서버측 (`topology_chains.py` 중심)

### 4-1. 방향 판정 보강 (R5 전제)

dir 를 키 종류 폴백만으로 정하지 않는다. 증거 우선순위(강→약):

1. **관측 kind 확정**: `rx`(SSM 수신) 관측 = up 확정 / `wifitx`(SSM 송신) 관측 = down 확정.
   이미 반대 dir 로 만들어진 활성 항목이 이 증거를 만나면 dir 를 **교정**한다(노드 role 재배치:
   up→down 교정 시 heard 를 수신 노드로 승격하는 등 — 단순화를 위해 교정 시 nodes 를
   재구성해도 됨. 실측상 교정은 드묾).
2. **페이로드 마커**(`ev["json"]` dict 검사): `Rev is True` → up(리프 응답).
   `INFO=="REQ"` / `REQRSSI=="REQ"` / `"CHPLAN" in json` → down(SSM 요청). §1-3 실측 근거.
3. **키 종류 폴백**(현행): Unique → up, Cidx → down.

구현 위치: `_event_key` 는 그대로(dedup 키), dir 힌트 함수 `_dir_hint(ev) -> "up"|"down"|None`
신설(kind→①, json→②), observe 에서 엔트리 dir 설정·교정에 사용.

### 4-2. 하달 src 추론 (R5)

`_public()` 변환 시(관측 상태 오염 없음): dir=="down" 이고 src 노드 없으면 합성 src 를 앞에 붙인다.

- group(ssm_port) 확정 → `{"name": None, "port": group, "role": "src", "rssi": None, "ms": None,
  "resolved": True, "inferred": True}` — 근거: Cidx=SSM 전용 송신 카운터(펌웨어 확정) + 이
  엔트리의 그룹 SSM 포트. 관측이 아닌 **의미론적 추론**이므로 `inferred:true` 로 구분(뷰어 dim).
- group 미확정(standalone 등) → `{"name": None, "port": None, "role": "src", "resolved": False,
  "inferred": True}` ("?" 칩).
- 기존 노드에도 `inferred` 키 추가(기본 False) — public shape 일관.

### 4-3. 자기 에코 승격 (잔여 "들림" 정리)

`observe(ev, scope, resolver, port_names, port_idents=None)` 로 시그니처 확장.
`port_idents = {port: ident}`(ident=unid∥mac) — 엔진 membership 유래 캐시(§5).

wifirx + dir up 처리에서: 페이로드 ident(`ids.unid∥ids.mac`)가 `port_idents.get(port)` 와
일치하면 **heard 가 아니라 src 확정**(그 포트 장비가 발신자 본인 — 자기 에코는 발신 증거다.
§1 [WiFi_Rx] dedup 전 출력·자기 에코 실측). 불일치/미상이면 기존 heard.

### 4-4. 엔진 (`topology_engine.py`)

- `_port_idents()` 캐시 신설: membership 에서 `{local_port: unid}` (+ ident 가 mac 인 항목도
  동일 규칙) — `_peer_scope`/`_names` 와 같은 dirty 무효화 공유. `_drain` 에서
  `chains.observe(..., port_idents=self._port_idents())` 전달.

### 4-5. 라벨 파리티 (`topology.py`·`server.py`)

- `topology.py` 에 순수 함수 신설:
  ```python
  def port_labels(roster: dict) -> dict:
      """roster.groups[].nodes[].ports[].port → 그 노드 label 매핑({port: label})."""
  ```
- `server.py get_topology`: roster 빌드 후(Lock 밖) `recent_chains` 각 노드에
  `label` 필드 부여 — 우선순위 **roster 라벨(port 있을 때) > name(mesh) > port > None**.
  뷰어 SSE 항목은 원문 유지(뷰어가 스스로 매핑 — §6-1).
- `server.py _viewer_topology_info`: `chains_n=30 → 200` (R9).
- MCP get_topology 는 `chains_n=20` 유지(AI 응답 크기 절제) + docstring 에 label 필드 한 줄.

## 5. 설계 — 뷰어 (`web_viewer.py`)

### 5-1. VIEWER-PURE (ES5 유지 — var/함수선언, template literal 금지)

- `portLabelMap(groups)` 신설: `{port: node_label}` (§4-5 JS 대응 — chainGroups 내부 or 별도
  export, 하네스 테스트).
- `chainRow(entry, labels)`: 칩 label 우선순위 = `labels[port] > name > port > "?"`.
  **dirLabel 제거**(R2). 칩 메타(rssi/ms)는 별도 span 이 아니라 **title 툴팁 문자열로만**
  (mesh 이름도 툴팁에 병기 — 예: `"SB1 · -21dBm · 52ms"`)(R4). `dim` = 포트 없음 ∥
  resolved===false ∥ **inferred===true**. nodes 가 비면 `[{label:"발신 미상", dim:true}]`.
- `chainGroups(chains, groups, cap)`: 정렬 **id 오름차순(최신 아래)** 로 변경(R9 오토스크롤),
  그룹당 cap 제거(cap 파라미터는 전체 상한으로 재정의 or 미사용 — 전체는 클라 200 상한이 담당).

### 5-2. 체인 패널 DOM (R2·R8·R9)

- 행 = 한 줄: `[상태 dot][칩→칩→칩][성공/미확정]` (.tch-head 에 path 통합, 배지 제거).
  화살표는 항상 `"→"`(R3 — `⇒` 제거).
- **증분 렌더로 전환**: 현행 전체 재렌더(innerHTML="") 대신 entry id → 행 요소 맵을 유지하고
  upsert 된 항목만 교체/추가. 새 행·교체 행에 `.tch-enter`(opacity/translateY 0.15s) 트랜지션.
  그룹 헤더는 그룹 등장 시에만 생성.
- **오토스크롤**: `.topohops` 컨테이너(이미 max-height+overflow-y) 바닥 고정 follow —
  위로 스크롤하면 자동 off, 바닥 근처 복귀 시 재고정. upsert 시 follow 면 scrollTop=바닥.
- 클라 `state.chains` 상한 100 → **200**.

### 5-3. 링크선 (R6)

- 기본: `stroke: #4a5563`(회색 고정 — rssiColor 사용 중단), `stroke-width: 1.2`,
  opacity 0.6 / stale 0.25 / heard 점선(dasharray 유지)+0.45. RSSI·출처·via 는 **툴팁에만**.
- 점등: `renderEdges` 가 각 line 에 `data-ekey`(무방향 `frozenset` 대응 — `[a,b].sort().join("|")`)
  부여. SSE 수신 시 활성화:
  - 홉: `src_port`/`rx_port` 쌍.
  - 체인 변경(`obj.chain`): nodes 의 **인접 포트 보유 노드쌍**(port 있는 노드끼리 인접 순서).
  - 해당 ekey 의 line 에 `.eactive` 클래스 → CSS `transition: stroke .15s, stroke-width .15s,
    stroke-opacity .15s` 로 초록(#3fb950)·width 2.5·opacity 1 점등, **500ms 후** 클래스 제거
    (타이머, 사용자 확정 — 1.5s 는 느림). 재렌더로 요소가 교체되면 진행 중 점등 유실 수용.

### 5-4. 로그 스크롤 (R7·R8)

- `#stream`/`#buffer` 를 자체 스크롤 컨테이너로 전환: `overflow-y:auto`,
  `height: calc(100vh - <상단바 실측 높이>)`(SB 헤더/탭 높이는 구현 시 실측), `overflow-anchor:auto`.
- follow 로직을 window → 컨테이너 기준으로 전면 교체: `setFollow`(:2179) 의
  `window.scrollTo` → `box.scrollTop = box.scrollHeight`; nearBottom(:2044·:2252) →
  `box.scrollHeight - box.scrollTop - box.clientHeight < 임계`; new pill 동일.
- **상단 행 제거 보정**: MAX_STREAM 초과 removeChild 루프에서 follow off 면 제거된 행 높이
  합만큼 `box.scrollTop -= removed_h` (점프 제거). follow on 이면 바닥 재고정이라 불필요.
- **follow 자동 off**: 컨테이너 scroll 이벤트에서 사용자가 바닥에서 임계 이상 벗어나면
  `setFollow(false)` (프로그램적 스크롤로 오발동하지 않게 자체 스크롤 직후 플래그 가드).
  바닥 복귀 on 은 ↓버튼/new-pill 클릭(자동 재고정 금지 — 사용자 의도 존중).
- **스무스**: 새 `.ln` 행에 `.ln-enter`(opacity 0→1, translateY(4px)→0, 0.15s) — appendEntry
  공용 경로에 넣어 **스트림·버퍼 양쪽 적용**(R8). follow-on 스크롤은 rAF 스로틀(매 줄
  scrollTo 호출 방지). MAX_STREAM 제거는 동기 removeChild 유지(퇴장 애니메이션은 스코프 외).

### 5-5. CSS 추가/변경 요약

`.thd-chip{min-width:64px; max-width:120px; text-align:center; overflow:hidden;
text-overflow:ellipsis; white-space:nowrap}` / `.thd-chip.dim`(기존) / `.tch-row` 한 줄 flex /
`.tch-enter`·`.ln-enter` keyframe or transition / `.tedges line` 회색 기본+`.eactive` /
`#stream,#buffer{overflow-y:auto; height:calc(...); overflow-anchor:auto}`.

## 6. 반드시 지킬 대원칙 (기존 확립 — 위반 금지)

- **관측만 그린다·강제 금지**. 유일한 예외가 §4-2 의 추론 src 이며 반드시 `inferred:true` 로
  구분 표기한다(단정 금지의 타협점 — 사용자 승인됨).
- **서버 시각 미노출·순서≠인과**: 체인/홉 항목에 서버 ts 직렬화 금지(id=seq 유지).
- **stdout 금지**(MCP JSON-RPC), 공유 상태는 엔진 Lock 안, 무거운 일은 Lock 밖.
- **클라이언트 파리티**: 라벨 통일은 get_topology(label 필드)와 뷰어 양쪽에.
- VIEWER-PURE 블록 ES5·DOM 비의존 유지, 하네스로 검증.
- 커밋: 한국어 + Conventional Commits, 아래 §8 단위 분리.

## 7. 테스트 목록

- `tests/test_topology_chains.py` 추가: ① REQRSSI 요청(wifirx, Unique+Cidx+REQ) → dir down·
  수신 노드(heard 아님) ② Rev:true 응답 → up 유지 ③ rx 관측이 down 오판을 up 으로 교정
  ④ down src 추론(group 확정 → inferred SSM src / 미확정 → "?" src) — public 에만, 내부 상태
  불변 ⑤ 자기 에코 승격(port_idents 일치 wifirx → src, 불일치 → heard) ⑥ inferred 필드 기본
  False ⑦ 기존 스위트 green.
- `tests/test_topology.py`: `port_labels` — SB 병합 노드(ports 2개)·SSM·미배치 포트 제외.
- `tests/test_tools.py`: get_topology recent_chains 노드 label enrich(roster 라벨 우선).
- `tests/test_topology_engine.py`: `_port_idents` 전달 관통(에코 승격 통합).
- `tests/viewer_logic_harness.cjs`: portLabelMap / chainRow(labels 우선순위·dirLabel 부재·
  툴팁 문자열·inferred dim·발신 미상) / chainGroups(id 오름차순·그룹 cap 제거).
- 뷰어 DOM/스크롤(§5-2~5-4)은 JS 하네스 범위 밖 — 실장비 검증(§9)으로 확인.

## 8. 구현 단계 (권장 커밋 단위)

1. `feat: 체인 방향 판정 보강·하달 src 추론·자기 에코 승격` — topology_chains + engine(_port_idents) + 테스트.
2. `feat: 체인 노드 라벨 파리티 — port_labels·get_topology enrich` — topology.py + server.py + 테스트.
3. `feat: 뷰어 체인 로그 개편 — 라벨맵·배지 제거·증분 렌더·오토스크롤·200줄` — VIEWER-PURE + DOM/CSS + 하네스.
4. `feat: 뷰어 링크선 회색·활동 점등 + 로그 스크롤 컨테이너·스무스` — renderEdges/.eactive + 스크롤 전환.
5. `docs:` 본 문서 머리에 완료 상태 한 줄(본문 소급 수정 금지).

## 9. 검증

- `uv run python -m pytest` — ⚠️ 이 PC 는 `uv run pytest` 가 trampoline 오류. 전 스위트 green
  + `py -m compileall -q src`.
- 실장비(COM4=SSM, COM12/13=SB, 115200): ① 체인 칩 라벨이 그래프와 동일(SSM·SB5 — SB1/COM12
  혼용 소멸, mesh 이름은 툴팁) ② 하달이 `[SSM] → [SB5]`(SSM 칩 dim) ③ REQRSSI 요청이 하달로
  분류(빈 "들림" 항목 소멸) ④ 링크선 idle 회색 → 통신 시 0.5s 점등 ⑤ follow off 에서 화면
  완전 정지(5000줄 초과 상태 포함), 위로 스크롤 시 자동 off ⑥ 새 줄·새 체인 행 트랜지션
  ⑦ 체인 로그 200개 축적·바닥 고정. 위험 명령 금지, 쓰기는 elicitation 게이트 준수.

## 10. 완료 기준

- [ ] 전 테스트 green + compileall OK
- [ ] 체인 노드 라벨 = 로스터 라벨(뷰어·get_topology 파리티), mesh 이름·메타는 툴팁
- [ ] 하달 `[SSM]→[SB]` 표현(inferred dim) + REQRSSI 오분류 수정
- [ ] 배지 제거·`→` 단일·칩 폭 통일로 행 슬림화
- [ ] 링크선 idle 회색·활동 0.5s 점등
- [ ] follow off 완전 정지·자동 off·스무스 밀림(스트림·버퍼·체인 모두)
- [ ] 체인 로그 200줄 + 바닥 고정 오토스크롤
- [ ] 이 문서 머리 상태 한 줄 + §8 단위 커밋
