# silotek-serial-mcp 명세
### (B안: MCP 코어 + 얇은 스킬 하이브리드)

> **핵심 원칙: "docstring은 자족적으로, 스킬은 그 위에 워크플로만 얹는다."**
> 코어 기능은 전부 MCP 서버가 제공하고, 스킬은 AI가 그 도구들을 올바른 순서·판단으로 쓰도록 안내하는 마크다운 문서일 뿐이다. 둘은 동일 플러그인에 동봉되어 설치·활성화 생명주기를 공유한다.

## 1. 목적 및 범위

ESP32, STM32 등 시리얼 인터페이스로 텍스트 로그를 출력하는 임베디드 보드의 로그를 COM 포트(Windows), `/dev/cu.*`(macOS), `/dev/ttyUSB*`(Linux)에서 읽어, Claude Code(이하 "AI")가 펌웨어 디버깅 과정에서 실시간으로 참조할 수 있는 최소 기능의 헤드리스 MCP 서버를 구현한다.

본 서버의 사용 주체는 사람이 아니라 AI이다. 사람은 장비를 물리적으로 동작시키는 역할만 수행하며, 그 결과로 출력되는 시리얼 로그를 AI는 직접 관측할 수 없다. AI는 본 서버를 통해 로그를 직접 조회하여 동작 결과를 확인하고 원인을 분석한 뒤, 코드를 수정하고 재시험하는 자율 반복 절차를 수행한다. 본 서버는 사람이 로그를 육안으로 확인하기 위한 모니터링 도구가 아니다.

본 서버는 칩 종류와 무관하게 시리얼로 수신되는 텍스트만을 처리 대상으로 한다. 특정 칩에 한정된 가정을 포함하지 않는다.

사용 맥락: 동일한 PC에 연결된 **복수 장비**(USB 시리얼 자동 인식 — 2개면 2개, 10개면 10개)의 로그를 복수의 코드베이스에서 공통으로 참조한다. 장비 식별은 포트명이 아니라 별칭(`SERIAL_NAMES`, 예: `SSM (COM4)`)으로 한다. 펌웨어 개발 중 장비 로그 확인, OTA 작업 중 장비 로그 확인, 웹과 장비 간 런타임 기능(예: 가격 변경)의 개선 및 점검 등, 작업 시점에 따라 서로 다른 코드베이스(펌웨어, OTA, 웹 등)에서 동일한 서버를 사용한다.

**본 작업의 산출물은 두 가지이다.**
1. 위 기능을 제공하는 MCP 서버.
2. AI가 해당 서버의 도구들을 블랙박스 시험 루프로 올바르게 사용하도록 안내하는 얇은 스킬(§9). 스킬은 절차 및 판단에 관한 안내 전용이며, 실행 로직을 포함하지 않는다.

## 2. 제약

- 헤드리스로만 동작한다. GUI를 포함하지 않으며, PyQt6 등 GUI 라이브러리를 사용하지 않는다. (localhost 웹 뷰어(§10)는 GUI 라이브러리가 아니라 사용자의 브라우저를 화면으로 쓰므로 이 제약에 위배되지 않는다.)
- 의존성은 두 가지로 한정한다: 최신 공식 MCP Python SDK(`mcp[cli]`, FastMCP 사용)와 `pyserial`.
- 운영체제에 독립적으로 동작한다(macOS, Windows, Linux). WSL은 대상이 아니다. 전송 방식(transport)은 stdio로 한다.
- 조회 도구는 읽기 전용으로 유지한다. 포트에 쓰는 기능은 `send_serial_command`(텍스트 명령 전송)와 `reset_board`(DTR/RTS 리셋) 2종만 제공하며, 기본값은 매 호출 서버측 elicitation 승인이다. `SERIAL_WRITE_CONFIRM=off`로 승인을 클라이언트 권한 게이트에 위임할 수 있고, `SERIAL_WRITE=off`로 쓰기를 전면 차단할 수 있다.
- 클린 코드, 클린 아키텍처 원칙을 지켜, 확장과 유지 보수가 좋은 코딩 패턴을 사용하라.(일관된 패턴을 사용하여 각기 다른 스타일이 되지않도록 한다.)
- stdio 주의: stdout으로 MCP JSON-RPC 메시지가 전송된다. stdout에는 어떠한 로그나 출력도 기록해서는 안 된다(프로토콜이 손상된다). 모든 진단 및 로그 출력은 stderr 또는 tee 파일로만 전송한다.
- 동시성: 백그라운드 수신 스레드와 도구 호출이 동일한 버퍼에 접근한다. 버퍼 접근은 Lock으로 보호한다.
- 스킬은 순수 마크다운 문서로만 작성한다. 스킬 내부에 실행 코드(.py/.ps1/.js 등)를 포함하지 않으며, 실제 동작은 전적으로 MCP 서버가 수행한다.

