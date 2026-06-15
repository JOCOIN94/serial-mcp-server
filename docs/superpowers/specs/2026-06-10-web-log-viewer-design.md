# 웹 로그 뷰어 설계 (SPEC §10 후보)

> 2026-06-10 브레인스토밍 산출물. 승인된 설계이며, writing-plans의 입력이 된다.
> 상태(2026-06-15): 포트 폴백과 per-port release 설명은
> `2026-06-15-single-owner-port-lock.md`의 whole-session 8743 bind 잠금 모델로 대체됐다.

## 1. 목적·배경

serial-mcp가 시리얼 포트를 점유하면 테라텀 등 다른 프로그램이 같은 포트를 열 수 없어, **사람이 로그를 눈으로 볼 방법이 사라진다**(README의 확장 아이디어). 이를 해결하기 위해 서버에 localhost 전용 웹 뷰어를 내장한다.

- 본 서버의 주 사용자는 여전히 AI다(SPEC §1). 웹 뷰어는 **사람을 위한 보조 기능**이며, 실패해도 MCP 서버 동작에 영향을 주지 않는다.
- 서버는 계속 헤드리스다. GUI 라이브러리를 추가하지 않으며, 화면 역할은 사용자의 브라우저가 한다(SPEC §2의 "헤드리스" 제약은 "GUI 라이브러리 금지"로 유지·명문화).
- **새 의존성 0**: 파이썬 표준 라이브러리(`http.server`)만 사용한다. mcp의 전이 의존성(starlette 등)에 기대지 않는다(선언 없는 의존은 mcp 버전업에 인질이 됨).

## 2. 확정 요구사항 (브레인스토밍 결정)

| 항목 | 결정 |
|---|---|
| 진입 | 도구 응답에 localhost 링크 자동 포함 → 클릭 → 브라우저 |
| 뷰 | 탭 2개 — ① 실시간 스트림(수신 원본, 테라텀 대체) ② 링버퍼(접힘·필터 적용된 가공 뷰) |
| 컬러 | 레벨 키워드 + ANSI 해석 + 메타 dim + JSON 절제 하이라이트. "색은 신호" 원칙으로 어지러움 방지 |
| 활성화 | 기본 켜짐. 첫 시리얼 도구 호출 때 고정 포트 8743을 whole-session 소유권 잠금으로 bind. 점유 시 임시 포트 폴백 없이 휴면/안내. `SERIAL_WEB=0`은 UI만 끄고 8743 잠금은 유지 |
| 접근 | `127.0.0.1` 바인딩만(외부 접속 불가). 인증 없음(localhost 한정으로 충분) |
| 구현 | A안 — stdlib `http.server` 내장 데몬 스레드. SSE 수동 구현 |

## 3. 아키텍처

```
시리얼 포트 → 리더 스레드 → _ingest ─┬─ ① LineBuffer (필터·dedup → AI 도구)
                                      ├─ ② tee 파일 (원본 영구 기록)
                                      └─ ③ RawFeed 허브 (원본 생중계, 신규)
                                              └→ SSE → 브라우저 탭들
```

- 기존 경로 ①②와 MCP 도구 6종은 무변경. ③만 추가.
- 스트림의 "수신 원본" = `_ingest`의 decode(utf-8/replace)·EOL 제거 후 텍스트. **tee와 동일한 충실도**(빈 줄·반복 줄·필터 제외 줄 포함).
- 핵심 불변식: **브라우저가 느리거나 끊겨도 시리얼 수신 경로는 절대 막히지 않는다**(발행 논블로킹, 구독자 큐 overflow 시 drop-oldest).

## 4. 컴포넌트

### 4.1 `viewer_feed.py` (신규, 순수 로직 — 단위테스트 대상)

RawFeed 허브. 시리얼 I/O·HTTP 의존성 없음(`ring_buffer.py`와 같은 계층).

- `publish(ts: datetime, text: str) -> None` — 리더 스레드가 호출. 논블로킹. 구독자 없으면 no-op.
- `subscribe() -> Subscription` / `unsubscribe(sub)` — 구독자마다 독립 bounded deque(기본 maxlen=1000). 가득 차면 가장 오래된 항목부터 버림.
- `Subscription.get(timeout) -> (ts, text) | None` — SSE 핸들러가 호출(Condition 대기).
- 스레드 안전(Lock/Condition). 동시 구독자 여러 명(브라우저 탭 여러 개) 지원.

