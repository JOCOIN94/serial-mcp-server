# ChainLog — 홉 체인 로그("최근 홉" 개편) 구현 핸드오프 (2026-07-02)

> **▶ 새 세션/에이전트 진입점 (자족적 맥락 캡슐).** 이 문서만 읽고 대화 맥락 없이 구현할 수
> 있게 쓴다. 선행 맥락: `docs/plans/2026-07-02-peerlinks-universal-correlation.md`(범용 포트쌍
> 상관·펌웨어 태그 전수), `docs/plans/2026-07-01-topology-link-unique-txrx.md`(링크=관측 원칙).
> 레포 공통 지침은 `AGENTS.md`(빌드·검증 명령, 커밋 규칙, 안전 제약) — 반드시 함께 따른다.

---

## 0. 한 줄 요약

correlator 홉(리프→SSM 단건) 표시를 **메시지 단위 "체인 로그"** 로 일반화한다: 같은 메시지
키의 관측들을 하나의 로그 항목으로 병합해 `a → REP1 → SSM` 체인으로 표기하고, SSM 그룹별로
로그처럼 누적하며, `get_topology`(MCP)에도 `recent_chains` 로 노출한다(AI 활용이 1급 목표).

## 1. 왜 (사용자 요구, 2026-07-02)

- 현행 뷰어 "#topohops 최근 홉" 패널은 마지막 홉 1건만 칩으로 표시 — "너무 빈약해".
- 요구 ① **체인 표기**: `a→b→c`(보고/상행), 하달(하행)은 방향 구분, `a→b`(단일 홉)도.
- 요구 ② **로그 누적**: 일회성 하이라이트가 아니라 로그처럼 쌓이고 **SSM 그룹별로** 구분.
- 요구 ③ **병합**: `a(송신)→b(수신)` 항목이 있고 같은 메시지의 `b(중계)→c(수신)` 이 관측되면
  **그 항목이 `a→b→c` 로 자란다**(새 항목 생성 금지).
- 요구 ④ **메타는 로그에 찍히는 값만**: RSSI·펌웨어 출력 ms(SSM 수신 블록 `takentime : 61`).
  서버 시계 기반 지연은 쓰지 않는다(사용자 확인: 모든 통신에 ms 없으면 생략 수용).
- 요구 ⑤ **미접속 경유지**: 중간 장비가 포트에 안 물려 있으면 이름([Passed Device]/Rt 해소)
  또는 `?` — `a → ? → c`.
- 요구 ⑥ (사용자 확인) **AI가 serial-mcp 를 쓸 때 도움**: get_topology 에서 구조화된 경로
  서사를 바로 읽을 수 있어야 한다(원시 로그 재파싱 불요) — 뷰어 전용 금지(클라이언트 파리티).

## 2. 설계 근거 — 코드·펌웨어 확정 사실 (2026-07-02 대조 완료)

1. **rx 이벤트 `metrics.rssi`(INFO[2]) = 송신 장비가 자기 주변 이웃 평균을 보고한 값**
   (`topology_events.py:122-125`) — 링크별·수신 구간 값이 아니다. 체인에선 **src 노드 메타**로
   붙이는 게 정직하다. `takentime` 은 SSM 수신 블록 연속줄에서 추출(**dst 노드 메타**).
2. **`[Passed Device]` 경로 문자열 = 소스가 첫 원소, 목적지(SSM) 미포함** (correlator
   `_parse_passed`, 뷰어 `hopWaypoints` 가 rx_port 를 뒤에 덧붙이는 이유). 펌웨어가 토큰→이름을
   이미 해소해 준다(`(05-SB5)->(01-REP1)`). → 체인 골격 = passed 노드들 + dst(rx_port).
3. **rt_tokens**: pass 이벤트(`[Data_Pass]`, 즉시 방출이라 인라인 JSON Rt 보유)와 SSM rx 블록의
   `[Proc-Raw Packet]` 연속줄에서 채워진다. 토큰(2-hex)→이름 해소는 `RoutingTable.tokens()`
   (`{token: {name,mac,unid}}`). 예약 토큰 "00"/"FF" 는 노드 아님.
