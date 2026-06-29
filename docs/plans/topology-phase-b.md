# 통합 토폴로지 사이드바 — 멀티홉 메시 시각화 + 전포트 로그 탐색 (총정리본 v2)

> **▶ 핸드오프 (2026-06-26 재작성)** — 이 문서는 GPT 리뷰 + cbm 펌웨어 검증 + SSM-부재 케이스까지 반영한 **단일 진실원 총정리본**이다.
> - **Phase A 완료·커밋·push** (main `3cdcbbf`, origin 동기화).
> - **Phase B 진행(2026-06-29, ultracode 자율 — 모듈별 TDD→Workflow 적대리뷰→수정→커밋)**:
>   ✅모듈1 classifier `9f23da9` · ✅모듈2 events `464e7d2` · ✅모듈3 correlator `9f83ca7`
>   · ✅모듈4 routing `f31a097`(링크그래프·토큰맵·RSSI ladder, 예약토큰 '00'/'FF' 가드)
>   · ✅모듈5 roster `a365786`(standalone 그룹·링크 edges·원격 mesh 노드·노드 enrich, build_roster 확장)
>   · ✅모듈6-a `topology_engine.py` TopologyEngine(observe·sweep·roster·recent_hops, 순수 상태+Lock)
>   · ✅모듈6-b server.py 배선 `a32693c`(on_line observe탭·sweep 데몬·engine.roster·bootstrap INFO[기본 OFF opt-in])
>   · ✅모듈6-b 후속 owner 생애주기 통합 테스트 보완(엔진 생성·sweep thread·release join·engine None 정리). **365 테스트 그린, 미push.**
>   · ✅모듈7 routes `/api/topology/stream` 홉 SSE(RawFeed payload 일반화·observe/sweep hop publish·ViewerServer 배선). **368 테스트 그린, 미push.**
>   · ✅Phase C `get_topology` MCP(전포트 roster + recent_hops 20, SPEC/README 동기화). **370 테스트 그린, 미push.**
>   남은: 8 front(홉 애니메이션·디테일 패널) → 실장비 e2e → 배포 레포 serial 스킬 도구목록 동기화.
>
> **▶ 모듈7-8 핸드오프(routes·front)**:
>   - **7 routes**: `/api/topology` 는 이미 `_viewer_topology_info`→`engine.roster` 로 동작(모듈6-b). 남은 건
>     `/api/topology/stream`(홉 SSE) — `web_viewer.py` 기존 `_serve_stream`(RawFeed 구독→헤더→하트비트, drop-oldest)
>     패턴 일반화. 홉 소스: `TopologyEngine` 에 `HopFeed`(viewer_feed.RawFeed payload Any 일반화) 추가하거나
>     observe/sweep 반환 홉을 feed.publish. ViewerServer 에 `topology_stream` 콜백 배선(server.py 가 주입).
>   - **8 front**: `web_viewer.py` `_HTML` 좌측 — `/api/topology/stream`→`window.topologyHop(hop)` 엣지 하이라이트·
>     경로 애니메이션(바닐라 SVG+rAF). 로스터 `edges`(standalone 그룹 포함) 렌더 + 디테일 패널(경로 칩·구간 RSSI·
>     quality·실패/미확정). 순수 로직(엣지 geometry·rssiColor)은 VIEWER-PURE + `viewer_logic_harness.cjs`.
>   - **Phase C**: `get_topology` MCP 도구 = `{status, message, roster, recent_hops, viewer_url}`.
>     `roster` 는 `engine.roster(entries)`, `recent_hops` 는 `engine.recent_hops(20)`. SPEC §5 조회 6→7종·README 동기화 완료. 배포 레포 `serial` 스킬 도구목록 동기화는 후속.
>   신규 모듈 flat 파일: topology_events·_correlator·_routing·_engine.py(향후 topology/ 패키지화 가능).
>   상관기 핵심: (UnID,Unique) 1차키·실패vs unconfirmed 는 'SSM이 들은 UnID' 단위 스코프(전역래치 아님).
> - **Phase B/C 설계 확정** = 이 문서. 상관기·분류기·경로 모델이 v1(단일키)에서 **대폭 개정**됐다(§5 펌웨어 사실이 근거).
> - **범위 = A(뷰어) + B(엔진) + C(`get_topology` MCP)**, 모두 확정.
> - 실측 TDD 픽스처는 **§14 부록**에 박제(스크래치패드는 세션 전용이라 사본 동결).

