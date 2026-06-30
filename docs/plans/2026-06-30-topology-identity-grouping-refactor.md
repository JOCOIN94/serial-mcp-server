# 토폴로지 식별·그룹 리팩토링 (P0+P1+P2)

> **상태: 완료(동결)** — 2026-06-30. P0-1~P2-2 전 단계 TDD 구현·커밋 완료(브랜치
> `refactor/topology-identity-grouping`: 1b9dda1·ada3083·51b668c·dd1ceac·22d60ce·e26a2bf + SPEC §5).
> 본문은 시점 기록으로 동결 — 소급 수정하지 않는다.
>
> _원래 배너_ — 진행 중(착수): 단일 컨텍스트(Claude) 직접 구현, 단계별 TDD→커밋.
> 범위 결정: 사용자가 "전부(P0+P1+P2)" 선택. 각 단계 = 실패테스트→구현→녹색→커밋.

## 배경 — 검증으로 확정된 모델 (펌웨어 + 실장비)

2026-06-30 실장비(COM4=SSM-ESP / COM12=SB-ESP / COM13=SB-STM) + 펌웨어(cbm) 교차검증으로
GPT '최종 단순 분류 규칙'을 평가·교정해 확정한 식별 모델. (memento caseId
`serial-mcp-topology-classify-2026-06-30`)

1. **`Unique`는 장비 ID가 아니라 1~99 롤링 카운터**(펌웨어 `getUniqueValue`, `MACUNIQUEVALUE=99`,
   매 송신 +1·wrap·10회마다 NVS). 장비마다 독립 순환→값 충돌 빈번. 라이브서 70→71→72→…→80 관측.
2. **`[Tx - my INFO]`·`[Proc-WiFiRx]` 식별 패킷에 Mac 필드가 없다**(필드=UnID/INFO/EQ/Unique).
   → 식별 패킷을 Mac으로 매칭 불가. Mac은 다른 계열(`[Tx_RSSI]`/REPRSSI 이웃 RSSI, WiFi 스캔)에만
   등장 — **링크 그래프 enrich용**(routing edges). 'Mac 1차 식별'은 INFO 레벨엔 적용 불가.
3. **UnID = 사용자 설정 BayID**(충돌 가능, 여러 SB가 전부 5). 라이브서 같은 SB가 roster 'SB5'(UnID)
   vs hop 'SB1'(라우트토큰)로 갈림 — UnID를 식별/라벨 키로 쓰면 안 됨.
4. **견고한 cross-port 매칭키 = (UnID, Unique) + 도착 시간창 + 발신 로컬포트.** (UnID,Unique) 단독
   불가. 각 leaf가 전용 USB 포트라 **포트가 안정 식별자**(= GPT '포트별 그룹' 통찰). 모호 매칭은
   unconfirmed로(억지 결합 금지).
5. **STM 분류는 상보적으로 합친다**: 현재 부팅배너+설정시그니처(BayID/MasterCard/Price1st)는
   부팅/설정 시점만 잡고, 카드 동작 런타임 로그를 놓침(라이브서 COM13 unplaced). GPT 런타임
   액션토큰을 추가해 메움.

## 현재 아키텍처 (정렬 상태 + 결함 위치)

events·correlator·routing은 **이미 위 모델과 정렬**돼 있다. 결함은 roster 층과 STM 분류에 집중.

| 모듈 | 소유 | 상태 |
|---|---|---|
| `topology_events.py` | 포트별 줄→Event(UnID/Unique/INFO[0]/Mac/Rt/REPRSSI/passed/takentime) | ✅ `_ID_KEYS`가 Mac을 기회적으로만 채움(INFO엔 없음 인지) |
| `topology_correlator.py` | (UnID,Unique)+`window_s=15` 시간창 상관, 포트내 dedup, TX-only→fail/unconfirmed | ✅ 모델과 정렬. 단 flow가 rx_port/tx_port 구분을 emit 안 함(P1서 보강) |
| `topology_routing.py` | 링크그래프(REPRSSI/[Route]Link)·토큰맵·RSSI ladder | ✅ Mac은 여기(edges) |
| `topology.py` `build_roster` | 그룹/노드/배치 | ❌ **결함 집중**(아래) |
| `topology_engine.py` | Lock+상태 조정, observe/sweep/roster 스냅샷 | ✅ membership 스냅샷 배선만 추가(P1) |

**`topology.py` 결함:**
- (C1) **멀티-SSM**: 비-SSM을 전부 `groups[0]`에 몰아넣음(`build_roster` 단일-SSM 가정 주석). SSM 2개↑ 깨짐.
- (C2) **`_merge_sb` 충돌**: SB ESP/STM을 `number`(=UnID/BayID)로 병합 → 같은 BayID 다른 베이 둘이 한
  노드로 오병합. (ESP+STM 같은 베이 병합은 의도된 동작이라 유지해야 함 — 구분 필요.)
- (C3) **leaf↔SSM 로컬포트 연결 없음**: correlator가 (UnID,Unique)로 leaf-TX↔SSM-RX를 이미 짝짓는데,
  그 포트 짝을 roster로 surface 안 함.
- (C4) **라벨**: UnID 기반 라벨(SB5)과 라우트토큰 이름(SB1) 불일치. 식별 소스 단일화 필요.
- (C5) **STM 런타임 미분류**: `_SIGNATURES` SB-STM이 BayID/MasterCard/Price1st(부팅·설정)만. 카드 동작 누락.

## SPEC 리뷰 (빈틈·모호함 → 결정)

