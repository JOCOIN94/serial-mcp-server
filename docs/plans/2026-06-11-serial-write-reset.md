# 구현 계획서: 시리얼 쓰기(명령 전송) + 보드 리셋 (Codex 인계용)

> **이 문서는 자족적(self-contained) 맥락 캡슐이다.** 구현자는 대화 맥락 없이 이 문서와 레포만으로 작업을 완수할 수 있어야 한다. 설계 결정은 모두 확정됐다 — 구현 중 설계를 재협의하지 말고, §10 실패 기준에 해당하면 중단하고 사용자에게 보고하라.

## 1. 목표

읽기 전용인 silotek-serial-mcp에 **쓰기 도구 2종**을 추가한다:

1. `send_serial_command` — 보드에 텍스트 명령을 전송하고, 전송 직후 수신된 응답 로그까지 한 번에 회수한다.
2. `reset_board` — DTR/RTS 펄스로 보드를 하드웨어 리셋하고, 부팅 로그를 회수한다. 블랙박스 루프의 "사람이 보드를 리셋해 주세요" 단계를 AI가 스스로 수행할 수 있게 된다.

두 도구 모두 **매 호출 사용자 승인**(MCP elicitation — 서버측 강제, 클라이언트 허용목록으로 우회 불가)을 거친다.

## 2. 배경 (맥락 캡슐)

- 이 레포는 임베디드 보드(ESP32-S3 등)의 시리얼 텍스트 로그를 AI(Claude Code 등)가 읽는 헤드리스 MCP 서버다(FastMCP + pyserial, stdio transport). 전체 명세는 `SPEC.md`, 공통 지침은 `AGENTS.md`.
- 현재 SPEC §2는 "읽기 전용으로 구현한다. … 향후 해당 기능을 용이하게 추가할 수 있도록 구조를 확장 가능하게 설계한다"고 명시 — 이번 작업이 그 "향후 확장"이다. 구현 완료 시 SPEC을 포함한 문서들을 코드와 **같은 작업 단위로** 개정한다(§8 문서 델타 — AGENTS.md "문서–코드 일치 유지" 규칙의 (B) 유형).
- **사용자 확정 결정(변경 불가):**
  1. 쓰기 **기본 켜짐 + 매 호출 사용자 승인**. env로 꺼야만 쓸 수 있는 옵트인 방식이 아니다. 승인은 서버측 elicitation이 1차 메커니즘.
  2. 범위는 명령 전송 + 보드 리셋 2종. (raw 바이트 모드, 서버측 승인 타임아웃 등은 이번 범위 밖.)
- 테스트 장비: SSM = ESP32-S3 게이트웨이, COM4, CH343 USB-UART(DTR/RTS 자동리셋 배선 있음 — esptool 플래싱이 되는 보드), 115200.

## 3. 작업 원칙·제약 (AGENTS.md 준수)

- **TDD**: 단계마다 실패 테스트 먼저 작성 → 구현 → `uv run pytest` 녹색. 기존 테스트(현재 58개+)는 §7에 명시된 의도적 갱신 2건 외에 수정 금지.
- **stdout 금지**: MCP JSON-RPC가 stdout으로 흐른다. 진단·로그는 `_log` 헬퍼(stderr)만.
- 주석·도구 메시지·docstring은 **한국어**. 기존 코드의 docstring 스타일(`[언제 호출]`/`[port 규약]`/`[무엇을 반환]` 구획)과 반환 dict 규약(`status`/`message` + 도구별 필드)을 그대로 따른다.
- 의존성은 `mcp[cli]` + `pyserial` 유지. `pydantic`은 mcp의 하드 의존성이므로 직접 임포트해도 새 top-level 의존성이 아니다(주석으로 근거를 남길 것).
- 이 PC의 `python`은 Windows Store 별칭이라 동작 안 함 — `py` 또는 `uv`만 사용.
- 커밋: 한국어 + Conventional Commits 접두사. **main push = 곧 배포**(uvx가 git main에서 직접 실행)이므로, push는 전체 녹색 + 실장비 검증(§12) 후에만.

## 4. 검증된 전제 (이미 소스에서 직접 확인된 사실)

라인 번호는 커밋 `b52eb6c` 기준 참고 좌표다 — 구현 전 실제 파일을 읽고 시작하라.

