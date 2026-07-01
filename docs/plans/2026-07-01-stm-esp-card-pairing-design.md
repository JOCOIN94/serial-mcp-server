# STM↔ESP 카드상관 페어링 설계 (sCuID 기반)

> **상태: 설계안(구현 전).** 2026-07-01. TDD·구현 없이 설계만. 근거는 실장비+펌웨어 검증
> (memento caseId `serial-mcp-topology-classify-2026-06-30`).

## 1. 문제와 "왜 이 방법뿐인가"

한 물리 베이 = ESP + STM이 **각각 별도 USB 포트**. 뷰어에서 한 노드[ESP|STM]로 병합하려면
두 포트를 같은 베이로 묶을 **공유 식별자**가 필요하다. 그런데:

- **ESP**: `UnID`(=configBay.BayID)를 항상 방송 → 베이번호 늘 앎.
- **STM**: 정상 운영 중 베이ID를 **아무것도 안 흘림** — BayID는 DIP 설정모드 부팅/config 명령
  때만, 콘솔 조회명령 없음, MAC 없음. 카드 동작 로그엔 `sCuID`/`Amnt`만.
- **SSM**: 카드 이벤트를 `UnID`+제네릭 `uID:"RF_uID"`로 로깅 — 카드ID(sCuID)를 지워 상관 무용.
- 1:1 휴리스틱=단일 베이만, 시간상관=동시 태그 모호 → 둘 다 기각.

**유일하게 남은 신호**: ESP가 카드를 SSM으로 포워딩할 때 **`sCuID`와 `UnID`를 함께** 로깅한다.
STM은 같은 카드의 `sCuID`를 (UnID 없이) 로깅한다. → **sCuID로 두 포트를 잇는다.**

## 2. 증거 (라이브 캡처 2026-07-01)

```
COM13(STM):  {"sCuID":"46C41E18..","Amnt":50000}                       ← sCuID만, UnID 없음
COM12(ESP):  {"sCuID":"46C41E18..","Amnt":50000}                       ← STM서 받은 echo
COM12(ESP):  CuID_Tx:{"sCuID":"46C41E18..","Amnt":50000,"UnID":5,..}   ← sCuID + UnID!
COM12(ESP):  [WiFi_Tx] {"sCuID":"46C41E18..",..,"UnID":5,..}           ← sCuID + UnID!
```
sCuID = 물리 카드 UID 16바이트 XOR 0xFF(카드 고유). 카드마다 달라 **비모호**.

## 3. 핵심 규칙 (한 줄)

> **어떤 포트든, 자기가 로깅한 카드 `sCuID`가 어딘가의 `sCuID+UnID` 로그와 일치하면 그 베이(UnID) 소속.**

한 베이의 ESP·STM은 같은 카드 `sCuID`를 둘 다 찍으므로 **둘 다 그 베이로 귀속 → 병합**. 포트가
ESP인지 STM인지 사전에 알 필요 없다(ESP는 자기 UnID로 이미 번호가 있고, 중복 귀속은 무해·멱등).

## 4. 설계

### 4.1 새 순수 모듈 `topology_pairing.py` (stateful, I/O 비의존)

기존 mesh용 `EventAssembler`/`Correlator`는 헤더(`[Proc-WiFiRx]` 등) 기반이라 카드 줄
(`{"sCuID"..}`·`[WiFi_Tx]`·`CuID_Tx:`)을 이벤트로 안 만든다. 그래서 카드상관은 **별도 경량 모듈**로
분리한다(관심사 분리). raw 줄을 그대로 받아 정규식 스캔.

```
class CardPairing:
    observe(port, ts, text) -> None
      # "sCuID" 미포함이면 즉시 반환(값싼 사전검사)
      # sCuID = 정규식 '"sCuID"\s*:\s*"([0-9A-Fa-f]+)"'
      # unid  = 정규식 '"UnID"\s*:\s*(\d+)'   (같은 줄에 있으면)
      # - unid 있으면: card_bay[sCuID] = (unid, ts)          # 베이번호 출처(ESP forward)
      # - 항상:        port_card[port]  = (sCuID, ts)          # 이 포트가 마지막 본 카드
      #   → 양쪽(card_bay·port_card)이 채워지는 순서 무관하게 resolve (아래 snapshot)
    forget_port(port) -> None     # 포트 disconnect/재오픈 시 그 포트 흔적 제거(휘발 무효화)
    snapshot() -> dict[port,int]   # {port: bay}. port_card[p].sCuID 가 card_bay 에 있으면 그 bay
```
- **상한·drop-oldest**: `card_bay`(distinct 카드 수만큼)와 `port_card`는 OrderedDict + maxlen로
  누수 방지(correlator `_recent` 패턴 재사용).
- **도착 순서 무관**: ESP는 bare `{sCuID}`를 UnID-태그 줄보다 ms 먼저 찍는다. snapshot 시점에
  `card_bay` 조회로 resolve하므로 순서 문제 없음(또는 card_bay 채워질 때 재-resolve).