---

## 1. Context — 원동기 (왜 이 변경인가)

같은 종류 장비가 여러 개인 메시 망(SSM 1 + REP/APU/SB 다수)에서 "SB가 TX → SSM에서 RX 확인"은 **틀린 전제**다.
실제 경로는 `SB→(REP)→(APU)→SSM`처럼 홉이 늘고 RX 로그가 **어느 포트에 떨어질지 모른다.** AI가 단일 포트만 보면
"도착 안 함/엉뚱한 포트 → 이상함"으로 **오판**한다. 이 오판을 닫는 게 목표다.

- **A·B(웹 뷰어)는 운영자(사람)용** 시각화.
- **AI-대면 해결은 C(`get_topology` MCP 도구)** — AI는 뷰어를 안 보고 MCP 도구를 쓰므로, C라야 원동기가 닫힌다. B 엔진이 C의 전제.

---

## 2. 확정 결정 요약

| 항목 | 결정 |
|---|---|
| 범위 | A(뷰어) + B(엔진) + C(MCP 도구) 전부 |
| 장비 분류 | **INFO[0] 장비타입 enum** 기반 별도 `DeviceClassifier` 모듈(4단계, confidence 부여) |
| 상관기 | **다중키(multi-key)** — 단일 `(UnID,Unique)` 폐기 |
| 경로 vs 링크 | **실제 경로=`Rt`/`[Passed Device]`**, **링크품질 그래프=`REPRSSI`/`[Route] Link`**(분리) |
| RSSI | 폴백 ladder, per-packet RSSI는 옵션(자동 토글 금지) |
| SSM 부재(SB 단독) | **standalone group** + 실패처리 금지(observed_tx) + 부트스트랩 INFO 비전송 |
| 부트스트랩 INFO | 첫 SSM 식별 후 **서버-내부 1회**(SSM 포트 한정), 부팅 window 중 금지 |
| 식별·배치 출처 | 로그 자동발견(별칭 우선) |
| [해제] | 세션 전체 반납(SPEC §10), 단일 버튼 |

**UI 사용자 확정 규칙**(디자인 목업과 의도적 차이 — 재현 시 준수): ①타입/식별 라벨은 노드 밖(위), 안엔 MCU(ESP/STM)
②SB는 좌우 절반 분할(왼 ESP·오 STM), 칸별 상태점·칸 클릭=그 포트 ③그룹 박스 위 "그룹 N" 순번 ④미분류는 `COMx`
뱃지만(텍스트·박스 없음, 흰색) ⑤범례 없음 ⑥접기/펼치기 없음·항상 펼침 ⑦SB 라벨=BayID/UnID(SB5). (memento `preference` 파편에도 있음.)

### 제약 (반드시 준수)
- 새 의존성 0. 디자인(React+support.js)을 **바닐라 JS/SVG로 포팅**(인라인·오프라인).
- stdout 금지(MCP JSON-RPC) — 진단은 stderr/tee(`_log`)만.
- 뷰어 실패가 MCP 코어에 영향 없어야 하고, 느린 브라우저가 시리얼 경로를 막으면 안 됨(drop-oldest, 관측 비차단).
- 버퍼/공유상태 Lock 보호. 순수 로직은 I/O와 분리(테스트 가능).
- 클라이언트 파리티: Claude Code·Codex 동일 동작.

---

## 3. 현재 상태 — Phase A (✅ 완료)

`src/serial_mcp/topology.py`(순수: `parse_alias`·`classify_lines`·`identify_port`·`build_roster`) + `tests/test_topology.py`(15) /
`server.py` `_viewer_topology_info`+`/api/topology` / `web_viewer.py` 좌측 `renderTopology`(절대배치·SB `[ESP|STM]` 분할·노드클릭
→`selectPort`·미분류 `COMx`·[해제]). 커밋 `3cdcbbf`. **실 하드웨어 e2e만 보류**(라이브 뷰어 점유) → Phase B와 함께.

> Phase B는 Phase A의 분류·로스터 골격을 **유지하고 그 위에 얹는다**(아래 모듈로 확장·분리).

---

## 4. 펌웨어 사실 — cbm 검증 (구현의 근거, 인덱스 `C-Users-User-projects-firmware-src`)