**MCP SDK (설치본 1.27.2):**
- `Context.elicit(message, schema)` 실재 — `.venv/Lib/site-packages/mcp/server/fastmcp/server.py:1194`. 반환은 `action` 필드를 가진 결과 객체(`accept`/`decline`/`cancel`), `mcp/server/elicitation.py`의 `AcceptedElicitation`/`DeclinedElicitation`/`CancelledElicitation`.
- elicitation 스키마는 **flat primitive 필드만** 허용(`_validate_elicitation_schema`가 위반 시 TypeError). **필드 0개인 빈 pydantic 모델도 허용**되며 클라이언트는 수락/거절만 띄운다.
- `session.elicit_form`은 클라이언트 capability를 사전 검사하지 않고 요청을 보낸다 — 미지원 클라이언트면 `McpError` 발생(`mcp/shared/exceptions.py`). 사전 검사는 `ctx.session.check_client_capability(ClientCapabilities(elicitation=ElicitationCapability()))` (`mcp/server/session.py:120`, elicitation 분기 :141). 타입은 `mcp.types.ClientCapabilities`(:417, `elicitation` 필드 :427) + `ElicitationCapability`(:319).
- async 도구 지원: `Context`(또는 `Optional[Context]`) 어노테이션 파라미터는 자동 주입되고 입력 스키마에서 제외된다(`fastmcp/tools/base.py:69-74`, `utilities/context_injection.py:34-43`).
- `@mcp.tool()` 데코레이터는 **원본 함수를 그대로 반환**(`fastmcp/server.py:504`) → 테스트에서 도구를 직접 호출 가능. async 도구는 `asyncio.run(...)`으로 호출하면 되므로 **pytest-asyncio/anyio 플러그인 추가 불필요**(현재 dev 의존성은 pytest뿐).

**현재 코드:**
- `src/serial_mcp/server.py`: `SerialReader`가 `self._ser`(serial.Serial)를 단독 소유. `_open()`(107-124)이 `serial.Serial(port, baud, timeout=1)` 생성 — write_timeout 없음, **`_ser` 접근 락 없음**(리더 스레드 전용이었으므로). `_run()`(126-148) 재연결 루프: 읽기 오류 시 `_ser.close()` 후 `self._ser = None`(138-143). `stop()`(94-105)도 close. `_ingest(raw, ts)`(150-171): decode(utf-8/replace) → `buffer.add`(include/exclude 필터 적용) → `feed.publish`(필터 무관) → `on_line` 훅 → tee 기록(필터 무관, `[YYYY-MM-DD HH:MM:SS.mmm]` 스탬프).
- 전역(174-185): `mcp = FastMCP("serial-mcp")`, `_monitors`, `_config: dict = {}`(181 — **기본 빈 dict**이므로 도구는 `.get(key, 기본값)`으로 읽어야 안전), `PortMonitor` dataclass(188-200, `label` 프로퍼티).
- 도구 6개(260-466): `@mcp.tool()` 동기 함수. `_resolve_port(port)`(228-257): 별칭/포트명/라벨(`SSM (COM4)`) 해석, 미지정 시 1개면 자동·복수면 에러 dict + `ports` 목록. 에러 시 호출부 패턴: `{**err, "count": 0, "lines": []}`.
- 설정(469-617): `_load_config(env)` 순수 함수 + `_parse_dedup`/`_parse_web`/`_parse_hotplug` 등 — `0/false/no/off` 끔, 해석 실패 시 `_log` 후 기본값 규약.
- `src/serial_mcp/ring_buffer.py`: 순수 로직 모듈(시리얼/MCP 의존 금지 원칙). `LogEntry(text, first_ts, last_ts, count)` — dedup 접힘 시 `last_ts` 갱신됨(95-96) → **타임스탬프 기반 응답 회수가 접힌 항목도 잡는다**. `LineBuffer`는 자체 `_lock` 보유. `snapshot()`은 시각을 문자열로 반환하므로 datetime 비교 불가 → 새 메서드 필요(§5.1).
- `src/serial_mcp/viewer_feed.py`: `RawFeed.publish(ts, text)` — 논블로킹, drop-oldest.
- `pyproject.toml`: version `0.1.0`, `mcp[cli]>=1.2.0`, dev=`pytest>=8.1.1`.
- 배포측(별도 레포): `C:\Users\User\projects\silotek-tools\plugins\serial-mcp\.claude-plugin\plugin.json`(env 패스스루 11종, version 0.1.1), `...\skills\serial-debugging\SKILL.md`(8행 "서버는 읽기 전용", 12-15행 표준 루프).

## 5. 구현 설계

### 5.1 `LineBuffer.entries_since()` — 응답 자동 회수의 기반

파일: `src/serial_mcp/ring_buffer.py` (`clear()` 뒤에 추가, 순수 로직 유지 — datetime 표준 모듈만)

```python
def entries_since(self, ts: datetime, max_lines: int = 200) -> list[str]:
    """ts 이후(last_ts >= ts) 활동한 항목을 render해 반환(시간 오름차순, 끝에서 max_lines개).

    쓰기 도구의 '응답 자동 회수'용 — 전송 직전 시각을 기록해 두고 이 메서드로
    그 이후 수신분만 가져온다. dedup으로 기존 항목에 접힌 응답도 last_ts가
    갱신되므로 잡힌다(접힌 항목은 '(N회 반복…)' 표기로 반환됨).
    """
```