### 4.2 `ring_buffer.py` 확장 (기존 파일)

- `snapshot() -> list[dict]` 추가 — 버퍼 탭용 구조화 뷰. 항목: `{"text": str, "first_ts": "HH:MM:SS.mmm", "last_ts": "HH:MM:SS.mmm", "count": int}`. 스레드 안전(기존 Lock). 기존 `render()` 경로는 무변경(AI 도구용).

### 4.3 `web_viewer.py` (신규, I/O 계층)

`ViewerServer` 클래스 — stdlib `ThreadingHTTPServer`를 데몬 스레드로 구동.

- 바인딩: `127.0.0.1` 고정. 포트: 설정값(기본 8743) 단일 시도. `OSError`(점유) 시 이 프로세스는 비소유 휴면 상태를 유지하고 시리얼 포트에 접근하지 않는다.
- `url` 속성: `http://127.0.0.1:{실제포트}` 또는 `None`(비활성/실패).
- 라우트(조회는 GET 읽기 전용. 2026-06-15 층2 소유권 백엔드에서 `GET /api/release`가 명시적 상태 변경 예외로 추가됨):
  - `GET /` — 단일 HTML 페이지(CSS/JS 인라인 문자열, 외부 CDN 없음 → 오프라인 동작, 패키징 추가 설정 불필요)
  - `GET /api/stream` — SSE. RawFeed 구독, 이벤트 `data: {"ts":"HH:MM:SS.mmm","text":"..."}` 1줄당 1이벤트. 15초마다 하트비트 코멘트(`: ping`). 클라이언트 끊김 감지 시 구독 해지.
  - `GET /api/buffer` — `{"status":"ok","entries":[snapshot()...],"capacity":N,"total_received":N,"total_stored":N,"dedup":bool}`
  - `GET /api/status` — `{"session":str|null,"ports":[{"port":str,"label":str,"connected":bool,"baud":int,"last_error":str|null,"hw":str|null,"board":str|null,...}]}` (헤더·소유권 보드 표시용)
  - `GET /api/release` — owner 세션의 전체 COM 핸들, 뷰어, 8743 잠금을 반납한다. `port=` 인자가 있어도 하위호환 입력으로만 받고 전체 반납으로 처리한다.
- `log_message` 오버라이드 → `_log`(stderr)로 우회. **stdout 금지 유지**.

### 4.4 `server.py` 변경 (기존 파일)

- `_ingest()`: `feed.publish(ts, text)` 한 줄 추가(버퍼 add·tee와 같은 위치, 예외 격리).
- `_load_config()`: `SERIAL_WEB` 파싱 추가 — 기본 `8743`(켜짐). `0`/`false`/`no`/`off` → 8743 잠금 유지, UI 미서빙. 정수 → 해당 포트. 그 외 → 기본값 + 경고 로그.
- `main()`: ViewerServer/리더를 기동하지 않고 휴면으로 stdio 대기. 첫 시리얼 도구 호출이 8743 bind에 성공하면 뷰어와 리더를 시작한다.
- 도구 반환 확장(§5 계약 변경): `get_serial_status`·`get_log_buffer_info` 응답에 `viewer_url: str|null` 추가. docstring에 "사람이 로그를 직접 보고 싶어 하면 이 링크를 안내하라" 한 줄 추가.

## 5. UI·컬러 명세

**레이아웃** (다크 터미널 스타일, 모노스페이스 단일 페이지):
- 헤더: `COM4 @ 115200` · 연결상태 점(녹/적, /api/status 5초 폴링) · 탭 [스트림 | 버퍼] · ⏸ 일시정지 · 자동스크롤 토글 · 화면 지우기(클라이언트 DOM만 — 서버 버퍼 무변경)
- 본문: 로그 영역. 스트림 탭은 클라이언트 최대 5,000줄 유지(초과 시 위에서 제거). 버퍼 탭은 활성 상태에서 2초마다 `/api/buffer` 자동 갱신, `(N회 반복, t0~t1)` 표기 렌더링.
- 검색 UI 없음 — 브라우저 Ctrl+F로 충분(YAGNI).