이 절이 §5~§8 설계의 근거다. **[✓]=cbm 소스 확인, [실장비]=실측 확인 필요.**

- **[✓] 장비타입 enum**: `SSM_esp32.h:468–472`·repeater 헤더 — `dTSSM=1`·`dTAPU=2`·`dTAPU_C_SLIM=3`·`dTSBB=4`·`dTRPT=5`.
- **[✓] INFO[0]=장비타입**: 각 장비 `[Tx - my INFO] {"INFO":["4",…]}`의 `INFO[0]`이 타입숫자(SB="4"). 실측 픽스처에도 존재.
- **[✓] simplevInfoBuffer는 5를 숫자로 흘림**: 타입 문자열 변환 삼항이 2/3/4(APU/APU_C/SB)만 처리 → Repeater(5)는 `"5"`로 남음 → 서버 `DEVICE_TYPE` 맵에 `"5"→REPEAT` 포함 필수.
- **[✓] 릴레이 2경로**: keepalive(`Alive==SSM`)는 `[Data_Pass]`로 원문 verbatim(`Repeat_esp32.ino:5924` `sendMessage(sWiFiRx)`); 일반 데이터는 `BypassJsonPacket`이 **자기 토큰을 `Rt` 배열에 append** 후 재직렬화. 둘 다 **UnID/Unique 보존**(+내용 dedup `chk_DuplicateRev`) — 단 일반경로는 byte-verbatim 아님(Rt 증가).
- **[✓] 실제 경로 = `Rt`**: `BypassJsonPacket`(SB·APU·APU_C·Repeater 공통)이 토큰(BayID 2-hex 또는 `retEncrytion(mac)`)을 `Rt[]`에 append. `RouteTokenForInfoPos`가 토큰=UnitID(2-hex)/retEncrytion 규칙. SSM `WiFi_rev_proc`이 `Rt`를 토큰→unitName 매핑해 **`[Passed Device] (05-SB5)->(01-REP1)`** 문자열 출력(이미 해소된 형태).
- **[✓] 링크 그래프 = REPRSSI**: `RouteUpdateLinkFromReprssi`(`SSM_esp32.ino:6709`)가 REPRSSI를 `RouteLinkMatrix` 인접행렬로 구축, `[Route] Link <mac> -> <mac> rssi=` 출력. `CheckInfoTable` 미스 MAC은 skip(등록 노드 한정). `lastUpdateMs`로 링크 신선도. → **경로가 아니라 "가능한 링크".**
- **[✓] 경로 품질 자체 산출**: `RouteRelayScore`(`-rssi*10 + avrtakentime/10 + linkPenalty`, :6394)·`RouteDirectGood`(rssi≥`ROUTE_MIN_RSSI` && avrtakentime≤1000ms, :6340). → 홉 `quality`는 이 출력 파싱으로(serial 출력 여부 [실장비]).
- **[✓] INFO 요청엔 Unique 없음**: `ReqInfoTo`(SSM)는 RTC·CHANNEL·INFO:REQ·UnID/mac만(`getUniqueValue` 미호출), `sendMessage`가 Cidx 추가. 장비 응답은 새 Unique 발급 → **요청↔응답을 Unique로 못 묶음.** Unique는 "장비응답 TX↔SSM RX" 단방향 leg와 dedup에만 유효.
- **[✓] RX 이벤트는 멀티라인 블록**: `WiFi_rev_proc`(`SSM_esp32.ino:6873–9582`)이 `[Proc-WiFiRx]`(UnID/Unique)→`[Proc-Raw Packet]`→`[Passed Device]`→`<<< From SBn`→` -- takentime`(:7298)→`[Proc-WebRTx] …REPRSSI…`를 **별개 `Serial.printf`**로 출력. JSON은 줄 앞에 태그가 붙음 → "첫 `{`/`[`부터 파싱".
- **[✓] per-packet RSSI는 조건부**: `[Proc-WiFiRx] From Mac.. RSSI`는 `fvExtUnitInfo==true`일 때만(`VEXTUNITINFO` 토글, 전 보드). 토글은 **상태 변경=비읽기전용** → 자동 송신 금지.
- **[✓] SSM이 통신성공률 집계**: 노드별 `cntRev/cntReq` → `simplevInfoBuffer`(INFO)가 `ComRate(Rev/Req)` 열 출력 → **실패/건강 신호를 상관 없이도 직접 획득.**
- **[✓] 부팅·serialCmd 파괴 명령**: SB-ESP `serialCmd`가 `REFLASHESP`/`REFLASHSTM`/`DOWNBIN` 등 수신·실행, 부트메뉴 단독키 `'D'`=다운로드/리플래시(serial-mcp `_is_r3`와 일치). → **부팅 window 중 자동 송신 절대 금지.**
- **[실장비] SB 단독(SSM 부재) 분류 시그니처**: boot log / `FWVER` / `Serial2 Begin` / STM32 `SmartBay FW v…` 배너. (사용자 제공 — 실 부팅 로그로 정확한 문자열 확정.)