구현 메모: `with self._lock:`으로 deque 스냅샷 → `e.last_ts >= ts` 필터. 접힌 항목은 버퍼 앞쪽에 있을 수 있으므로 **조기 break 금지, 전수 스캔**(maxlen 2000이라 저렴) → 끝 `max_lines`개 → `render()`. 경계는 `>=`(포함).

### 5.2 `SerialReader` 스레드 안전 쓰기 경로

파일: `src/serial_mcp/server.py`

**락 도입 + 기존 경로 보정 (동작 보존 리팩토링 — 기존 테스트 전부 녹색 유지가 검증 기준):**
- `__init__`: `self._ser_lock = threading.Lock()`(핸들 교체 ↔ write/DTR·RTS 경합 차단), `self._tee_lock = threading.Lock()`(리더 스레드 ↔ 도구 스레드의 tee 동시 기록 보호) 추가.
- `_open()`: `serial.Serial(self.port, self.baud, timeout=1, write_timeout=2)`로 변경(쓰기 무한 블록 방지). Serial 생성은 지역 변수로 하고 `with self._ser_lock: self._ser = ser`로 대입.
- `_run()`: **readline 블로킹 중 락을 잡지 않는다** — 루프에서 `ser = self._ser` 지역 참조를 뜨고 `raw = ser.readline()`. 에러 경로의 `close() + self._ser = None`만 `with self._ser_lock:`으로 감싼다. (다른 스레드가 핸들을 close하면 readline이 예외를 던지고 기존 복구 경로로 재연결된다 — 기존 흐름 그대로.)
- `stop()`: `_ser.close()`를 락 하에 수행.
- `_ingest()`의 tee 기록 구간을 `with self._tee_lock:`으로 감싼다(형식·동작 불변).

**새 메서드 3개:**

```python
def write(self, data: bytes, audit: Optional[str] = None) -> int:
    """페이로드를 포트에 기록(스레드 안전). 성공 시 audit 텍스트로 TX 감사 기록.

    - 미연결(_ser is None or not connected)이면 serial.SerialException.
    - 쓰기 오류 시: connected=False·last_error 기록·핸들 close 후 _ser=None으로
      리더의 재연결 루프를 유도하고, SerialException을 다시 던진다(도구 레이어가
      status dict로 변환). flush()는 호출하지 않는다(블로킹 위험 — write_timeout=2가
      OS 버퍼 기록까지의 상한, 115200에서 짧은 명령은 충분).
    - 락을 쥔 채 write하므로 도중 핸들 교체 불가. 반환값은 기록 바이트 수.
    """

def pulse_reset(self, pulse_s: float = 0.1) -> None:
    """DTR/RTS 펄스 하드웨어 리셋(esptool 클래식 시퀀스).

    락 하에: ser.dtr = False → ser.rts = True → pulse_s 대기 → ser.rts = False.
    (CH343 등 자동리셋 회로: RTS→EN, DTR→IO0. dtr=False로 IO0를 높게 유지한 채
    EN을 펄스해 '일반 부팅' 리셋 — 다운로드 모드 진입 아님.)
    미연결·오류 규약은 write()와 동일. native-USB/미배선 보드는 예외 없이
    no-op일 수 있다 — 호출부가 '회수 0줄'로 판정한다.
    성공 시 reader 자신이 _audit_tx("[RST] DTR/RTS 하드웨어 리셋 펄스", now) 호출.
    """

def _audit_tx(self, text: str, ts: datetime) -> None:
    """송신 감사 기록 — _ingest와 같은 3경로에 마커 포함 text를 남긴다.

    buffer.add(include/exclude 필터 적용 — 걸러져도 무방: 응답 회수는 타임스탬프
    기반이라 무관) → feed.publish(웹 뷰어 스트림 표시) → tee(_tee_lock 하,
    리더와 동일한 [날짜시각] 스탬프 형식). _ser_lock 해제 후 호출한다
    (LineBuffer는 자체 Lock 보유 — 락 중첩 불필요).
    """
```

마커 규약: `write()`는 도구가 넘긴 `audit=f"[TX] {command}"`, 리셋은 `[RST] ...`. 감사 기록을 도구가 아니라 SerialReader에 두는 이유: tee 핸들·feed·buffer의 단독 소유자가 SerialReader이고, 도구는 dict 변환만 담당하는 기존 책임 분리 유지. pyserial Win32 구현은 read/write가 별도 overlapped 이벤트라 리더의 readline 블로킹과 타 스레드 write 동시 수행이 안전 — 락은 오직 **핸들 교체와의 경합** 차단용이다.

### 5.3 설정 — `SERIAL_WRITE` / `SERIAL_WRITE_CONFIRM`

파일: `server.py` — `_parse_hotplug` 뒤에 공용 헬퍼:

```python
def _parse_flag(env: Mapping[str, str], name: str, default: bool = True) -> bool:
    """불리언 환경변수 파싱 — 미설정/빈값→기본, 0/false/no/off→False,
    1/true/yes/on→True, 해석 실패→_log 후 기본(기존 _parse_* 규약 일치)."""
```

