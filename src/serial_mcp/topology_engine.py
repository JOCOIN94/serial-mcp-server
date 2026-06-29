"""topology_engine.py — 토폴로지 상태 조정기(상태+Lock, Phase B 모듈6).

순수 모듈(events·correlator·routing·roster)을 한데 묶는다:
  리더 스레드 → observe(port, ts, text) → 포트별 EventAssembler 로 블록 조립 → 각 Event 를
  routing(링크그래프·토큰맵)·correlator(다중홉 상관)에 흘려 홉을 방출하고 히스토리에 적재.
  sweep(now) → 유휴 pending 블록 flush + correlator 만료 처리(TX-without-RX).
  roster(entries) → routing 을 얹은 로스터 스냅샷. recent_hops(n) → 최근 홉(get_topology 용).

리더 스레드(observe)·도구 호출(roster/recent_hops)·sweep 타이머가 공유 상태에 동시 접근하므로
단일 Lock 으로 보호한다(AGENTS.md 버퍼/공유상태 Lock). 윈도 클럭은 **서버 도착 단조시각 ts**
(server.py 가 time.monotonic 주입, 펌웨어 RTC 아님 — plan §6). 시리얼 I/O·타이머·부트스트랩
송신은 server.py 가 배선한다 — 이 클래스는 I/O·스레드 기동에 비의존이라 단위 테스트가 쉽다.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Optional

from .topology import build_roster
from .topology_correlator import Correlator
from .topology_events import EventAssembler
from .topology_routing import RoutingTable


class _RoutingSnapshot:
    """build_roster 가 보는 routing 인터페이스(tokens()/edges())의 불변 스냅샷.

    roster() 가 엔진 Lock 안에서 한 번만 떠서(RoutingTable.tokens()/edges() 는 이미 사본 반환),
    CPU 무거운 build_roster(포트별 정규식 분류)는 Lock 밖에서 돌게 한다 — 뷰어 폴링이 리더
    스레드 observe 를 Lock 으로 막지 않도록(관측 비차단 불변식). edges 는 now 로 미리 계산됐다.
    """

    __slots__ = ("_tokens", "_edges")

    def __init__(self, tokens: dict, edges: list) -> None:
        self._tokens = tokens
        self._edges = edges

    def tokens(self) -> dict:
        return self._tokens

    def edges(self, now=None) -> list:    # now 무시 — 스냅샷 시점 fresh 로 이미 계산됨
        return self._edges


class TopologyEngine:
    """관측 줄 → 홉/로스터. 순수 상태(Lock 보호), I/O 비의존."""

    def __init__(self, window_s: float = 15.0, hop_history: int = 200) -> None:
        self._lock = threading.Lock()
        self._assemblers: dict[str, EventAssembler] = {}     # port → 누산기
        self._last_ts: dict[str, float] = {}                 # port → 최근 관측 ts(유휴 flush 판정)
        self._correlator = Correlator(window_s=window_s)
        self._routing = RoutingTable()
        self._hops: deque = deque(maxlen=hop_history)         # 최근 홉(상한·drop-oldest)

    def observe(self, port: str, ts: float, text: str) -> list:
        """리더 스레드가 수신 줄마다 호출(비차단·예외삼킴은 호출측 on_line 훅). 새 홉 리스트 반환."""
        with self._lock:
            asm = self._assemblers.get(port)
            if asm is None:
                asm = EventAssembler(port)
                self._assemblers[port] = asm
            self._last_ts[port] = ts
            return self._drain(asm.feed(ts, text))

    def sweep(self, now: float, flush_idle_s: float = 2.0) -> list:
        """유휴 pending 블록 flush + correlator 만료 처리. 새 홉 리스트 반환.

        flush_idle_s 이상 새 줄이 없던 포트의 마지막 블록을 방출한다(활성 블록은 절단 금지 —
        멀티라인 블록은 ms 단위 버스트라 수 초 유휴면 완결로 본다). 그 뒤 correlator.sweep 로
        윈도 만료 pending(TX-without-RX)을 실패/미확정으로 방출한다.
        """
        with self._lock:
            flushed: list = []
            for port, asm in list(self._assemblers.items()):
                if now - self._last_ts.get(port, now) >= flush_idle_s:
                    flushed += self._drain(asm.flush())   # _drain 이 이미 _hops 에 적재함
            swept = self._correlator.sweep(now)
            self._hops.extend(swept)                      # correlator.sweep 분만 추가 적재
            return flushed + swept

    def flush(self) -> list:
        """모든 포트의 pending 블록을 즉시 방출(종료/테스트). 새 홉 리스트 반환."""
        with self._lock:
            new: list = []
            for asm in self._assemblers.values():
                new += self._drain(asm.flush())
            return new

    def roster(self, entries, now: Optional[float] = None) -> dict:
        """관측된 routing(링크그래프·토큰맵)을 얹은 로스터 스냅샷. 읽기 전용.

        Lock 안에서 routing 스냅샷만 뜨고, CPU 무거운 build_roster(포트별 정규식 분류)는 Lock
        밖에서 돈다 — 뷰어 폴링이 엔진 Lock 으로 리더 observe 를 막지 않게(관측 비차단).
        """
        with self._lock:
            snap = _RoutingSnapshot(self._routing.tokens(), self._routing.edges(now))
        return build_roster(entries, routing=snap, now=now)

    def recent_hops(self, n: int = 20) -> list:
        """최근 홉 n개(get_topology·SSE 보강용). 시간순 tail. n<=0 이면 빈 리스트."""
        with self._lock:
            if n <= 0:
                return []
            return list(self._hops)[-n:]

    def _drain(self, events) -> list:
        """방출된 Event 들을 routing·correlator 에 흘려 홉을 모은다(Lock 보유 중 호출).

        observe/flush/sweep(유휴 flush) 모두 이 경로로 홉을 적재한다 — _drain 이 self._hops 에
        직접 extend 하므로(routing 은 부수상태 갱신, 방출 없음), 호출측은 반환값만 쓰고 _hops 에
        다시 넣지 않는다(이중 적재 주의). correlator.sweep 산출분은 _drain 을 안 거치므로 sweep 이
        그 분만 따로 _hops 에 적재한다.
        """
        out: list = []
        for ev in events:
            self._routing.observe(ev)
            out += self._correlator.observe(ev)
        self._hops.extend(out)
        return out
