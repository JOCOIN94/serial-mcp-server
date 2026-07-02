# 토폴로지 노드간 링크 재설계 — Unique TX↔RX 매핑 (핸드오프)

> **▶ 새 세션 진입점 (2026-07-01 작성, 자족적 맥락 캡슐).** 이 문서만 읽고도 대화 맥락 없이
> "왜 고치는지·왜 이 방식인지·무엇이 버그였는지·무엇을 할지"를 알 수 있게 쓴다. 긴 실장비
> 진단 세션(690k 토큰) 끝의 핸드오프다. memento 파편(아래 §9)과 함께 본다.

---

## 0. 한 줄 요약

토폴로지 뷰어의 **노드 간 링크**를 REPRSSI(RF mac)로 그리려던 접근이 **근본적으로 틀렸음**을
실장비로 확인했다. 올바른 방법은 **`Unique`(+UnID) 키로 TX↔RX 로그를 매핑해 "어느 포트가 보내
어느 포트가 받았는지"(=포트 간 링크)를 관측**하는 것이다. correlator가 이미 절반 구현했으나
`src_port:null` 버그가 있다. 미커밋 백엔드(self-mac 스텝 A/B/C)는 **폐기**한다.

---

## 1. 원동기 — 왜 고치나

serial-mcp는 임베디드 메시망(SSM 게이트웨이 + SB/APU/REP 등)의 시리얼 로그를 AI가 읽는 MCP
서버다. 웹 뷰어에 **토폴로지 그래프**(노드=장비, 링크=누가 누구와 통신)를 그려 사람·AI가 메시
경로를 본다. 이번 작업은 그 프론트(모듈8)의 링크 렌더를 실장비로 검증하다 결함이 드러난 것이다.

**문제**: 프론트 ①(로스터 edges 링크선)이 실장비에서 **아무 선도 안 그려졌다.** get_topology 로
백엔드를 보니 `edges`(REPRSSI 링크)의 양끝은 **mac**인데 노드의 `mac`이 전부 **null**이라 매칭이
0이었다. "노드 mac만 채우면 되나?" 하고 여러 우회를 팠고(아래 §2), 결국 **접근 자체가 틀렸음**을
알아냈다.

---

## 2. 여정 — 왜 이 방식에 도달했나 (시행착오와 근거)

새 세션이 같은 함정을 다시 파지 않도록 **틀린 경로와 그 이유**를 남긴다.

1. **시도: 노드에 mac 채우기(self-mac 폴백).** REPRSSI edges from(SSM)에 매칭시키려 함.
   - 미커밋 스텝 A/B: `RoutingTable`이 webtx(Proc-WebRTx) src mac을 "그 포트 self-mac"으로 저장.
   - **틀림**: Proc-WebRTx는 SSM이 **하위 유닛(SB)의 REPRSSI를 소켓 중계**하는 것이라, src
     (`macAddress`)는 **원 측정자(SB)**지 포트 주인(SSM)이 아니다. → COM4(SSM) 노드에 SB mac을
     오부여하는 버그. (MAC 조회로 확정: SSM=A0,85,E3,EA,5C,C4 / SB=30,AE,A4,4B,1A,0C.)

2. **시도: REPRSSI 이웃 mac을 원격 노드로(스텝 C).** edges to(이웃)를 노드로 만들어 매칭.
   - **부분적으로만 유효**: REPRSSI는 GID 필터가 없어(펌웨어 `WrAroundUnitInfo`) **같은 채널의
     무선 이웃 전부**(다른 그룹·주변 장비 포함)를 담는다. 이웃을 다 노드로 그리면 잡음이 뜬다.