## 3. 동작

- 포트 결정: `SERIAL_PORT` 미설정이면 시작 시 USB 시리얼(VID 보유)을 자동 스캔해 전부 모니터링하고, 이후에도 `SERIAL_HOTPLUG` 간격(기본 5초, 소수 허용, `0`/`false`/`no`/`off`로 끔)으로 재스캔해 **새로 꽂힌 USB 포트를 런타임에 자동 추가**한다(핫플러그 — 블루투스 가상 포트 제외). 사라진 포트의 모니터는 제거하지 않는다(버퍼·tee 보존, 재연결은 리더의 재시도 루프 담당). `SERIAL_PORT` 설정 시 그 목록만 고정 모니터링하며 핫플러그 스캔은 돌지 않는다(`COM4` 또는 `COM4,COM13@9600` — `@N`은 포트별 보드레이트, 늦게 꽂힌 포트는 재연결 루프가 잡음). 포트마다 독립 버퍼·리더·tee(`log.txt`→`log.SSM.txt`)를 갖는다.
- 보드 별칭: 정적 매핑 `SERIAL_NAMES`(포트명/USB 시리얼넘버 키) 또는 **로그 내용 기반 자동 식별** `SERIAL_AUTONAME`(`이름=정규식;…` — 이름 없는 포트의 수신 줄을 대조해 첫 매칭에서 1회 확정, 중복 이름 미부여, 명시 매핑 우선). 시리얼넘버 없는 어댑터의 포트 번호 변동 문제를 푼다.
- 백그라운드 스레드가 시리얼 포트를 지속적으로 읽어, 수신된 데이터를 줄 단위로 ring buffer(기본 2000줄)에 저장한다. 버퍼가 가득 차면 가장 오래된 줄부터 순차적으로 제거하여 최근 2000줄을 유지한다.
- 각 줄에 수신 시각 타임스탬프를 부여한다(예: `[14:02:17.123]`).
- 시리얼 포트와 보드레이트는 하드코딩하지 않으며, 환경변수 또는 인자로 입력받는다(보드레이트 기본값 115200).
- 포트가 다른 프로그램에 의해 이미 점유된 경우, 서버는 종료되지 않고 명확한 오류 메시지를 반환한다(주기적 재연결 시도).
- 선택 기능: 수신된 로그를 파일에도 함께 기록하는 tee 옵션을 제공한다. 버퍼에서 제거된 줄도 이 파일에는 영구히 보존된다. 보조 기록이며, 주된 조회 경로는 AI의 도구 호출이다.

## 4. 수집 필터 (버퍼 저장 시점에 적용)

**4.1 exclude/include 정규식 훅** (기본값: 양쪽 모두 비어 전체 통과)
- exclude에 매칭되는 줄은 저장하지 않는다.
- include가 지정된 경우, 매칭되는 줄만 저장한다.
- 양쪽 모두 비어 있으면 전체를 저장한다. 환경변수 또는 인자로 적용한다.