---

## 5. 아키텍처 — 모듈 분리

```
[SerialReader._run] 줄마다 (port, ts, text)
   ├─ buffer.add / feed.publish                 (기존)
   └─ engine.observe(port, ts, text)            ← 신규 탭 (관측만, 비차단, 예외 삼킴)
                │
        [TopologyEngine] (server.py 보유, 상태+Lock)
          ├─ classifier   : 포트/로그 → {type, confidence, source, mcu}   (DeviceClassifier)
          ├─ events       : 포트별 라인 누산 → 논리 Event(block, JSON 파싱)
          ├─ correlator   : 다중키 매칭 → Hop(path/segments/ok/quality)
          ├─ routing      : Rt/token map / REPRSSI 링크그래프 / RSSI ladder
          ├─ roster       : groups(SSM별 + standalone) · nodes · edges
          ├─ bootstrap    : 첫 SSM 식별 후 서버-내부 INFO 1회(SSM 포트, boot-window guard)
          └─ sweep        : 타이머 → 만료 pending 처리(no-SSM은 unconfirmed)
                │                         │
        /api/topology (로스터 JSON)   /api/topology/stream (홉 SSE, RawFeed 일반화)
                │                         │
        [web_viewer.py 좌측] 바닐라 JS/SVG: 노드·엣지·경로 애니메이션·디테일 패널 · 노드클릭→selectPort
                │
        [get_topology MCP 도구] (Phase C) — 로스터+최근 홉 요약, 읽기 전용
```

순수 로직(`topology/` 하위: classifier·events·correlator·routing·roster)은 I/O·DOM과 분리해 단위테스트.
`topology.py` 단일 파일이 커지면 `topology/` 패키지로 분리(Phase A 함수는 호환 유지).

---

## 6. 자료구조 (계약)

```python
# 논리 이벤트(블록 조립 결과)
Event = {
  "port", "ts",                       # ts = 서버 도착 단조시각(윈도 클럭). 펌웨어 RTC 아님
  "kind",                             # tx | rx | route | info | webtx | stm32 | unknown
  "tag",                              # Proc-WiFiRx | Tx-my-INFO | Route-Link | Passed-Device ...
  "raw_lines", "json",                # json: 첫 {/[ 부터 파싱(태그 접두사 제거)
  "ids":    {mac, unid, unique, asn, cidx, route_pid, rt_tokens:[]},
  "hints":  {src_name, dst_name, device_type},   # device_type = INFO[0]
  "metrics":{rssi, takentime_ms, reprssi:[{mac,rssi,snr?}], rs:[], comrate?},
}

# 노드(로스터·get_topology 공용)
Node = {
  "id",                               # "mac:AA:.." 또는 "port:COM14"
  "type",                             # SSM|APU|APU_C|REPEAT|SB|SB_STM32|UNKNOWN
  "type_confidence", "type_source",   # info_json|ssm_table|stm32_banner|signature|manual|route_name
  "label", "mac", "unit_id", "route_token",
  "row", "col", "status",             # good|stale|unknown (직접노드=good/stale, 원격 mesh=unknown)
  "ports":[{mcu, port, connected}],   # mesh-only 원격노드면 []
}

# 홉(상관기 출력, SSE)
Hop = {
  "id", "ts", "kind",                 # info|event|reprssi|route_ack|web_bridge|inferred_timeout
  "confidence",                       # observed | inferred | timeout | unconfirmed(no-SSM)
  "keys": {mac, unid, unique, asn, cidx, route_pid, rt_tokens:[]},
  "path": ["node:SB5","node:REP1","node:SSM"],
  "segments":[{from, to, rssi, source}],   # source: Rt | RouteLink | inferred
  "ok", "metrics":{takentime_ms, rtt_ms, rssi, response_ms, quality},
}

# 로스터 스냅샷
Roster = {
  "groups":[{id, label, ssm_port|None, kind:"ssm"|"standalone", nodes:[Node], edges:[{from,to,rssi,fresh}]}],
  "unplaced":[port...],
}
```
프론트는 **`path`/`segments`만** 보고 그린다(Unique·Rt·REPRSSI 해석은 백엔드에서 끝낸다).

