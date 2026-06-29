# serial-mcp-server

ESP32·STM32 등 시리얼로 텍스트 로그를 출력하는 임베디드 보드의 로그를, **AI(Claude Code,codex 등)가 펌웨어 디버깅 중 직접 읽도록** 해 주는 헤드리스 MCP 서버.

사람은 장비를 물리적으로 동작시키고, AI는 이 서버의 도구로 그 결과 로그를 스스로 조회해 원인을 분석하고 코드를 고친다. 자동리셋 회로가 있는 보드는 AI가 승인 팝업을 거쳐 직접 리셋할 수 있고, 펌웨어 CLI/AT 명령도 승인 후 전송할 수 있다.
**사람이 로그를 눈으로 보기 위한 모니터가 아니다.** 다만 포트를 MCP가 점유하면 테라텀으로 볼 수 없으므로, owner 세션에는 **localhost 웹 뷰어**를 내장한다 — 첫 시리얼 도구 호출이 `http://127.0.0.1:8743` (기본) 소유권을 잡으면 실시간 스트림·링버퍼를 컬러로 볼 수 있고, 좌측 세션 카드의 해제로 포트를 사람·다른 세션에 양보할 수 있다(도구 응답의 `viewer_url` 참조).

- 조회 도구는 읽기 전용 · 쓰기는 `send_serial_command`/`reset_board` 2종만 승인 게이트로 허용 · stdio transport · 의존성은 `mcp[cli]` + `pyserial` 뿐
- OS 무관(macOS / Windows / Linux, WSL 제외)
- 백그라운드 스레드가 포트를 계속 읽어 ring buffer(기본 2000줄)에 적재 · 근접 중복 접기(dedup, 룩백 기본 5줄) · 정규식 수집 필터 · 공백뿐인 줄 미저장(tee 파일에는 원본 그대로)

## 도구

| 도구 | 용도 |
|---|---|
| `list_serial_ports` | 포트 목록 + VID/PID/description + 별칭 `name` (보드 식별은 별칭으로 — VID/PID는 어댑터 칩 확인용) |
| `get_serial_status` | 연결 상태 / 포트 / 보드레이트 / 마지막 에러 |
| `get_topology` | 전 포트 메시 토폴로지 로스터 + 최근 홉 20개(웹 뷰어 없이 AI가 경로 해석) |
| `get_recent_logs(lines=200)` | 최근 N줄 (접힌 묶음 표기 포함) |
| `query_serial_logs(pattern, max_results=100)` | 정규식 검색 |
| `get_log_buffer_info` | 버퍼 크기 / 최신·최오래 항목 |
| `clear_log_buffer` | 버퍼 비우기 (시험 시작) |
| `send_serial_command(command, port="", eol="\n", wait_ms=500)` | 보드 CLI/AT 명령 전송 + 직후 응답 회수(매 호출 승인) |
| `reset_board(port="", wait_ms=2000)` | DTR/RTS 자동리셋 펄스 + 부팅 로그 회수(매 호출 승인) |

**블랙박스 루프:** `clear_log_buffer` → 가능하면 `reset_board` 승인 후 직접 리셋(거부/미지원/0줄이면 사람이 물리 리셋) → `get_recent_logs` / `query_serial_logs`. 메시/멀티홉 경로를 해석할 때는 `get_topology`로 로스터와 최근 홉을 먼저 확인한다.

## 설치

### A. silotek 마켓플레이스 (권장)

이미 silotek 마켓을 등록한 팀은 **serial-mcp** 플러그인을 설치한다(장비를 다루는 인원만).

Claude Code:

```text
/plugin install serial-mcp@silotek --scope user
```

Codex는 플러그인으로 `serial` 스킬을 설치한 뒤, 현재 구조상 MCP 도구를 top-level 설정에도 한 번 등록해야 한다. `silotek-plugin-marketplace` 저장소 루트에서:

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File .\plugins\serial-mcp\scripts\install-codex.ps1
pwsh -NoProfile -ExecutionPolicy Bypass -File .\plugins\serial-mcp\scripts\verify-codex.ps1 -RequireDirectConfig
```

### B. 직접 등록 (마켓 미경유)

```bash
claude mcp add --scope user serial-mcp \
  -e SERIAL_PORT=<your-port> -e SERIAL_BAUD=115200 \
  -- uvx --from git+https://github.com/JOCOIN94/serial-mcp-server serial-mcp
