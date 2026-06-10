# 다중 포트 자동 모니터링 설계 (SPEC §1 전제 개정)

> 2026-06-10 브레인스토밍 산출물. 승인된 설계이며 writing-plans의 입력이 된다.
> **SPEC §1의 "단일 장비" 전제를 "복수 장비(자동 인식)"로 개정하는 변경.**

## 1. 목적·배경

PC에 보드가 여러 개 연결된다(예: SSM 게이트웨이=ESP32-S3@COM4, SB=Smart Board(ESP32-S2+STM32)@COM13 등). 현행 서버는 `SERIAL_PORT` 하나만 읽으므로, **연결된 USB 시리얼을 자동 인식해 있는 만큼 전부**(2개면 2개, 10개면 10개) 동시 모니터링하도록 확장한다. 사람이 궁금한 것은 포트 이름이 아니라 **어느 보드인지**이므로, 별칭 매핑으로 `SSM (COM4)` 형태 표기를 전면 적용한다.

## 2. 확정 결정사항 (브레인스토밍)

| 항목 | 결정 |
|---|---|
| 자동 인식 대상 | **USB 시리얼만**(VID/PID 보유 — CH343·CP210x·FTDI·Prolific 등). 블루투스 가상 포트 제외 |
| 스캔 시점 | **시작 시 1회만**. 핫플러그 없음(보드 추가는 서버 재시작) |
| 도구 계약 | `port` 인자 추가, **지정 호출이 기본**. 미지정 시: 포트 1개면 그 포트(현행 호환), 복수면 `status:"error"` + 포트 목록 반환(AI가 즉시 재호출). 예외: `clear_log_buffer` 미지정=전체 비우기 |
| 병합 뷰 | 없음(YAGNI — 사용자 결정. 포트 지정 호출로 충분) |
| 뷰어 | **포트별 화면 전환만**(셀렉터). 통합 뷰 없음 |
| 별칭 | `SERIAL_NAMES`로 포트→보드명 매핑, 표기는 `SSM (COM4)`. 도구 `port` 인자에 별칭/포트명 양방향 허용 |
| dedup 룩백 | **기본 5줄**로 확장(`SERIAL_DEDUP=N`). 교차 반복 압축. §6 참조 |

## 3. 환경변수 계약 (변경·신설)

| 변수 | 기본값 | 의미 |
|---|---|---|
| `SERIAL_PORT` | (없음) | **미설정 = USB 시리얼 자동 전부**(권장 기본). 설정 시 그 목록만: `COM4` 또는 `COM4,COM13@9600`(포트별 보드레이트 `@N` 문법, 생략 시 `SERIAL_BAUD`) |
| `SERIAL_NAMES` | (없음) | 별칭 매핑. `COM4=SSM,COM13=SB1` 또는 USB 시리얼넘버 키 `5909024173=SSM`(포트 번호가 바뀌어도 유지). 미매핑 포트는 포트명 그대로 표기 |
| `SERIAL_BAUD` | `115200` | 전역 기본 보드레이트(포트별 `@N`이 우선) |
| `SERIAL_DEDUP` | `5` | **의미 확장**: `0`/`false`=끔, `1`=직전 줄만(구버전 동작), `N≥2`=최근 N항목 룩백 접기. 기존 `1`/`true` 사용자와 하위호환 |
| `SERIAL_TEE` | (없음) | **포트별 파일로 분리**: `log.txt` 지정 시 `log.COM4.txt`·`log.COM13.txt` 생성(별칭 있으면 `log.SSM.txt`). 원본 순수성 유지 |
| `SERIAL_BUFFER_LINES` | `2000` | **포트당** 버퍼 크기 |
| `SERIAL_EXCLUDE`/`INCLUDE`/`SERIAL_WEB` | 기존 | 전 포트 공통 적용(필터 포트별 분리는 비범위) |

하위호환: 기존처럼 `SERIAL_PORT=COM4` 단일 지정이면 동작이 현행과 동일(자동 스캔 안 함).

## 4. 아키텍처 — PortMonitor 일반화 (접근안 A)

```
main()
 ├─ 포트 결정: SERIAL_PORT 파싱 또는 USB 자동 스캔(list_ports에서 vid≠None)
 ├─ 별칭 해석: SERIAL_NAMES (포트명 키 + serial_number 키)
 └─ _monitors: dict[str(port), PortMonitor]   ← 신규 집합 구조
       PortMonitor = { name(별칭|포트명), SerialReader, LineBuffer, RawFeed, tee }
                       └ 기존 클래스 그대로 N개 인스턴스 — 새 로직은 스캔·라우팅뿐
```

- 단일 포트도 N=1로 같은 경로(코드 한 갈래, 드리프트 없음).
- 전역 `_buffer`/`_reader`/`_feed` → `_monitors` dict로 대체. `_resolve_port(p)` 헬퍼가 별칭/포트명/미지정을 해석(미지정: 1개면 그것, 복수면 None→에러 응답).
- 표기 헬퍼 `_label(mon)` → `"SSM (COM4)"` 또는 `"COM13"`.

## 5. 도구 계약 변경 (SPEC §5 개정)

모든 조회 도구에 `port: str = ""` 추가. `port`는 별칭(`"SSM"`)·포트명(`"COM4"`) 모두 허용, 대소문자 무시.