---

## 7. Phase B — 모듈별 구현 (TDD)

### 7-1. `DeviceClassifier` (장비 분류, 별도 모듈)
4단계, 신뢰도 부여:
1. **강한 증거**: `[Tx - my INFO]` `INFO[0]` 숫자(2/3/4/5) → APU/APU_C/SB/REPEAT. `<< Information on the entire equipment >>`→SSM. STM32 `SmartBay FW` 배너→SB_STM32.
2. **SSM INFO 테이블**: `simplevInfoBuffer` 행 파싱(자기+원격). `DEVICE_TYPE` 맵에 **`"5"→REPEAT` 포함**(숫자로 흘림).
3. **SSM 시그니처 로그**: `[Proc-WebRTx]`/`[Proc-WiFiRx]`/`[Route] Link`/`<< Information…`.
4. **confidence/type_source** 부여(`info_json`>`ssm_table`>`stm32_banner`>`signature`). 미확정은 `UNKNOWN`.
- **SSM 부재 대비**: `[Tx-my INFO]`만 기다리지 말 것 — SB 단독이면 boot/FWVER/Serial2/STM32 배너로 분류.

### 7-2. `events` (블록 조립 + JSON 파싱)
- 포트별 라인 누산기: `[Proc-WiFiRx]` 헤더에서 블록 시작 → 후속 `[Passed Device]`/`<<<From SBn`/`-- takentime`/`[Proc-WebRTx] REPRSSI` 부착 → 다음 헤더/비연속/타임아웃에 flush. **줄단위 무상태 금지.**
- JSON: "첫 `{`/`[` 부터 파싱"(태그 접두사·Socket.IO 배열 `[` 대응). REPRSSI 행 `[mac,rssi]`/`[mac,rssi,snr]` 가변 수용.

### 7-3. `correlator` (다중키, 순서무관, 단측 허용)
- **이벤트 종류별 키**: INFO=`Mac/UnID`+시간창 / 일반=`Asn`(보조 Mac·UnID·Unique) / 라우팅=`Rt`/`R`토큰 / ACK=`RS` packetId / 링크=`from→to mac` / **dedup**=raw JSON 해시 또는 `Cidx`/`Unique`.
- **윈도 클럭=서버 도착 ts**(단조), pending **상한+drop-oldest**. **포트내 dedup**(메시 브로드캐스트 다중수신).
- **경로 우선순위**: ① `Rt`/`[Passed Device]` ② `R` 헤더(srcToken/nextHop/dstToken) ③ `<<<From SBn`+SSM 포트 ④ Mac/UnID unknown 노드.
- **성공/실패/미확정**:
  - SSM RX(REPRSSI/Passed Device) → `ok:true` 즉시 방출.
  - TX 있고 윈도 내 SSM RX 없음(SSM 존재 context) → 스윕 후 `ok:false`(실패 레이어).
  - **SSM 부재 context → 실패 처리 금지**: `confidence:"unconfirmed"`/`kind:"observed_tx"`로 방출.
  - 보강 신호: SSM INFO표 `ComRate(Rev/Req)`로 노드 성공률 직접 반영.

### 7-4. `routing` (토큰맵·링크그래프·RSSI)
- 토큰→노드 맵: SSM INFO 테이블(UnitID/mac) 기반, `RouteTokenForInfoPos` 규칙(UnitID 2-hex / retEncrytion).
- 링크 그래프: `[Route] Link` → `edges`(rssi, `lastUpdateMs`→fresh). REPRSSI도 동일 그래프.
- **RSSI 폴백 ladder**: `[Route] Link → REPRSSI → INFO[2] → INFO표 RF열 → takentime → RS`. per-packet RSSI는 사용자가 "상세 RSSI 켜기" 누를 때만(VEXTUNITINFO 자동 금지).

