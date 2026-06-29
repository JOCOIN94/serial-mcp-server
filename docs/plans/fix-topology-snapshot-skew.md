# get_topology roster ↔ recent_hops 비원자 스냅샷 skew 수정 계획

> ✅ **완료**(2026-06-29) — Codex `8499dac` 구현·검증 통과. 시점 기록으로 동결(본문 소급수정 안 함).
>
> 자족적 작업 계획서(맥락 캡슐). 대화 맥락 없이 이 문서만으로 구현 가능하게 작성했다.
> 출처: serial-mcp 토폴로지 Phase B/C code-review(Workflow) **확정 발견 #3**.

## 1. 문제 (무엇이 잘못됐나)

`get_topology` MCP 도구가 로스터와 최근 홉을 **서로 다른 시점의 두 Lock 세션**으로 읽어, 둘이
일관되지 않은 스냅샷을 반환할 수 있다. 그 사이(T0→T1)에 리더/sweep가 홉을 방출하면, 그 홉은
`recent_hops`에는 있는데 `roster`의 routing 스냅샷(T0에 frozen)에는 그 홉의 링크(edge)가
없어서, 소비자(AI·뷰어)가 "이 홉 경로의 링크가 로스터에 없다 = 경로 미완"으로 **일시 오진**한다.
다음 호출에 자가치유되지만, 단일 호출 결과의 내부 정합성이 깨진다.

### 현재 코드 (확인된 사실)

`src/serial_mcp/server.py` `get_topology`(약 1047~1079):
```python
    roster = _viewer_topology_info()           # ← 내부에서 eng.roster() = Lock #1 (routing 스냅샷 T0)
    eng = _topology_engine
    try:
        recent_hops = eng.recent_hops(20) if eng is not None else []   # ← Lock #2 (hops 스냅샷 T1)
    except Exception as e:
        ...
        recent_hops = []
```

`src/serial_mcp/topology_engine.py`(이미 존재, 변경 대상):
```python
    def roster(self, entries, now=None):
        with self._lock:
            snap = _RoutingSnapshot(self._routing.tokens(), self._routing.edges(now))
        return build_roster(entries, routing=snap, now=now)   # build_roster 는 Lock 밖(관측 비차단)

    def recent_hops(self, n=20):
        with self._lock:
            if n <= 0:
                return []
            return list(self._hops)[-n:]
```

### 왜 단일 Lock이면 일관되나 (근본 근거)

`topology_engine.py`의 `_drain`은 한 이벤트당 **같은 Lock 안에서** `self._routing.observe(ev)`
(routing 갱신) → `self._correlator.observe(ev)`(홉 방출 후 `self._hops`에 적재)를 순차 수행한다.
따라서 어떤 홉이 `self._hops`에 들어간 시점에는 그 홉의 routing 링크도 이미 반영돼 있다.
즉 **routing 스냅샷과 hops를 한 Lock 세션에서 함께 뜨면 둘은 인과적으로 일관**한다. 현재 버그는
순전히 두 스냅샷을 별도 Lock(T0, T1)으로 떠서 생긴다.

## 2. 수정 설계 (3개 변경)

### 2-1. `topology_engine.py` — 원자 스냅샷 메서드 추가

`TopologyEngine`에 메서드를 추가한다. **routing 스냅샷 + hops를 한 Lock 세션에서 캡처**하고,
CPU 무거운 `build_roster`(포트별 정규식 분류)는 **Lock 밖**에서 수행한다(관측 비차단 불변식 유지 —
리더 스레드 observe가 뷰어 폴링에 막히면 안 됨).

```python
    def roster_and_recent_hops(self, entries, now=None, n=20):
        """로스터용 routing 스냅샷과 recent_hops 를 한 Lock 세션에서 원자 캡처해 (roster, hops) 반환.

        roster 의 routing 상태와 hops 가 같은 시점이라 'recent_hops 의 홉이 가리키는 링크가
        roster.edges 에 없다'는 skew 오진을 막는다(_drain 이 routing.observe→correlator 를 같은
        Lock 에서 하므로 인과 일관). build_roster(CPU)는 Lock 밖에서 수행(관측 비차단).
        """
        with self._lock:
            snap = _RoutingSnapshot(self._routing.tokens(), self._routing.edges(now))
            hops = [] if n <= 0 else list(self._hops)[-n:]
        return build_roster(entries, routing=snap, now=now), hops
```

기존 `roster()`는 중복을 없애기 위해 이 메서드에 위임한다(동작 동일, n=0이라 hops 미수집):
```python
    def roster(self, entries, now=None):
        roster, _ = self.roster_and_recent_hops(entries, now=now, n=0)
        return roster
```
`recent_hops()`는 **그대로 둔다**(get_recent 단독 조회·기존 테스트가 사용).

### 2-2. `server.py` — entries 조립 헬퍼 분리

`_viewer_topology_info`의 entries 조립 로직을 작은 헬퍼로 뽑아 `get_topology`와 공유한다(중복 제거).
예외 격리는 호출부가 담당하므로 헬퍼는 단순 조립만 한다.

```python
def _topology_entries() -> list:
    """모니터별 (port, alias, 최근 300줄, connected) entries 조립 — 로스터 분류 입력."""
    entries = []
    for m in _monitors.values():
        r = m.reader
        entries.append({
            "port": m.port,
            "alias": m.name,
            "lines": m.buffer.get_recent(300),
            "connected": bool(r and r.connected),
        })
    return entries
```

