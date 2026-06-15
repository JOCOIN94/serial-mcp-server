# 시리얼-MCP 단일 소유권 — 8743 bind 잠금 (Path A)

> 2026-06-15 산출물(스펙 only — 구현상세는 코덱스).
> layer1(뷰어 재디자인 890fa04) + layer2(소유권 백엔드 00a7217)에 이어, **stdio 다중
> 인스턴스의 COM 포트 경합·좀비**를 구조적으로 없앤다. layer2의 **per-port `released`**
> 모델을 본 문서가 **whole-session 소유권(8743 bind 잠금)** 으로 대체·단순화한다.

## 1. 목적·배경

stdio MCP는 클라이언트(Claude Code·Codex·각 세션)당 서버를 자식 프로세스로 1개씩
띄운다. 현재 각 서버는 **기동 즉시 모든 COM 포트를 열고** 뷰어를 띄우며(8743 점유 시
임시 포트로 폴백), 세션이 백그라운드로 빠져도 종료되지 않는다. 결과:

- N개 서버가 동시에 같은 COM 포트를 잡으려 경합 → 먼저 잡은 1개만 성공, 나머지 `PermissionError`.
- 폴백 때문에 인스턴스마다 뷰어가 따로 떠 "어느 포트가 live냐" 혼란(실측: 6개 뷰어 8743/50970/…).
- 죽지 않은 인스턴스가 COM·뷰어를 계속 쥠 = 좀비. 사람이 손으로 트리 kill 해야 회복.

**핵심 통찰**: stdio엔 프로세스 간 공유 잠금이 없어 다들 시리얼을 막 잡는다. 그러나
**TCP 포트 bind는 OS가 보장하는 시스템 전역 배타 자원**이다. → **8743 bind를 소유권
잠금(baton)으로 쓰면**, 잠금을 쥔 1개만 시리얼·뷰어를 갖고 나머지는 휴면(무해)하게 만들 수 있다.

대안 **Path B(http streamable-transport 공유 서버)** 는 검토 후 반려: always-on 서버
관리·24/7 포트 점유가 "AI가 쓸 때만 켜짐" 요구와 충돌. (Claude↔Codex가 *동시에* 같은
로그를 읽어야 하는 요구가 생기면 재고. 그 경우에만 B가 우월.)

## 2. 확정 요구사항 (사용자 결정, 2026-06-15)

| 항목 | 결정 |
|---|---|
| transport | **stdio 유지**. 클라가 사용 시 자동 spawn = "사용 시 켜짐"(상시 서버·autostart·런처 불필요) |
| 소유권 잠금 | **8743 bind**. `allow_reuse_address=False`로 Windows에서도 진짜 배타. 폴백 제거 |
| 소유권 단위 | **whole-session = 전 포트 통째**. 부분(포트별) 소유 없음 → layer2 per-port `released` 폐기 |
| 획득 시점 | **lazy — 첫 시리얼 도구 호출 때**(기동 시 아님). 그 전엔 휴면(8743·COM 안 쥠) |
| 비소유 세션 도구 호출 | 8743 비었으면 획득 후 수행 / 점유 중이면 **"다른 세션이 점유 — 거기서 해제 먼저" 안내**(하드 에러 아님) |
| release | 웹 "해제" 버튼 = 8743 + 시리얼 통째 반납(뷰어 종료 포함) → 다른 세션 획득 가능 |
| **owner 종료** | **클라 끊김(stdin EOF) 시 owner는 자동 release+exit** — 좀비 owner가 전원을 막는 것 방지(make-or-break) |
| 뷰어 | 항상 8743 1개(=현 owner). 비소유·무소유 시 뷰어 없음 |
| 동시 사용 | 비범위(한 번에 한 세션, 전환은 release 핸드오프) |

## 3. 핵심 메커니즘

### 3.1 소유권 = 8743 bind (폴백 제거)
8743(또는 `SERIAL_WEB` 포트) bind 성공 == 소유권 보유. **임시 포트 폴백을 제거**한다
(폴백이 다중 뷰어·다중 grabber 공존의 원인). `allow_reuse_address`는 **반드시 끈다** —
stdlib `ThreadingHTTPServer` 기본값이 `True`라 Windows에서 두 프로세스가 같은 포트에
동시 bind될 수 있어 잠금이 깨진다.

### 3.2 lazy 획득 (첫 시리얼 사용 시)
서버는 **기동 시 COM 포트를 열지 않고 8743도 bind하지 않는다**(현재와 정반대). 휴면으로
대기하다가, 자기 클라가 **첫 시리얼 도구를 호출**할 때 획득을 시도:

1. 8743 bind 시도. 성공 → **owner 전환**: 뷰어 시작 + 전 COM 포트 reader 시작.
2. bind 실패(이미 점유) → 휴면 유지, 호출엔 "다른 세션 점유, 해제 먼저" 결과 반환.

