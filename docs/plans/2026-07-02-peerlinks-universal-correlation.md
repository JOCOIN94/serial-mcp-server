# PeerLinks — 범용 H.W↔H.W 링크 상관 구현 핸드오프 (2026-07-02)

> **▶ 새 세션/에이전트 진입점 (자족적 맥락 캡슐).** 이 문서만 읽고 대화 맥락 없이 구현할 수
> 있게 쓴다. 선행 맥락: `docs/plans/2026-07-01-topology-link-unique-txrx.md`(링크=관측 원칙 확립),
> `docs/plans/2026-07-02-topology-review-fixes.md`(직전 수정 — 복합키·TX 즉시방출·INFO테이블·ladder).
> 레포 공통 지침은 `AGENTS.md`(빌드·검증 명령, 커밋 규칙, 안전 제약) — 반드시 함께 따른다.

---

## 0. 한 줄 요약

토폴로지 링크선 상관을 "리프 `[Tx - my INFO]` ↔ SSM `[Proc-WiFiRx]`" 전용에서 **"모든 송신
태그 ↔ 모든 수신 태그"의 범용 포트쌍 상관(PeerLinks)** 으로 일반화한다. SSM 로그 없이도
A↔B(예: SB↔REP, SB↔SSM)가 관측되고, INFO 외 트래픽(카드·상태·명령응답)으로도 링크가 뜬다.

## 1. 왜 (사용자 요구)

- 현행: correlator(`topology_correlator.py`)가 kind `tx`([Tx - my INFO])와 `rx`([Proc-WiFiRx],
  SSM 전용)만 상관 → 비-SSM 장비쌍 A↔B 는 SSM 로그를 기다려야만 간접 관측됐다.
- 사용자: "SSM에 목매달지 말고, A↔B가 SSM이든 SB든 뭐든 **H.W↔H.W 일반화된 규칙**이 더 강력."
- 사용자 확정 스코프 규칙(§5)과 기존 대원칙(§7)을 반드시 지킬 것.

## 2. 펌웨어 확정 사실 (2026-07-02 소스 전수 조사 — 설계의 근거)

소스: cbm 소스뷰 `C:/Users/User/.cache/codebase-memory-mcp/source-views/firmware-selected`
(SB-SmartBay/sb-esp32/SB_ESP32.ino, repeator-esp32/Repeat_esp32.ino,
APU-slim-working/APU_C_SLIM_esp32/APU_C_SLIM_esp32.ino, ssm-esp32/SSM_esp32.ino)

1. **Unique 는 INFO 전용이 아니다.** 리프 발신 16종 메시지(INFO 응답·상태보고·초기화·명령응답·
   리부트·파일·에러·DNFWVER·WHO·RSSI·REQRESP·CHKCOMM·SPECIAL…)가 전부 `getUniqueValue()`
   Unique(1..99 롤링, MACUNIQUEVALUE=99, 0 금지)를 싣는다.
2. **리프 TX 로그 태그 전수**(전부 "태그 + JSON 한 줄" 완결, JSON 에 UnID∥Mac + Unique):
   `[Tx - my INFO]` `[Tx - resp for DNFWVER]` `[Tx - resp for WHO]` `[Tx_RESP]` `[Tx_RSSI]`
   `[Tx_REGMAC]` `[Tx_REQRESP]` `[WiFi_Tx…]`(CHANNEL/CHKCOMM 등) `[WiFi_Tx-PendingRFTimeout]`
   `ForceQuit_Tx`. 일부 메시지(상태보고 StateUsingBay, order_WiFi, Reboot 응답 등)는 TX 태그
   자체가 없다 → 로그에 없으니 상관 불가(수용).
3. **SSM TX**: `[Proc_WiFiTx]`(INFO/BayConfig 요청) `[Proc_Alarm]`(알람 요청) `[Tx]`(데이터) —
   JSON 에 대상 UnID∥Mac + **Cidx**(SSM 전용 송신 카운터, `jsonWiFiTxBuf["Cidx"]=TxInx_Cnt++`,
   SSM_esp32.ino:5399, 부팅 시 random(0,1000) 초기화). SSM 송신엔 Unique 없음.
