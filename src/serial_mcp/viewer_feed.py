"""수신 라인 생중계 허브(RawFeed) — 웹 뷰어 실시간 스트림의 데이터원.

리더 스레드가 publish()로 한 줄씩 흘리고, SSE 핸들러가 구독자 큐에서 꺼내 간다.
이 모듈은 순수 로직만 담는다 — 시리얼 I/O·HTTP 의존성이 없어 단위 테스트가 쉽다.

핵심 불변식: publish는 논블로킹이다. 구독자(브라우저)가 느리거나 끊겨도 시리얼
수신 경로를 막지 않는다 — 큐가 가득 차면 가장 오래된 항목을 버린다(drop-oldest).
"""

from __future__ import annotations

import threading
from collections import deque
from datetime import datetime
from typing import Any, Optional


class Subscription:
    """구독자 하나의 수신 큐. RawFeed.subscribe()가 만들어 준다."""

    def __init__(self, maxlen: int) -> None:
        self._q: deque[tuple[datetime, Any]] = deque(maxlen=maxlen)
        self._cond = threading.Condition()

    def _put(self, ts: datetime, payload: Any) -> None:
        with self._cond:
            self._q.append((ts, payload))   # maxlen 초과 시 deque가 oldest를 자동으로 버림
            self._cond.notify()

    def get(self, timeout: float = 1.0) -> Optional[tuple[datetime, Any]]:
        """다음 (ts, payload)를 반환. timeout까지 없으면 None."""
        with self._cond:
            if not self._q:
                self._cond.wait(timeout)
            if self._q:
                return self._q.popleft()
            return None


class RawFeed:
    """구독자들에게 payload를 분배하는 허브. 스레드 안전."""

    def __init__(self, queue_maxlen: int = 1000) -> None:
        self._subs: list[Subscription] = []
        self._lock = threading.Lock()
        self._queue_maxlen = queue_maxlen

    def subscribe(self) -> Subscription:
        sub = Subscription(self._queue_maxlen)
        with self._lock:
            self._subs.append(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        with self._lock:
            if sub in self._subs:
                self._subs.remove(sub)

    def publish(self, ts: datetime, payload: Any) -> None:
        """리더 스레드가 호출. 논블로킹 — 구독자가 없으면 no-op."""
        with self._lock:
            subs = list(self._subs)
        for sub in subs:
            sub._put(ts, payload)
