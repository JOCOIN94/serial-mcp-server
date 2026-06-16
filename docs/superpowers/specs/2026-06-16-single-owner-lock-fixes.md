# 단일 owner 포트잠금 — 리뷰 후속 수정 (63b0ba4 → fixes)

> 2026-06-16 산출물(스펙 only — 구현=Codex). main@63b0ba4(v1.0.0) 멀티에이전트 리뷰
> 결과 중 **합의된 항목만** 담는다. 기반 설계: `2026-06-15-single-owner-port-lock.md`.
> 합의: Medium 3건 + Low 2건은 이번에 수정, 나머지 Low/nit은 후속(§3)으로 명시 보류.

## 1. Medium (이번 수정)

### 1.1 쓰기 도구 — 승인 게이트가 포트 검증보다 먼저 도는 회귀
- **위치**: `server.py` `send_serial_command`(964 부근 `_confirm_write` 선행), `reset_board`(동일 패턴).
- **현상**: 현재 순서가 `_confirm_write`(승인 UI) → `_ensure_owner` → `_resolve_port`. `SERIAL_WRITE_CONFIRM=true`(README 기본)에서 **없는 포트/모호한 포트(다중 포트 미지정)에도 승인 팝업이 먼저 뜨고**, 승인해야 비로소 `unknown port`/ambiguous 에러가 난다. 승인 메시지도 해석 라벨을 잃고 `"기본/단일 포트"`로 퇴화.
- **수정 방향(합의)**: 순서를 **owner 획득 → 포트 resolve → 승인 → (승인 거부 시) 이번 호출에서 *새로* 잡은 owner면 release** 로 바꾼다.
  1. `_ensure_owner(ctx)` — busy면 즉시 반환(승인 팝업 없음).
  2. `_resolve_port(port)` — err면 즉시 반환(승인 팝업 없음). resolve된 `mon.label`로 정확한 승인 메시지 복원.
  3. `_confirm_write(...)` — 승인.
  4. 승인 **거부** 시: 이번 호출에서 새로 owner를 잡았다면 `_release_owner` 로 되돌린다(거부된 쓰기가 owner를 잡는 부작용 방지 — 기존 `reclaim_released=False` 의도 보존).
- **구현 주의(Codex)**: "이번 호출에서 새로 잡았는지" 판별이 필요. `_ensure_owner` 호출 직전 `was_owner = _owner_active or bool(_monitors)` 캡처 → `not was_owner` 이고 거부면 release. (또는 `_ensure_owner`가 acquired 여부를 반환하도록 시그니처 확장.) 이미 owner면 churn 없음 — release-on-decline은 "첫 쓰기가 거부된" 드문 경로에만 발생.
- **테스트**: `ctx`에 `write_confirm=True` 주입 상태로 ①없는 포트 ②다중 포트 미지정 → **승인 호출 없이** 에러 반환 단언(_confirm_write가 안 불렸음을 spy로). ③거부(decline) + 비-owner 시작 → 호출 후 `_owner_active is False`(새로 잡은 owner 반납) 단언. ④이미 owner + 거부 → owner 유지 단언.

### 1.2 make-or-break(stdin EOF → release+exit) 테스트 전무
- **위치**: `tests/test_single_owner.py`(해당 테스트 부재), 대상 배선 `main()`의 `finally: _release_owner(...)`(1353 부근) + `atexit`(588 부근).
- **현상**: 단일-owner 설계의 핵심인 self-exit 경로가 회귀 테스트 0건(현재는 `_release_owner` 직접호출만 검증). mcp 업그레이드로 EOF→`mcp.run()` 반환 의미가 바뀌면 좀비가 조용히 재발해도 스위트가 못 잡음.
- **수정 방향**: `mcp.run`을 monkeypatch해 main()의 finally 발화를 검증.
- **테스트**: ①`srv.mcp.run`을 즉시 반환(또는 예외)하도록 monkeypatch → owner 상태를 만들어 둔 뒤 `main()` 호출 → `_release_owner`가 호출돼 `_owner_active is False`/`_monitors=={}`/`_viewer is None` 단언(예외 경로에서도 finally가 release하는지 포함). ②atexit idempotency: `_release_owner` 2회 연속 호출 → 2번째 no-op(예외·중복정리 없음) 단언. ③휴면(미획득) 상태 EOF → release가 무해 no-op(쥔 것 없음) 단언.