`_load_config` 반환 dict에 2키 추가:
- `"write": _parse_flag(env, "SERIAL_WRITE")` — **기본 True(켜짐)**. False면 두 도구가 즉시 에러 dict 반환(보수적 사용자·쓰기 불가 환경용 전면 차단).
- `"write_confirm": _parse_flag(env, "SERIAL_WRITE_CONFIRM")` — **기본 True(매 호출 승인)**. False면 elicitation 생략 → 클라이언트 권한 게이트(도구 호출 허용 프롬프트)에 위임.

도구는 `_config.get("write", True)` / `_config.get("write_confirm", True)`로 읽는다(전역 `_config` 기본이 `{}`이므로 `.get` 기본값 필수). 도구 등록은 import 시점 데코레이터이므로 **off여도 도구는 등록되되 에러를 반환**(조건부 등록은 구조 변경이 커서 배제 — 확정).

### 5.4 승인 게이트

파일: `server.py`. 임포트 추가: `import asyncio`, `import time`, `from pydantic import BaseModel`(mcp 하드 의존 — 주석 필수), `from mcp.server.fastmcp import FastMCP, Context`(기존 행 확장), `from mcp.shared.exceptions import McpError`, `from mcp.types import ClientCapabilities, ElicitationCapability`.

```python
class _WriteApproval(BaseModel):
    """쓰기 승인 폼 — 빈 스키마(필드 없음): 클라이언트는 수락/거절만 띄운다.
    elicitation flat-primitive 제약(SDK가 강제)을 자명하게 충족한다."""

async def _confirm_write(ctx: Optional[Context], summary: str) -> Optional[dict]:
    """매 호출 사용자 승인 게이트. 통과면 None, 차단이면 도구가 그대로 반환할 dict."""
```

`_confirm_write` 분기(순서 고정):
1. `_config.get("write_confirm", True)`가 False → 즉시 None(통과 — 클라이언트 게이트 위임).
2. `ctx is None` 또는 `ctx.session.check_client_capability(ClientCapabilities(elicitation=ElicitationCapability()))`가 False → `{"status": "error", "message": "클라이언트가 elicitation(승인 팝업) 미지원 — 승인을 클라이언트 권한 게이트에 위임하려면 SERIAL_WRITE_CONFIRM=off 설정"}` (**전송하지 않음 — fail-safe 방향**).
3. `result = await ctx.elicit(message=summary, schema=_WriteApproval)` → `result.action == "accept"`면 None(통과). decline/cancel → `{"status": "declined", "message": "사용자가 전송을 거부했다 — 같은 명령을 재시도하지 말고, 사람에게 이유를 묻고 다음 행동을 합의하라."}`.
4. elicit 중 `McpError` → 2)와 동일 에러 dict(capability를 광고만 하고 실패하는 클라이언트 방어).

### 5.5 MCP 도구 2개

파일: `server.py` — `clear_log_buffer` 뒤에 배치. 둘 다 async + `ctx: Optional[Context] = None`(FastMCP가 자동 주입, 입력 스키마에서 제외됨).

```python
@mcp.tool()
async def send_serial_command(command: str, port: str = "", eol: str = "\n",
                              wait_ms: int = 500, ctx: Optional[Context] = None) -> dict:

@mcp.tool()
async def reset_board(port: str = "", wait_ms: int = 2000,
                      ctx: Optional[Context] = None) -> dict:
```

공통 흐름(두 도구 동일 골격):
1. `_config.get("write", True)` False → `{"status": "error", "message": "쓰기 비활성(SERIAL_WRITE=off) — 활성화하려면 SERIAL_WRITE 환경변수를 지우거나 1로", "count": 0, "lines": []}`.
2. `_resolve_port(port)` → 에러면 `{**err, "count": 0, "lines": []}`.
3. 입력 검증(send만): `eol`은 `{"\n", "\r\n", "\r", ""}`만 허용(밖이면 허용 목록을 동봉한 에러 dict). `command == "" and eol == ""`이면 에러(빈 페이로드). `wait_ms = max(0, min(wait_ms, 30000))` 클램프. `payload = (command + eol).encode("utf-8")`(멀티바이트 안전).
4. `block = await _confirm_write(ctx, summary)` → block이면 `{**block, "count": 0, "lines": []}` 반환.
   - send summary: `f"{mon.label} 포트로 시리얼 명령 전송 승인 요청\n명령: {command!r} (eol={eol!r}, {len(payload)}바이트)"`
   - reset summary: `f"{mon.label} 보드를 DTR/RTS 펄스로 하드웨어 리셋합니다. 승인하시겠습니까?"`