3. **깨달음: 두 공간이 다르다.**
   - **노드** = 논리 장비(UnID·카드페어링·직접연결 포트) 공간.
   - **REPRSSI edges** = RF(mac) 공간, 그것도 "가능한 무선 이웃"(경로 아님).
   - BayID 설정 장비(SB)는 payload에 `UnID`만 싣고 **mac을 숨긴다**(펌웨어
     `if(configBay.BayID) UnID else Mac`). 그래서 RF mac ↔ 논리 노드를 잇는 다리가 로그에 없다.
   - 계획서 `topology-phase-b.md` §2·§4도 이미 **"실제 경로(Rt/[Passed Device]) ≠ 링크품질
     (REPRSSI)"**로 분리해 놨는데, 프론트 ①이 REPRSSI를 "노드 간 선"으로 그리며 이 분리를 어긴 것.

4. **사용자 핵심 지적(여러 번 강조)**: **노드 간 링크를 고정 구조로 '강제로' 그리면 안 된다.**
   홉 경로·링크는 RSSI에 따라 동적으로 바뀐다(가동 중 RSSI 하락 시 SB가 SSM 직접이 아니라 REP
   경유로 바뀔 수 있고, 리라우팅 기능도 추가될 수 있음). **경로를 미리 아는 게 아니라 로그에서
   관측된 '그 순간'을 그려낸다.** mac↔노드 매핑이 돼도 그건 "지금 이렇게 들린다"는 관측일 뿐.

5. **사용자 돌파구 아이디어**: **RX 로그엔 "받았다", TX 로그엔 "보냈다"가 있고, `Unique`가
   고유 메시지 ID다. 보낸 쪽 Unique와 받은 쪽 Unique가 같으면 그 둘을 매핑하면 된다.**
   → mac 필요 없이 포트↔포트 링크를 직접 관측. 관측·동적이라 §4 원칙에도 정확히 부합.

---

## 3. 확정 방향 — Unique(+UnID) TX↔RX 매핑

| 항목 | 결정 |
|---|---|
| **노드 간 링크** | correlator가 `Unique`(+UnID)로 TX↔RX 매핑한 **포트쌍**(hop.src_port ↔ rx_port). 노드=포트(논리)와 같은 축. **관측된 것만·동적**(고정 앵커 금지) |
| self-mac 노드화 / REPRSSI 이웃 원격노드화 (미커밋 스텝 A/B/C) | **폐기** (RF mac 축이라 논리 노드와 안 맞음) |
| REPRSSI | 링크 **구조**가 아니라 **RSSI 품질 메타**로만 사용(선택) |
| 매칭 키 | `(UnID, Unique)`. **시간 순서로 인과 추론 금지**(§5) |

---

## 4. 실증 근거 (실장비 대조, 2026-07-01)

두 포트에서 같은 `Unique`가 같은 시각대에 TX/RX로 뜨는지 실측 대조:

| Unique | SB TX (COM12) `[Tx - my INFO]` | SSM RX (COM4) `[Proc-WiFiRx]` | 시간차 |
|:---:|:---:|:---:|:---:|
| 61 | 17:15:12.614 | 17:15:12.511 | RX가 103ms 먼저 |
| 62 | 17:15:20.709 | 17:15:20.529 | RX가 180ms 먼저 |
| 64 | 17:15:34.574 | 17:15:34.453 | RX가 121ms 먼저 |

→ 세 쌍 모두 **같은 Unique + 같은 시각대** → `SB(COM12) → SSM(COM4)` 링크가 확정적으로 관측됨.
mac 불필요. 이게 방향 검증.