### 1.3 release가 hotplug 스레드를 join 안 함 → 좁은 TOCTOU
- **위치**: `server.py` `_release_owner_locked`(552 부근) — `_hotplug_stop.set()`만 하고 `_hotplug_thread` join 없이 `_monitors` 정리.
- **현상**: stop 이벤트 set 직후 아직 살아있는 스캔 스레드가 release/재획득 사이에 `_monitors`/reader를 건드릴 수 있는 단일-작성자 불변식 위반 창(로컬 단일사용자라 확률 낮음).
- **수정 방향(합의: join)**: release 시 `_hotplug_thread`를 **join**(작은 timeout)한 뒤 `_monitors`를 정리한다.
- **구현 주의(Codex)**: 락 순서 데드락 점검 — `_hotplug_scan_once`가 `_owner_lock`을 잡지 않음을 확인(현재 `_autoname_lock`만 사용). 만약 잡는다면 join을 `_owner_lock` 밖으로 빼거나 generation-guard로 대체. join은 daemon 스레드라 timeout 후 진행해도 안전.
- **테스트**: release 후 hotplug 스레드가 살아있지 않음(`_hotplug_thread is None` 또는 not alive) 단언. 가능하면 "release 직후 stale 스캔이 `_monitors`를 못 되살린다" 회귀 케이스.

## 2. Low (이번 수정 — 저비용)

### 2.1 플러그인 문서: `SERIAL_WEB=0` 의미 변경 미반영 (교차 repo)
- **위치**: `silotek-plugin-marketplace`의 serial-mcp 플러그인 README/SKILL(필요 시 CHANGELOG).
- **현상**: 서버 v1.0.0에서 `SERIAL_WEB=0`이 **"뷰어 비활성"→"UI만 끄고 8743 잠금 bind는 유지"** 로 바뀌었는데 플러그인 문서 미반영.
- **수정**: 해당 env 설명 한 줄 갱신("0 = 웹 UI 끔, 단 8743 소유권 잠금은 유지"). 매니페스트 3종은 git URL 핀이라 버전 강제 동기 불요 — 플러그인 버전 범프는 선택(문서 변경이면 패치).

### 2.2 lock-only 모드 busy 메시지가 죽은 "웹뷰어" 링크 노출
- **위치**: `server.py` `_owner_busy_result`/`_probe_owner_info`(390~421 부근).
- **현상**: owner가 lock-only(SERIAL_WEB=0)면 `/api/status` 프로브가 실패하는데, busy 메시지는 항상 `"웹뷰어: http://127.0.0.1:8743"` 를 하드코딩 — 그 모드엔 UI가 없어 죽은 링크.
- **수정 방향**: **프로브가 실제로 도달(200 응답)했을 때만** "웹뷰어: {url}" 라인을 포함한다. 도달 실패 시(=owner가 lock-only이거나 미응답)에는 링크 대신 "owner 세션을 종료하거나 그 세션 뷰어에서 해제하세요" 식의 링크-없는 문구로 분기. (`_probe_owner_info`가 `reachable` 여부를 분리 반환하도록.)
- **테스트**: lock-only owner(또는 미응답 포트) 점유 시 busy 메시지에 `http://` 링크가 **없음** 단언 + UI owner 점유 시 링크 **있음** 단언.

## 3. 후속(이번 범위 밖 — 명시 보류)

지금 막을 이슈 아님, 후속 개선으로 충분(합의):
- 런타임 중 8743 상실 시 self-heal/재검증 부재(`_ensure_owner`가 `_owner_active`만으로 owner 단정).
- async 쓰기 도구가 동기 `_ensure_owner`(bind·urlopen)로 이벤트루프 단기 블록.
- 웹 release 후 뷰어 페이지 stale·"연결 끊김" 미표시(frozen-but-interactive).
- viewer bind 경로 `SO_EXCLUSIVEADDRUSE` 비대칭(127.0.0.1 위협모델 밖).
- lock-only **성공** 경로(`_bind_lock_socket` bind→release) 테스트 공백 / monitors=0 owner가 잠금 점유 / `_owner_busy_result(for_status=)` 데드코드.
- backgrounded-but-alive owner(파이프 미EOF) 잔재 — 설계상 한계, 웹 해제로 완화(필요 시 stdin 워치독은 후속).

## 4. 문서·버전

- 위 §2.1 외 서버측 사용자 문구 변화 없음. 코드 수정이므로 `__version__` **patch 범프(1.0.0 → 1.0.1)** + uv.lock 동기.
- 본 문서 하단에 Codex가 구현 결정/완료를 기록.

## 5. Codex 구현 기록

- 2026-06-16: §1 Medium 3건과 §2 Low 2건만 구현. §3 항목은 보류 유지.
- 쓰기 도구는 owner 획득 후 포트 resolve를 먼저 수행하고, 승인 차단 시 이번 호출에서 새로 획득한 owner만 반납한다.
- busy 안내는 `/api/status` 프로브가 실제 도달한 경우에만 웹뷰어 링크를 포함하고, lock-only/미응답 owner에는 링크 없는 해제 안내를 반환한다.
- release는 hotplug 스레드 stop 이벤트 설정 후 join을 시도한 뒤 monitor/viewer/lock을 정리한다.
- `main()`의 stdio 종료 finally와 release idempotency 회귀 테스트를 추가했다.