5. `t0 = datetime.now()`(**쓰기 직전 기록**) → `try: mon.reader.write(payload, audit=f"[TX] {command}")`(reset은 `mon.reader.pulse_reset()`) `except serial.SerialException as e:` → `{"status": "error", "message": f"{mon.label}: 전송 실패 — {e}", "count": 0, "lines": []}`(미연결·쓰기 실패 공통 경로 — reader가 이미 재연결 유도까지 마친 상태).
6. `await asyncio.sleep(wait_ms / 1000)`(이벤트 루프 비점유 — stdio 단일 클라이언트 + asyncio 백엔드라 asyncio.sleep로 충분).
7. `lines = mon.buffer.entries_since(t0)` → 반환:
   - send: `{"status": "ok", "message": f"{mon.label}: {len(payload)}바이트 전송, {wait_ms}ms 대기 후 {len(lines)}줄 회수", "port", "name", "sent": command, "eol": eol, "bytes": len(payload), "wait_ms", "count": len(lines), "lines"}`
   - reset: `{"status": "ok", "message": ..., "port", "name", "wait_ms", "count", "lines"}` — `lines`가 0줄이면 message에 **"부팅 로그 없음 — native-USB 보드이거나 자동리셋 미배선일 수 있다. 사람에게 물리 리셋을 요청하라"** 덧붙임.

docstring 요구(자족적, 기존 스타일):
- send: `[언제 호출]` 보드 CLI/AT 명령 전송 + 응답 회수. 매 호출 승인 팝업이 뜨며 거부 시 `status="declined"` — **재시도 금지**. `[port 규약]` 기존 도구와 동일. `[무엇을 반환]` sent/bytes/wait_ms/lines — 전송한 명령 자체도 `[TX]` 마커로 lines에 포함될 수 있음. `[루프 단계]` 능동 시험(쓰기).
- reset: `[언제 호출]` 블랙박스 루프 시작 시 사람 대신 직접 리셋. 자동리셋 회로 보드만 동작, native-USB는 no-op(0줄이면 사람에게 물리 리셋 요청). 부트 배너 회수를 위해 기본 대기 2000ms.

**`status` 값 계약 확장**: 기존 `ok|error`에 `declined` 추가(거부는 시스템 오류가 아니며, AI가 재시도하지 않고 사람과 협의해야 함을 구분) — SPEC §5와 docstring에 명시.

### 5.6 pyproject

- `version = "0.2.0"`.
- `mcp[cli]>=1.2.0` → **`mcp[cli]>=1.10`**(elicitation API 보유 플로어). 상향 직후 `uv sync`로 락 갱신 + `.venv/Lib/site-packages/mcp/server/fastmcp/server.py`에 `def elicit`이 있는지 1회 확인(설치본 1.27.2에서 확인됨 — 플로어 상향은 신규 설치 보호용).

## 6. 실행 순서 (TDD — 각 단계: 실패 테스트 → 구현 → `uv run pytest` 녹색 → 커밋)

| 단계 | 내용 | 커밋 예 |
|---|---|---|
| 1 | §5.1 `entries_since` (테스트 4개 → 구현) | `feat: LineBuffer.entries_since — 시각 기반 응답 회수` |
| 2 | §5.2 SerialReader 락+write/pulse_reset/_audit_tx (테스트 7~8개 → 구현, **기존 58개+ 회귀 없음 필수**) | `feat: SerialReader 스레드 안전 쓰기 경로` |
| 3 | §5.3 설정 (기존 계약 테스트 2건 갱신 + parametrize 신규 → `_parse_flag`) | `feat: SERIAL_WRITE·SERIAL_WRITE_CONFIRM 설정` |
| 4 | §5.4+§5.5 승인 게이트 + 도구 2개 (test_write_tools.py 15개 → 구현) | `feat: send_serial_command·reset_board 도구 — elicitation 승인 게이트` |
| 5 | §8 문서 델타(서버 레포) + §5.6 버전 | `docs: 쓰기·리셋 반영 — SPEC·README·AGENTS 개정, 0.2.0` |
| 6 | §8 silotek-tools 레포 동기 변경 | (해당 레포에서) `feat: serial-mcp 0.2.0 — 쓰기·리셋 env/스킬 동기화` |
| 7 | 최종 검증 §12 → 통과 후 push(서버 레포 먼저, silotek-tools 다음) | — |

## 7. 테스트 목록 (전문)

**tests/test_ring_buffer.py에 추가 (4):**
1. `test_entries_since_returns_only_after_ts` — t0 이전/이후 항목 분리.
2. `test_entries_since_boundary_inclusive` — `last_ts == ts` 포함(`>=`).
3. `test_entries_since_catches_folded_entry` — t0 이전 생성 항목에 t0 이후 같은 줄이 접힘(add 2회) → 회수됨.
4. `test_entries_since_empty_and_max_lines` — 빈 결과 `[]`, max_lines 절단.