→ 소유권이 **사용을 따라간다**(먼저 태스크 받은 세션이 owner). 아무 AI도 안 쓰면 8743·COM
모두 비어 사람이 TeraTerm으로 바로 사용 가능.

### 3.3 휴면 서버
8743 미보유 서버. 시리얼·뷰어 아무것도 안 쥠 → 좀비로 쌓여도 경합 0. 자기 클라의 도구
호출엔 §4 규칙대로 응답(획득 시도 or 안내).

### 3.4 release (웹 해제 버튼)
owner의 뷰어 세션 카드 "해제" → owner가 **COM 닫기 + 뷰어 종료 + 8743 unbind**. 8743이
풀리는 즉시 다른 휴면 서버가 다음 도구 호출에서 획득 가능. (프론트 `releaseSession`은
이미 전 포트 루프 호출 — whole-session과 정합. 백엔드가 "한 번 호출 = 통째 반납"으로 수렴.)
해제 직후 8743 뷰어는 내려가므로 그 페이지는 끊긴다(= 합의된 "뷰어 공백"). 새 owner가
획득하면 8743 재접속 시 새 뷰어.

### 3.5 owner self-exit on stdin EOF (필수)
owner의 클라가 사라지면(앱 종료·세션 닫힘 → stdio stdin EOF) owner는 **즉시 release(§3.4)
후 프로세스 종료**해야 한다. 이게 없으면 좀비 owner가 8743+COM을 영구 점유해 전 세션을
막는다(= 우리가 없애려는 원래 통증). 휴면 서버도 EOF 시 그냥 종료(쥔 게 없어 단순 exit).

## 4. 도구별 소유권 동작

| 도구 | 소유권 필요? | 비소유 시 동작 |
|---|---|---|
| `list_serial_ports` | 불요 | OS 포트 열거만. 획득 안 함 |
| `get_serial_status` | **획득-if-free** | 8743 비었으면 획득 후 상태, 점유 중이면 소유 세션 명시한 상태 보고(에러 아님). 사용자 "전체조회→권한도 생김" 요구 충족 |
| `get_recent_logs`·`query_serial_logs`·`get_log_buffer_info`·`clear_log_buffer` | 필요 | 획득 시도 → 점유 중이면 "다른 세션 점유, 해제 먼저" 결과(빈 lines) |
| `send_serial_command`·`reset_board` | 필요 | 동일. 단 획득은 **승인 게이트 통과 후**(거부된 쓰기가 소유권 가로채지 않게 — layer2 `reclaim_released=False` 패턴 계승) |

"안내" 결과 형식은 기존 에러 dict 관례(`status:"error"`, 소유 세션·해제 방법 메시지,
`count:0`, `lines:[]`)를 따른다. AI가 같은 호출 반복 말고 사람에게 해제를 요청하도록 문구 유도.

## 5. 데이터 계약·뷰어 변경

- `/api/status`(`_viewer_status_info`)는 **owner 프로세스에서만** 응답(뷰어가 owner에만 존재).
  `session`·`hw`·`board`는 layer2 그대로. **per-port `released` 필드는 제거**(whole-session이라
  owner의 포트는 전부 소유 상태). 
- `/api/release`는 **포트 단위가 아니라 세션 단위 반납**으로 의미 변경(§3.4). `port=` 인자는
  하위호환 위해 받되 전체 반납 트리거로 취급(또는 프론트가 1회 호출로 단순화 — 구현 선택).
- 프론트(layer1) 변경 최소: 세션 카드 "해제"는 그대로 전체 반납. per-port released 음영 처리
  로직은 제거 가능(휴면 서버엔 뷰어 자체가 없으므로 released 포트 표시 케이스가 사라짐).
- layer2의 `_reclaim_if_released`/per-port `released` Event/`reconnect_paused`는 **whole-session
  획득/반납 메커니즘으로 대체**. (재사용 가능한 부분은 구현 시 판단.)

## 6. 동시성·레이스

- **획득 레이스**: 두 휴면 서버가 동시에 8743 bind 시도 → OS가 1개에만 부여(배타 bind). 패자는
  "점유 중" 경로. 별도 락 불필요 — OS가 중재.
- bind→COM open 순서: bind 성공 후 reader 시작. open 실패(케이블 빠짐 등)면 owner이되 COM은
  `last_error` 표기(기존 reader 재연결 루프가 처리).
- self-exit(§3.5)와 release(§3.4)의 정리 순서 고정: COM 닫기 → 뷰어/8743 unbind → exit.

## 7. 엣지 케이스

- **`SERIAL_WEB=0`(뷰어 끔)**: 8743 bind = 잠금이라, 뷰어를 끄면 잠금 매개가 사라진다.
  → **구현 결정 필요**(§11): (a) 뷰어 비활성이어도 잠금용으로 포트는 bind(UI만 미서빙),
  (b) 별도 제어 포트/lockfile. 권고 (a) — 단일 매개 유지.
