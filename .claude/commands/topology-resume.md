---
description: serial-mcp 토폴로지 Phase B/C를 다음 미완 모듈부터 자율로 이어서 구현(TDD→적대 워크플로 리뷰→커밋)
argument-hint: "[시작 모듈 번호, 예: 4]"
---

serial-mcp 웹 뷰어 **토폴로지 Phase B/C**를 자율로 이어서 작업한다. 승인 없이 모듈별로 끝까지 진행한다.

## 0. 상태 파악 (먼저)
1. 계획서를 읽는다(레포 정본): `docs/plans/topology-phase-b.md`
   (없으면 `C:\Users\User\.claude\plans\gleaming-seeking-anchor.md`). §6 자료구조·§7 모듈 설계·§9 안전·§14 픽스처가 기준.
2. `git -C C:\Users\User\projects\serial-mcp-server log --oneline -8` 로 마지막 "Phase B 모듈N" 커밋을 확인해 **다음 미완 모듈**을 정한다.
   인자($ARGUMENTS)로 시작 모듈이 주어지면 그걸 우선한다.
3. 펌웨어 근거는 **cbm(codebase-memory-mcp) 프로젝트 'C-Users-User-projects-firmware-src'** 로 검증한다(거대 .ino grep 금지). 기존 모듈(topology.py·topology_events.py·topology_correlator.py)의 패턴·자료구조를 따른다.

## 1. 모듈 순서
4 routing(Rt 토큰맵·`[Route] Link` 링크그래프·RSSI ladder) → 5 roster(standalone 그룹·edges, build_roster 확장) → 6 engine(server.py: `SerialReader._run` observe 탭·bootstrap INFO(SSM 한정·서버발신·boot-window guard)·sweep 타이머·홉 feed) → 7 routes(`/api/topology`·`/api/topology/stream` SSE) → 8 front(web_viewer.py 홉 애니메이션·디테일 패널) → Phase C(`get_topology` MCP 도구, SPEC §5·serial 스킬 동기화).

## 2. 모듈별 루프 (각 모듈 반복)
1. **TDD**: 실패 테스트부터 작성(`tests/test_*.py`), 그 다음 구현. 실측 픽스처는 계획서 §14.
2. **그린 확인**: `cd C:\Users\User\projects\serial-mcp-server; uv run python -m pytest -q` (이 PC는 `uv run pytest` 가 깨지므로 반드시 `uv run python -m pytest`). 문법은 `py -m compileall -q src`.
3. **적대 리뷰 — Workflow 도구로 실행**(필수): 다중 렌즈(정확성/펌웨어정합/회귀/커버리지/설계) 병렬 리뷰 → 각 발견을 적대적으로 검증 → **확정 문제만** 반환하는 워크플로를 author·실행한다. 컨텍스트·세션 한도가 빡빡하면 2~3 렌즈 린 버전으로.
4. **수정**: 확정 문제를 모두 반영하고 다시 그린 확인. (없으면 다음 단계)
5. **커밋**: 모듈 단위로 한국어 Conventional Commits(`feat: ...(Phase B 모듈N)`), 끝에 `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`. **push 는 사용자가 시킬 때만.**
6. memento 진행 파편(serial-mcp topology 진행)을 amend 로 갱신하고 다음 모듈로.

## 3. 원칙
- 클라이언트 파리티·읽기전용 불변식·관측 비차단·stdout 금지(§9, AGENTS.md) 준수.
- 컨텍스트가 ~70%를 넘으면 현재 모듈을 안전히 커밋한 뒤 핸드오프 상태(계획서 배너·memento)를 갱신하고 멈춘다(반쯤 된 통합 코드 남기지 말 것).
- 막히거나 (A)코드 드리프트인지 (B)설계 변경인지 애매하면 그때만 사용자에게 확인(AGENTS.md 문서드리프트 규칙).

$ARGUMENTS