**tests/test_serial_reader.py에 추가 (7~8, 기존 StringIO tee 주입 패턴 답습 + dtr/rts를 property setter로 기록하는 FakeSerial):**
5. `test_write_sends_payload_and_audits_tx` — FakeSerial에 페이로드 기록 + buffer/feed/tee에 `[TX]` 마커.
6. `test_write_raises_when_disconnected` — `_ser=None` → SerialException.
7. `test_write_failure_marks_disconnected_for_reconnect` — FakeSerial.write가 SerialException → `connected=False`, `_ser is None`, `last_error` 설정, 예외 전파.
8. `test_write_audit_skipped_in_buffer_by_filter_but_teed` — `exclude=r"\[TX\]"`여도 tee/feed엔 남음.
9. `test_pulse_reset_sequence_order` — setter 기록이 `[("dtr", False), ("rts", True), ("rts", False)]` 순서(`pulse_s=0`), 성공 후 `[RST]` 감사 기록.
10. `test_pulse_reset_raises_when_disconnected`.
11. `test_open_sets_write_timeout` — `monkeypatch.setattr(srv.serial, "Serial", 기록자)` 후 `_open()` → `write_timeout=2` 전달 확인.
12. (선택 — 결정적으로 작성 가능할 때만) `test_write_blocks_while_handle_swap` — 테스트 스레드가 `_ser_lock`을 쥔 동안 write 대기를 Event로 확인.

**tests/test_config.py (의도적 계약 갱신 2건 + 신규):**
13. 기존 `test_load_config_defaults_when_empty`·`test_load_config_reads_all_vars`(완전 일치 비교)에 `write`/`write_confirm` 키 추가 — **이 2건만 기존 테스트 수정 허용**.
14. `test_load_config_write_flags` parametrize — `{}`→True/True, `off/0/false/no`→False, `on/1/true/yes`→True, `"abc"`→기본 True.

**신규 tests/test_write_tools.py (15 — test_tools.py의 SimpleNamespace fixture 패턴 답습. reader 목은 write/pulse_reset 호출 기록형, ctx 목은 덕타이핑: `session.check_client_capability(...)->bool` + `async def elicit(...)->SimpleNamespace(action=...)`. 실행은 `asyncio.run(...)`. `_config`는 monkeypatch로 주입·원복):**
15. `test_send_requires_approval_and_sends_on_accept` — accept → write 호출, payload `b"AT+GMR\n"`.
16. `test_send_declined_does_not_write` — decline → `status=="declined"`, write 미호출.
17. `test_send_cancel_does_not_write` — cancel 동일.
18. `test_send_without_elicitation_capability_errors_with_guidance` — capability False → error + "SERIAL_WRITE_CONFIRM" 안내, 미전송.
19. `test_send_elicit_mcperror_falls_back_to_error` — elicit가 McpError raise → error, 미전송.
20. `test_send_skips_elicit_when_confirm_off` — `write_confirm=False` → elicit 미호출·전송됨.
21. `test_send_blocked_when_write_off` — `write=False` → error, elicit·write 모두 미호출.
22. `test_send_eol_variants_and_empty_payload_rejected` — `\r\n`/`\r`/`""` 부착 검증, `command="" eol=""` 에러, 허용 외 eol 에러.
23. `test_send_multibyte_utf8` — `"안녕"` → UTF-8 바이트 검증.
24. `test_send_routes_by_port_and_errors_on_ambiguous` — dual fixture: 미지정 에러+`ports` 목록, 별칭 라우팅.
25. `test_send_write_exception_returns_error_dict` — reader.write가 SerialException → error, `lines==[]`.
26. `test_send_harvests_only_lines_after_t0` — write 목이 호출 시점에 buffer.add로 응답 주입 + t0 이전 항목 선재 → 이후 것만 회수(`wait_ms=0`).
27. `test_reset_calls_pulse_and_shares_approval_contract` — accept → pulse_reset 호출; decline → 미호출.
28. `test_reset_zero_lines_message_hints_human_fallback` — 0줄 → message에 사람 폴백 안내.
29. `test_wait_ms_clamped` — 음수→0, 31000→30000(전송은 수행).

## 8. 문서 델타 (코드와 같은 작업 단위 — AGENTS.md "문서–코드 일치", SPEC §6.1)

**서버 레포 (이 레포):**