**4.2 근접 중복 접기(dedup, 룩백 윈도)** (기본값: 룩백 5)
- 수신 줄이 (타임스탬프 제외) **룩백 윈도(버퍼 끝 N개, 기본 5)** 안의 기존 항목과 내용이 동일하면 새 줄로 저장하지 않고 그 항목의 반복 횟수와 최종 수신 시각을 갱신한다(최초 수신 시각은 항목 생성 시 고정, 항목 위치는 유지 — first_ts 순서 보존). 접힌 묶음은 버퍼에서 한 줄만 차지한다.
- 같은 줄이 윈도 밖으로 밀려난 뒤 다시 나타나면 새 묶음으로 시작한다. 다른 줄이 끼어들어도 윈도 안이면 기존 묶음에 접힌다(교차 반복도 압축).
- 조회 시 `(N회 반복, HH:MM:SS~HH:MM:SS)` 형식으로 표시한다.
- `SERIAL_DEDUP=N`으로 윈도를 조정한다. `1`(=`true`)=직전 줄만(구버전 동작 — 다른 줄 수신 즉시 묶음 종료), `0`/`false`/`no`/`off`=끔. 근거: 실로그(2026-06-10)에서 메시지가 교차 출력돼 연속 접기가 무력했음. 접힘은 요약이므로 반복 줄들의 정밀한 교차 순서가 필요하면 N을 낮춰 재시험한다(tee에 원본 보존).

**4.3 빈 줄 저장 제외** (항상 적용, 2026-06-10 실로그 확인 후 조정)
- 공백뿐인 줄(strip 후 빈 문자열)은 버퍼에 저장하지 않는다.
- 근거: 실장비(SSM) 펌웨어가 메시지 사이에 빈 줄을 교대로 출력해 §4.2의 중복 접기 판정(당시 직전 줄 비교)이 항상 깨졌다(dedup 무력화 + 버퍼 절반 낭비). 빈 줄은 AI 디버깅에 정보 가치가 없다.
- tee 파일(§3)에는 빈 줄을 포함한 수신 원본이 그대로 기록된다.

## 5. 도구 (조회 6종 읽기 전용 + 쓰기 2종 승인 게이트)

각 도구는 `status`와 `message`를 포함한 dict를 반환한다. `status`는 기본적으로 `ok|error`이며, 사용자가 쓰기 승인을 거부한 경우 `declined`를 반환한다(시스템 오류가 아니므로 AI는 같은 요청을 반복하지 말고 사람과 다음 행동을 합의한다). docstring은 사람을 위한 설명이 아니라 AI를 위한 사용 지침으로 작성하며, AI가 해당 도구를 언제·어떤 목적으로 호출하는지 명확히 기술한다. 반환 dict는 AI가 파싱·추론하기 좋은 구조로 구성한다. 다중 포트에서는 모든 조회/쓰기 도구가 `port` 인자(별칭/포트명/라벨 `SSM (COM4)` 형태 모두 허용, 대소문자 무관)를 받는다 — 에러 응답의 `ports` 목록 항목을 그대로 되돌려 호출해도 해석된다 — 미지정 시 포트 1개면 그 포트, 복수면 에러와 함께 `ports` 목록을 반환한다(`get_serial_status` 미지정은 전 포트 상태 배열, `clear_log_buffer` 미지정은 전체 비우기).