4. **리프 `[WiFi_Rx]`** (SB:7803, APU:6646, REP:5891): 채널·hidden ID 일치 패킷을 **수신 원본
   그대로**(UnID/Mac/Unique/Cidx 포함), **중복제거(chk_DuplicateRev) 전에** 출력. 채널 일치면
   남의 패킷(오버히어)도 출력. 릴레이 메시에선 **자기가 보낸 패킷의 에코**도 찍힐 수 있다
   (출력 후 CHKSEND 로 폐기 — 로그엔 남음). → **자기 에코 가드 필수**(§4).
5. **`[Data_Pass]`** (SB:7836, APU:6677, REP:5922 + SSM:7083): `Alive=="SSM"` 패킷을 릴레이할 때
   원본 전체(Rt·키 포함)를 출력하고 재송신. **재송신엔 TX 태그가 없다** → 하류 수신은 원 송신자
   키와 매칭됨(A→C 직선으로 관측, 물리 경유는 홉 경로 [Passed Device] 가 보여줌 — 한계 수용).
6. Cidx ≠ Unique: 요청(Cidx)과 응답(Unique)은 키가 달라 **요청-응답 쌍 매칭은 불가**(범위 외).
7. UnID 규칙(기존 확정): 전 리프 공통 `if(ConfigBay.BayID) UnID else Mac` — BayID=0 이면 Mac.
   파서는 이미 Mac 을 대문자 콜론 정규형으로 추출한다.

## 3. 현행 아키텍처 요약 (이 레포, 2026-07-02 시점 — 전부 커밋/테스트 green 상태)

- `src/serial_mcp/topology_events.py` — `EventAssembler`: 포트별 줄 누산 → Event dict 방출.
  헤더 테이블 `_HEADERS`(현행): rx=`[Proc-WiFiRx]`, webtx=`[Proc-WebRTx]`,
  wifitx=`[Proc_WiFiTx]|[Proc_Alarm]`, tx=`[Tx - my INFO]`, wifirx=`[WiFi_Rx]`,
  route=`[Route] Link`. **tx 는 즉시 방출**(한 줄 완결), 나머지는 다음 헤더/유휴 flush(엔진
  sweep 2s)에서 방출. `[Data_Pass]`/`[Proc-Raw Packet]` 은 현재 헤더가 아니라 연속줄(`_attach`)
  로 rt_tokens 만 추출.
  Event shape: `{port, ts, kind, raw_lines, json, route, ids:{mac,unid,unique,asn,cidx,rt_tokens},
  hints:{src_name,dst_name,device_type,passed}, metrics:{rssi,takentime_ms,avr_takentime_ms,reprssi,rs}}`
- `src/serial_mcp/topology_correlator.py` — `Correlator`: (UnID∥Mac, Unique) 키로 tx↔rx(SSM)
  상관 → Hop 방출(path·ok·confidence·rx_port·src_port·rssi). **홉엔 시각(ts) 없음(불변 원칙)**.
- `src/serial_mcp/topology_engine.py` — `TopologyEngine`: Lock 보호 조정자. `observe(port,ts,text)`
  가 매 줄: `_pairing.observe`(카드) + `_routing.observe_table_line`(INFO 테이블) + 어셈블러 feed
  → `_drain`(routing.observe → correlator.observe → 홉 적재 + `_record_membership`).
  `_membership = {ssm_port: {ident: {device_type, local_port, last_ts, rssi}}}` (ident=UnID∥Mac).
  `roster_and_recent_hops()` 가 Lock 안에서 스냅샷(_RoutingSnapshot: tokens/edges/info_table +
  membership + pairing) 뜨고 Lock 밖에서 `build_roster`(관측 비차단). `forget_port(port)` =
  pairing + membership(해당 ssm_port drop, local_port==port 무효화) 정리.
- `src/serial_mcp/topology_routing.py` — `RoutingTable`: [Route] Link/REPRSSI 링크그래프(mac쌍),
  토큰맵, SSM INFO 테이블 파서(`observe_table_line` — mac↔UnID↔이름 다리 + `ssm_mac[port]`),
  `pick_link_metric`/`RSSI_LADDER`(route_link > reprssi > info_rssi > info_table_rf > …).
- `src/serial_mcp/topology.py` — `build_roster(entries, routing, membership, pairing, now)`:
  포트 분류(별칭 최우선)·그룹 골격(그룹↔SSM 1:1, SSM 없으면 standalone 1그룹)·
  `_local_port_to_ssm(membership)` 로 leaf 귀속·`_membership_edges(...)` 가 그룹 edges 생성
  (ladder 로 rssi + rssi_source). edge shape: `{from, to, fresh, rssi, rssi_source}`.