| 파일:위치 | 변경 |
|---|---|
| `SPEC.md:26` (§2) | "읽기 전용으로 구현한다…" 불릿 개정: 조회 도구는 읽기 전용 유지, 쓰기는 `send_serial_command`/`reset_board` 2종으로만 하며 **매 호출 서버측 elicitation 승인이 기본** — `SERIAL_WRITE_CONFIRM=off`로 클라이언트 권한 게이트에 위임, `SERIAL_WRITE=off`로 전면 차단 |
| `SPEC.md:60` (§5 제목·서문) | "(모두 읽기 전용이며…)" → 조회 6종 읽기 전용 + 쓰기 2종 승인 게이트로 개정 |
| `SPEC.md` §5 본문 | 도구 목록에 2종 추가 + **§5.2 신설**: 승인 규약(elicitation 1차·capability 폴백·허용목록 우회 불가 근거), `status="declined"` 계약, TX 감사 기록(`[TX]`/`[RST]` — tee·feed 무조건, buffer는 필터 적용), 응답 회수는 t0 타임스탬프 기반(`entries_since`, dedup 접힘 포함). 블랙박스 절차(71행)에 reset_board 경로 추가 |
| `SPEC.md:124-128` (§9.2) | 스킬 내용 개정: 루프 2단계 = reset_board 우선(승인 팝업), 사람 물리 리셋은 폴백(미지원·거부·native-USB 0줄) |
| `SPEC.md` 부록 | 구현 상태 항목 추가(2026-06-11, 쓰기·리셋·승인 게이트, 최종 테스트 수) |
| `README.md` | "읽기 전용" 표제·도구 표(2행 추가)·블랙박스 루프 문구 개정 + 환경변수 표에 `SERIAL_WRITE`/`SERIAL_WRITE_CONFIRM` 추가 + 승인 동작·elicitation 미지원 클라이언트 폴백 설명 단락 |
| `AGENTS.md:30` | "읽기 전용. 쓰기(명령 전송)는 향후 확장이며 구조만 열어둔다." → 현행 서술(쓰기 2종 + 매 호출 승인 게이트) |
| `server.py:1-12` | 모듈 docstring의 "6개의 읽기 전용 도구"·"현재 읽기 전용" 서술 개정 |
| `pyproject.toml` | §5.6 |

**silotek-tools 레포 (`C:\Users\User\projects\silotek-tools`):**

| 파일 | 변경 |
|---|---|
| `plugins/serial-mcp/.claude-plugin/plugin.json` | `env`에 `"SERIAL_WRITE": "${SERIAL_WRITE:-}"`, `"SERIAL_WRITE_CONFIRM": "${SERIAL_WRITE_CONFIRM:-}"` 추가(기존 11종 패턴 동일), `version` 0.1.1→0.2.0, description에 쓰기·리셋 한 줄 |
| `plugins/serial-mcp/skills/serial-debugging/SKILL.md` | 8행 "서버는 **읽기 전용**" 개정. 표준 루프(12-15행) 2단계: "`reset_board(port=...)`로 AI가 직접 리셋(승인 팝업 — 사람이 수락) → 거부/미지원/0줄(native-USB)이면 사람에게 물리 리셋 요청(폴백)". `send_serial_command` 사용 판단(명령 문법은 펌웨어 몫), `status="declined"` 해석(재시도 금지·사람과 합의) 추가 |
| `.claude-plugin/marketplace.json` | serial-mcp 항목에 버전 표기가 있으면 동기화(구현 시 확인) |

## 9. 완료 기준 (Definition of Done)

- [ ] 신규 테스트 ~26개(§7) 전부 + 기존 테스트 전부 녹색: `uv run pytest` (예상 합계 84개±).
- [ ] `py -m compileall -q src` 통과.
- [ ] 기존 테스트 수정은 test_config.py 완전 일치 계약 2건뿐(§7-13). `_ingest` 시그니처·동작, 기존 도구 6종 시그니처·반환 불변.
- [ ] stdout에 어떤 출력도 추가되지 않음(새 코드 전부 `_log` 또는 tee만).
- [ ] §8 문서 델타 전부 반영(두 레포). 한 사실의 중복 서술(SPEC·README의 env 목록 등)은 함께 갱신됨.
- [ ] pyproject 0.2.0 + mcp 플로어 상향, plugin.json 0.2.0.
- [ ] 실장비 검증(§12) 통과 — **사람 개입 필요**: 사용자가 자리할 때 수행, 통과 후에만 push(서버 레포 → silotek-tools 순).

## 10. 실패 기준 (해당 시 중단하고 사용자에게 보고 — 임의 우회 금지)

1. **의도되지 않은 기존 테스트 실패**: §7-13의 2건 외 기존 테스트가 깨지면 설계와 코드 중 무엇이 틀렸는지 진단해 보고. 테스트를 고쳐서 녹색을 만드는 식의 우회 금지.
2. **SDK 전제 붕괴**: `Context.elicit`/`check_client_capability`/`ElicitationCapability`가 설치 SDK에 없으면(§4에서 1.27.2 확인됐으므로 가능성 낮음) 중단·보고.
3. **빈 스키마 거부**: `_WriteApproval`(필드 0개)이 elicitation 스키마 검증(TypeError)이나 클라이언트 렌더링에서 실패하면 → 승인된 대안인 `confirm: bool` 단일 필드 모델로 교체. 그래도 실패하면 보고.
4. **실장비에서 승인 팝업 미표시**(클라이언트 elicitation 미지원): 이때 도구는 §5.4-2의 에러를 반환해야 정상이다. `SERIAL_WRITE_CONFIRM=off`를 **임의로 설정해 우회하지 말 것** — 안전 결정은 사용자 몫이므로 현상 그대로 보고.
5. **reset_board가 SSM(COM4, CH343)에서 부트 배너 회수 실패**: 시퀀스 코드 결함인지 배선 문제인지 분리 진단(같은 포트에서 esptool 플래싱이 동작하는 보드라는 사실이 배선 검증의 기준) 후 보고. "native-USB no-op" 메시지로 얼버무리지 말 것.
6. **의존성 추가 필요 판단이 들 때**(pytest-asyncio 등): §4에서 불필요함이 검증됐다. 추가하지 말고, 정말 필요해 보이면 사유와 함께 보고.

