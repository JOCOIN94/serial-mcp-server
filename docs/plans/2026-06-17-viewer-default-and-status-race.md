# 계획: 뷰어 기본포트 스마트 선택 + status 첫-open race 가드 + viewer_url 안내 일원화

작성 2026-06-17. 상태: 구현 진행. 근거 RCA는 이 세션의 워크플로 분석(4 에이전트, high confidence).

## 배경 — 세 가지 구조적 결함

메모리(개인 노트)로는 팀 배포 환경에서 재발을 못 막는다. 셋 다 **배포 산출물(서버 코드·docstring·SPEC·SKILL)** 에 박는다. (AGENTS.md "docstring 자족적 + 클라이언트 파리티")

1. **뷰어가 빈 화면** — `web_viewer.py`가 포트 미선택 시 무조건 `ports[0]`(첫 모니터=COM4)에 스트림을 붙인다. SSM이 COM13으로 옮겨가 COM4가 유휴(0줄)면 스트림·버퍼 둘 다 빈 화면 → "고장" 오인.
2. **status 첫-open race** — 서버는 첫 시리얼 도구 호출 때 owner/reader를 lazy 기동하는데, `reader.start()`는 스레드만 띄우고 즉시 반환(`_open`은 백그라운드). 동기화 없이 곧장 스냅샷하면 그 첫 호출 스스로가 띄운 reader가 포트를 열기 전이라 전 포트 `connected=false/opened_at=null`이 나온다(self-trigger race). → AI가 "보드 꺼짐" 오판.
3. **viewer_url 안내 문서 충돌** — `server.py` docstring과 `SPEC.md`는 "사람이 원하면 안내"(약), SKILL §0은 "진입 시 필수"(강). 스킬 없는 클라이언트(raw `mcp add`·Codex)는 약한 docstring만 보고 링크를 영영 안 줌.

## 수정

### #2 status race (server.py) — 핵심
- `SerialReader.__init__`: `self._first_open_done = threading.Event()` 추가.
- `_open()`: `finally`에서 `self._first_open_done.set()` — 첫 시도가 성공/실패 어느 쪽으로든 **결판나면** 신호(멱등). 죽은 포트는 즉시 실패→즉시 set이라 안 매달림.
- 모듈 상수 `_STATUS_FIRST_OPEN_WAIT_S = 1.5`(상한, 보통 1초 미만에 결판). 테스트는 0으로 monkeypatch.
- `_await_first_open(budget)`: 모니터들의 `_first_open_done`을 공유 예산만큼 대기(신호 오면 즉시 깸, 이미 결판난 포트는 0). `get_serial_status` 본문 첫 부분에서 호출.
- `one(m)`에 `opening` 필드: `bool(r) and ev is not None and not ev.is_set()`.
- `_status_message(d)`: 단일 포트 message 규칙 — 연결됨 / 안 됨(응답 없음 — 여는 중, opening일 때) / 안 됨: {last_error} / 안 됨. **"연결 중" 지속 상태는 노출 안 함**(사용자 합의: 죽은·먹통 포트=안 됨, 연결 중은 실제 연결되는 찰나만, 그 찰나는 대기가 흡수).

### #1 뷰어 기본포트 (web_viewer.py — 프런트 JS)
- `/api/status` 포트 객체는 이미 `connected`·`buffer_entries`를 싣는다(`_viewer_status_info`).
- `pickDefaultPort(ports)`: 저장된 선택(localStorage `sv_port`) > `buffer_entries>0` 첫 포트 > `connected` 첫 포트 > `ports[0]`.
- `refreshStatus`: `ports[0]` → `pickDefaultPort(ports)`.
- `init`: 빈약한 `/api/ports` 기반 pre-select 제거 → 바로 아래 `refreshStatus`(리치 데이터)가 선택하게 위임.
- `selectPort`: 선택 시 `localStorage.setItem('sv_port', port)`.

### #3 viewer_url 안내 일원화 (문서)
- `server.py` get_serial_status docstring: "원하면 안내"(약) → "세션 첫 호출 시 요청 없어도 viewer_url 안내"(강) + `opening` 해석 한 줄.
- `SPEC.md §5`: 같은 약한 문구 → 강한 문구 + `connected`/`opening` 명시.
- `SKILL.md 함정·해석`: 첫 status의 `connected=false`/`opening` 해석 bullet 추가(마켓플레이스 레포).

## TDD (실패 테스트 먼저 → tests/test_tools.py)
- `make_monitor`에 `_first_open_done`(기본 set) 추가, `first_open_done=` 파라미터.
- 미결판 → `opening=true`, message '응답 없음'.
- 죽은 포트(결판된 실패+last_error) → `opening=false`, message '안 됨: ...'(연결 중 아님).
- 대기 창 안에 결판나면 `connected=true` 반환(거짓 false 안 뱉음).
- 이미 결판난 포트들만이면 대기 0(즉시 반환).
- 집계 경로도 `opening` 필드 포함.
- 뷰어 계약 가드: `_viewer_status_info` 포트가 `connected`·`buffer_entries`를 싣는지.

## 검증·배포
- `py -m compileall -q src` + `uv run pytest`.
- 커밋 분리(한국어 Conventional Commits): `fix: status 첫-open race 가드` / `feat: 뷰어 기본포트 스마트 선택` / `docs: viewer_url 안내 의무 일원화`. (서버 레포)
- SKILL.md는 silotek-plugin-marketplace 레포에 별도 커밋.

## 비고
- 뷰어 프런트 JS는 pytest 하니스가 없어 `pickDefaultPort` 로직은 백엔드 계약 가드 + 브라우저 육안 검증으로 커버(정직).
- 신선 부팅에서 데이터 포트가 1~2초 늦게 흐르기 시작하면 첫 자동선택이 연결된 빈 포트로 갈 수 있음 — localStorage 기억 + 수동 전환으로 보완(과도한 재선택 churn 회피).