- `src/serial_mcp/web_viewer.py` — 내장 HTML/JS. VIEWER-PURE 블록(줄 ~796-1307, DOM 비의존·
  ES5 스타일): `edgeSegments`(포트→노드중심 매칭, `{x1..y2, rssi, fresh, source}`),
  `hopWaypoints`, `hopColor`, `hopDetail`, `rssiColor`. DOM: `renderEdges`(SVG line + 출처 툴팁),
  `renderTopology`(전체 재렌더 + lastSig 가드 + 진행중 .thop 펄스 이식), SSE
  (`/api/topology/stream`) 홉 수신 → 펄스 + 400ms 디바운스 roster 재조회. 하네스:
  `tests/viewer_logic_harness.cjs` (pytest `tests/test_viewer_logic.py` 가 VIEWER-PURE 블록을
  추출해 node 로 실행 — node 직접 실행은 인자 필요하므로 pytest 경유).
- `src/serial_mcp/server.py` — 배선만(이번 작업에서 변경 불필요): 리더 on_line →
  `_topology_observe` 가 `eng.observe` 반환 홉을 SSE 발행, sweep 타이머 2s.

## 4. 설계 — 신규 순수 모듈 `topology_peerlinks.py` :: `PeerLinks`

correlator(홉·멤버십·그룹 귀속)는 **건드리지 않는다**. 링크선 일반화는 별도 모듈.

- **관측 분류**: 송신 kind ∈ {`tx`, `wifitx`} / 수신 kind ∈ {`rx`, `wifirx`, `pass`(신규)}.
- **키 추출**(ev.ids): Unique 있으면 상행 `("u", unid∥mac, unique)`;
  Unique 없고 cidx 있으면 하행 `("c", cidx)`; 둘 다 없으면 무시.
- **상관**: 같은 키의 송신 포트 ↔ 수신 포트 (순서 무관 — rx 선행 pending 수용, 윈도 15s,
  (port,kind) 포트내 dedup, pending/links 상한 + drop-oldest — correlator 의 기존 패턴 참조).
- **산출**: `links = {(src_port, dst_port): {"last_ts": ts, "via": "handled"|"heard"}}`.
  via: `rx`/`pass` 수신 = handled(실제 처리·중계), `wifirx` 만 = heard(들림·오버히어 포함).
  같은 쌍 재관측 시 last_ts 갱신, via 는 강한 쪽(handled)으로만 승격.
- **자기 에코 가드**: src_port == dst_port 면 링크 아님(§2-4 — 릴레이 메시 자기 패킷 에코).
- **그룹 veto(§5)**: 매칭 시점에 두 포트의 그룹이 둘 다 확정 & 서로 다르면 상관 거부.
- **snapshot(now, fresh_s=30.0)** → `[{"from","to","via","fresh"}]` 사본. `forget_port(port)` →
  그 포트가 낀 pending·links 제거.
- API 제안: `observe(ev, scope: dict) -> None`(scope = {port: ssm_port|None}),
  `snapshot(now) -> list`, `forget_port(port) -> None`.
- 파일 머리 docstring 에 §2 근거(태그 전수·에코·Data_Pass 한계)와 §5 규칙을 요약해 남긴다
  (레포 컨벤션 — 순수 모듈은 펌웨어 근거를 머리에 쓴다).

## 5. 멀티-SSM(복수 그룹) 스코프 규칙 — **사용자 확정, 필수**

- **매칭 시점 그룹 veto**: 두 포트의 그룹 귀속이 **둘 다 확정됐고 서로 다르면 상관 거부** —
  표시 필터가 아니라 원천 차단(오염 방지·더 정확).
- **같은 그룹이거나 한쪽이라도 미귀속이면 허용** — 미귀속(아직 어느 SSM 도 못 들은/SSM 없는
  standalone)까지 막으면 "SSM 없는 A↔B"라는 이 기능의 출발점이 죽는다. 미귀속 포트가 나중에
  귀속되면 그때부터 veto.
- 스코프 원천: 엔진 `_membership` → `{local_port: ssm_port}` 역인덱스(기존
  `topology.py:_local_port_to_ssm` 로직 참조) + SSM 포트 자신({ssm_port: ssm_port}).