## 11. 위험·엣지 (구현·문서에 반영)

1. **포트 열기 자체의 의도치 않은 리셋**: `serial.Serial` open 시 DTR/RTS 어서트로 자동리셋 보드가 리셋될 수 있음 — **기존 동작**이며 이번 변경과 무관하나, 리셋 도구 도입으로 사용자가 인과를 혼동할 수 있으니 SPEC §5.2/README에 명시.
2. **native-USB·미배선 보드**: `pulse_reset`은 예외 없이 no-op → "0줄 회수"로 나타남. 도구 message·docstring·SKILL.md 3곳 모두 사람 폴백 안내.
3. **비표준 배선 보드**: esptool 클래식 시퀀스가 안 먹는 보드 존재 가능 — 폴백 동일.
4. **elicitation 무기한 대기**: SDK `elicit_form`에 서버측 타임아웃 없음. 사용자 미응답 시 도구 호출이 대기하고, 클라이언트 취소는 `cancel`로 수신. 서버측 타임아웃 도입은 **이번 범위 밖**(보류 — 필요 시 후속).
5. **이벤트 루프 블로킹**: write(최대 write_timeout 2s)·pulse_reset(0.1s)을 async 도구가 직접 호출 — 단일 stdio 클라이언트 환경에서 수용. 문제가 되면 `anyio.to_thread.run_sync` 전환(코드 주석으로 남길 것).
6. **include 필터와 TX 마커**: `SERIAL_INCLUDE` 설정 시 `[TX]` 항목이 buffer에서 빠질 수 있음 — 응답 회수는 `entries_since(t0)` 타임스탬프 기반이라 무관(§7-8 테스트). tee·feed에는 항상 남음(감사 추적 보장).
7. **dedup 접힘과 회수**: 응답이 룩백 윈도 내 기존 항목에 접혀도 `last_ts` 갱신으로 회수됨 — 단 `(N회 반복…)` 표기로 옴(§7-3 테스트).
8. **`_config` 미초기화 경로**: 전역 기본 `{}` — 도구는 `.get(key, True)`로 읽어 직접 호출/테스트에서도 안전.
9. **병렬 쓰기 호출**: 동시 2건이어도 `_ser_lock`이 물리 쓰기를 직렬화. 승인 팝업 표시 순서는 클라이언트 책임.
10. **TX 마커가 회수 lines에 섞임**: t0 직후 감사 기록되므로 `entries_since(t0)`에 `[TX]` 줄 자체가 포함될 수 있음 — 버그 아님, docstring에 명시.

## 12. 최종 검증 절차

1. `uv run pytest` — 전체 녹색(신규 ~26 + 기존 58+).
2. `py -m compileall -q src` — 문법 검증.
3. **실장비 검증(사람 필요 — 사용자에게 요청)**: Claude Code 세션에서 serial-mcp 재기동(SSM, COM4) 후 —
   a. `reset_board(port="SSM")` 호출 → **승인 팝업이 뜨는지**(elicitation 클라이언트 지원 확인) → 수락 → `ESP-ROM:esp32s3` 부트 배너가 `lines`로 회수되는지.
   b. `send_serial_command(command="HELP", port="SSM", wait_ms=1000)` 호출 → 승인 팝업 수락 → 명령 목록(`Command : RESET, REFLASHESP, ...`)이 `lines`로 회수되는지. 실장비 확인 결과 SSM 펌웨어는 `/help`가 아니라 대문자 `HELP`(슬래시 없음)를 명령 목록 출력 명령으로 처리한다. 이 항목은 단순 전송 성공이 아니라 SSM 펌웨어가 실제 명령을 처리하는지 확인하는 smoke test다.
   c. `send_serial_command` 거절 경로: 팝업에서 거절 → `status=="declined"` + 미전송 확인.
   d. 웹 뷰어/tee에 `[RST]`/`[TX]` 마커 표시 확인(`HELP` 송신은 `[TX] HELP`로 남아야 함).
4. 통과 후 push: 서버 레포 main 먼저(uvx 배포원), 이어서 silotek-tools(플러그인 env·스킬). 마지막으로 `/code-review`(워크플로 4단계)를 사용자에게 권고.