- `list_serial_ports` : 사용 가능한 포트 목록 + VID/PID·description(어댑터 칩 식별용, 예: CH343, CP210x) + 별칭 `name`·`monitored_ports`(보드 식별은 별칭으로).
- `get_serial_status` : 현재 연결 상태, 포트, 보드레이트. (웹 뷰어 활성 시 `viewer_url` 포함 — `get_log_buffer_info`도 동일. 사람이 로그를 직접 보고 싶어 하면 AI가 이 링크를 안내한다.)
- `get_recent_logs(lines=200)` : 최근 N줄(접힌 묶음 반복 횟수 표기 포함).
- `query_serial_logs(pattern, max_results=100)` : 정규식으로 버퍼 검색.
- `get_log_buffer_info` : 버퍼 크기 / 최신·최오래 항목.
- `clear_log_buffer` : 버퍼 수동 비우기.
- `send_serial_command(command, port="", eol="\n", wait_ms=500)` : 보드 CLI/AT 명령을 UTF-8 텍스트로 전송하고, 전송 직후 `wait_ms` 동안 들어온 응답 로그를 회수한다.
- `reset_board(port="", wait_ms=2000)` : DTR/RTS 펄스로 자동리셋 회로 보드를 하드웨어 리셋하고, 부팅 로그를 회수한다. native-USB/미배선 보드는 0줄 회수로 나타날 수 있으며, 이때 사람에게 물리 리셋을 요청한다.

블랙박스 시험 절차: `clear_log_buffer`(시작) → 가능하면 `reset_board`(승인 팝업) 또는 사람이 장비 동작/리셋 → `get_recent_logs` / `query_serial_logs`(확인). AI가 자율 반복한다.

**5.1 docstring과 스킬의 책임 분리**
- 각 도구의 docstring은 **자족적(self-contained)**으로 작성한다. 스킬이 없는 환경(예: `claude mcp add`로 MCP만 등록)에서도 AI가 그 도구를 단독 사용할 수 있도록, 도구 하나의 목적·호출 시점·반환 구조를 완결적으로 기술한다.
- "clear→동작→조회" 루프의 전체 순서·판단·함정은 docstring에 중복하지 않고 스킬(§9)에 위임한다. docstring에는 해당 도구가 루프에서 차지하는 단계를 한 줄로 참조한다.
- 요컨대 docstring은 "도구 하나가 무엇을, 언제"(지역), 스킬은 "여러 도구를 어떤 순서·판단으로 엮는가"(전역)를 담당한다.

**5.2 쓰기 승인·감사 계약**
- `send_serial_command`와 `reset_board`는 도구 등록 자체는 항상 유지하되, `_config.get("write", True)`가 false(`SERIAL_WRITE=off`)이면 전송하지 않고 즉시 `status="error"`를 반환한다.
- 기본값은 매 호출 서버측 elicitation 승인이다(`SERIAL_WRITE_CONFIRM` 미설정/true). 클라이언트가 elicitation capability를 지원하지 않거나 capability를 광고했지만 요청이 `McpError`로 실패하면 전송하지 않고 `SERIAL_WRITE_CONFIRM=off` 안내를 포함한 에러를 반환한다. 이 fail-safe 때문에 클라이언트 허용목록만으로 서버측 승인을 우회할 수 없다.
- `SERIAL_WRITE_CONFIRM=off`는 서버측 elicitation을 생략하고 클라이언트의 일반 도구 권한 게이트에 위임한다. 안전 정책 변경이므로 사용자가 명시적으로 설정해야 한다.
- 사용자가 승인 팝업에서 decline/cancel하면 `status="declined"`를 반환한다. AI는 같은 명령이나 리셋을 반복 호출하지 말고 사람에게 이유를 묻고 다음 행동을 합의한다.
- 송신 감사 기록은 `[TX] {command}` 또는 `[RST] DTR/RTS 하드웨어 리셋 펄스` 마커로 남긴다. tee 파일과 웹 feed에는 항상 남고, ring buffer에는 include/exclude 필터가 적용된다. `send_serial_command`의 응답 회수는 쓰기 직전 `t0` 이후 `LineBuffer.entries_since(t0)` 기반이라 dedup으로 기존 항목에 접힌 응답도 `last_ts` 갱신으로 회수된다.
- 포트를 여는 순간 DTR/RTS 어서트로 일부 자동리셋 보드가 리셋될 수 있다. 이는 기존 pyserial open 동작이며 `reset_board` 도입과 별개다. 리셋 도구는 명시적 DTR/RTS 펄스만 감사 마커로 남긴다.