`_viewer_topology_info`는 이 헬퍼를 쓰도록 정리한다(동작 불변, try/except 폴백 유지):
```python
def _viewer_topology_info() -> dict:
    """... (docstring 유지) ..."""
    try:
        entries = _topology_entries()
        eng = _topology_engine
        if eng is not None:
            return eng.roster(entries, now=time.monotonic())
        return build_roster(entries)
    except Exception as e:   # noqa: BLE001 - 뷰어 보조기능: 어떤 실패도 코어로 전파 금지
        _log(f"토폴로지 로스터 생성 실패: {e}")
        return {"groups": [], "unplaced": []}
```

### 2-3. `server.py` — `get_topology`가 원자 스냅샷 사용

`get_topology`의 busy 폴백·반환 스키마·읽기전용·status 문자열은 **그대로 유지**하고, roster+hops
획득만 원자 메서드로 바꾼다. 엔진 None·예외는 안전 폴백(빈 로스터·빈 hops).

```python
    busy = _ensure_owner(ctx)
    if busy:
        return {**busy, "roster": {"groups": [], "unplaced": []}, "recent_hops": []}

    eng = _topology_engine
    try:
        if eng is not None:
            roster, recent_hops = eng.roster_and_recent_hops(
                _topology_entries(), now=time.monotonic(), n=20)
        else:
            roster, recent_hops = build_roster(_topology_entries()), []
    except Exception as e:  # noqa: BLE001 - 토폴로지 보조 조회 실패는 빈 스냅샷으로 격리
        _log(f"topology 스냅샷 생성 실패: {e!r}")
        roster, recent_hops = {"groups": [], "unplaced": []}, []

    groups = roster.get("groups", []) if isinstance(roster, dict) else []
    return {
        "status": "ok",
        "message": f"토폴로지 그룹 {len(groups)}개, 최근 홉 {len(recent_hops)}개",
        "roster": roster,
        "recent_hops": recent_hops,
        "viewer_url": _viewer_url(),
    }
```

> 주의: 이 변경으로 `get_topology`는 더 이상 `_viewer_topology_info()`를 호출하지 않는다(직접 원자
> 메서드 사용). `_viewer_topology_info`는 `/api/topology` 폴러 전용으로 남는다(roster만 필요).

## 3. TDD (실패 테스트 먼저)

`tests/test_topology_engine.py`에 추가:
1. **원자 스냅샷 반환·동치**: `roster_and_recent_hops(entries, now, n)`가 `(roster, hops)` 튜플을
   반환하고, 같은 입력에서 `roster`는 `roster(entries, now)`와, `hops`는 `recent_hops(n)`와 동치인지.
2. **인과 일관성**: 홉을 하나 observe→flush 시켜 routing edge와 hop이 함께 생긴 상태에서,
   `roster_and_recent_hops`가 반환한 `hops`의 홉이 존재하면 그 홉이 가리키는 링크가 반환한
   `roster`에도 반영돼 있는지(둘이 같은 시점 스냅샷이라 한쪽에만 있지 않음). 실측 픽스처는
   기존 `SSM_RX_BLOCK`(REPRSSI 포함) 사용 — webtx 가 routing edge 를, rx 가 hop 을 만든다.
3. **n<=0 경계**: `roster_and_recent_hops(entries, now, n=0)` 의 hops 가 `[]`.
4. **`roster()` 회귀**: 기존 `roster()` 동작(위임 후)이 변하지 않는지 — 기존 roster 테스트 그린 유지.

`tests/test_tools.py`에 추가/유지:
5. `get_topology`가 roster·recent_hops 키를 반환하고 busy 폴백 스키마가 유지되는지(기존 테스트 그린).
   (기존 `test_get_topology_*`는 그대로 통과해야 한다 — 동작·반환 스키마 불변.)

## 4. 검증 (필수)

- 문법: `py -m compileall -q src`
- 테스트: `uv run python -m pytest -q`
  - ⚠️ 이 PC는 `uv run pytest` 가 trampoline 오류로 깨진다. **반드시 `uv run python -m pytest`** 를 쓴다.
  - `python` 명령은 Windows Store 별칭이라 작동 안 함 → `py` 또는 `uv` 사용.
- 전체 그린 확인(회귀 0).

## 5. 제약 (반드시 준수 — AGENTS.md·SPEC §9)

- **관측 비차단**: `build_roster`(정규식 분류)는 Lock **밖**에서. Lock 안에서는 routing 스냅샷 +
  hops 슬라이스만(가벼운 사본). 리더 스레드 observe 가 뷰어 폴링에 막히면 안 된다.
- **읽기 전용**: `get_topology`·새 메서드는 시리얼 송신·상태변경 없음.
- **stdout 금지**: 진단은 `_log`(stderr)만.
- **Lock 보호**: 공유 상태(routing·_hops) 접근은 `self._lock` 안에서.
- **클라이언트 파리티**: Claude Code·Codex 동일 동작(특정 클라이언트 기능 의존 금지).
- 반환 스키마·status 문자열·busy 폴백은 **변경 금지**(다른 발견과 분리 — 이 작업은 skew만 고친다).

## 6. 커밋

- 한국어 Conventional Commits: `fix: get_topology roster·recent_hops 원자 스냅샷으로 skew 제거`
- 본문에 근본원인(두 Lock 분리)·해법(단일 Lock 캡처, build_roster는 Lock 밖) 요약.
- 끝에: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`
- **push 는 하지 않는다**(사용자가 별도 지시할 때만).

## 7. 산출 보고 (작업 후)

변경 파일 목록, 추가/수정 테스트 수, 전체 테스트 통과 수, 커밋 해시를 보고한다.
범위 밖(다른 코드리뷰 발견 #1·#2·#4 등)은 건드리지 않는다 — 이 작업은 **#3 skew 단일 수정**이다.