- 보조 방어(기존 유지): Mac 키=전역 유일 / 펌웨어가 [WiFi_Rx] 를 채널·hidden 일치만 출력 /
  같은-그룹 렌더 필터(§6-4).

## 6. 구현 단계 (파일별, 권장 커밋 단위)

**1. `topology_events.py`** — 헤더 확장:
- tx 정규식 일반화: `("tx", re.compile(r"\[(?:WiFi_)?Tx\b[^\]]*\]|\[Tx_[^\]]*\]|ForceQuit_Tx"))`
  로 교체(§2-2 태그 전부 커버 — `[Tx - my INFO]` 포함. `[Proc_WiFiTx]` 는 "Proc" 시작이라 미매칭
  확인). _HEADERS 순서 유의: wifitx(`[Proc_WiFiTx]|[Proc_Alarm]`) 항목을 tx 보다 **앞**에 둔다.
- `("pass", re.compile(r"\[Data_Pass\]"))` 헤더 추가. `_attach` 의 `[Data_Pass]` 분기 제거
  (`[Proc-Raw Packet]` 은 유지 — SSM rt_tokens 경로, `tests/test_topology_events.py::
  test_rt_tokens_from_proc_raw_packet_not_proc_wifirx` 가 고정).
- 즉시 방출 확대: 한 줄 완결 kind = {tx, pass, wifirx} 는 `feed()` 에서 즉시 방출(기존 tx
  즉시방출과 같은 근거 — 연속줄이 기여하는 필드 없음. rx(SSM)는 [Passed Device]/takentime
  부착이 필요하므로 **버퍼링 유지**). 모듈 docstring 의 헤더 흐름 설명 갱신.
- ⚠️ correlator 영향: kind "tx" 태그가 늘어나 INFO 외 트래픽도 홉/멤버십을 갱신하게 된다 —
  의도된 부수효과(카드·WHO 응답으로도 멤버십 최신화). 기존 correlator 테스트 green 유지 확인.

**2. `topology_peerlinks.py` 신규** — §4 대로.

**3. `topology_engine.py`** —
- `__init__` 에 `self._peerlinks = PeerLinks()`.
- `_drain` 에서 각 ev 를 `self._peerlinks.observe(ev, scope)` 에도 급전. scope 는 membership
  에서 도출한 {port: ssm_port} 캐시 — membership 갱신(`_record_membership`) 시에만 재계산.
- `roster_and_recent_hops` 스냅샷에 `peer_links = self._peerlinks.snapshot(now)` 추가,
  `build_roster(..., peer_links=peer_links)` 로 전달(같은 Lock 세션 — skew 방지).
- `forget_port` 에 `self._peerlinks.forget_port(port)` 추가.

**4. `topology.py`** — `build_roster(..., peer_links=None)`:
- 그룹별 edges 병합: 기존 `_membership_edges(...)` 결과 ∪ peer edges 중 **양끝 포트가 그
  그룹에 배치된 것만**(포트→그룹 매핑은 배치 결과에서 도출. standalone 그룹 포함 — standalone
  은 멤버십 edges 가 없으므로 peer edges 가 유일한 링크원이 된다. cross-group 링크는 v1 드랍,
  주석으로 명시 — 프론트 canvas 가 그룹 단위라 그릴 곳이 없음).
- 무방향 dedup: frozenset({from,to}) 기준, **멤버십 edge 우선**(rssi 보유), 중복 peer 는 via 만
  기존 edge 에 보강. 순수 peer edge shape: `{from, to, fresh, via, rssi: None,
  rssi_source: None}` (mac 다리가 되면 후속에서 ladder enrich — v1 은 rssi 없이).
- edge 에 `via` 필드 신설: 멤버십 유래 = "handled" 로 통일.

**5. `web_viewer.py`** —
- VIEWER-PURE `edgeSegments`: 출력에 `via` 통과(`via: e.via || null`).
- `renderEdges`: `via === "heard"` 면 `stroke-dasharray: "4 4"` + opacity 한 단계 낮게(점선 =
  "들림", 실선 = "처리·중계"). 툴팁 문자열에 via 표기.
- ⚠️ VIEWER-PURE 블록은 브라우저/노드 겸용 — ES5 스타일(var, 함수 선언) 유지.