- SPEC §10은 웹 뷰어를 다루지만 **roster/그룹/식별 의미론은 SPEC에 없다**(코드+계획서에만). → P2서 §10에
  불변식 한 줄 추가(그룹=SSM포트별 / 식별=로컬포트 / UnID=표시메타 / Mac=링크층). **절 번호 불변**.
- 모호함: **leaf가 여러 SSM에 들리면** 어느 그룹? → 결정: 가장 최근 RX(`last_ts`)가 primary 그룹.
  (직접 USB 연결 leaf는 보통 한 SSM에 귀속. 다중은 best-effort.)
- 클라이언트 파리티: roster는 **서버 계산·클라이언트 중립**(elicitation 무관) → Claude/Codex 동일. 영향 없음.
- 불변식 유지: 노드 shape `{id,type,label,mac,unit_id,route_token,row,col,status,ports}`·그룹 shape는
  뷰어(SPEC §10)·get_topology(§5) 계약. **필드 추가만, 제거/개명 금지**.

## 단계 (각 단계: 실패테스트→구현→녹색→커밋)

### P0-1 · SB-STM 런타임 분류 (C5)
- **실패테스트**: 라이브 COM13 카드스와이프 줄 → `classify_device` → type SB·mcu STM.
- **구현**: `_SIGNATURES` SB-STM에 **SB/STM 전용 토큰** 추가 — `Send state of STM32`(SB-SmartBay 전용),
  `Released to touch Card`(STM main.c 전용). cbm 검증 완료: `Check the Card`/`Lower Disp. Step`은
  APU·SSM에도 있어 **제외**. weight=3, conf 유지(⑤ signature 0.6).
- **green**: 기존 SB-STM(BayID 등) 테스트 유지 + SSM/APU 윈도가 STM으로 오분류 안 됨 확인.

### P0-2 · `_merge_sb` 충돌 (C2)
- **실패테스트**: 같은 UnID=5 SB-ESP 2포트(다른 COM) → **2 노드**(병합 X). ESP+STM 같은 번호 → 1 노드 유지.
- **구현**: 병합은 **번호 같고 MCU 다를 때만**(ESP+STM=같은 베이). 같은 MCU·같은 번호 → 별개 노드 +
  collision 플래그/낮은 신뢰. 포트가 타이브레이커.

### P1-1 · correlator rx/tx 포트 surface (C3 기반)
- **실패테스트**: leaf-TX(port A)+SSM-RX(port B) 같은 (UnID,Unique) → 방출 홉에 rx_port=B·tx_port=A(또는
  src_port) 포함.
- **구현**: `flow`에 rx_port/tx_port 기록, `_emit`에 노출. 기존 `ports`(정렬 list) 하위호환 유지.

### P1-2 · 엔진 membership 스냅샷
- **실패테스트**: 엔진 observe 후 `membership_snapshot()` → `{ssm_port:{unid:{device_type,local_port,last_ts}}}`.
- **구현**: `_drain`이 rx-완료 홉에서 membership 누적(Lock 안). `roster_and_recent_hops`가 같은 Lock
  세션에서 snapshot 캡처(skew 방지, 기존 routing 스냅샷과 동일 패턴).

### P1-3 · build_roster 멀티-SSM 그룹 (C1)
- **실패테스트**: 합성 2-SSM(SSM_a/SSM_b) + 각자 받은 leaf → leaf가 올바른 SSM 그룹에 배치 + local_port 부착.
  membership 없으면(Phase A) 현재 동작(첫 그룹) 폴백.
- **구현**: `build_roster(..., membership=None)` 추가. membership 있으면 SSM포트별 그룹 멤버십·로컬포트
  연결. 원격 mesh 노드(`_remote_descriptors`) 기존 유지. 다중 SSM 수신은 최근 RX 우선.

### P2-1 · 라벨 일관성 (C4)
- **실패테스트**: 같은 장비의 roster 노드 라벨과 hop src_name 일치. UnID는 `unit_id` 필드(메타)로만.
- **구현**: 라벨 소스 단일화 — 별칭 > 라우트토큰 해소 이름 > type+로컬포트. UnID 라벨 단정 제거.

### P2-2 · SPEC §10 불변식
- 그룹=SSM포트별 / 식별=로컬포트 / UnID=표시메타 / Mac=링크층 한 줄 추가. 절 번호 불변.

## 검증 (완료 기준)
- 전체 `uv run python -m pytest` 녹색(현재 376 통과 — 이 PC는 `uv run python -m pytest`, `uv run pytest`는
  trampoline 깨짐).
- 노드/그룹 shape 계약 보존(필드 추가만). 뷰어 깨지지 않음.
- 코드리뷰: SPEC §2 클린아키텍처·stdout 금지·Lock·읽기전용 조회 유지.

## 픽스처 (라이브 캡처 2026-06-30)
```
COM12 SB-ESP TX : [Tx - my INFO] {"UnID":5,"INFO":["4","SB260526-002",-21,false,false,"0",false],"EQ":[0,0,0,0,0,0],"Unique":71}
COM4  SSM   RX : [Proc-WiFiRx] {"UnID":5,"INFO":["4","SB260526-002",-21,false,false,"0",false],"EQ":[0,0,0,0,0,0],"Unique":71,"Rev":true,"Cidx":1787}
COM4  SSM REPRSSI: [Proc-WiFiRx] {"UnID":5,"REPRSSI":[["A0,85,E3,EA,5C,C4",-21],["98,3D,AE,EC,C9,C4",-52]],"Unique":72,"Rev":true,"Cidx":1788}
COM13 SB-STM   : Check the Card. - Our Card 1.
COM13 SB-STM   : Send state of STM32 : 0x0005      ← SB/STM 전용
COM13 SB-STM   : Released to touch Card.            ← STM 전용
```
