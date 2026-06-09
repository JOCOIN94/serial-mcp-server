# CLAUDE.md — silotek-serial-mcp

AI가 임베디드 보드(ESP32/STM32 등)의 시리얼 텍스트 로그를 읽는 헤드리스 MCP 서버. 사용 주체는 사람이 아니라 AI(Claude Code, Codex 등). 전체 명세는 `SPEC.md`, 설치·사용은 `README.md`.

## 개발 워크플로 (이 레포에서 기능 구현·변경 시 따른다)

기능을 추가하거나 동작을 바꿀 때는 다음 순서를 **기본으로** 따른다. 이미 합의된 워크플로이므로, 세션이 바뀌어도 재협의 없이 적용한다(변경이 필요하면 이 파일을 갱신한다).

1. **writing-plans** (superpowers) — `SPEC.md`를 자족적인 단계별 구현 계획으로 분해한다. 이 과정에서 스펙의 빈틈·모호함을 함께 검토한다(= 스펙 리뷰 겸함). 계획서는 이후 단계의 "맥락 캡슐"이 되므로 자족적으로 쓴다.
2. **test-driven-development** (superpowers) — 각 구현 단위를 🔴 실패 테스트 → 🟢 최소 구현 → 🔵 리팩토링 순으로 만든다. 특히 **기존 코드 리팩토링은 현재 동작을 테스트로 고정한 뒤** 구조를 바꾼다(클린 아키텍처 전환의 안전망).
3. **구현 실행 — ultracode(Workflow)** — 독립 작업(테스트 작성·플러그인 문서·리팩토링 등)은 `effort ultracode`로 전환해 Workflow로 병렬 실행한다. 서브에이전트는 이 대화 맥락을 자동 상속하지 않으므로, **각 에이전트에 계획서(1단계 산출물)를 맥락으로 명시 전달**한다. (superpowers의 `subagent-driven-development`를 Workflow로 대체)
4. **requesting-code-review** (superpowers, 또는 `/code-review`) — 결과물이 `SPEC.md`를 충족하는지 검증한다.

## 빌드·검증

- ⚠️ 이 PC의 `python` 명령은 **Windows Store 별칭이라 작동하지 않는다.** `py`(Python 3.14) 또는 `uv`(0.11+)를 쓴다.
- 문법 검증: `py -m compileall -q src`
- 순수 로직 스모크: `$env:PYTHONPATH="src"; py -c "from serial_mcp.ring_buffer import LineBuffer; ..."`
- 의존성 설치: `uv sync`
- 로컬 실행: `$env:SERIAL_PORT="COM4"; uv run serial-mcp`

## 아키텍처·코드 원칙

- **클린 코드·클린 아키텍처·일관된 패턴**(SPEC §2). 스타일이 제각각 되지 않게 한다.
- 순수 로직(`ring_buffer.py`)은 시리얼 I/O·MCP 의존성과 분리해 테스트 가능하게 유지한다.
- **stdout 금지**: MCP JSON-RPC가 stdout으로 흐른다. 모든 진단·로그는 stderr 또는 tee 파일로만(`_log` 헬퍼 사용).
- 버퍼 접근은 Lock으로 보호한다(리더 스레드 ↔ 도구 호출 동시 접근).
- 읽기 전용. 쓰기(명령 전송)는 향후 확장이며 구조만 열어둔다.

## 문서–코드 일치 유지

`CLAUDE.md`·`README.md`·`SPEC.md`·`pyproject.toml`·`plugin.json` 등 문서와 실제 코드·방향이 어긋나면, 먼저 **어느 쪽이 정답인지** 판별한다:

- **(A) 코드 드리프트** — 코드가 합의된 설계(문서)에서 벗어난 경우. → **코드를 문서에 맞게** 고친다(개발 워크플로를 따라). 문서가 정답.
- **(B) 문서 갱신 필요** — 설계 미스 수정·설계 개선, 사용자의 수동 방향 조정, 새 결정·추가 사항으로 **문서가 현실을 못 따라가는** 경우. → **해당 문서를 자동으로 갱신**해 일치시킨다. 새 방향이 정답.

규칙:
- **(B)가 명확하면 묻지 말고 즉시 문서를 갱신**한다. 무엇을 왜 바꿨는지 커밋 메시지·보고에 남긴다.
- **(A)인지 (B)인지 애매하면** 추측하지 말고 사용자에게 확인한다 — 드리프트된 코드를 정답으로 굳히면 안 된다.
- 한 사실을 여러 문서가 중복 서술하면(예: 환경변수 목록이 `SPEC.md`와 `README.md`에 모두 있음) **함께 갱신**해 부분 드리프트를 막는다.
- `pyproject.toml`(의존성·엔트리포인트)·`plugin.json`(args·env)은 코드·배포의 단일 진실원이므로, 코드 변경이 이들과 어긋나면 즉시 동기화한다.

## 커밋

- 커밋 메시지는 **한국어 + Conventional Commits 접두사**(`feat:`, `fix:`, `chore:`, `test:`, `refactor:`, `docs:` 등). 접두사는 영어, 설명은 한국어. 예: `test: ring_buffer dedup 경계 케이스 추가`.

## 문서 맵

- `SPEC.md` — 전체 명세(§1~9). 구현의 기준.
- `README.md` — 설치·사용·환경변수.
- 배포측(플러그인 매니페스트 `plugin.json` + 안내 스킬 `SKILL.md`)은 별도 레포 `silotek-tools`의 `plugins/serial-mcp/`에 둔다. 이 레포에는 Python 코드만.