- **가장 최근 우선**: 카드가 다른 베이로 옮겨지면 그 베이에서 새 태그가 `card_bay`/`port_card`를
  갱신 → 최신 매칭이 이긴다(자기교정).

### 4.2 엔진 배선 `topology_engine.py`

- `__init__`에 `self._pairing = CardPairing()` 추가.
- `observe(port, ts, text)`(Lock 안)에서 `EventAssembler.feed` 옆에 `self._pairing.observe(port, ts, text)`
  **한 줄 추가**. → 페어링은 **라이브 스트림에서 포착**되므로, 카드 줄이 링버퍼 밖으로 밀려나도
  포트→베이 매핑은 유지된다(기존 롤오프 문제 자동 해소 — 핵심 이점).
- `forget_port(port)` 메서드 추가 → `self._pairing.forget_port(port)`. server.py가 포트
  disconnect/hotplug 제거/재오픈 시 호출.
- `roster_and_recent_hops`가 같은 Lock 세션에서 `self._pairing.snapshot()`을 떠서 build_roster에 전달.

### 4.3 `build_roster` 통합 `topology.py`

- 시그니처에 `pairing=None`(= `{port: bay}`) 추가.
- 번호 해소 순서: **별칭 번호 > 로그 파생 번호(_number_from_lines) > pairing[port]**.
  즉 STM 포트가 카드-only라 번호 None이면 `pairing[port]`를 번호로 채택.
- 그러면 기존 `_merge_sb`(P0-2: **번호 같고 MCU 다르면** 병합)가 STM(bay5)+ESP(UnID5)를
  한 노드로 병합 → `SB5`[ESP|STM].
- (선택) 노드에 `number_source: "card_pairing"` 필드로 출처 표기(투명성). 별칭/로그 번호와 구분.

### 4.4 server.py

- 이미 `eng.observe(port, ts, text)`를 매 줄 호출(line 539) → 페어링 자동 급전.
- 포트 monitor의 disconnect/hotplug 제거·재오픈 지점에서 `eng.forget_port(port)` 호출 추가.

### 4.5 휘발성·무효화 (사용자 요구)

- **휘발성**: `CardPairing` 상태는 엔진 인메모리 필드 → **세션 한정, 재시작 시 소멸**(디스크 X).
- **포트 변경 무효화**: `forget_port`로 disconnect/재오픈 시 그 포트 페어링 삭제 → 재꽂아 포트번호가
  바뀌거나 다른 보드가 꽂혀도 스테일 매핑이 안 남는다(새 카드 태그로 재학습).
- **자기교정**: 최신 sCuID 매칭이 이김 → 리케이블·베이 이동 대응.

## 5. 데이터 흐름

```
리더스레드 ──observe(port,ts,line)──▶ 엔진(Lock)
                                     ├─ EventAssembler→Correlator→Routing (mesh 홉·엣지, 기존)
                                     └─ CardPairing.observe (신규)
                                          card_bay[sCuID]=UnID  (ESP forward 줄)
                                          port_card[port]=sCuID  (모든 카드 줄)
get_topology ─▶ roster_and_recent_hops(Lock): pairing.snapshot() ─▶ build_roster(pairing=)
                                          STM 포트 번호=pairing[port]=베이 ─▶ _merge_sb 병합
포트 disconnect ─▶ engine.forget_port ─▶ pairing 삭제
```

## 6. 엣지·한계

- **베이당 카드 1탭 후 페어링**: 태그 전 STM은 별도 무번호 `SB` 노드(P0-1 현행). 침묵하는 STM을
  깨우는 유일 방법이라 불가피. 탭 즉시 병합.
- **동시 태그**: 서로 다른 카드=서로 다른 sCuID → 비모호. 같은 카드를 두 베이에 연속 태그(물리적
  희귀)만 최신 우선으로 처리.
- **ESP 자기 echo**: ESP 포트도 bare `{sCuID}`를 찍지만, ESP는 자기 UnID로 이미 번호가 있어
  pairing이 같은 번호를 얹어도 멱등(무해).
- **멀티 베이 확장**: 베이3 카드는 STM3·ESP3만 그 sCuID를 봄 → 정확히 짝. ✅
- **무설정**: SERIAL_NAMES 불필요(설정하면 여전히 최우선 오버라이드).

## 7. 이번 범위 밖 (안 함)

- 구현·테스트(TDD) — 본 문서는 설계만.
- 시간상관 페어링·1:1 휴리스틱 — 기각(§1).
- SSM 경유 상관 — SSM이 sCuID를 지워 불가(§1).
- 뷰어 프론트 표기(페어링 전/후 전이 애니메이션 등) — 후속.

## 8. 계약 영향 요약

- 신규 파일 1: `topology_pairing.py`(순수).
- 수정 3: `topology_engine.py`(pairing 필드·observe 급전·forget_port·snapshot 전달),
  `topology.py`(`build_roster(pairing=)` + 번호 폴백 + 선택 `number_source`),
  `server.py`(disconnect 시 forget_port).
- 노드 shape: additive만(`number_source` 선택 추가). 기존 필드·병합 계약 보존.
