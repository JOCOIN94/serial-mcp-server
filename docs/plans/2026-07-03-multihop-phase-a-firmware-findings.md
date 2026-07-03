# 멀티홉 relay 증거 — Phase A 펌웨어 정밀 재검증 결과 (XXX-1~7 해소)

상태: **Phase A 완료(2026-07-03).** 마스터플랜(`2026-07-03-multihop-relay-evidence-masterplan.md`) §3의 XXX-1~7을 전부 "확정" 또는 "확인 불가로 설계에 명시 반영"으로 종결했다.
역할: Phase B/C/D/E **세부 구현계획서가 전제로 삼는 사실 대장.** 세부 계획서는 마스터플랜 §2(D1~D6, D4-1)와 이 문서를 함께 전제로 하고, 여기 적힌 판정을 재조사하지 않는다(단, "실측 미확인"으로 표시된 항목을 실측으로 승격하는 것은 환영).
근거: firmware-src(cbm 인덱스 — **§5 버전 스큐 주의 필수**) + 실측 캡처 4종(`docs/archive/`) + 전수조사 2건(Data_Pass 49지점, REGMAC 의미론). 라인 번호는 각 워킹카피 기준(SB는 `.compile/` 사본과 라인 동일).

---

## 0. 최우선 요약 — 마스터플랜을 보정하는 반전 3가지

세부 계획서 작성자는 이 3가지를 모르면 잘못된 파서를 설계하게 된다.

1. **`[Data_Pass] Protected to bypass.`(B형)는 relay 증거가 아니라 relay '거부' 증거다.** 마스터플랜 원인 #4는 "B형이 파서 한계로 조용히 드롭된다"를 결함으로 봤지만, 실체는: B형 = `sendMessage()`가 재전송을 **차단**하고 찍는 흔적(송신 없음). 이걸 '다음 줄 JSON을 붙여 relay 홉으로 인식'하도록 "고치면" 오히려 가짜 경유 노드를 만든다. B형의 올바른 용도는 "그 포트 장비가 그 패킷을 **들었다**(수신 관측)" + "중계기 자격이 없다(fAllowToBypass=false)"까지다. → §1-XXX-2.
2. **A/B 포맷 분화는 장비별·호출부별 규칙이 아니라 단일 코드의 런타임 분기다.** 49개 지점 전부 동일한 `print("[Data_Pass] ") → sendMessage() → println(원문JSON)` 형태이고, sendMessage 내부 보호 분기가 태그 줄에 `Protected to bypass.`를 삽입하느냐(B)/침묵하느냐(A)로 갈린다. **깨끗한 `[Data_Pass] {json}` 한 줄 = 실제 브로드캐스트 완료의 증거로 써도 된다.** → §1-XXX-2.
3. **CHPLAN은 멀티홉 경로가 아니라 'relay 후보 우선순위 목록'이고, 리피터는 CHPLAN을 아예 소비하지 않는다.** `[2,["7C","02"],4,30,0]`의 `["7C","02"]`는 "7C를 먼저, 안 되면 02를" (대안 목록)이지 "7C 다음 02를 경유"(체인)가 아니다. 소비 주체는 엣지(SB) 단 하나 — 자기 상행 Event.txt 재시도 스테이징에 쓴다. relay 실행과는 인과가 없다(별개 REGMAC 메커니즘). → §1-XXX-1/XXX-6.

---

## 1. XXX 항목별 판정

### XXX-1. CHPLAN 배열의 실제 구조 — **확정 (v1/v2 두 포맷 공존, 위치 1 배열은 v2)**

