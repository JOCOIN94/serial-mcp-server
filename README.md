# silotek-serial-mcp

ESP32·STM32 등 시리얼로 텍스트 로그를 출력하는 임베디드 보드의 로그를, **AI(Claude Code)가 펌웨어 디버깅 중 직접 읽도록** 해 주는 헤드리스 MCP 서버.

사람은 장비를 물리적으로 동작시키고, AI는 이 서버의 읽기 전용 도구로 그 결과 로그를 스스로 조회해 원인을 분석하고 코드를 고친다. **사람이 로그를 눈으로 보기 위한 모니터가 아니다.**

- 읽기 전용 · stdio transport · 의존성은 `mcp[cli]` + `pyserial` 뿐
- OS 무관(macOS / Windows / Linux, WSL 제외)
- 백그라운드 스레드가 포트를 계속 읽어 ring buffer(기본 2000줄)에 적재 · 연속 중복 접기(dedup) · 정규식 수집 필터

## 도구 (모두 읽기 전용)

| 도구 | 용도 |
|---|---|
| `list_serial_ports` | 포트 목록 + VID/PID/description (어느 포트가 그 보드인지 추론) |
| `get_serial_status` | 연결 상태 / 포트 / 보드레이트 / 마지막 에러 |
| `get_recent_logs(lines=200)` | 최근 N줄 (접힌 묶음 표기 포함) |
| `query_serial_logs(pattern, max_results=100)` | 정규식 검색 |
| `get_log_buffer_info` | 버퍼 크기 / 최신·최오래 항목 |
| `clear_log_buffer` | 버퍼 비우기 (시험 시작) |

**블랙박스 루프:** `clear_log_buffer` → [사람이 장비 동작/리셋] → `get_recent_logs` / `query_serial_logs`.

## 설치

### A. silotek-tools 마켓플레이스 (권장)

이미 silotek-tools 마켓을 등록한 팀은 `/plugin` 에서 **serial-mcp** 플러그인을 설치한다(장비를 다루는 인원만). user 레벨로 활성화하면 모든 코드베이스·세션에서 도구가 노출되고, **사용 안내 스킬**도 함께 따라온다.

### B. 직접 등록 (마켓 미경유)

```bash
claude mcp add --scope user serial-mcp \
  -e SERIAL_PORT=<your-port> -e SERIAL_BAUD=115200 \
  -- uvx --from git+https://github.com/JOCOIN94/silotek-serial-mcp serial-mcp
```

> ⚠️ B 경로는 **MCP 도구만** 등록되고, 사용 안내 스킬은 포함되지 않는다(스킬은 플러그인 경로에만 동봉). docstring 이 자족적이라 도구 자체는 정상 동작한다.

## 환경변수

| 변수 | 기본값 | 설명 |
|---|---|---|
| `SERIAL_PORT` | (필수) | 대상 포트. Windows 의 COM10 이상은 `\\.\COM10` 형식 |
| `SERIAL_BAUD` | `115200` | 보드레이트 |
| `SERIAL_TEE` | (없음) | 로그를 파일에도 영구 기록할 경로(버퍼에서 밀려난 줄도 보존) |
| `SERIAL_EXCLUDE` | (없음) | 이 정규식에 매칭되는 줄은 저장하지 않음 |
| `SERIAL_INCLUDE` | (없음) | 지정 시 매칭되는 줄만 저장 |
| `SERIAL_BUFFER_LINES` | `2000` | ring buffer 크기 |
| `SERIAL_DEDUP` | `1` | 연속 중복 접기 (`0`/`false` 로 끔) |

### 자기 포트 설정

- **Windows** (PowerShell): `setx SERIAL_PORT COM4`  (새 터미널부터 적용)
- **macOS / Linux**: `export SERIAL_PORT=/dev/cu.usbserial-XXXX`

### 자기 포트 찾기

- `list_serial_ports` 도구 (VID/PID·description 까지 보여 줌)
- 또는 OS 명령: macOS `ls /dev/cu.*` · Linux `ls /dev/ttyUSB*` · Windows 장치 관리자

## uv / uvx

이 서버는 `uvx` 로 git 에서 바로 실행된다. uv 설치는 <https://docs.astral.sh/uv/> 참고(Windows 는 설치 후 PATH 확인). private 레포면 팀원의 git 인증이 필요하다.

## 로컬 개발

```bash
uv sync
$env:SERIAL_PORT = "COM4"   # PowerShell 예시
uv run serial-mcp
```

> 시리얼 포트는 **포트당 한 프로그램**만 열 수 있다. 이 서버가 떠 있는 동안에는 같은 포트를 테라텀 등 다른 프로그램이 열 수 없다(그 반대도 마찬가지).