```

> ⚠️ B 경로는 **MCP 도구만** 등록되고, 사용 안내 스킬은 포함되지 않는다(스킬은 플러그인 경로에만 동봉). docstring 이 자족적이라 도구 자체는 정상 동작한다.

## 환경변수

| 변수 | 기본값 | 설명 |
|---|---|---|
| `SERIAL_PORT` | (없음=자동) | 미설정이면 USB 시리얼 전부 자동 모니터링(실행 중 꽂은 포트도 핫플러그로 자동 추가). 지정 시 그 목록만: `COM4` 또는 `COM4,COM13@9600`. COM10 이상도 `COM10` 그대로(pyserial이 `\\.\` 접두를 자동 처리 — `\\.\` 표기는 별칭 매칭이 깨질 수 있어 비권장) |
| `SERIAL_NAMES` | (없음) | 포트→보드 별칭. `COM4=SSM,COM13=SB1` 또는 USB 시리얼넘버 키 `5909024173=SSM`(포트 번호가 바뀌어도 유지). 표기·도구 port 인자에 별칭 사용 가능 |
| `SERIAL_AUTONAME` | (없음) | **로그 내용으로 보드 자동 식별**: `이름=정규식;…`(세미콜론 구분, 순서=우선순위). 첫 매칭에서 1회 확정, `SERIAL_NAMES`가 우선. 예: `SSM=FW Ver:SSM|\[IOc\];SB1=Send to the STM32`(**모든 동작 상태**에서 나오는 패턴을 고를 것 — 아래 §다중 포트·별칭 주의) |
| `SERIAL_BAUD` | `115200` | 보드레이트 |
| `SERIAL_TEE` | (없음) | 로그 영구 기록 경로 — 포트별 파일로 분리(`log.txt`→`log.SSM.txt`). 버퍼에서 밀려난 줄도 보존 |
| `SERIAL_EXCLUDE` | (없음) | 이 정규식에 매칭되는 줄은 저장하지 않음 |
| `SERIAL_INCLUDE` | (없음) | 지정 시 매칭되는 줄만 저장 |
| `SERIAL_BUFFER_LINES` | `2000` | ring buffer 크기 |
| `SERIAL_DEDUP` | `5` | 중복 접기 룩백 윈도 — 최근 N줄 안의 같은 줄을 접음. `1`(=`true`)=직전 줄만, `0`/`false`/`no`/`off`로 끔 |
| `SERIAL_WEB` | `8743` | 웹 뷰어이자 whole-session 소유권 잠금 포트. 점유 시 임시 포트 폴백 없이 휴면/안내로 동작한다. `0`/`false`/`no`/`off`는 UI만 끄고 8743 잠금은 유지 |
| `SERIAL_HOTPLUG` | `5` | 포트 감시 간격(초, 소수 허용). 자동 스캔 모드: 서버 실행 중 꽂은 보드를 자동 추가(연속 2회 스캔 확인 후 — 드라이버 정착 유예). 모든 모드: 열거 목록에서 연속 2회 사라진 포트의 좀비 핸들 강제 해제(플래키 어댑터 대응). `0`/`false`/`no`/`off`로 끄면 시작 시 1회 스캔만 |
| `SERIAL_WRITE` | `true` | 쓰기 도구 전면 스위치. `0`/`false`/`no`/`off`면 `send_serial_command`/`reset_board`가 등록은 유지하되 전송하지 않고 에러 반환 |
| `SERIAL_WRITE_CONFIRM` | `all` (서버 기본) | 쓰기 도구의 서버측 elicitation 승인 범위(3-state). `all`(=`true`/`1`/`on`/`yes`)=모든 쓰기 승인, `r3`(=`risky`)=R3 파괴 명령(reflash/format/download/파일삭제/임의 JSON 주입 + boot-menu `D`)만 승인하고 그 외(조회·복원 가능한 설정·재부팅)는 통과, `off`(=`0`/`false`/`no`)=승인 생략 후 클라이언트 권한 게이트에 위임. Silotek 플러그인 설치 경로는 실장비 표준값으로 `r3`을 주입하며, 직접 실행하거나 값을 비우면 서버 기본값 `all`을 쓴다 |
| `SERIAL_CHAR_DELAY` | `10` (서버 기본) | `send_serial_command` 전송 시 문자 간 지연(ms, 소수 허용, 상한 100). 폴링 수신 펌웨어(STM32 등)가 기계 속도 연속 바이트를 흘리는 문자 유실 방지 — 모든 보드에 공통 적용. Silotek 플러그인 설치 경로는 실장비 표준값으로 `100`을 환경변수에 주입하며, 직접 실행하거나 값을 비워 두면 서버 기본값 `10`을 쓴다. `0`/`false`/`no`/`off`로 끄면 통짜 전송 |

### 쓰기 승인 동작

`send_serial_command`와 `reset_board`는 기본(`SERIAL_WRITE_CONFIRM=all`)으로 매 호출 사용자 승인을 요구한다. `SERIAL_WRITE_CONFIRM=r3`이면 R3 파괴 명령(reflash/format/download/파일삭제/임의 JSON 주입 + boot-menu `D`)에만 승인 팝업을 띄우고, 조회·복원 가능한 설정·재부팅(`reset_board` 포함)은 승인 없이 통과한다. `all`/`r3`에서 승인이 필요한 호출인데 클라이언트가 elicitation(승인 팝업)을 지원하지 않으면 전송하지 않고 `SERIAL_WRITE_CONFIRM=off` 안내가 포함된 에러를 반환한다. 사용자가 거절하거나 취소하면 `status="declined"`이며, AI는 같은 명령을 반복 호출하지 않고 사람과 다음 행동을 합의해야 한다.

송신 감사 마커는 `[TX] ...` 또는 `[RST] ...`로 기록된다. 웹 스트림과 tee 파일에는 항상 남고, 링버퍼에는 include/exclude 필터가 적용된다. 포트를 여는 순간 일부 자동리셋 보드가 리셋될 수 있는데, 이는 pyserial open 시 DTR/RTS 상태 변화로 생기는 기존 동작이며 명시적 `reset_board` 호출과 구분된다.

### 다중 포트 · 별칭

기본값(미설정)이면 USB 시리얼을 전부 자동 모니터링한다 — 보드 2개면 2개, 10개면 10개. 사람이 보는 모든 표기는 별칭을 설정하면 `SSM (COM4)` 형태가 된다. 서버 실행 중에 보드를 새로 꽂아도 몇 초 안에 자동으로 모니터링이 시작된다(핫플러그, `SERIAL_HOTPLUG`):

- **Windows** (PowerShell): `setx SERIAL_NAMES "COM4=SSM,COM13=SB1"`  (새 터미널부터 적용)
- **macOS / Linux**: `export SERIAL_NAMES="COM4=SSM"`
- 특정 포트만 보려면: `setx SERIAL_PORT "COM4,COM13@9600"` (`@N`=포트별 보드레이트)
- **포트 번호가 자주 바뀌는 어댑터**(시리얼넘버 없는 클론 등)는 로그 내용 기반 자동 식별이 편하다: `setx SERIAL_AUTONAME "SSM=FW Ver:SSM|\[IOc\];SB1=Send to the STM32"`. 패턴 고를 때 두 가지 함정:
  - ① **그 보드 로그에서만 나오는 패턴**이어야 한다 — 상대 보드 이름이 로그에 인용되는 경우(예: SSM 로그 속 "SB1") 오인 주의.
  - ② **모든 동작 상태에서 나오는 패턴**이어야 한다 — 정상 동작 중에만 나오는 줄(예: 하위장비 패킷 처리 `\[Proc-`)을 고르면 **고장·유휴 상태에서 식별이 안 된다**(WiFi 끊긴 SSM은 `\[Proc-` 대신 `[IOc] Disconnected!`만 뱉음). 부팅 배너(`FW Ver:…`)나 상태 무관 상시 마커(`\[IOc\]` 등)가 안전하다.
- **별칭의 `유닛-칩` 규칙**: 웹 뷰어는 별칭의 첫 `-` 앞을 유닛, 뒤를 칩으로 묶는다(`SB-ESP`·`SB-STM`→유닛 "SB", `SSM-ESP`→유닛 "SSM"·칩 "ESP"). 한 유닛에 칩이 여러 개면 한 박스로 묶여 보인다.

포트 단위 AI 도구는 보드가 여러 개면 `port` 인자(별칭/포트명/`SSM (COM4)` 라벨)를 지정해 호출한다. 미지정 시 예외는 둘 — `get_serial_status`는 전 포트 상태 배열을 반환하고, `clear_log_buffer`는 전체 버퍼를 비운다. `get_topology`는 전 포트 토폴로지 스냅샷이라 `port` 인자가 없다.

### 자기 포트 찾기

- `list_serial_ports` 도구 (VID/PID·description 까지 보여 줌)
- 또는 OS 명령: macOS `ls /dev/cu.*` · Linux `ls /dev/ttyUSB*` · Windows 장치 관리자

## uv / uvx

이 서버는 `uvx` 로 git 에서 바로 실행된다. uv 설치는 <https://docs.astral.sh/uv/> 참고(Windows 는 설치 후 PATH 확인). private 레포면 팀원의 git 인증이 필요하다.

## 웹 로그 뷰어

owner 세션이 획득된 동안 브라우저로 `http://127.0.0.1:8743` (기본)을 열면:

- **포트 셀렉터** — 보드가 여러 개면 헤더에서 `SSM (COM4)` 식으로 전환(1개면 숨김). 핫플러그로 보드가 늘면 새로고침 없이 자동 추가된다.
- **소유권 보드** — MCP 클라이언트 세션(`clientInfo.name`)과 H.W/board 묶음을 표시한다. **해제(release)** 는 이 owner 세션의 전체 COM 핸들, 뷰어, 8743 잠금을 반납해 테라텀·다른 AI 세션이 쓸 수 있게 하는 양보다. 해제 뒤 다음 세션의 첫 시리얼 도구 호출이 새 owner 획득을 시도한다.
- **스트림 탭** — 수신 원본 실시간 표시(테라텀 대체). 일시정지·자동스크롤·화면 지우기 지원.
- **버퍼 탭** — AI가 보는 것과 같은 가공 뷰(중복 접힘 `(N회 반복…)` 표기 포함).
- **검색·필터** — 리터럴 검색(로그의 태그 클릭으로도 토글), ERR/WARN/BOOT 레벨 칩, 에러·경고·성공만 보는 **Focus 모드**, 스크롤 업 중 새 로그 배지.
- **가독성(구조 기반)** — 줄을 score로 분류해(특정 문자열 암기 아님, 모호하면 neutral) 좌측 bar·작은 badge 중심으로 **절제된 컬러**를 입힌다. ANSI 색 해석(16/256/truecolor, 깨져도 안전), 태그별 저채도 고정색, JSON inline/접기/펼침 + correlation key badge, 반복(정확·유사) 접기 `×N`, 수신 시간 간격·부팅/리셋 구간 구분선, MAC/IP/URL/UUID 등 구조 인식.
- **설정 패널(⚙)** — 색 강도(Off/Min/Normal/Vivid), 줄 간격(조밀/보통/여유), JSON 표시(한 줄/접기/펼침), 반복 접기 ON·OFF와 판정(정확/값 무시), ANSI·semantic 색 토글, 타임스탬프·줄바꿈·글자 크기. 모든 설정은 브라우저에 저장된다. 색 강도를 Off로 두면 거의 원문 그대로 본다.

뷰어는 보조 기능이다 — 실패해도 MCP 도구는 정상 동작하며, `127.0.0.1` 전용이라 외부에서 접속할 수 없다.

## 로컬 개발

```bash
uv sync
$env:SERIAL_PORT = "COM4"   # PowerShell 예시
uv run serial-mcp
```

> 시리얼 포트는 **포트당 한 프로그램**이 원칙이다. serial-mcp는 첫 시리얼 도구 호출 전까지 휴면이라 COM 포트를 열지 않는다. owner가 된 뒤에는 **Windows가 OS 배타 점유를 강제**해 같은 포트를 테라텀 등이 열 수 없다(그 반대도 마찬가지). **macOS/Linux는 단독 점유가 강제되지 않아** 동시에 열 수는 있지만 수신이 쪼개져 양쪽 로그가 깨지므로 피하라 — 사람이 볼 화면은 내장 웹 뷰어를 쓰면 된다.