- 인덱스 소스의 SSM(`SSM260525-004`)은 **v1**을 보낸다: `[ROUTE_PROTO_VERSION=1, planA, planB, TTL, expire_s]` — planA/planB는 **별개 2글자 토큰 문자열 2개**가 맞다(SSM_esp32.ino:6482-6487). 호출 사슬: `RouteRefreshAndMaybeSend`(6503) → `RouteSelectPlan`(6435)이 planA/planB를 채움 → `RouteSendChplan(targetPos, planA, planB)`(6528). **v1 실물 확보(2026-07-03 로컬 벤치, SSM260526-004)**: `{"CHPLAN":[1,"00","FF",4,120],"Asn":70,"UnID":5,"Cidx":898}` — 직결 상태의 no-relay 플랜(A="00" direct, B="FF" empty)도 CHPLAN으로 발신됨을 실측(XXX-5 fixture 참조). 파서는 "00"/"FF"를 경유 노드로 그리면 안 된다(`_RESERVED_TOKENS` 기존 처리와 일치).
- 실측 `[2,["7C","02"],4,30,0]`은 **v2**: `[ROUTE_CHPLAN_VERSION=2, [relay토큰...], ttl, expire_s, packetId]`. 수신측 `RouteApplyChplan`(SB_ESP32.ino:10740-10802)이 **v1·v2 둘 다 파싱**한다(10748 v2 분기: `arr[1].is<JsonArray>()`, 10765 v1 분기). v2 정의는 SB 헤더에만 있고(SB_ESP32.h:105 `ROUTE_CHPLAN_VERSION 2`) 인덱스된 SSM 소스엔 v2 송신 코드가 없다 → **배포 SSM(SSM260702-002)이 v2 송신자**(§5 스큐).
- 토큰 의미: `RouteTokenForInfoPos`(SSM_esp32.ino:6299) = UnitID의 `%02X`(없으면 `retEncrytion(mac)`). `"00"`=direct, `"FF"`=empty(SB_ESP32.h:109-110). 실측 `"7C"`=REP, `"02"`=UnID 2인 SB2 — **SB도 relay 후보가 된다.**
- 의미론: `RouteSelectPlan`(6435-6461) — direct 양호하면 planA="00"·planB=최선 relay(백업), 불량하면 planA=최선 relay·planB=차선 relay(없으면 "00"). 즉 **대안 목록**. SB는 이를 `routeConfig.relayTokens[]`(최대 5개, `ROUTE_CHPLAN_MAX_RELAYS`)로 저장하고 `routeAttemptStage`로 단계 재시도한다.
- **세부 계획서 제약**: 하행 CHPLAN 파서는 위치 1이 문자열(v1)이든 배열(v2)이든 수용해야 한다(수신 펌웨어가 양쪽을 받으므로 양쪽 다 실존 포맷). ttl/expire/pid 위치가 버전에 따라 밀리는 것도 반영. CHPLAN 토큰을 "경유 순서"로 렌더링하는 것은 **금지**.

### XXX-2. `[Data_Pass]` 포맷 분기 규칙 — **확정 (전수 49지점, 규칙은 런타임 조합)**

전수조사(서브에이전트, grep 61건 = 워킹카피 49 + SB `.compile` 사본 12 중복) 결과:

- **정적으로는 49지점 전부 같은 코드**(전부 `WiFi_rev_proc(String)` 내부, 전부 fSerial 게이트):
  ```c
  if(fSerial==true) Serial.print("[Data_Pass] ");
  fBypass = true;                    // SSM 1지점만 이 행 없음
  sendMessage(sWiFiRx);              // DNFWVER 지점만 사이에 delay(random(50,200))
  if(fSerial==true) Serial.println(sWiFiRx);   // 수신 원문 그대로
  ```
- **분기는 `sendMessage(String)` 내부**(SB 6375 / REP 4799 / APU 5571 / APU_C 5749):
  `(fBypass && !fAllowToBypass && !fexept)` → `Serial.println("Protected to bypass.")`(**fSerial 게이트 없음**) 후 **송신 없이 return** → 물리 B형. 아니면 3회 브로드캐스트 → 물리 A형.