### 7-5. `roster` (그룹·노드·엣지)
- **그룹**: SSM별 그룹(`kind:"ssm"`, 1:1 불변식) + **SSM 부재 시 `kind:"standalone"` 그룹**. 멀티 SSM 귀속은 GID/채널(합성 테스트).
- 직접연결 노드 + 원격 노드(`ports:[]`) + `edges`. MAC→이름은 INFO 레지스트리로 해소, 미해소는 단축 라벨.

### 7-6. `TopologyEngine` (server.py, 상태+Lock)
- **탭**: `SerialReader._run`에 `engine.observe(port, ts, text)` 한 줄(예외 삼킴, 비차단).
- **홉 feed**: `viewer_feed.RawFeed`를 payload `Any`로 일반화(또는 `HopFeed`), drop-oldest 유지.
- **bootstrap**: 첫 SSM 포트 확정 시 **서버-내부 write로 `INFO` 1회**(별도 워커, 리더 비차단). `_confirm_write` 게이트 **비경유**(서버발신·ctx 없음). **비-SSM 포트엔 전송 금지**, **부팅 window 중 금지**(파괴 명령 오작동 위험).
- **sweep**: 데몬 타이머가 만료 pending 처리(no-SSM은 unconfirmed).

### 7-7. 라우트 (`web_viewer.py`)
- `GET /api/topology` → `engine.roster()` 스냅샷.
- `GET /api/topology/stream` → 홉 SSE(기존 `_serve_stream` 패턴: 구독→헤더→하트비트, drop-oldest).

### 7-8. 프론트 (`web_viewer.py` `_HTML`)
- `/api/topology/stream` → `window.topologyHop(hop)` → 엣지 하이라이트·경로 애니메이션(바닐라 SVG+rAF). `Rt` 경로는 실선, REPRSSI 링크는 약한 선.
- 로스터 `edges` 렌더, standalone 그룹 표시. **디테일 패널**: 경로 칩·구간 RSSI·quality·RTT·**실패/미확정 구간**.
- 순수 로직(엣지 geometry·rssiColor)은 VIEWER-PURE + `viewer_logic_harness.cjs`.

---

## 8. Phase C — `get_topology` MCP 도구
```python
@mcp.tool()
def get_topology() -> dict:
    """로스터(노드·엣지) + 최근 홉 요약(경로·RSSI·성공/실패/미확정). 읽기 전용."""
    return {"roster": engine.roster(), "recent_hops": engine.recent_hops(20)}
```
AI가 "SB5는 멀티홉이라 SSM 포트 도착이 정상", "이 포트는 SB 단독(SSM 없음)"을 인지 → 원동기 종결. **SPEC §5 조회 6→7종**·`serial` 스킬 동기화.

---

## 9. 안전·불변식

- **자동 송신 정책**: 부트스트랩 `INFO`는 **SSM 포트 + 식별확정 후 + 부팅 window 종료 후**에만, 서버-내부 1회. 비-SSM 포트 송신 금지.
  - **구현(모듈6-b)**: `_bootstrap_due`(SSM·연결·미송신·부팅window) + 포트별 1회 래치(`_topology_bootstrapped`). **기본 OFF**(env `SERIAL_TOPOLOGY_BOOTSTRAP=1` opt-in) — 실HW e2e 전까지 자동 시리얼 송신 신중. `SERIAL_WRITE=off`면 미송신.
  - **[미래 강화 — default-ON 승격 전 필수]** 부팅 window(`_TOPOLOGY_BOOT_WINDOW_S=8s`)는 **owner 획득 시각 기준**이라 늦게 hotplug/reset 되는 포트엔 per-board 유예가 없다. 현재 안전은 시간 window 가 아니라 **SSM 분류 게이트가 부팅완료 게이트로 작동**해 보장된다(classify_device 가 SSM 판정하려면 라이브 SSM 앱 시그니처[`Information on the entire equipment`/`[Proc-WiFiRx]`/`[Route] Link`/REPRSSI] 필요 → 부트로더 단계 보드는 SSM 분류 안 됨). 위험 보드 SB-ESP 는 INFO[0]=4 로 항상 SB 분류돼 타깃 불가. **유일 노출**: opt-in ON + 비-SSM 보드를 'SSM'으로 **오설정 별칭**(conf 1.0 으로 분류 게이트 우회) + 그 보드 부팅 중. default-ON 으로 올리기 전 (a)per-board connect 시각 기준 window 또는 (b)별칭 SSM 도 라이브 시그니처 1회 확인 후 송신으로 닫을 것.