## 6. 프로젝트 구조 및 배포 (하이브리드)

- Python/uv 서버 코드는 독립 git 저장소에 둔다.
  - 로컬: `C:\Users\User\projects\silotek-serial-mcp\`
  - GitHub: `https://github.com/JOCOIN94/silotek-serial-mcp`
  - uv 프로젝트(`pyproject.toml`), 실행 엔트리포인트는 `serial-mcp`. 서버 코드를 다른 저장소에 포함하지 않는다.
- 코드는 GitHub에 게시한다. 팀원은 uvx로 git에서 직접 실행하므로 클론·경로 지정이 불필요하며, 갱신은 git push로 반영된다.
- 배포는 기존 silotek-tools 플러그인 마켓플레이스 채널을 재사용한다. silotek-tools 저장소(`C:\Users\User\projects\silotek-tools\`, GitHub `JOCOIN94/silotek-claude-plugins`)에 매니페스트와 스킬 문서만 포함하는 플러그인을 추가한다.
    - `plugins/serial-mcp/.claude-plugin/plugin.json` : `mcpServers`에 다음을 정의한다.
        - `command`: `uvx`
        - `args`: `--from git+https://github.com/JOCOIN94/silotek-serial-mcp serial-mcp`
        - `env`: `SERIAL_PORT`을 `${SERIAL_PORT:-}`로 참조(빈 기본값 폴백 — 미설정 시 빈 문자열로 치환되고 서버가 기본값 처리). `SERIAL_BAUD`, `SERIAL_TEE`, `SERIAL_EXCLUDE`, `SERIAL_INCLUDE`, `SERIAL_WRITE`, `SERIAL_WRITE_CONFIRM` 등도 동일한 `${VAR:-}` 패스스루로 노출(미지정 시 서버 기본값, 보드레이트 115200, 쓰기 승인 기본 켜짐).
    - `plugins/serial-mcp/skills/serial-debugging/SKILL.md` : §9의 스킬을 동일 플러그인에 동봉한다.
    - 루트 `.claude-plugin/marketplace.json`에 serial-mcp 플러그인 항목을 추가한다.
- silotek-tools 저장소에는 Python 코드를 포함하지 않으며, 매니페스트(JSON)와 스킬(마크다운)만 포함한다. 실제 서버 코드는 외부 저장소에서 uvx가 가져온다. silotek-tools는 Node 기반 저장소이므로 언어·도메인을 혼재시키지 않는다(스킬은 마크다운이라 언어중립).

**6.1 버전 동기화 규칙 (B안 운영 부채 관리)**
- 도구 이름·시그니처는 "안정 계약"으로 취급한다. 변경 시 같은 폴더의 스킬(`SKILL.md`)도 동일 PR에서 함께 수정한다.
- 스킬·매니페스트는 silotek-tools 한 저장소에 함께 있어 동시 갱신이 가능하다. 서버 코드(외부 레포)와는 분리되므로, 드리프트 최소화를 위해 도구 이름 변경 빈도를 낮게 유지한다.

## 7. 등록 및 사용 (플러그인 방식)

- 팀원은 `/plugin`에서 serial-mcp 플러그인을 설치하며(장비를 다루는 인원에 한함), user 레벨로 활성화하면 모든 코드베이스·세션에서 도구가 노출된다.
- 플러그인 설치 한 번으로 MCP 도구와 스킬이 함께 설치·활성화된다. 도구가 없는 세션에 스킬만 노출되어 존재하지 않는 도구를 참조하는 "유령 참조"가 발생하지 않는다(생명주기 동기화).
- 개인별로 상이한 값(포트, 보드레이트, tee 경로)은 plugin.json에 하드코딩하지 않고 `${SERIAL_PORT:-}` 등 환경변수 참조(빈 기본값 폴백)로 처리한다.
- 대안(마켓 미경유):
    ```
    claude mcp add --scope user serial-mcp -e SERIAL_PORT=<해당 포트> -e SERIAL_BAUD=115200 -- uvx --from git+https://github.com/JOCOIN94/silotek-serial-mcp serial-mcp
    ```
    > 이 경로는 MCP 도구만 등록되며 스킬은 포함되지 않는다. docstring이 자족적이라 도구 자체는 정상 동작하나, 스킬의 워크플로 보강은 적용되지 않는다.

## 8. README 요구사항 (팀 공유, macOS/Windows 혼용)

- 마켓에서 serial-mcp 설치 방법(및 대안 `claude mcp add --scope user`).
- 플러그인 설치 시 MCP 도구와 함께 스킬이 따라오며, `claude mcp add` 대안 경로엔 스킬이 빠진다는 주의.
- macOS/Windows 각각 환경변수 설정법(macOS `export`, Windows `setx`), 포트 확인법(`list_serial_ports` 또는 macOS `ls /dev/cu.*`, Windows 장치 관리자).
- uv·uvx 설치와 PATH(특히 Windows), COM10 이상도 `COM10` 그대로(pyserial이 `\\.\` 접두를 자동 처리), private 레포면 git 인증 필요.

## 9. 스킬 명세

**9.1 식별**
- 위치: `plugins/serial-mcp/skills/serial-debugging/SKILL.md` (MCP와 동일 플러그인에 동봉)
- 형식: 순수 마크다운. 실행 코드 없음.
- description(트리거, 항상 컨텍스트 상주 1줄): "시리얼/펌웨어 디버깅 중 장비 로그를 확인하거나, 사람이 장비를 동작시키고 그 결과 로그를 AI가 확인하는 블랙박스 시험 루프를 돌릴 때 사용한다."

**9.2 스킬이 담는 내용**
- 블랙박스 루프 절차: `clear_log_buffer` → `reset_board(port=...)` 우선 호출(승인 팝업) → 적절히 대기 → `get_recent_logs`/`query_serial_logs`로 회수 → 분석 → 코드 수정 → 반복. 필요 시 `get_log_buffer_info`로 신규 유입 확인.
- 사람 협업 프로토콜(B안에서 스킬이 가장 기여하는 부분): AI는 자동리셋 회로 보드에서는 `reset_board`로 직접 리셋을 시도한다. 사용자가 승인 거부(`status="declined"`), 클라이언트 elicitation 미지원, native-USB/미배선으로 0줄 회수 등일 때만 **사람에게 물리적 동작/리셋을 명시적으로 요청**한다. 그 요청 문구·타이밍·회수 시점을 스킬이 규정한다.
- 명령 전송 판단: `send_serial_command`는 펌웨어가 제공하는 CLI/AT/진단 명령을 알고 있을 때만 사용한다. 명령 문법 자체는 펌웨어의 책임이며, 승인 거부(`declined`)는 재시도 금지 신호로 해석한다.
- 보드 식별 절차: 별칭(`SERIAL_NAMES`/`SERIAL_AUTONAME` 산출 `name`·`label`)이 진실 — 범용 USB-UART 어댑터(CH343 등) 환경에서 VID/PID로 보드를 단정하지 않는다. idle 보드는 자동 식별이 첫 로그 유입 때 1회 확정되므로 지연될 수 있고, 모호하면 사람에게 포트↔보드 매핑을 묻는다.
- 함정·해석: 포트 점유 오류 대응(점유 프로그램 종료 요청·자동 재연결 안내), 핫플러그 스캔 지연(기본 5초 — `monitored=false`면 재조회, 고정 포트 모드는 스캔 없음), 플래싱 직후 부팅 로그 잘림(루프로 재판정), dedup 표기 `(N회 반복, …)` 해석(정밀 순서 필요 시 `SERIAL_DEDUP` 하향), tee 파일은 보조 기록, 사람이 직접 볼 땐 도구 응답의 `viewer_url`(웹 뷰어) 안내.
- 사일로텍 장비 메모: SSM/SB 명칭과 `SERIAL_AUTONAME` 규칙 예시(배포 환경 종속 참고 정보).

**9.3 스킬이 담지 않는 내용**
- 실행 코드(전부 MCP 서버 담당).
- 각 도구의 상세 시그니처·반환 구조(도구 docstring의 몫).

## 10. 웹 로그 뷰어 (localhost 전용 보조 기능)

서버가 포트를 점유하면 테라텀 등으로 사람이 로그를 볼 수 없으므로, stdlib `http.server` 기반 웹 뷰어를 내장한다(새 의존성 0). 상세 설계: `docs/superpowers/specs/2026-06-10-web-log-viewer-design.md`.

- `SERIAL_WEB` 환경변수: 기본 `8743`(켜짐). `0`/`false`/`no`/`off` → 비활성. 포트 점유 시 임시 포트로 자동 폴백, 실제 URL은 도구 응답 `viewer_url`로 보고.
- `127.0.0.1` 바인딩만(외부 접속 불가). 전 라우트 GET 읽기 전용 — 서버 상태를 바꾸는 엔드포인트 없음.
- 탭 2개: 실시간 스트림(수신 원본 — 빈 줄·필터 제외 줄 포함, tee와 동일 충실도) / 링버퍼(접힘·필터 적용 가공 뷰). 다중 포트에서는 헤더의 포트 셀렉터로 보드를 전환한다(`SSM (COM4)` 표기, 1개면 셀렉터 숨김). 셀렉터는 상태 폴링(5초)으로 서버와 동기화 — 핫플러그로 늘어난 포트를 런타임에 추가(2개째부터 셀렉터 재노출), `SERIAL_AUTONAME` 별칭이 늦게 확정되면 라벨 갱신, 포트 0개로 기동한 뒤 첫 보드가 꽂히면 새로고침 없이 스트림 자동 연결.
- 컬러: ANSI 해석 > 에러·경고 라인 틴트 > 성공 키워드 > JSON 절제 > 메타 dim ("색은 신호" 원칙). 부팅/리셋 마커(`ESP-ROM:`/`rst:0x`/`entry 0x`)는 에러와 구분되는 파란 틴트.
- 가독성 장치(2026-06-10 실사용 반영): 타임스탬프·태그 고정폭 정렬(래핑 침범 방지), 표시 시각은 초 단위(hover 시 ms), 태그 해시 고정색(클릭=해당 태그 필터), 라이브 정규식 필터, 수신 공백 2초+ 간격 구분선, 같은 초 반복 타임스탬프 흐리기, 빈 줄 시각 압축, 탭에 적재 카운터(`스트림 N/5000`·`버퍼 N/2000`), 스크롤 업 중 새 로그 배지, 글자 크기 A−/A+ 조절(11~18px, `localStorage` 영속), SSE 연결·포트 전환 시 스트림 시작 구분선(`실시간 수신 시작 — 이전 기록은 [버퍼] 탭`).
- 불변식: 뷰어 실패는 MCP 서버에 영향 없음 / 느린 브라우저가 시리얼 경로를 막지 않음(drop-oldest).

---

### 부록: 구현 상태 (2026-06-10)
- 코어 서버 스캐폴딩 완료: `ring_buffer.py`(순수 로직), `server.py`(FastMCP + 리더 스레드 + 6개 도구), `pyproject.toml`, README.
- 단위 테스트 완료(2026-06-10): pytest 56개 — `ring_buffer`(dedup·필터·ring·query·동시성, 21) + 도구 6종 계약(11) + 리더 라인처리·tee(6) + 설정 로딩(16) + 스모크(2). 코드 리뷰 보강 4개 포함(tee×필터/dedup 상호작용, query 원문 검색, status 미연결 분기). 테스트 가능성 확보를 위해 `SerialReader._ingest()`(I/O 루프에서 라인처리 분리)와 `_load_config()`/`_env_int(env,…)`(환경변수 계약)를 **동작 보존** 추출. 계획서: `docs/superpowers/plans/2026-06-09-serial-mcp-test-suite.md`.
- 실장비 검증 완료(2026-06-10): MCP stdio 클라이언트로 서버를 스폰해 6개 도구 전부 엔드투엔드 확인. 블랙박스 루프 검증 — `clear_log_buffer` → 사람이 리셋 → 부트 ROM 배너(`ESP-ROM:esp32s3-20210327`, `rst:0x1 (POWERON)`, `entry 0x403c88b8`)부터 부팅 시퀀스 전체 회수, `query_serial_logs`로 부팅 마커 17줄 매칭. 부팅 버스트(2초에 50줄+) 손실 없음, stdout 오염 없음.
- 실로그 관찰 → 조정 완료(2026-06-10): SSM 펌웨어가 메시지마다 빈 줄을 교대로 출력(`"" → [IOc] Disconnected! → "" → …`)해 dedup이 실전에서 한 번도 접지 못하던 문제를 **공백뿐인 줄 저장 제외(§4.3)**로 해결. TDD로 구현, 테스트 58개.
- 웹 로그 뷰어 구현(2026-06-10, §10): RawFeed 허브 + ViewerServer(stdlib HTTP/SSE) + 단일 페이지. 도구 응답 viewer_url 포함.
- 다중 포트 자동 모니터링 구현(2026-06-10): USB 자동 스캔·PortMonitor×N·별칭(SERIAL_NAMES)·도구 port 라우팅·뷰어 포트 셀렉터·dedup 룩백(기본 5). 설계: `docs/superpowers/specs/2026-06-10-multi-port-design.md`.
- 보드 자동 식별 구현(2026-06-10): `SERIAL_AUTONAME`(`이름=정규식;…`, 세미콜론 구분, 순서=우선순위)으로 이름 없는 포트의 수신 줄을 대조해 첫 매칭에서 1회 확정(§3). `SERIAL_NAMES` 우선·중복 이름 미부여·잘못된 정규식은 무시(서버 생존). 설계: `docs/superpowers/specs/2026-06-10-multi-port-design.md` §11.
- 배포 완료(2026-06-10): GitHub 공개 push(`JOCOIN94/silotek-serial-mcp`, uvx 원격 실행 검증), silotek-tools 마켓에 serial-mcp 플러그인 등록(plugin.json — env 10종 패스스루, SKILL.md — 베이스라인 대조 검증 거침, 0.1.0). **§부록 미완 항목 전부 해소** — 이후 변경은 main push가 곧 배포.
- 핫플러그 구현(2026-06-11): 자동 스캔 모드에서 `SERIAL_HOTPLUG` 간격(기본 5초)으로 comports() 재스캔, 신규 USB 포트를 런타임 모니터 추가. `_monitors`는 copy-on-write로 원자 교체(리더 스레드 순회와 무충돌). 모니터 조립 규칙은 `_make_monitor()`로 추출해 기동·핫플러그가 공유. 뷰어도 동조: 상태 폴링(5초)이 신규 포트를 셀렉터에 추가하고, 포트 0개 기동 후 첫 보드에 스트림 자동 연결(코드리뷰 반영). 계획서: `docs/superpowers/plans/2026-06-11-serial-hotplug.md`.
- 쓰기·리셋 구현(2026-06-11): `send_serial_command`·`reset_board` 추가, 기본 매 호출 elicitation 승인 게이트, `SERIAL_WRITE`/`SERIAL_WRITE_CONFIRM` 설정, `[TX]`/`[RST]` 감사 마커, `LineBuffer.entries_since()` 기반 응답 회수(dedup 접힘 포함). 단위 테스트 186개 통과(실장비 reset/승인 팝업 검증은 사용자 자리에서 별도 수행 필요). 계획서: `docs/plans/2026-06-11-serial-write-reset.md`.
- 테스트 장비: ESP32-S3(SSM 펌웨어), COM4(CH343), 115200.