**6. 테스트(구현 후 작성 — TDD 생략은 사용자 합의, 기존 스위트 green 유지가 필수)** —
- `tests/test_topology_events.py`: 새 tx 태그 각각 kind "tx" 로 파싱(픽스처: §2-2 실제 태그
  문자열), `[Data_Pass]` 가 kind "pass" + ids 보유, wifirx/pass 즉시 방출.
- `tests/test_topology_peerlinks.py` 신규: 상행 상관(A tx ↔ B wifirx → 링크) / 하행
  Cidx([Proc_WiFiTx] ↔ 리프 wifirx) / rx 선행 / heard vs handled 승격 / 자기 에코 가드 /
  그룹 veto(다른 그룹 거부·미귀속 허용·귀속 후 veto 적용) / 윈도 만료 / 상한 drop-oldest /
  snapshot fresh 감쇠 / forget_port.
- `tests/test_topology_engine.py`: 관통 — 두 리프 포트 라인 급전(SSM 이벤트 없이) → roster 에
  peer edge(via 포함).
- `tests/test_topology.py`: 병합 dedup(멤버십 우선)·standalone 그룹 peer edge·cross-group 드랍.
- `tests/viewer_logic_harness.cjs`: edgeSegments via 통과 단언.

**7. 문서·메모리** —
- `docs/plans/2026-07-02-topology-review-fixes.md` 는 동결(수정 금지). 본 문서가 이번 작업 기록
  — 완료 시 머리에 상태 한 줄만 추가(본문 소급 수정 금지, docs/plans 관례).
- 파일메모리 `memory/firmware-payload-semantics.md`(Claude 메모리 디렉터리)에 §2 신규 사실
  (TX 태그 전수·Cidx=SSM 송신 카운터·[WiFi_Rx] dedup 전 출력·자기 에코) 반영 — Claude 세션에서
  수행 시에만 해당.

## 7. 반드시 지킬 대원칙 (기존 확립 — 위반 금지)

- **링크 강제 금지·동적**: 관측(키 상관)된 포트쌍만 그린다. 고정 토폴로지 박제 금지.
  (사용자가 가장 강하게 반복 강조한 원칙.)
- **홉/링크에 시각 미노출**: RX 가 TX 보다 먼저 관측될 수 있다(펌웨어가 sendMessage 후 [Tx]
  출력). 순서=인과 아님. 상관은 키로만.
- **stdout 금지**(MCP JSON-RPC 가 stdout) — 진단은 `_log`/stderr. 공유 상태는 엔진 Lock 안에서만.
- **관측 비차단**: build_roster 등 무거운 일은 Lock 밖. 리더 스레드 훅은 예외 삼킴.
- **클라이언트 파리티**: Claude Code·Codex 동일 동작(서버측 로직이라 자동 충족 — 확인만).
- 커밋: 한국어 + Conventional Commits(`feat:`/`test:`/`docs:` …). 단계별 분리 커밋 권장.

## 8. 검증

- `uv run python -m pytest` — ⚠️ 이 PC 는 `uv run pytest` 가 trampoline 오류로 깨짐. 현재
  **413 passed 가 기준선**(회귀 0 + 신규 green). 문법: `py -m compileall -q src`.
- JS 하네스는 pytest(test_viewer_logic.py) 경유로 자동 실행됨.
- 실장비(있으면): COM4=SSM(ESP32-S3, 115200), COM12/COM14=SB. ① SB 에 `WHO` 같은 안전 조회 시
  `[Tx - resp for WHO]` ↔ SSM `[Proc-WiFiRx]` 상관으로 INFO 없이 링크선 뜨는지 ② SSM 의 주기
  요청([Proc_WiFiTx], Cidx) ↔ SB `[WiFi_Rx]` 하행 링크(heard 점선) ③ 뷰어에서 실선/점선 구분.
  위험 명령(SETCONFIG·DOWNBIN·FORMAT·REFLASH 계열) 금지, 쓰기는 elicitation 게이트 준수.

## 9. 완료 기준

- [ ] 전 테스트 green(기존 413 + 신규), compileall OK  
- [ ] 두 리프 픽스처만으로(SSM 이벤트 없이) roster 에 peer edge 생성됨
- [ ] 다른 그룹 확정 포트쌍은 링크 상태에 아예 안 들어감(veto 테스트)
- [ ] heard/handled 가 뷰어에서 점선/실선으로 구분
- [ ] 이 문서 머리에 완료 상태 한 줄 추가 + 단계별 커밋