- **자동 금지 명령**: `CREQINFO`·`VEXTUNITINFO`·`REQSTCOMM`·`REQRSSI`(RF 송신/상태 토글). 파괴 명령(REFLASH*/DOWNBIN/FORMAT)은 당연 금지.
- **읽기전용 사상**: SSM serial `INFO`(=`simplevInfoBuffer`)는 안전 read. 단 SSM→원격 `ReqInfoTo`(ESP-NOW RF 송신, 카운터·takentime 영향)는 "read-only-ish"로 §5/§10에 문서화.
- 관측 비차단·drop-oldest·뷰어 실패 코어 무영향·Lock 보호·클라이언트 파리티(§2 제약).

---

## 10. TDD / 픽스처 (실측+합성)

**픽스처**(`tests/fixtures/topology/`) — §14 박제 + 아래 보강:
1. SSM INFO 테이블(자기+SB/APU/APU_C/Repeater, **Repeater가 숫자 `5`로 남는 케이스**)
2. SSM RX 블록(`[Proc-WiFiRx]`/`[Proc-Raw Packet]`/`[Passed Device]`/`<<<From SBn`/`-- takentime`)
3. 확장 RSSI 없는 RX / 있는 RX(VEXTUNITINFO 켠 상태)
4. SSM INFO 요청 TX(`[Proc_Alarm] Ask Info`, **Unique 없는** JSON)
5. INFO 응답: SB `INFO[0]=="4"` / APU_C `"3"` / Repeater `"5"`(+RS 포함)
6. REPRSSI `[[mac,rssi]]` 및 `[[mac,rssi,snr]]`
7. `[Route] Link` / 실제 경로(`Rt` 배열, `[Passed Device]` 문자열)
8. **SB 단독(SSM 부재)**: SB-ESP boot/`[Tx-my INFO]`만 있고 SSM RX 없는 로그 / SB STM32 `SmartBay FW` 배너

**테스트 순서**: classifier(INFO[0]·5→REPEAT·배너) → events(블록조립·JSON파싱) → correlator(다중키·dedup·스윕·**no-SSM unconfirmed**) → routing(Rt 토큰맵·링크그래프·RSSI ladder) → roster(standalone·edges) → `TopologyEngine`(observe→홉·bootstrap 1회·boot-window guard) → routes → 프론트 순수로직 → `get_topology` → [실장비] 멀티포트 e2e.

---

## 11. 문서 동기화 (마지막)
- **SPEC §10**: 좌측 토폴로지 패널·전포트 다중키 상관·standalone 그룹·부트스트랩 INFO(서버발신 1회·읽기전용·SSM 한정) 명문화.
- **SPEC §5**: `get_topology` 추가(조회 7종).
- **README** 웹 뷰어 절 + 신규 환경변수.
- `serial` 스킬 도구 목록 동기화.

---

## 12. 작업 순서
1. ✅ 실측 캡처(§14) + cbm 펌웨어 검증(§4) + GPT 리뷰 반영(이 문서).
2. ✅ **Phase A** 구현·커밋·push(main `3cdcbbf`). 실장비 e2e만 Phase B와 함께.
3. **(다음) Phase B** TDD — §7 모듈 순서대로(classifier→events→correlator→routing→roster→engine→routes→front).
4. **Phase C** `get_topology` + SPEC §5·스킬 동기화.
5. [실장비] 멀티포트(SSM+SB[+REP]) + **SB 단독** 양쪽으로 분류·경로·실패/미확정·bootstrap·`get_topology` e2e.
6. SPEC §10/§5·README 동기화, 커밋(한국어 Conventional Commits). 배포는 별도(2레포 버전 동기화).

## 핵심 파일
- `src/serial_mcp/topology.py`(또는 `topology/` 패키지) — classifier·events·correlator·routing·roster(순수).
- `src/serial_mcp/server.py` — `TopologyEngine`, `SerialReader._run` 탭, bootstrap·sweep, `get_topology`, `ViewerServer` 배선.
- `src/serial_mcp/web_viewer.py` — `/api/topology[/stream]`, `_HTML` 그래프·애니메이션·디테일 패널, VIEWER-PURE.
- `src/serial_mcp/viewer_feed.py` — 홉 feed로 `RawFeed` 일반화.
- `tests/test_topology*.py`, `tests/viewer_logic_harness.cjs`, `tests/test_web_viewer.py`, `tests/fixtures/topology/`.
- `SPEC.md` §10·§5, `README.md` — 문서 동기화.

