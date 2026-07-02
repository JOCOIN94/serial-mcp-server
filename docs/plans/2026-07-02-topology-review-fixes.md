# 토폴로지 자동분류 재검토 → 수정 기록 (2026-07-02)

> **시점 기록(동결).** 그룹 규칙·홉경로 생성·프론트 렌더 전면 재검토(펌웨어 소스 대조 포함)에서
> 나온 결함과 그 수정을 같은 세션에서 구현했다. 배경 맥락은 `2026-07-01-topology-link-unique-txrx.md`.

## 펌웨어 대조로 확정한 사실 (cbm `firmware-selected`)

1. **INFO[2] ≠ 링크 RSSI.** `INFO[2] = avrRssi` = 그 장비가 들은 **모든 이웃의 평균**(SB_ESP32.ino
   INFO 조립부, `stNearbyInfo[]` 평균). SSM INFO 테이블의 **RF 열도 같은 값**(`InfoListArr[].rf =
   jsonWiFiRxBuf["INFO"][2]`, SSM_esp32.ino:2323). 링크별 품질은 [Route] Link / REPRSSI 뿐.
2. **UnID 는 BayID 있을 때만.** 전 리프(SB/REP/APU) 공통 `if(ConfigBay.BayID) UnID else Mac`.
   BayID=0 장비는 payload 에 Mac 이 실린다.
3. **INFO 테이블 (ID) 열 = payload UnID.** `InfoListArr[pos].UnitID = jsonWiFiRxBuf["UnID"]`
   (SSM_esp32.ino:7105) → 테이블이 **mac↔UnID↔unitName 다리**가 된다.
4. **Unique = 1..99 롤링**(MACUNIQUEVALUE=99, 0 금지) — "uint8 0~255" 아님(주석 정정).
5. SB 포트 로그엔 [WiFi_Rx]·[Data_Pass] 로 **남의 UnID** 가 섞인다(중계·요청 오버히어).

## 결함 → 수정 (전부 이 세션에서 구현·테스트 완료)

| # | 결함 | 수정 |
|---|---|---|
| 1 | correlator 키 (UnID,Unique) 필수 → BayID=0 장비는 상관·멤버십·링크선 조용히 전부 불가 | 키를 **(UnID ∥ Mac, Unique)** 복합으로 (`topology_correlator._key`) |
| 2 | TX 이벤트가 다음 헤더/2s 유휴 flush 까지 버퍼링 → rx_grace(1s)가 먼저 만료돼 src_port **확률적(~과반) 유실**(스윕 위상 경쟁) | tx([Tx - my INFO])는 헤더 한 줄에 완결이므로 어셈블러가 **즉시 방출** (`EventAssembler.feed`) |
| 3a | `_number_from_lines` 가 블롭 첫 `"UnID"` 매칭 → 중계 구성에서 남의 번호 오귀속 | **[Tx - my INFO] 줄 한정** 추출 |
| 3b | `forget_port` 가 pairing 만 정리 → leaf 재꽂/제거 후에도 멤버십 local_port 가 남아 **유령 링크선** | 멤버십도 정리: ssm_port 엔트리 drop + local_port 무효화 |
| 4 | 링크선 색이 INFO[2](장비 평균) 직결 — "링크 품질" 라벨이 축 오류. `pick_link_metric`/ladder 데드코드 | **정공**: SSM INFO 테이블 파서(`RoutingTable.observe_table_line`, 무상태 라인 매칭)로 mac 다리 구축 → [Route] Link/REPRSSI **링크별** rssi 를 ladder 로 우선 선택, INFO[2]/테이블RF 는 장비 단위 폴백. edge 에 `rssi_source` 노출, SSM 노드 mac 도 자기 행에서 채움 |
| 5a | SSE 홉 → 400ms 재조회가 roster 변경과 겹치면 전체 재렌더가 **홉 펄스 중간 사멸** | 재렌더 시 진행 중 `.thop` 를 새 canvas 로 **이식** |
| 5b | hopWaypoints(src_port 폴백) vs hopDetail(src_name 폴백) 불일치 → 펄스는 그려지는데 패널 빈 칩 | hopDetail 폴백 사슬을 path→src_name→**src_port** 로 통일 |

오탐 정리: "토폴로지 이중 fetch"(refreshStatus 는 /api/status 만 fetch, 렌더는 캐시)와
"홉 SVG 누수"(parentNode 가드+GC)는 재검토 결과 문제 아님.

## 파급 규칙

- 멤버십 키가 이제 UnID(int) 또는 Mac(str) — 타입이 달라 충돌 없음. Mac 키 멤버는 unid enrich 만
  자연 미적용(우아한 열화).
- edges 스키마 additive: `rssi_source` 추가(기존 소비자 무영향). SPEC 은 edges 형태를 명세하지
  않아 갱신 불요.
- INFO 테이블 파싱은 bootstrap INFO(서버 1회 발신) 또는 사용자 INFO 실행 시 채워진다 — 없으면
  장비 평균 폴백으로 동작(다리 없어도 기존 동작 유지).

## 검증

- `uv run python -m pytest`(이 PC 는 `uv run pytest` 깨짐) — 413 passed. 신규: mac 폴백 상관,
  tx 즉시 방출, forget_port 멤버십 정리, 자기 보고 줄 한정 번호, INFO 테이블 파서 5종,
  ladder 우선순위 3종(roster), 관통(engine) 1종. JS 하네스(edge source·hopDetail 폴백) 포함.
- `py -m compileall -q src` OK.
- 실장비 검증(잔여): COM4(SSM)+COM12/14(SB) 라이브 — INFO 1회 실행 후 링크선 툴팁에
  `route_link` 출처가 뜨는지, SB 재꽂 시 유령 링크가 사라지는지.