4. **`[Data_Pass]` 는 상행(Alive=="SSM") 중계만 확인됨** — 하행 중계 로그 태그는 미확인.
   v1 하행 체인은 `SSM ⇒ 수신자들` 형태일 수 있다(한계 수용, §9 위험).
5. 키·롤링(기존 확정): 상행 = (UnID∥Mac, Unique), Unique 1..99 롤링·0 금지. 하행 = Cidx(SSM
   송신 카운터, Unique 없음). 요청(Cidx)↔응답(Unique)은 키가 달라 쌍 매칭 불가(범위 외).
6. 리프 `[WiFi_Rx]` 는 dedup 전 수신 원본 출력 — 오버히어·자기 에코 포함(에코 가드 필요 없음:
   체인에선 (port,kind) dedup + 역할 분류로 흡수).

## 3. 현행 아키텍처 요약 (이 레포, v1.7.0 시점 — 전부 커밋/테스트 green)

- `src/serial_mcp/topology_events.py` — EventAssembler. kind: `tx`(리프 [Tx*]/[WiFi_Tx*]/
  ForceQuit_Tx — 즉시 방출), `wifitx`([Proc_WiFiTx]/[Proc_Alarm], SSM 송신·Cidx), `rx`
  ([Proc-WiFiRx], SSM 수신 — 버퍼링: takentime/[Passed Device]/[Proc-Raw Packet] 부착),
  `wifirx`([WiFi_Rx], 리프 수신 — 즉시), `pass`([Data_Pass] — 즉시), `route`, `webtx`.
  Event shape: `{port, ts, kind, raw_lines, json, route, ids:{mac,unid,unique,asn,cidx,rt_tokens},
  hints:{src_name,dst_name,device_type,passed}, metrics:{rssi,takentime_ms,avr_takentime_ms,reprssi,rs}}`
- `src/serial_mcp/topology_correlator.py` — (UnID∥Mac,Unique) 키 tx↔rx(SSM) 상관 → Hop
  `{key,ok,confidence("observed"|"timeout"|"unconfirmed"),path([Passed Device] 이름 리스트),
  src_name,device_type,rtt_ms(takentime 유래),rssi,rx_port,src_port,ports}`. **홉엔 ts 없음
  (불변)** — RX 가 TX 보다 먼저 관측될 수 있어 순서=인과 아님, 상관은 키로만.
- `src/serial_mcp/topology_peerlinks.py` — 범용 포트쌍 상관(링크선용). `_flows:
  OrderedDict{key: {first_ts,last_ts,seen:(port,kind)set,tx_ports,rx_ports}}`, 키
  `("u",ident,unique)|("c",cidx)`, 윈도 15s, 그룹 veto(scope={port:ssm_port}, 둘 다 확정+상이=
  거부), drop-oldest `_evict`. **ChainLog 가 이식할 준거 패턴 — 수정하지 않는다.**
- `src/serial_mcp/topology_engine.py` — Lock 조정자. `_drain`: 각 ev 를 routing.observe →
  peerlinks.observe(ev,_peer_scope()) → correlator.observe, 홉을 `_hops`(deque maxlen=200) 적재 +
  `_record_membership`. `_peer_scope()`: membership 유래 {port:ssm_port} dirty-캐시.
  `roster_and_recent_hops(entries,now,n)`: Lock 안 스냅샷(routing/membership/pairing/peer_links/
  hops tail) → Lock 밖 build_roster → `(roster, hops)`. `sweep(2s)`, `forget_port`.
- `src/serial_mcp/topology_routing.py` — `tokens()`, `info_table() = {by_mac, ssm_mac{port:mac}}`.
- `src/serial_mcp/server.py` — `get_topology`(~line 1079): roster + recent_hops(20) +
  hops_caveat. SSE: `_topology_feed.publish(ts, hop)` ← `_topology_observe`(리더 on_line 훅)/
  `_topology_loop`(sweep). `_publish_topology_hops(hops)`(~line 566, 예외 삼킴).