## 검증
- 문법 `py -m compileall -q src` / 단위 `uv run python -m pytest`(이 PC는 `uv run pytest` trampoline 깨짐) / JS `tests/viewer_logic_harness.cjs`(node) / 실장비 COM4 SSM(+SB[+REP], +SB단독).

---

## 13. 펌웨어 소스 참조 (cbm 인덱스)
`getUniqueValue`(보드별 롤링) · `ReqInfoTo`(SSM, Unique 없음) · `BypassJsonPacket`(SB/APU/APU_C/Repeater, `Rt` append) ·
`RouteTokenForInfoPos`(토큰=UnitID/retEncrytion) · `RouteUpdateLinkFromReprssi`(SSM_esp32.ino:6709, `[Route] Link`) ·
`RouteRelayScore`(:6394)/`RouteDirectGood`(:6340) · `WiFi_rev_proc`(:6873–9582, RX 블록·`[Passed Device]`·takentime) ·
`simplevInfoBuffer`(:1487, INFO 테이블·5 숫자) · `serialCmd`(INFO/TINFO/STCOMM·REFLASH* 등) · 장비타입 enum `SSM_esp32.h:468–472`.

---

## 14. 부록 — 실측 픽스처 원문 (2026-06-26 라이브 캡처)

세트업: **COM4=SSM-ESP / COM12=SB-STM(BayID 5) / COM14=SB-ESP(dev SB260526-002, UnID 5)**. 공통 `CHANNEL:"11"`, `UnID`=`BayID`.
구현 시 `tests/fixtures/topology/`로 이관.

```
# 매칭 사례(같은 Unique=15) — RX가 TX 로그보다 먼저 찍히기도(인과는 정상, 펌웨어가 sendMessage 후 [Tx] 출력)
COM14 SB-ESP  14:48:27.806  [Tx - my INFO] {"UnID":5,"INFO":["4","SB260526-002",-22,false,false,"0",false],"Unique":15}
COM4  SSM     14:48:27.678  [Proc-WiFiRx] {"UnID":5,"INFO":["4","SB260526-002",-22,false,false,"0",false],"Unique":15,"Rev":true,"Cidx":861}

# SSM 요청/응답 + 경로(REPRSSI) + RTT(takentime/avrTakenTime)
[Proc_WiFiTx] Ask Info : To. SB1, {"RTC":[46,49,14,26,6,2026],"CHANNEL":"11","INFO":"REQ","UnID":5}
[Proc-WiFiRx] {"UnID":5,"INFO":["4","SB260526-002",-21,...],"Unique":25,"Rev":true,"Cidx":873}
 -- Checking finished
<<< From SB1.
 -- takentime : 61
 -- Normal Inspect cntRev[0] : 16, avrTakenTime : 121[ms]
[Proc-WebRTx] ["message",{"now":3777495,"data":{"macAddress":"30,AE,A4,4B,1A,0C","REPRSSI":[["A0,85,E3,EA,5C,C4",-22],["10,06,1C,16,97,AC",-41],["98,3D,AE,EC,C9,C4",-52],["80,7D,3A,82,5A,AC",-55],["EC,E3,34,47,B3,C0",-57]],"Done":"OK"}}]

# SB-ESP: REQ 수신 → 내 INFO 송신
[WiFi_Rx] {"RTC":[26,46,14,26,6,2026],"CHANNEL":"11","INFO":"REQ","UnID":5,"Cidx":475}
Save a new Reved-Packet for me.
[Tx - my INFO] {"UnID":5,"INFO":["4","SB260526-002",-24,...],"Unique":82}

# SB-STM(BayID 5): 베이 컨트롤러
BayID:5,    < MasterCard >    Price1st:3000,    Price2nd:1000,    minCoinSensingTime:25,
```

> **상관키 주의(개정)**: `(UnID,Unique)`는 **"장비 응답 TX↔SSM RX" leg + dedup 전용**(예: 15↔15). INFO 요청엔 Unique 없음 →
> 요청↔응답·라우팅·ACK은 §7-3 다중키로 처리. **실제 경로는 `Rt`/`[Passed Device]`**(REPRSSI는 링크 그래프).