**결정적 부수 사실**: **모든 쌍에서 SSM RX가 SB TX보다 100~180ms *먼저* 관측된다**(펌웨어가
`sendMessage` 후 `[Tx]`를 출력하기 때문). 그래서:
- **도착 순서로 인과·시간차를 추론하면 안 된다**(홉에 시각 안 싣는 #1 제약의 실증). 매칭은 오직
  `Unique` 키(순서 무관).
- **correlator 버그의 원인**(§6): RX가 먼저 오는데 거기서 즉시 완료하니 늦은 TX를 놓친다.

---

## 5. 반드시 지킬 원칙·제약

- **링크 강제 금지·동적**: 노드 간 링크를 고정 토폴로지로 박지 말 것. 관측(TX↔RX)된 것만, 관측이
  바뀌면 그림도 갱신. (사용자가 가장 강하게 반복 강조한 원칙.)
- **홉에 시각 없음(#1 제약)**: 홉/링크에 timestamp 싣지 말 것. RX가 TX보다 먼저 관측되므로
  순서=인과가 아니다. `path`·`ok`·`confidence`·포트쌍만.
- **dedup 필수**: 메시(ESP-NOW broadcast)는 같은 패킷이 한 포트에 여러 번 도착한다(정상). 같은
  `Cidx`(=송신자 TX 카운터)로 `[Proc-WiFiRx]`가 두 번 찍히는 건 재수신이고, 펌웨어도
  `chk_DuplicateRev`로 버린다. correlator의 `(port,kind) seen` + `_recent` dedup으로 이미 흡수됨.
- **AGENTS.md 공통**: stdout 금지(진단은 `_log`/stderr), 관측 비차단, Lock 보호, 클라이언트
  파리티(Claude Code·Codex 동일 동작), 순수 로직은 I/O 분리(테스트 가능), TDD.

---

## 6. 고칠 코드 — correlator `src_port:null` 버그

`src/serial_mcp/topology_correlator.py` `Correlator.observe`:
```python
# kind == "rx" (SSM 수신) → 성공 즉시 방출
out.append(self._emit(flow, ok=True, confidence="observed"))
self._complete(key, ts)   # ← RX 도착 즉시 flow 제거(완료)
```
**문제**: RX가 도착하면 그 자리에서 hop 방출 + flow 제거. 그런데 실측상 **RX가 TX보다 먼저 온다**
(§4). 그래서 나중 오는 SB TX(=`tx_port`/`src_port`)가 `_recent` 잔향으로 무시됨(`observe`
상단의 `if last is not None and ts - last < window: return`). → hop.`src_port`가 항상 null.

**수정 방향**: RX에서 즉시 완료하지 말고 **짧은 윈도 동안 TX를 기다려** `tx_port`(SB)와
`rx_port`(SSM)를 **둘 다 채운 뒤 방출**한다. 그러면 hop이 `src_port=COM12 → rx_port=COM4`로
완성되어 그게 곧 SB→SSM 포트 간 링크가 된다. (실시간성 유지하려면: RX hop을 즉시 방출하되 flow를
짧게 유지해 TX 도착 시 보강 방출하는 방식도 검토 — trade-off는 새 세션이 TDD로 결정.)

hop 자료구조엔 이미 `rx_port`·`src_port` 필드가 있다(`_emit`, correlator.py ~150). RX만 채워지고
`src_port:null`인 게 지금 상태(get_topology recent_hops로 확인됨).

---

## 7. 해야 할 작업 (권장 순서)

1. **미커밋 백엔드 되돌리기**: `git restore src/serial_mcp/topology.py
   src/serial_mcp/topology_engine.py src/serial_mcp/topology_routing.py tests/test_topology.py
   tests/test_topology_routing.py` — self-mac 스텝 A/B/C(버그)를 main 상태로. (`CLAUDE.md`는 세션
   시작부터 있던 것이니 건드리지 말 것.)
2. **correlator TDD**: `test_topology_correlator.py`에 "RX가 TX보다 먼저 와도 src_port·rx_port
   둘 다 채워진다"는 실패 테스트부터. 그다음 §6 수정.
3. **프론트 재배선**: 노드 간 링크를 REPRSSI(edges)가 아니라 **hop(src_port↔rx_port)** 기반으로
   그리게. `web_viewer.py`의 `renderTopology`/`edgeSegments`(현재 노드 mac↔edge mac 매칭)를
   hop 포트쌍 기반으로 교체. REPRSSI는 RSSI 품질 메타로만.
4. **검증**: `uv run python -m pytest`(이 PC는 `uv run pytest` 깨짐) + `node
   tests/viewer_logic_harness.cjs` + 실장비 라이브 뷰어(COM4 SSM + COM12 SB, 115200).

---

## 8. 현재 상태 (커밋/미커밋)

- **프론트 모듈8, main 커밋됨**:
  - `e6028bd` ① 로스터 링크선(REPRSSI SVG) — **데이터 축이 틀림**(REPRSSI mac). 렌더 로직
    자체(SVG glue)는 유효하나, 링크 데이터원을 hop 포트쌍으로 **재배선 필요**.
  - `042e2c8` ② 홉 경로 애니메이션(/api/topology/stream SSE) — **방향 맞음**(hop.path 기반). 단
    hop.path 이름("SB1")↔로스터 label("SB5") 정합은 후속.
  - `e747e4e` ③ 홉 디테일 패널 — 유효(hop 필드 표시).
- **미커밋(폐기 대상)**: `topology.py`·`topology_engine.py`·`topology_routing.py` +
  `test_topology.py`·`test_topology_routing.py` = self-mac 스텝 A/B/C.

---

## 9. 실장비 기술 사실 (확정)

- **mac**(MAC 명령 조회): SSM(COM4)=`A0,85,E3,EA,5C,C4`, SB(COM12)=`30,AE,A4,4B,1A,0C`.
- **Proc-WebRTx `macAddress`**는 SSM 자기 것이 아니라 **중계된 원 측정자(SB)**다. REPRSSI 배열 안에
  A0,85(SSM)이 -9dBm으로 있음 = SB가 SSM을 관측. (self-mac 버그의 근원.)
- **SB BayID=5** → mesh payload에 `UnID`만, `Mac` 숨김(`if(configBay.BayID) UnID else Mac`).
- **REPRSSI = `stAroundUnit`**(주변 감지 유닛). GID/그룹 필터 없음, promiscuous도 없음. ESP-NOW
  broadcast는 같은 WiFi 채널(11)이면 그룹 무관 다 수신 → 다른 그룹·주변 장비 mac 혼입.
- **UnitID 0은 정상**: SSM `UNITID`가 22개 다 0인 건 mac 기반 운영(펌웨어가 mac↔UnID 폴백,
  `if(UnitID) UnID else Mac`; 7140 주석 "테스트 중 UnitID 공유 허용"). UnitID에 기대면 안 됨.
- **`Cidx` = 송신자 TX 카운터**. 같은 Cidx로 `[Proc-WiFiRx]` 2회 = 같은 패킷 재수신(메시 정상),
  펌웨어 `chk_DuplicateRev`가 폐기. 결함 아님.
- **조회 명령**(SSM, 다 안전): `MAC`·`GID`(config.Sid)·`CHANNEL`(ichannel)·`UNITID`·`INFO`·`STCOMM`.
  SB에도 `MAC` 있음. (`SETCONFIG`·`SETBAYCONFIG`·`SETSSMID`는 설정 변경이라 주의.)

**memento 파편**(WSL localhost:57332): `frag-9604cfeb`(원칙: 링크 강제 금지·동적) ·
`frag-118962a6`(실측 mac·REPRSSI 중계) · `frag-a87111e2`(self-mac 버그) · `frag-22b1c6fb`(Unique
TX↔RX 실증) · `frag-d3818517`(링크 설계 결정). 파일메모리: `memory/topology-links-not-forced-dynamic.md`.

---

## 10. 핵심 파일

- `src/serial_mcp/topology_correlator.py` — **주 수정 대상**(observe: RX 즉시완료 → TX 대기).
- `src/serial_mcp/web_viewer.py` — 프론트 `renderTopology`/`edgeSegments`/`topologyHop`(링크
  데이터원 재배선), `_HTML` VIEWER-PURE + `tests/viewer_logic_harness.cjs`.
- `src/serial_mcp/topology.py`·`topology_engine.py`·`topology_routing.py` — 미커밋 되돌린 뒤
  correlator hop을 로스터/get_topology로 노출하는 배선만 최소 손질.
- `tests/test_topology_correlator.py` — TDD.
- `docs/plans/topology-phase-b.md` — 원 설계(§2·§4가 경로 vs REPRSSI 분리를 이미 명시).