- `fexept` = 송신 JSON에 `REGMAC` 또는 `SPECIAL` 키 존재 → 보호 우회(항상 송신). 실측 A형 예시(`{"REGMAC":...}`)와 B형 예시(RTC/CHPLAN)가 정확히 이 분기로 설명된다.
- `fAllowToBypass` = **RegMacBuff에 UpDn==1 엔트리가 하나라도 있으면 true**(SB 8757-8774·12933-12948·14466-14483, 부팅 SPIFFS 로드·SETREGMAC 시 재계산, 콘솔에 `AllowToBypass : YES/NO`). "내 밑에 장비가 등록돼 있다"가 중계기 자격 스위치다.
- 트리거 12클래스(각 장비 공통 세트): Alive/CHKRSSI/WHO/TEST/ReqPriceToSSM/ReqAdjPriceToSSM/ReqCBayToSSM/RESET=ALL/APMODE=ALL/COMMAND=ALL/DNFWVER/**유니캐스트 not-me**(Mac/UnID ≠ 나). SSM은 CHKRSSI 1지점뿐(SSM_esp32.ino:7083)이고 SSM `sendMessage`엔 보호 분기 자체가 없어 **SSM의 [Data_Pass]는 항상 A형·항상 실송신**.
- **파서 요구사항(Phase B에 그대로 전달)**:
  - `[Data_Pass] {` → A형: 같은 줄 JSON = **재전송 완료 관측**(relay 홉 증거로 사용 가능).
  - `[Data_Pass] Protected to bypass.` → B형: 다음 줄 단독 JSON = **재전송 안 됨**(경유 홉 아님; '들었다' 수신 관측으로만 사용).
  - 줄 시작 앵커 금지: Alive 지점은 직전 무조건 `Serial.print('.')`로 물리 줄이 `.[Data_Pass] …`.
  - SSM 콘솔 변형: `[Data_Pass]  To. <이름>, {json}` — SSM sendMessage가 `" To. %s, "`를 **무조건** 삽입(SSM_esp32.ino:5347). 또 SSM은 송신 전 Cidx 재스탬프·Mac→UnID 치환을 하므로 **SSM 콘솔의 JSON ≠ 실제 재전송 JSON**.
  - DNFWVER 지점은 태그와 JSON 사이 최대 200ms 열림 — 다른 태스크 출력이 줄 중간에 낄 수 있음.
  - fSerial=false면 태그·JSON은 침묵하고 `Protected to bypass.`만 고아 줄로 남는다.
  - C형(설명문만·후속 JSON 없음)은 소스상 0건.

### XXX-3. 상행 relay 경로가 BypassJsonPacket 하나뿐인지 — **확정 (relay 의미 코드 경로 4계열)**

1. **`BypassJsonPacket()`** (SB 11105 / REP 7333 / APU 9149 / APU_C 8386, 메인 loop 매회 호출) — **상행 전용 주력**. 큐 게이트는 수신부: 패킷에 `Rev` 존재 && 발신 MAC이 RegMacBuff에 UpDn==1로 등록 && 중복 아님 → WriteJsonBuffer(SB 5756-5770). 함수 내부: XOR 디코드 → **bare 원문 JSON println(무게이트)** → 자기 토큰(BayID hex, 없으면 mac 축약)을 `Rt`에 스탬프(`- Empty point of Rt`/`- Encryted data of Rt` 진단 출력) → `[BypassJson] <스탬프된 JSON>`(무게이트, REP 7434-7435) → 1회 재브로드캐스트. `Rev`는 장비 자체발신 상행 마커(sendMessage가 fBypass=false일 때 `Rev:true`+Cidx 부착, SB 6322-6327)라 SSM발 하행엔 없음 → 이 경로는 사실상 상행 전용이 맞다.
2. **`WiFi_rev_proc`의 [Data_Pass] 12클래스** (XXX-2) — **양방향**(하행 유니캐스트 not-me, 브로드캐스트 플러드류). fAllowToBypass 게이트.
3. **바이너리 패킷 bypass** (SB 7585 `if(fAllowToBypass) WriteBinSendBuffer(...)`) — OTA 등 바이너리 중계, JSON 콘솔 마커 없음(`[Bin] ...` 계열만). 텍스트 파서 범위 밖임을 명시.
4. **SSM 자신의 재전송 경로** — `[Proc-WiFiTx]`(fSerial, 하행 응답·ACK), `[Proc-WiFiTxBypass]`(fSerial, 웹 명령→하위장비 중계, SSM 11253 등 8지점), `[Proc-WiFiTx-ACK]`(무게이트, 6867), CHKRSSI [Data_Pass].
- **R-헤더(Event.txt) 라우팅은 relay 실행 메커니즘이 아니다**: REP 소스에 R 파싱·`RouteParseHeader` 0건. R/CHPLAN은 SB(송신 스테이징: `[Route] Event route Asn=.. pid=.. stage=.. rescue=.. relays=.. len=..` SB TX측 로그, 실측 2_bay:405,502)와 SSM(중복 이벤트 ACK 재전송 `[Route] Duplicate event ACK replay`, SSM 7050; ACK 회신 시 `routeReturnHopToken`=수신 Rt 마지막 토큰 사용) 간의 장부다. 경유 장비는 그 사이에서 위 1·2 메커니즘으로 나른다.

### XXX-4. 복수 SSM 그룹 걸침 relay — **확정 (조건부: GID·채널 다르면 불가, 같으면 가능)**

- `RegMacconfig{ uint8_t mac[6]; uint8_t UpDn; }`(SB_ESP32.h:194-198, 30슬롯). **UpDn = 등록 이웃의 위치**: 0=SSM측(상위) 이웃, 1=반대편(말단측) 이웃(SETREGMAC 프롬프트 원문, SB 12890). UpDn=0은 수신 화이트리스트 역할(미등록 MAC 패킷은 기본 폐기, SB 5672-5677), UpDn=1은 상행 bypass 대상 + 중계기 자격(fAllowToBypass).
- 채움 경로 3개뿐(자동 학습 없음): ①SETREGMAC 시리얼 ②REGMAC JSON 무선(웹서버발, SSM이 대상 지정 중계, 타깃 게이트 후 적용) ③부팅 SPIFFS 복원.
- **등록 시 GID/채널/그룹 검증 없음**(`RegMacAddrOfSender` SB 3016-3041: 빈 슬롯 memcpy뿐) — 타 그룹 MAC을 넣는 것 자체는 가능.
- 그러나 실제 중계엔 물리 병목 2개: **장비 전역 단일 채널**(`config.Chn`, `WiFi.setChannel` — 듀얼채널 불가) + **장비 전역 단일 GID XOR 키**(`cHidden = nibble-swap(255-GID)`, SB 15714-15717 등 3장비 공통 — 타 GID 패킷은 디코드 실패로 수신 소멸, 주석 원문 `"Differnt GID."` SB 5704).
- **결론**: GID나 채널이 다른 두 SSM 그룹의 동시 중계는 펌웨어상 불가능. **같은 GID·같은 채널의 복수 SSM**(테스트베드 혼재, GID 미분리 배치, GID=255 미설정)이면 교차 relay를 막는 코드가 없다.
- **세부 계획서 제약**: `topology_chains.py` 그룹 베토의 '그룹' 정의가 "GID/채널 도메인"과 일치하는지 확인하라. "그룹 = SSM 포트 1개"로 정의돼 있으면, 같은 GID·채널에 SSM이 2대 물린 환경에서 실존 가능한 교차 증거를 오폐기한다. 단 serial-mcp는 GID를 직접 관측할 수 없으므로(로그에 안 나옴), 현실적 처리는 "단일 SSM 관측 시 현행 유지 + 복수 SSM 그룹 관측 시 베토를 '폐기'가 아닌 '미확정(unconfirmed)' 강등" 같은 보수적 완화를 검토하되, **이 변경은 additive 원칙(D4-1) 안에서만**.

### XXX-5. `[Route] CHPLAN` 로그가 항상 찍히는지 — **확정 (아니오 — 세대 의존, 구세대는 TX 콘솔 무증거)**

- **로컬 벤치 실측(2026-07-03 18:25, SSM260526-004 + SB260526-002 직결)이 소스 전제를 반증했다**: SSM은 INFO 수신마다 CHPLAN을 실제 발신하는데(SB COM12 수신 확인) **SSM 콘솔(COM4)엔 `[Route]` 줄이 단 한 줄도 없다.** 즉 이 세대에서 CHPLAN TX는 **콘솔 무증거 송신**이다.
- 소스(워킹카피)의 `RouteSendChplan` printf(SSM 6499, fSerial 게이트 없음)·`[Route] Plan update`(6526)는 **워킹카피가 FW_VERSION(260525-004) 문자열보다 앞선 WIP**임을 시사한다 — 260526-004(로컬)엔 없고, 소스에 있으니 이후 세대(배포 260702-002 추정)에 들어갔을 가능성이 높다. 단 **배포 SSM의 실제 출력 여부·문구는 여전히 실측 미확인**(1_ssm 캡처는 16:33:19에 끝나 CHPLAN 시점 16:33:35 미포함). → 파서는 SSM측 `[Route] CHPLAN to`를 **있으면 쓰는 선택 증거**로 다루고, 부재를 발신 부정으로 해석하지 않는다.
- 발신 자체도 엣지 트리거: INFO 수신(7452)·REPRSSI(7593, urgent는 forceSend 7591)가 트리거이되 `RoutePlanChanged`일 때만 발신(6516) + `plan.expiresMs`(=ROUTE_EXPIRE_MS) 경과 시 재발신. 로컬 실측 주기: 18:24:59 → 18:25:11(부팅 직후 재계산) → 18:27:15(만료 재발신, expire=120s와 정합).
- **v1.14 발행 게이트와의 정합이 실측으로 확인됐다**: CHPLAN 하행 체인은 구세대 SSM에서 송신 콘솔 증거가 원천 부재 → 게이트가 의도대로 이를 걸러낸다(회색 점프 버튼 방지). Phase C는 게이트를 완화하지 말고, 신세대 SSM이 `[Route] CHPLAN to`를 찍으면 그게 송신 증거가 되어 자연히 발행되는 구조를 유지한다.
- **로컬 벤치 v1 실물 fixture(SB COM12 수신면, Phase C용)**:
  ```
  [WiFi_Rx] {"CHPLAN":[1,"00","FF",4,120],"Asn":70,"UnID":5,"Cidx":898}
  {"CHPLAN":[1,"00","FF",4,120],"Asn":70,"UnID":5}          ← 후속 bare 에코(Cidx 없음 변형)
  [Route] CHPLAN applied A=00 B=FF ttl=4 expiry=120s        ← v1 세대 문구
  [Route] Event route Asn=8 pid=3 stage=0 retry=0 next=00 len=154   ← v1 세대(retry=/next=)
  ```
  v2 세대(260610+) 문구는 다르다: `[Route] CHPLAN applied v=%u cnt=%u ttl=%u pid=%u`, `[Route] Event route ... rescue=0 relays=00`(2_bay 실측). **`[Route]` 하위 문구는 세대별 변형을 전제로 파싱할 것.**
- **연쇄 확정 — SSM 콘솔의 relay 증거 소멸 메커니즘**(마스터플랜 원인 #2·#3의 코드 근거): SSM `WiFi_rev_proc`은 `[Proc-Raw Packet]`+`[Passed Device]`를 `strRtData`로 조립만 해두고(6901-6964), 출력은 `fprintAllReceivedPackets`(기본 false, SSM_esp32.h:2205, 토글 명령 19070) 기준 — false면 **중복수신 검사(7026 CHKSEND/7037 CHKREV) 통과 후에만** 출력(7059). 직결 사본이 먼저 도착하면 뒤따르는 Rt 실린 relay 사본은 중복으로 소멸 → **[Passed Device]는 "relay가 유일한(또는 최초) 도달 경로였을 때만" 나타난다**. 또 `[Proc-WiFiRx]`는 Rt/DT를 remove한 뒤의 JSON을 찍는다(6967-6968 → 7021-7022) — [Proc-WiFiRx]에서 Rt를 찾는 것은 원천 불가. 테스트베드(직결 가능 거리)에서 0건이 나온 이유이자, 실배치(직결 불가 거리)에선 유효한 권위 소스라는 뜻.

### XXX-6. CHPLAN이 relay를 인과적으로 발생시키는지 — **확정 (아니오 — 의도 정보다)**

- CHPLAN 소비 코드는 **SB에만** 있다(REP·APU 0건). SB `RouteApplyChplan`이 자기 `routeConfig`(relay 후보 목록·ttl·30s expire·pid)에 저장 → 자기 **상행 Event.txt 송신 스테이징**에만 쓴다.
- relay 실행(BypassJsonPacket·Data_Pass 경로)은 **REGMAC UpDn 기반으로 CHPLAN과 완전 독립**. 실측 정합 사례: SSM이 SB2(토큰 02)를 relay 후보로 계획(CHPLAN)했지만 SB2는 fAllowToBypass=false라 `Protected to bypass.`로 거부 — **의도≠실행의 실증**.
- **D1 라벨링 규칙 확정 재료**: observed(실행 관측) = `[BypassJson]`(Rt 스탬프 포함), `[Data_Pass] {json}` A형, `[Passed Device]`/Rt 배열. intent(의도) = CHPLAN, `[Route] Plan update`, `[Route] Event route ... relays=..`(그 시도에 선택한 경로 — 성공 여부는 별개), R-헤더. 기존 `confidence`/`inferred` 필드 재사용으로 충분한지 vs intent 전용 값 추가가 필요한지는 Phase C 세부 계획서에서 결정.

### XXX-7. 2홉 이상 실측 — **확인 불가로 설계에 명시 반영 (+ 신규 위험 발견)**

- 현 PC 테스트베드 = SSM(COM4)+SB5(COM12/13)뿐, 리피터 없음 → 물리 2홉 불가. 캡처 출처 테스트베드(별도 머신)는 SSM+REP+SB2+Bay_B02로 **1홉만 실증**.
- **신규 위험**: 인덱스 소스의 Rt 스탬프 루프(REP 7393-7400, 4파일 공통)는 `if(jsonWiFiRxBuf["Rt"][i]) break;` — 첫 **존재하는** 슬롯에서 break 후 그 슬롯을 덮어쓴다(주석·출력문 의도는 '빈 슬롯 찾기'로 보이나 조건이 역전됨). 이 코드대로면 **2홉이어도 Rt엔 마지막 relay 토큰 1개만 남는다**(경유 이력 소실). 배포 펌웨어(REP260603-001)에서 수정됐는지는 소스 부재로 미확인.
- **판정**: (a) Phase D는 합성(fixture) 멀티홉 테스트로 파서·`_rebuild_with_skeleton`의 N-토큰 처리를 검증하되 "구조상 가능"으로만 표기, (b) "실측 확인됨" 승격은 리피터 2대(또는 REP+relay 자격 SB) 직렬 실측 후에만, (c) 파서는 Rt 길이 1~N 전부 수용(N≥2 미실측이어도 코드 경로는 동일 — D4), (d) **펌웨어 소유자에게 Rt 루프 로직 확인을 전달할 것**(수정되면 진짜 N홉 이력이 생기고, 안 되면 '마지막 홉만 관측됨'이 펌웨어 한계임을 문서화).

---

## 2. Phase B/C가 다뤄야 할 콘솔 마커 카탈로그 (관측면 전수)

| 마커(물리 형태) | 어디 | 게이트 | 의미 | 신뢰도 분류 |
|---|---|---|---|---|
| `[BypassJson] {json+Rt}` | 비-SSM relay | 없음 | 상행 relay **실행** 관측(Rt에 자기 토큰 스탬프 후) | observed |
| bare `{json}` (BypassJson 직전 줄) | 비-SSM relay | 없음 | 같은 이벤트의 수신 원문(Rt 스탬프 전) — 중복 주의 | observed(RX) |
| `- Encryted data of Rt : XX == XX.` / `- Empty point of Rt : N.` | 비-SSM relay | 없음 | Rt 스탬프 진단(토큰=XX) | 보조 |
| `[Data_Pass] {json}` (같은 줄) | 전 장비 | fSerial | relay **실행** 관측(브로드캐스트 완료) | observed |
| `[Data_Pass] Protected to bypass.` + 다음 줄 `{json}` | 비-SSM | 태그=fSerial, 문구=무게이트 | relay **거부**(송신 없음) — 경유 홉 아님, 수신 관측만 | observed(RX-only) |
| `.[Data_Pass] …` | 전 장비 | fSerial | Alive 재전파 변형(선행 `.`) — 줄 시작 앵커 금지 | — |
| `[Data_Pass]  To. <이름>, {json}` | SSM | fSerial(태그)/무게이트(To.) | SSM CHKRSSI 재전파 변형. SSM 콘솔 JSON은 재스탬프 전 원문 | observed |
| `[Passed Device] (07-name)->(7C-name)…` | SSM | fprintAllReceivedPackets 로직+중복검사 통과 시 | Rt 전체를 로스터 이름으로 해소한 경로 — **있으면 권위, 없어도 부정 아님** | observed(최고) |
| `[Proc-Raw Packet] : {json+Rt}` | SSM | 위와 동일(strRtData 일부) | Rt 제거 전 원문 — Rt 보조 소스(같은 조건에서만 출력) | observed |
| `[Proc-WiFiRx] {json}` | SSM | fSerial | 수신(단, **Rt/DT 제거 후**) — Rt 탐색 금지 | observed(RX) |
| `{"CHPLAN":[…]}` (수신면 [WiFi_Rx] 등) | SB·오버히어 장비 | — | SSM의 relay 후보 계획(v1/v2, XXX-1) — 경로 아님 | intent |
| `[Route] CHPLAN to <mac> A=.. B=..` | SSM | **세대 의존** — ≤260526 세대는 아예 미출력(무증거 송신, 실측), 이후 세대만 추정(문구 미실측) | CHPLAN 송신과 1:1(간헐 발신) — 있으면 쓰는 선택 증거 | intent |
| `[Route] Plan update (...) token=.. A=.. B=.. reason=..` | SSM | 세대 의존(위와 동일) | 플랜 변경 감지(CHPLAN 직전) | intent |
| `[Route] CHPLAN applied ...` | SB | 없음 — 단 **문구 세대 변형**: v1세대 `A=00 B=FF ttl=4 expiry=120s` / 260610+ `v=.. cnt=.. ttl=.. pid=..` | 엣지가 계획을 수용했다는 관측 | intent 수용 |
| `[Route] Event route Asn=.. pid=.. stage=.. ...` | SB | 없음 — 문구 세대 변형: v1 `retry=.. next=..` / 260630+ `rescue=.. relays=..` | 그 송신 시도에 선택한 경로(00=direct) | intent(시도) |
| `[Proc-WiFiTx] {json}` / `[Proc-WiFiTxBypass] {json}` / `[Proc-WiFiTx-ACK]` | SSM | fSerial/fSerial/없음 | 하행 송신(Cidx 스탬프 **전** 출력) | observed(TX) |
| `AllowToBypass : YES/NO` | 비-SSM | 없음 | 그 장비의 중계기 자격 상태(부팅·SETREGMAC 시) | 로스터 보조 |

공통 주의: fSerial은 3개 펌웨어 모두 기본 true(각 헤더, `Display full`)이나 런타임 토글 명령 존재 + 대화형 명령(SETCONFIG 등) 중 일시 억제(저장/복원). 관측 부재를 장비 부재로 해석하지 말 것.

## 3. Cidx/Rev 의미론 정정 (기존 세션 결론 보정)

- **Cidx는 SSM 전용이 아니다.** 모든 장비가 자체 발신(fBypass=false) 시 `Rev:true`+Cidx를 스탬프한다(SB 6322-6327). 실측: REP 콘솔의 [BypassJson] 연쇄에서 Bay_B02발 패킷들이 Cidx 996→1008 단조증가(Bay 자신의 카운터). SSM은 수신 중계 시 Cidx를 **재스탬프**하고 Mac→UnID 치환한다. 비-SSM relay는 바이트 동일 재전송(Cidx 불변).
- 함의: Cidx 기반 dedup/ident(v1.15 기능)는 "Cidx=발신 장비별 카운터, 비-SSM relay 통과 시 불변, SSM 경유 시 변조"를 전제로 재점검 필요(Phase B 세부 계획서에서 기존 로직과 대조).
- `Rev:true` = 장비 자체발신 마커(SSM발 하행엔 없음) — 방향 판정 보조로 사용 가능.

## 4. 실측 캡처의 장비 대응표 (docs/archive/ 재해석 시 참조)

| 캡처 | 장비 | 배포 FW | 비고 |
|---|---|---|---|
| 1_ssm_last500.txt | SSM | SSM260702-002 | 16:33:19에 끝남 — CHPLAN(16:33:35) 미포함 |
| 2_bay_b01_1chi-bay_last500.txt | SB2 (UnID 2, 토큰 02, MAC 80:7D:3A:82:5A:AC) | SB260630-002 | fAllowToBypass=false → Protected 다수 |
| 3_repeater_last500.txt | REP (토큰 7C, MAC 10:06:1C:16:97:AC) | REP260603-001 | [BypassJson] 94건 — Bay_B02 상행 중계 |
| (콘솔 미연결) | Bay_B02 (UnID 1, MAC 30:AE:A4:4C:94:20) | SB260702-001 | SSM RX로만 관측 |

## 5. 버전 스큐 경고 — firmware-src는 배포보다 구세대

인덱스 소스 SSM260525-004 / SB260610-001 ↔ 배포 SSM260702-002 / SB260630-002·SB260702-001 / REP260603-001. 확인된 차이: SSM CHPLAN v1(소스)→v2(배포). REP의 bypass 출력 흐름(bare JSON→Rt 진단→[BypassJson])은 소스와 실측이 정확히 일치. **원칙: 물리 포맷 확정은 실측 캡처(배포) 우선, 메커니즘·게이트 의미론은 소스 — 어긋나면 실측이 이긴다.** 세부 계획서의 fixture는 반드시 실측 캡처의 원문 라인에서 채취할 것(소스에서 상상으로 조립 금지).

## 6. 세부 구현계획서에 주는 추가 지침 (마스터플랜 §0 이행 시드)

1. Phase B(상행 인식): [BypassJson]·[Data_Pass] A형을 additive relay 증거로 추가. B형은 relay 홉 금지(수신 관측/heard로만). bare-JSON 선행줄·`.` 접두·`To.` 삽입·200ms 지연 등 §2의 물리 변형을 fixture로 고정. 기존 `[Passed Device]`/Rt 경로는 그대로 두되(여전히 유효한 권위 소스) "없음=부정"으로 쓰지 않게 확인.
2. Phase C(하행): CHPLAN v1/v2 파싱 + intent 라벨(D1) + SSM TX면([Proc-WiFiTx]계열)과의 상관. v1.14 "송신 콘솔 증거 게이트"와의 정합: SSM TX 마커가 fSerial 의존임을 감안해 게이트 판정 로직이 이를 어떻게 다루는지 명시할 것.
3. Phase D: 합성 N-토큰 Rt fixture + 직결(2-node) 회귀(D4-1) + "Rt 단일 토큰 한계 가능성"(XXX-7) 명시.
4. 모든 Phase: 기존 테스트 변경 금지 목록·수정 파일 범위 고정·"애매하면 멈추고 보고" 조항 필수(마스터플랜 §0).