- `list_serial_ports` — 각 포트에 `monitored: bool`, `name`(별칭) 추가. configured_port → `monitored_ports: ["SSM (COM4)", ...]`.
- `get_serial_status(port="")` — 미지정: **전 포트 상태 배열** `ports:[{name,port,connected,baud,last_error,opened_at}]` 반환(상태 조회는 전체가 자연스러움 — 에러 아님). 지정: 해당 포트 단일 상태(현행 키 유지).
- `get_recent_logs(lines=200, port="")` / `query_serial_logs(pattern, max_results=100, port="")` / `get_log_buffer_info(port="")` — 미지정: 1개면 그 포트 / 복수면 `{"status":"error","message":"포트를 지정하라","ports":[...]}`.
- `clear_log_buffer(port="")` — 미지정=**전체** 비우기(반환에 포트별 cleared 내역), 지정=해당 포트만.
- docstring 갱신: port 인자 규약 + "접힘은 요약(룩백 dedup) — 정밀 교차 순서가 필요하면 SERIAL_DEDUP을 1/0으로 낮춰 재시험" 1줄.

도구 이름 6종은 불변(안정 계약 §6.1 — 시그니처 확장만).

## 6. dedup 룩백 (SPEC §4.2 개정)

- `add()` 시 직전 1줄 대신 **버퍼 끝에서 N개(기본 5)** 안에 동일 text가 있으면 그 항목의 `count`·`last_ts` 갱신(항목 위치 유지 — first_ts 순서 보존).
- 근거: 실로그가 `[IOc] → >> It doen't… → [IOc]` 교차라 연속 접기가 무력함을 실장비에서 확인. 룩백 5면 교차 스팸 수십 분이 항목 2개로 요약.
- 트레이드오프(승인됨): 반복 줄의 정밀 교차 순서는 요약됨. 안전망 — ① `(N회 반복, t0~t1)` 시각 구간 ② `SERIAL_DEDUP=1/0` 강등 스위치 ③ tee 원본.

## 7. 웹 뷰어 변경

- 헤더에 **포트 셀렉터**(`SSM (COM4)` 표기, 포트 1개면 셀렉터 숨김). 선택 포트의 스트림/버퍼만 표시.
- `/api/stream?port=COM4` — 해당 포트 RawFeed 구독(SSE). `/api/buffer?port=`·`/api/status` 확장(status는 전 포트 배열 + 선택 포트 강조용).
- 탭 카운터·필터 등 기존 가독성 장치는 선택 포트 기준으로 동작. 포트 전환 시 스트림 화면 클리어 후 새 구독.

## 8. 테스트 전략 (TDD)

- 순수: 포트 스캔 필터(USB만 — `list_ports` mock), `SERIAL_PORT`/`SERIAL_NAMES`/`@baud` 파싱, `_resolve_port`(별칭/미지정/모호), dedup 룩백(교차 패턴·N=1 호환·0 끔).
- 도구: 다중 monitor 주입 후 port 라우팅·미지정 에러 계약·clear 전체. 기존 단일 테스트는 N=1 형태로 이식(특성화 유지).
- 뷰어: `?port=` 라우팅, status 배열 계약.
- 실장비: COM4+COM13 동시 연결 스모크(수동).

## 9. 문서 영향

- SPEC §1(전제 개정)·§3(자동 스캔)·§4.2(룩백)·§5(port 계약)·§10(셀렉터)·환경변수. README 동기화(표·예시). 부록 갱신.

## 10. 비범위 (YAGNI)

- 핫플러그(실행 중 추가/제거 감지), 전 포트 병합 조회, 포트별 EXCLUDE/INCLUDE, 쓰기(명령 전송).
- ~~로그 내용 기반 보드 자동 식별~~ → §11로 승격(2026-06-10 사용자 요청).

## 11. 추록: 로그 내용 기반 보드 자동 식별 (SERIAL_AUTONAME, 2026-06-10 추가)

비범위였던 항목을 사용자 요청으로 구현. 동기: 시리얼넘버 없는 어댑터(클론 PL2303 등)는 USB 자리가 바뀌면 COM 번호가 바뀌어 `SERIAL_NAMES`의 포트명 키가 무효가 된다 — 로그 내용은 보드를 따라다닌다.

- `SERIAL_AUTONAME="이름=정규식;…"` — **세미콜론 구분**(정규식에 쉼표 가능), 순서=우선순위. 패턴 지식은 환경변수에(서버는 범용 유지 — 특정 펌웨어 지식 미내장, SPEC §1 원칙).
- 동작: 이름 없는 모니터의 수신 줄마다(`SerialReader.on_line` 훅) 규칙 대조 → **첫 매칭에서 1회 확정** 후 검사 중단. 명시 `SERIAL_NAMES` 우선. 이미 쓰인 이름은 중복 부여 안 함(오인 방지). 잘못된 정규식 규칙은 경고 후 무시.
- 함정(문서화): 패턴은 "그 보드에서만 나오는 것"이어야 함 — SSM 로그에 "SB1"이 인용되므로 `SB` 같은 순진한 패턴은 오인. idle 보드는 단서가 나올 때까지 포트명으로 표시. tee 파일명은 시작 시점 이름 기준(식별 후엔 다음 재시작부터 별칭 반영).
- 뷰어: 셀렉터 라벨을 5초 상태 폴링에서 동기화(식별 순간 자동 갱신).
