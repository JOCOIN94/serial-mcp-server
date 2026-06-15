# 웹 뷰어 소유권 백엔드 설계 (층2 — SPEC §10 증보)

> 2026-06-15 산출물. 층1(프론트 재디자인, 커밋 890fa04)에 이어 좌측 소유권 보드의
> 백엔드를 채운다. `2026-06-10-web-log-viewer-design.md`(v1 뷰어)는 단일 포트·읽기 전용
> 설계라 소유권/세션/release를 §9 비범위로 제외했다 — 본 문서가 그 확장을 정의한다.

## 1. 목적·배경

층1에서 좌측 nav를 [AI 세션 카드] + [H.W 유닛 박스]로 재디자인했으나, 백엔드가
세션·hw/board·release를 제공하지 않아 **degraded**로 동작한다:

- 세션 카드는 항상 "세션 대기 중" placeholder, 해제 버튼 비활성(`web_viewer.py` `buildSession`).
- hw/board는 서버가 안 주므로 프론트가 `label` 별칭에서 역추론(`unitOf`/`boardOf`).
- `/api/release`는 미구현(404), `releaseSession`은 평소 미호출.

코드의 `TODO(codex)` 3곳이 정확한 소비 지점이다. 본 설계는 이 셋을 서버에서 채운다.
근본 동기는 [lifecycle 통증] — stdio라 클라이언트당 서버 1개가 뜨고, 여러 세션이 같은
COM 포트를 경합한다(시리얼 OS 독점). 먼저 잡은 쪽이 임자 → 나중 세션 `PermissionError`.
사람이 TeraTerm로 보려 해도 서버가 포트를 쥐고 있으면 못 연다. **release는 이 경합을
사용자가 직접 끊는 수단**이다.

## 2. 확정 요구사항 (이번 세션 결정)

| 항목 | 결정 |
|---|---|
| release 의미론 | **영구 양보 + 재개**. 해제하면 그 포트 재연결을 억제해 사람·타 세션이 점유. 이 세션이 그 포트로 MCP 도구를 다시 호출하면 자동 재점유 |
| 소유권 단위 | 세션 = 서버 인스턴스 = 모든 포트 통째(층1 board.js 모델). "해제"는 그 세션의 전 포트를 양보 |
| session 출처 | MCP `clientInfo`(initialize 핸드셰이크) — 자동, 설정 불요. 캡처 전엔 None(degraded 유지) |
| hw/board 출처 | `m.name` 별칭을 `-`로 split(`"SB-STM"`→hw `SB`·board `STM`). 별칭 없으면 둘 다 None |
| 작업 주체 | **미정** — 본 명세 확정 후 결정(직접 구현 vs codex:rescue 위임) |

## 3. 데이터 계약 변경 (`/api/status`)

`_viewer_status_info()`(server.py:771) 반환을 확장한다. 기존 필드는 무변경.

```jsonc
{
  "session": "claude-code" | null,   // 신설: 최상위. clientInfo.name, 미캡처 시 null
  "ports": [{
    "port": "COM8", "label": "SB-STM (COM8)",
    "connected": true, "baud": 115200, "last_error": null,
    "buffer_entries": 0, "buffer_capacity": 2000,
    "hw": "SB" | null,               // 신설: m.name 별칭의 유닛부
    "board": "STM" | null,           // 신설: m.name 별칭의 칩부
    "released": false                // 신설: release로 양보된 상태인지(프론트 회색 처리·재점유 단서)
  }]
}
```

신설 라우트 — **상태 변경 엔드포인트(서버 최초)**:

```
GET /api/release?port=COM8   → {"status":"ok","port":"COM8","released":true}
                               | {"status":"error","message":"unknown port"} (404)
```

프론트(`releaseSession`, web_viewer.py:1186)는 세션의 전 포트에 대해 이 GET을 루프 호출한다.

## 4. 메커니즘

### 4.1 hw/board 파생 (TODO #1)

`_viewer_status_info`의 plist 항목에서 `m.name`을 split. 프론트 `unitOf`/`boardOf`와
동일 규칙을 서버로 승격(`p.hw`/`p.board`가 추론보다 우선이라 자동으로 덮인다).
- 실익은 추론 로직 단일화이며, 출처가 같은 별칭이라 **결과 자체는 기존 추론과 동일**하다
  (별칭 없는 포트는 여전히 null). board를 칩 풀네임(`STM32F4`)으로 격상하는 것은
  autoname/FWVER 출처가 필요해 **본 설계 범위 밖**(§7).

### 4.2 session 캡처 (TODO #2)

`_viewer_status_info`는 HTTP 스레드에서 호출되므로 MCP 요청 컨텍스트(`ctx`)가 없다.
→ **clientInfo를 첫 도구 호출 시 전역에 1회 캡처**한다.

- 모든 도구가 `ctx: Optional[Context]`를 받게 하고(읽기 도구는 현재 미수신 — 시그니처 확장),
  첫 호출에서 `ctx`의 `client_params.clientInfo.name`을 전역 `_session_label`에 저장.
- 캡처 전(도구 호출 0회)에는 None → 프론트 degraded placeholder 유지(설계대로).
- FastMCP에서 clientInfo 접근 경로는 구현 시 확정(`ctx.session.client_params` 계열).
  실패 시 fallback은 §8 미결.

### 4.3 release — 영구 양보 (TODO #3)

`PortMonitor`에 `released: bool`(또는 `threading.Event`) 추가. `/api/release` 콜백이:

1. `mon.reader.force_disconnect("뷰어 release — 사용자 양보")` — OS 핸들 즉시 해제.
2. `mon.released = True` — **재연결 억제 플래그 ON**.

억제 플래그를 두 재연결 경로가 존중해야 한다(현재는 양쪽 다 무조건 재연결):
- **SerialReader 재시도 루프**: `released`면 재연결 시도를 건너뛰고 대기 유지.
- **`_hotplug_scan_once` 좀비 해제/추가**: `released` 포트는 force_disconnect·absent 카운팅 대상에서 제외(이미 의도적 해제).

> 플래그가 없으면 force_disconnect 후 ~3초 만에 리더 루프가 도로 잡아 경합이 재발한다
> (`force_disconnect` docstring: "재연결은 기존 리더 루프가 맡는다"). 이 억제가 설계의 핵심.

### 4.4 재개 (자동)

`released` 포트로 이 세션이 MCP 도구를 호출하면 `_resolve_port`(또는 도구 진입부)에서
`mon.released = False`로 풀어 리더 루프가 자동 재연결하게 한다. 모니터는 release 중에도
제거되지 않으므로(버퍼·feed 보존) resolve는 정상 동작한다.
- **세분화 미결**(§8): 읽기 폴링까지 재개 트리거로 보면 양보가 쉽게 무효화된다 →
  쓰기 도구(send/reset)에만 한정할지, 웹 "재점유" 버튼을 별도로 둘지 결정 필요.

### 4.5 스레드 안전

- HTTP 스레드: 플래그 set + force_disconnect(이미 `_ser_lock` 보유). 리더/핫플러그 스레드: 플래그 read.
- `released`는 단일 bool read/write라 GIL로 원자적이나, force_disconnect와의 순서를 위해
  set→disconnect 순서 고정. `threading.Event`면 의도가 더 분명(`is_set`/`set`/`clear`).
- `_session_label` 전역은 첫 캡처 1회 write 후 read-only에 가까움 — 단순 모듈 전역으로 충분.

### 4.6 읽기 전용 불변식 처리 (중대)

`web_viewer.py` 모듈 docstring·SPEC §10·06-10 설계 §4.3가 **"라우트 전부 GET, 서버 상태를
바꾸는 엔드포인트 없음"을 불변식**으로 명문화했다. `/api/release`는 이를 깨는 첫 쓰기 경로다.

- **결정(기본)**: GET 유지. 근거 — 127.0.0.1 단일 사용자 로컬 뷰어, 프론트가 이미 GET 호출,
  멱등(같은 포트 반복 release는 동일 결과). 단 **불변식 문구를 "조회는 GET 읽기 전용,
  소유권 제어(release/재점유)만 명시적 예외"로 갱신**하고 그 사유를 SPEC/06-10/모듈 docstring에 반영.
- 대안(POST 전환)은 REST 관례엔 맞으나 프론트 수정까지 동반 — 로컬 뷰어 가치 대비 과함. 미채택.

## 5. 테스트 전략 (기존 스위트에 추가)

- `test_web_viewer.py`: `/api/release?port=` 200/404 계약, `/api/status`의 `session`·`hw`/`board`·s
  `released` 필드 존재. (마커는 층1에서 `portboard`/`tabStream`로 갱신됨)
- `test_server.py`(또는 해당): release → `force_disconnect` 호출 + `released=True`, 재개 →
  `released=False`. 리더 재시도 루프가 `released`를 존중하는지(가짜 리더로).
- `_hotplug_scan_once`가 `released` 포트를 좀비 해제·absent 카운팅에서 제외하는지.
- session 캡처: 가짜 `ctx`로 첫 호출 후 `_viewer_status_info().session` 반영.
- 회귀: 기존 9/9(test_web_viewer) + 전체 스위트 유지.

## 6. 문서 영향 (구현 시 동시 갱신)

- `SPEC.md` §10: 읽기 전용 불변식에 release/재점유 예외 명문화. 소유권 보드·세션 모델 한 줄.
- `2026-06-10-web-log-viewer-design.md`: §4.3 라우트 표에 `/api/release` 추가 또는 "층2에서
  확장됨" 주석(본 문서 참조). §9 비범위의 "다중 포트 동시 뷰" 항목은 이미 구현으로 초과됨을 명기.
- `README.md`: 웹 뷰어 절에 "해제(release) = 포트 양보, 도구 재호출 시 자동 재점유" 설명.
- `web_viewer.py` 모듈 docstring(line 7): "라우트 전부 GET 읽기 전용" → 예외 명시.
- serial-mcp-server `__version__` 범프(0.4.0 → 0.5.0, 계약 확장). 마켓플레이스 plugin.json은
  env 신설이 없으면 무변경(session=clientInfo라 환경변수 추가 불요).

## 7. 비범위 (YAGNI)

- board 칩 풀네임 식별(`STM32F4`) — 별칭 split이 주는 `STM32`으로 충분. autoname/FWVER 출처는 별건.
- 다중 세션 동시 표시 — stdio 단일 클라이언트라 한 서버 = 한 세션. 타 세션 점유는 그 세션의 뷰어에서.
- release 인증·권한 — 127.0.0.1 로컬 한정으로 충분(06-10 §9 계승).
- 서버 프로세스 종료(좀비 kill) — release(포트 양보)와 별개 사안. [lifecycle]의 "터미널 vs
  웹 종료 버튼" 택일은 본 설계와 분리해 추후 결정.