- **hotplug**: owner만 COM을 쥐므로 핫플러그 스캔도 owner에서만. 휴면 서버는 스캔 안 함.
- **idle owner**: 클라 연결은 살아 있으나 시리얼을 안 쓰는 owner는 소유권 유지(EOF·release 전까지).
  전환하려면 release. (합의됨.)
- **뷰어 공백**: release~다음 획득 사이 8743 무소유 구간 — 뷰어 일시 부재(합의됨).

## 8. 테스트 전략 (기존 스위트에 추가)

- 8743 bind 성공/실패에 따른 owner/휴면 분기(가짜 bind로 점유 시뮬).
- lazy 획득: 휴면 상태에서 첫 시리얼 도구 호출 시 bind+COM open 발생, 기동 시엔 미발생.
- 점유 중 도구 호출 → "해제 먼저" 안내 결과(COM 미접근).
- `get_serial_status`: 8743 free면 획득, 점유면 보고-only.
- release → COM close + 8743 unbind + 다른 서버 획득 가능.
- **stdin EOF → owner release+exit**(가짜 EOF 신호). 휴면 EOF → 단순 exit.
- 회귀: layer2에서 per-port released에 묶인 테스트는 whole-session으로 갱신/대체.

## 9. 문서 영향

- `SPEC.md` §10: 뷰어 포트 폴백 제거·8743=소유권 잠금·whole-session 모델 반영.
- `2026-06-10-web-log-viewer-design.md`: 포트 폴백 기술 갱신(폴백 → 잠금 실패=휴면).
- 본 layer2 스펙(`2026-06-15-viewer-ownership-backend.md`): per-port `released`가 본 문서로
  대체됨을 상단 주석.
- `README.md`: "해제=세션 통째 양보, 다음 사용 세션이 자동 획득, 클라 종료 시 자동 반납".
- `__version__` 메이저 범프(소유권 모델 breaking). 마켓플레이스 plugin.json은 env 신설 없으면 무변경.

## 10. 비범위 (YAGNI)

- **Claude↔Codex 동시 사용**(공유 읽기) — Path B 영역. 본 설계는 한 번에 한 세션.
- **부분(포트별) 소유/양보** — whole-session 결정으로 제외.
- **재점유 버튼** — 획득은 도구 호출(사용)이 트리거. 별도 UI 불요(사용자 결정).
- **인증·외부 접속** — 127.0.0.1 한정(계승).
- **자동 takeover**(새 세션이 이전 owner를 강제 인수) — self-exit + lazy 획득으로 충분. 락파일
  기반 강제 인수는 보류.

## 11. 구현상세 — 코덱스 결정 사항

스펙은 "무엇/왜"까지. 아래는 구현에서 정한다:

1. **stdin EOF 감지 훅**(§3.5) — FastMCP stdio 루프 종료/시그널에서 release+exit 거는 정확한 지점.
2. **lazy 획득 전환 구현** — 기동 시 미오픈, 첫 도구에서 bind+reader 시작하는 코드 구조(현재
   `main()` 기동 오픈을 도구 진입 경로로 이전).
3. **`allow_reuse_address=False` 적용** 및 OS별 bind 예외 처리(EADDRINUSE 분기).
4. **`SERIAL_WEB=0`일 때 잠금 매개**(§7) — 권고 (a)(미서빙 bind) vs (b)(lockfile) 택1.
5. **소유 세션 식별 노출** — 점유 중 안내 메시지에 owner 세션명(다른 프로세스라 직접 못 읽음 —
   8743 응답 prove or 표기 생략 등) 표시 방법.
6. layer2 per-port `released`/`_reclaim_if_released`/`reconnect_paused` **재사용 vs 제거** 범위.

구현 결정(2026-06-15):

1. stdin EOF는 `main()`의 `mcp.run()` `finally`에서 `_release_owner("stdio 종료")`로 정리한다. 보조 안전망으로 `atexit`에서도 idempotent release를 호출한다.
2. `main()`은 설정만 로드하고 휴면으로 stdio를 시작한다. `get_serial_status`와 시리얼 로그/버퍼 도구 진입부가 `_ensure_owner()`를 호출해 첫 사용 시 bind+reader를 시작한다.
3. `ViewerServer.start()`는 지정 포트 단일 bind만 시도하고 폴백하지 않는다. `allow_reuse_address=False`를 유지한다.
4. `SERIAL_WEB=0`은 권고 (a)를 채택했다. UI는 미서빙하지만 owner 잠금용 8743 bind는 유지한다.
5. 점유 중 세션 식별은 `http://127.0.0.1:{port}/api/status`를 짧게 조회해 `session`을 얻으면 안내에 포함하고, 실패하면 owner URL만 표시한다.
6. layer2 per-port `released`, `_reclaim_if_released`, `reconnect_paused`는 제거했다. `/api/status`에 per-port `released` 필드는 없다.