- `src/serial_mcp/web_viewer.py` — 내장 HTML/JS. **VIEWER-PURE 블록(줄 ~796-1309, ES5·DOM
  비의존)**: hopWaypoints/hopColor/hopDetail/edgeSegments/rssiColor — `tests/
  viewer_logic_harness.cjs` 가 pytest(test_viewer_logic.py) 경유로 검증. DOM 측:
  renderHopDetail(#topohops, "최근 홉" 제목, `.thd-*` 칩/화살표/메타 CSS 기존재), SSE
  (`/api/topology/stream`) onmessage → topologyHop(hop) 펄스 + 400ms 디바운스 roster 재조회.
  `/api/topology` = 로스터만(topology_info).

## 4. 설계 — 신규 순수 모듈 `topology_chains.py` :: `ChainLog`

PeerLinks(포트쌍 집계=링크선)와 관심사 분리 — ChainLog 는 "메시지 단위 서사"를 소유한다.
correlator Hop 은 그대로 유지(판정·멤버십·홉 펄스의 원천), ChainLog 는 같은 키로 Hop 산출물을
**접목**한다(판정 로직 이중 구현 금지).

### API

```python
class ChainLog:
    def __init__(self, window_s: float = 15.0, max_entries: int = 300,
                 max_active: int = 500) -> None: ...
    def observe(self, ev: dict, scope: Optional[dict] = None,
                resolver=None, port_names: Optional[dict] = None) -> list:
        """Event 1개 반영. 변경(생성/성장)된 항목 사본 0~1개 반환(SSE 발행용)."""
    def apply_hop(self, hop: dict) -> Optional[dict]:
        """correlator Hop 을 같은 키 항목에 접목(ok/confidence/rtt_ms/path 백필). 변경 사본 or None."""
    def sweep(self, now: float) -> list:
        """윈도 만료 활성 항목 complete 확정. 변경 사본 리스트 반환."""
    def recent(self, n: int = 30) -> list:   # id 오름차순 tail(끝이 최신 — recent_hops 규약 일치)
    def forget_port(self, port: str) -> None # 그 포트 낀 활성 항목 complete 확정(히스토리 보존)
```

- `resolver`: `resolve_token(tok) -> Optional[{name,mac,unid}]` 덕타입 — 엔진이 RoutingTable 을
  그대로 넘긴다(같은 Lock 안이라 안전). 테스트는 스텁. RoutingTable 에 `resolve_token` 이
  없으면 tokens() 기반 얇은 메서드를 추가한다(공개 API 최소).
- `port_names`: `{port: name}` — 엔진 dirty-캐시(§5) 산출물. 없으면 포트 문자열 폴백.

### 항목(entry) shape — JSON 직렬화 가능, **서버 시각(ts) 없음**

```python
{
  "id": 41,                      # 단조 seq — UI upsert 키·로그 정렬용(시각 아님 — caveat 로 방어)
  "key": ["u", 5, 34],           # ("u", ident, unique) | ("c", cidx)
  "dir": "up" | "down",          # up=보고(Unique 키) / down=하달(Cidx 키)
  "group": "COM7" | None,        # 귀속 SSM 포트(scope 유래). None=미귀속(standalone)
  "ordered": true,               # 릴레이 순서가 skeleton([Passed Device]/Rt) 근거인지
  "nodes": [                     # 전송 순서(송신→…→수신). down 은 [src] + 수신자들
    {"name": "SB5",  "port": "COM3", "role": "src",   "rssi": -71,  "ms": None, "resolved": true},
    {"name": "REP1", "port": None,   "role": "relay", "rssi": None, "ms": None, "resolved": true},
    {"name": None,   "port": "COM7", "role": "dst",   "rssi": None, "ms": 61,   "resolved": true},
  ],
  "heard": ["COM5"],             # up 체인을 곁귀로 들은 리프 포트(경로 밖 — 본선에 안 섞음)
  "ok": true | false | None,     # up: apply_hop 접목 / down: 수신자 ≥1 이면 true, 0이면 None(실패 단정 금지)
  "confidence": "observed" | "timeout" | "unconfirmed" | None,
  "rtt_ms": 61 | None,           # dst.ms 와 같은 원천(로그 takentime) — Hop 파리티용
  "complete": false,             # 윈도 만료/timeout 접목/forget 후 true — 이후 병합 금지
}
```

메타 규칙(§2-1): **rssi = src 노드**(INFO[2] 자기보고 평균 — rx 이벤트에 실려 와도 의미상
소스 것), **ms = dst 노드**(takentime). 값 없으면 None(표시 생략). 내부 윈도 판정용
first_ts/last_ts 는 항목 내부(_로 시작 또는 별도 dict)에만 두고 **직렬화에 노출 금지**.

### 조립 알고리즘 (유사코드)

```
KEY(ev): PeerLinks._key 와 동일 파생 — Unique 있으면 ("u", unid∥mac, unique)·dir="up",
         없고 cidx 있으면 ("c", cidx)·dir="down", 둘 다 없으면 무시.

observe(ev, scope, resolver, port_names):
  kind ∉ {tx, wifitx, rx, pass, wifirx} → []
  key 없음 → [];  port 없음 → [];  ts = ev.ts or 0.0
  changed = _expire(ts)                        # 윈도 지난 활성 항목 complete 확정(반환에 포함)
  ent = _active.get(key)
  if ent 없음 or ent.complete:
      ent = 새 항목(id=next_seq++, dir, group=scope.get(port)); _active[key]=ent; _entries.append
      _evict(_active, max_active); _entries 는 deque(maxlen=max_entries)
  else:
      (port,kind) ∈ ent._seen → return changed          # 브로드캐스트 잔향 dedup
      g = scope.get(port)
      if ent.group 과 g 둘 다 확정 and 상이 → return changed   # 그룹 veto — 관측 폐기
      if ent.group is None and g 확정 → ent.group = g          # 뒤늦은 귀속
  ent._seen.add((port,kind)); ent._last_ts = ts

  kind == "tx":      src 슬롯 확보/부착 {port, name=port_names.get(port,port), role="src"};
                     ev.metrics.rssi 있으면 src.rssi ← (첫 값 보존)
  kind == "rx":      dst 슬롯 확보 {port, role="dst", ms=ev.metrics.takentime_ms};
                     skeleton = passed 파싱([(이름)]) or rt_tokens→resolver(예약 00/FF skip,
                                미해소 → {name:None, resolved:False})
                     skeleton 있으면 nodes 재구성 = [src(기존 포트 재부착)] + skeleton relay 들
                                + dst, ordered=True; 기존 pass 포트는 이름 일치 슬롯에 재부착
                     ev.metrics.rssi 있으면 src.rssi ←(INFO[2]=소스 자기보고 — src 슬롯 없으면 생성)
  kind == "pass":    relay 노드 {port, name=port_names.get(port,port), role="relay"};
                     skeleton 있으면 이름 일치 슬롯에 port 부착(미일치 → dst 앞 삽입);
                     skeleton 없이 미일치 relay ≥ 2 → ent.ordered=False   # 순서 주장 안 함
  kind == "wifirx":  dir=="up"  → ent.heard 에 port 추가(본선 불변)
                     dir=="down"→ 수신 노드 {port, name, role="rx"} append;
                                  ent.ok=True; ent.confidence="observed"
  kind == "wifitx":  dir=="down" src 노드(port=SSM 포트); ent.group=scope.get(port)
  return changed + [ent 사본]                   # 실제 변경이 있었을 때만

apply_hop(hop):    # 상행 전용(Hop 은 (ident,unique) 키만 존재)
  key = ("u", hop.key[0], hop.key[1]); ent = _active.get(key) (없으면 최근 완료 동일키 — 윈도 내)
  없으면 None. ent.ok/confidence/rtt_ms ← hop(rtt 는 dst.ms 에도 반영).
  ent 에 skeleton 없고 hop.path 있으면 relay 백필(이름만, port=None).
  hop.confidence ∈ {"timeout","unconfirmed"} → ent.complete=True(sweep 유래 — 흐름 종결).
  return ent 사본
```

- **순서 원천은 오직** ① tx=기점 ② rx=종점 ③ skeleton(passed/Rt)=릴레이 순서. **서버 관측
  시각을 순서 원천으로 쓰지 않는다**(불확실하면 ordered=False 로 정직 표기 — 대원칙 §8).
- pass 포트↔skeleton 슬롯 매칭 v1: 이름 일치(port_names[port] == slot.name) 우선, 그 외
  "미일치 pass 1개 + 빈 relay 슬롯 1개"면 채움, 그 외 별도 relay 로 append(한계 수용).

## 5. `topology_engine.py` 변경

- `__init__`: `self._chains = ChainLog(window_s=window_s)`,
  `self._chain_updates: dict[int, dict] = {}`(id→최신 사본 — 배치 내 코얼레싱).
- `_names()` 신규: `{port: name}` dirty-캐시 — `_peer_scope` 와 같은 dirty 플래그로 재빌드.
  원천: membership `local_port→unid` + routing 토큰맵/INFO 테이블의 unid→이름. 미상 포트는
  미포함(ChainLog 가 포트 문자열 폴백).
- `_drain`: 각 ev 에 `for c in self._chains.observe(ev, self._peer_scope(),
  resolver=self._routing, port_names=self._names()): self._chain_updates[c["id"]] = c`;
  홉 적재 루프에서 `c = self._chains.apply_hop(hop)` 도 동일 적재.
- `sweep`: `self._chains.sweep(now)` 변경분 + correlator.sweep 산출 홉들의 `apply_hop` 적재.
- `forget_port`: `self._chains.forget_port(port)` 추가.
- 공개 메서드 신규:
  ```python
  def drain_chain_updates(self) -> list:   # Lock 안에서 _chain_updates 를 pop-all → 사본 리스트
  ```
- `roster_and_recent_hops(entries, now=None, n=20, chains_n=0)` → **`(roster, hops, chains)`
  3-튜플로 확장**(chains 는 같은 Lock 스냅샷에서 `self._chains.recent(chains_n)`).
  호출부 갱신: `roster()`(chains 무시), `server.get_topology`, 뷰어 `topology_info` 경로,
  기존 테스트 언패킹(`test_topology_engine.py` 등).

## 6. `server.py` 변경

- `_publish_topology_chains(updates)` 신규(기존 `_publish_topology_hops` 와 동형, 예외 삼킴):
  `_topology_feed.publish(ts, {"chain": entry})` — 기존 홉 push 와 같은 feed 공존.
- `_topology_observe` / `_topology_loop`: 엔진 호출 뒤 `eng.drain_chain_updates()` → 발행.
- `get_topology`: `roster, recent_hops, recent_chains = eng.roster_and_recent_hops(...,
  n=20, chains_n=20)`; 응답에 `"recent_chains"` 추가 + message 개수 반영 + caveat 확장:
  "recent_chains 의 id/나열 순서는 로그 표시용이며 시각이 아니다. 체인 내 노드 순서는
  ordered=true 일 때만 [Passed Device]/Rt 근거의 경로 순서다."

## 7. `web_viewer.py` 변경

### VIEWER-PURE (ES5·DOM 비의존 유지 — 하네스 검증)

```js
function chainRow(entry) {}
// → {id, group, dirLabel("보고"|"하달"), status("ok"|"fail"|"pending"), color(hopColor 규칙 재사용),
//    ordered, chips:[{label, meta, dim}], heard:[port...]}
// chip.label: name || port || "?" ; chip.meta: rssi("-71dBm")·ms("61ms") 있는 것만 " · " 연결
// chip.dim: port 없음(미접속 경유지) 또는 resolved===false
function chainGroups(chains, groups, cap) {}
// → [{label, items:[chainRow...]}] — entry.group(ssm_port)→로스터 그룹 label 매핑,
//    미귀속은 "미분류" 그룹, 그룹 내 id 내림차순(최신 위), cap(기본 8)
```
`SViewer` export 에 chainRow/chainGroups 추가. hopWaypoints/hopColor 는 유지(펄스는 계속 hop
기반). hopDetail 은 렌더 호출만 제거하고 함수·기존 하네스 단언은 존치(정리는 후속 판단).

### DOM/glue

- `state.chains = []`; `refreshTopology` 에서 `/api/topology` 응답의 `chains` 로 전체 시드.
  `topology_info` 응답은 `{**roster, "chains": [...]}` (기존 소비자 `d.groups` 하위호환).
- SSE onmessage: `obj.chain` 이면 id 로 upsert(교체/append, 클라 상한 100) 후 **디바운스
  (기존 400ms 패턴) 렌더**; 아니면 기존 topologyHop(obj) 펄스 경로.
- `renderChainLog()`: `SV.chainGroups(...)` → `#topohops` 를 그룹 헤더 + 행 리스트로 렌더 —
  방향 배지(보고 →/하달 ⇒ 또는 색 구분), 칩 체인(`.thd-chip` 재사용, dim 칩은 점선/흐림),
  칩 메타, heard 배지("들림: COM5"), 상태 색점. 기존 renderHopDetail 호출 제거.
- CSS 추가: `.tch-group`(그룹 헤더), `.thd-chip.dim`, `.thd-dir-down`, `.thd-heard`;
  `.topohops` max-height + overflow-y 스크롤.

## 8. 반드시 지킬 대원칙 (기존 확립 — 위반 금지)

- **링크/경로 강제 금지·동적**: 관측된 것만 그린다/기록한다. 고정 토폴로지 박제 금지.
- **서버 시각 미노출·순서≠인과**: RX 가 TX 보다 먼저 관측될 수 있다. 체인 순서는 키+skeleton
  근거로만, 불확실하면 ordered=False. 항목·노드에 서버 ts 를 직렬화하지 않는다(id 는 seq).
- **stdout 금지**(MCP JSON-RPC) — 진단은 `_log`/stderr. 공유 상태는 엔진 Lock 안에서만.
- **관측 비차단**: 무거운 일(build_roster 등)은 Lock 밖. 리더 스레드 훅은 예외 삼킴.
- **클라이언트 파리티**: get_topology(MCP) 와 뷰어가 같은 체인 데이터를 본다.
- VIEWER-PURE 블록은 ES5(var/함수 선언·template literal 금지) + DOM 비의존 유지.
- 커밋: 한국어 + Conventional Commits, 단계별 분리(§10).

## 9. 위험 / 구현 중 판단 항목

- **하행 중계 태그 미확인**(§2-4): v1 하행 체인은 `SSM ⇒ 수신자들` 뿐일 수 있음 — 수용,
  펌웨어 재확인은 후속.
- **cross-group 키 충돌 관측 폐기**(PeerLinks veto 일관): 로그라 유실이 보일 수 있음 —
  필요 시 (key,group) 이중 인덱스 후속 확장.
- **INFO[2] rssi 출처 표기**: src 칩 메타로 붙이되 툴팁/라벨로 "장비 평균" 구분할지 UI 에서 결정.
- **SSE 발행량**: 배치 코얼레싱(_chain_updates) + 뷰어 디바운스로 1차 완화, 부족하면 스로틀 후속.
- **이름 해소 지연**: _names 캐시가 membership dirty 에만 재빌드 — 갱신 직후 잠깐 포트 문자열
  폴백 표시 가능(수용).

## 10. 구현 단계 (권장 커밋 단위)

1. `feat: ChainLog 체인 로그 순수 모듈 추가` — `topology_chains.py` + `tests/test_topology_chains.py`(TDD 권장).
2. `feat: 엔진 체인 배선 — 관측·홉 접목·스냅샷 3-튜플` — engine + engine 테스트 갱신.
3. `feat: get_topology recent_chains 노출 + 체인 SSE 발행` — server + 관련 테스트.
4. `feat: 뷰어 최근 홉 패널 → SSM 그룹별 체인 로그 개편` — PURE 함수 + 하네스 + DOM/CSS.
5. `docs:` 본 문서 머리에 완료 상태 한 줄(동결 관례 — 본문 소급 수정 금지).

## 11. 테스트 목록

- **`tests/test_topology_chains.py` 신규**(순수): ① tx→rx 단일 홉(항목 1개 `a→SSM`) ② rx 의
  [Passed Device]로 기존 항목이 `a→REP1→SSM` 성장(**항목 수 불변 — 핵심 요구**) ③ rt_tokens
  미해소 → resolved=False("?")·예약 00/FF skip ④ pass 포트의 skeleton 슬롯 부착 ⑤ skeleton 없는
  다중 pass → ordered=False ⑥ 하행 wifitx→wifirx 수신 누적·ok=True·dir="down" ⑦ 하행 수신 0 →
  ok=None ⑧ 윈도 만료 후 같은 키 → 새 항목 ⑨ 그룹 veto 관측 폐기·뒤늦은 귀속 ⑩ (port,kind)
  dedup ⑪ 상행 wifirx 는 heard 로만(본선 불변) ⑫ apply_hop ok/timeout/unconfirmed 접목 +
  path 백필 + timeout 시 complete ⑬ rssi=src·ms=dst 부착(없으면 None) ⑭ 직렬화 항목에 ts 계열
  키 부재 단언 ⑮ 상한 evict·forget_port.
- **`tests/test_topology_engine.py`**: 관통(두 리프+SSM 픽스처 → recent_chains), 3-튜플 언패킹
  갱신, drain_chain_updates 1회성 소진, sweep timeout 홉 접목, forget_port 전파.
- **get_topology 테스트**(기존 위치는 `recent_hops` 로 grep 확인): recent_chains + caveat 문구.
- **`tests/test_web_viewer.py`**: /api/topology 응답 chains 키, SSE {"chain":...} 통과.
- **`tests/viewer_logic_harness.cjs`**: chainRow(label 폴백 name→port→"?", 메타 생략, dim, 방향),
  chainGroups(그룹 매핑·미분류·최신순·cap). 패턴 검증 스타일 유지.

## 12. 검증

- `uv run python -m pytest` — ⚠️ 이 PC 는 `uv run pytest` 가 trampoline 오류로 깨짐. 기존 스위트
  전부 green + 신규 green 이 기준. 문법: `py -m compileall -q src`.
- JS 하네스는 pytest(test_viewer_logic.py) 경유 자동 실행.
- 실장비(있으면): COM4=SSM(ESP32-S3, 115200) + SB. ① SB `WHO` 응답이 체인 항목으로 쌓이고
  후속 관측에 항목이 자라는지 ② SSM 주기요청(하달)이 dir=down 으로 구분되는지 ③ get_topology
  `recent_chains` 반환 확인 ④ 뷰어 그룹 섹션·dim/?/들림 표기. 위험 명령(SETCONFIG·DOWNBIN·
  FORMAT·REFLASH 계열) 금지, 쓰기는 elicitation 게이트 준수.

## 13. 완료 기준

- [ ] 전 테스트 green(기존 + 신규), compileall OK
- [ ] `a→b` 항목이 같은 키 후속 관측으로 `a→b→c` 로 **자라는**(항목 수 불변) 테스트 통과
- [ ] get_topology 응답에 recent_chains(+caveat) — 뷰어 없이 MCP 만으로 체인 조회 가능
- [ ] 뷰어 #topohops 가 그룹별 체인 로그 리스트(방향·dim·"?"·들림·메타 표기)
- [ ] 서버 시각이 항목/노드에 직렬화되지 않음(테스트 ⑭)
- [ ] 이 문서 머리에 상태 한 줄 추가 + 단계별 커밋