**컬러 — "색은 장식이 아니라 신호"** (우선순위 순, 위가 적용되면 아래 생략):
1. **ANSI 이스케이프 해석** — 펌웨어가 보낸 색 코드(SGR: 색상·굵기·리셋)를 그대로 렌더링. 미지원 코드는 제거(원문 텍스트만 표시). ANSI가 있는 줄은 2번 휴리스틱 생략.
2. **레벨 라인 틴트** — `ERROR|FAIL|Exception|rst:` 포함 줄 적색, `WARN` 포함 줄 호박색(대소문자 무시).
3. **성공 키워드** — `OK|Success|Done` 단어만 녹색(줄 전체 아님).
4. **JSON 절제 하이라이트** — `{...}` 감지 시 키만 차분한 시안, 괄호·구두점 dim, 값은 본문색 유지.
5. **메타 dim** — 타임스탬프·반복 표기는 저대비 회색.

평상시 화면은 회색 2~3톤. 채도 있는 색이 보이면 "주목할 일이 있다"는 신호가 되도록.

## 6. 에러·엣지 처리

- 뷰어 기동 실패 → MCP 서버는 정상 동작, `viewer_url: null`, stderr 로그. 뷰어는 어떤 경우에도 서버 생존에 영향 없음.
- 다중 인스턴스(세션 2개가 각자 서버 스폰) → 8743 bind에 성공한 1개만 owner. 패자는 임시 포트 폴백 없이 휴면하며 "다른 세션에서 해제 먼저" 안내를 반환한다.
- SSE 클라이언트 끊김 → write 예외 잡아 구독 해지(좀비 구독자 방지).
- 하트비트로 프록시/브라우저 타임아웃 방지.
- 빈 줄도 스트림에는 그대로(테라텀 충실도). 버퍼 탭에는 §4.3 규칙대로 없음.

## 7. 테스트 전략 (TDD, 기존 58개 스위트에 추가)

- `tests/test_viewer_feed.py` — 발행/구독/다중 구독/overflow drop-oldest/해지 후 미수신 (순수)
- `tests/test_ring_buffer.py` — `snapshot()` 구조·접힘 반영·빈 버퍼 (순수)
- `tests/test_web_viewer.py` — 임시 포트로 실제 기동: `/`(200·HTML), `/api/buffer`·`/api/status`(JSON 계약), SSE 첫 이벤트 수신, 포트 점유 시 폴백 없음, `url` 속성
- `tests/test_serial_reader.py` — `_ingest` → feed 발행 연결
- `tests/test_config.py` — `SERIAL_WEB` 파싱(기본 8743/끔/정수/이상값)
- `tests/test_tools.py` — `viewer_url` 필드 계약
- HTML/JS 내부 로직은 단위테스트 제외 — 실장비 스모크(브라우저 육안)로 검증

## 8. 문서 영향 (구현 시 동시 갱신)

- `SPEC.md` §2: "헤드리스 = GUI 라이브러리 금지, localhost 브라우저 뷰는 허용" 명문화. §5: `viewer_url` 반영. 새 §10: 본 설계 요약. 환경변수 `SERIAL_WEB`.
- `README.md`: 환경변수 표 `SERIAL_WEB` 행 추가, 사용법 1절(링크 클릭 → 탭 2개), 6번째 줄의 "확장 아이디어단계" 문구를 실기능 설명으로 교체.
- (차후 패키징 시) plugin.json `env`에 `SERIAL_WEB` 노출, SKILL.md에 "사람이 보고 싶어 하면 링크 안내" 항목.

## 9. 비범위 (YAGNI)

- 쓰기/명령 전송 UI(시리얼 TX). 웹 뷰어의 상태 변경은 소유권 `release`만 예외로 허용한다.
- 인증·외부 접속(127.0.0.1 한정)
- 서버측 검색·필터 UI(브라우저 Ctrl+F + AI의 query_serial_logs로 충분)
- 여러 포트를 한 화면에 나란히 펼치는 동시 뷰. 포트 전환·소유권 보드·다중 포트 상태 표시는 2026-06-15 층1/층2에서 범위를 초과해 구현됐다.
- 로그 다운로드 버튼(tee 파일이 그 역할)
