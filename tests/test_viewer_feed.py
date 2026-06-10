"""RawFeed — 수신 라인 생중계 허브(웹 뷰어 스트림 데이터원) 단위 테스트.

핵심 불변식: publish는 논블로킹이며, 구독자가 느리거나 없어도 발행자를 막지 않는다.
"""

import threading
from datetime import datetime

from serial_mcp.viewer_feed import RawFeed

BASE = datetime(2026, 6, 9, 14, 0, 0, 0)


def test_publish_without_subscribers_is_noop():
    RawFeed().publish(BASE, "no one listening")   # 예외 없이 통과해야 함


def test_subscribe_then_receive():
    feed = RawFeed()
    sub = feed.subscribe()
    feed.publish(BASE, "hello")
    assert sub.get(timeout=1.0) == (BASE, "hello")


def test_get_timeout_returns_none():
    sub = RawFeed().subscribe()
    assert sub.get(timeout=0.05) is None


def test_multiple_subscribers_each_receive():
    feed = RawFeed()
    a, b = feed.subscribe(), feed.subscribe()
    feed.publish(BASE, "x")
    assert a.get(timeout=1.0) == (BASE, "x")
    assert b.get(timeout=1.0) == (BASE, "x")


def test_unsubscribed_stops_receiving():
    feed = RawFeed()
    sub = feed.subscribe()
    feed.unsubscribe(sub)
    feed.publish(BASE, "after")
    assert sub.get(timeout=0.05) is None


def test_overflow_drops_oldest():
    feed = RawFeed(queue_maxlen=3)
    sub = feed.subscribe()
    for i in range(5):
        feed.publish(BASE, f"line{i}")
    got = [sub.get(timeout=0.1) for _ in range(3)]
    assert [t for _, t in got] == ["line2", "line3", "line4"]
    assert sub.get(timeout=0.05) is None   # 오래된 line0/1은 버려짐


def test_cross_thread_delivery():
    feed = RawFeed()
    sub = feed.subscribe()
    t = threading.Thread(target=lambda: [feed.publish(BASE, f"n{i}") for i in range(100)])
    t.start()
    got = [sub.get(timeout=1.0) for _ in range(100)]
    t.join()
    assert [x[1] for x in got] == [f"n{i}" for i in range(100)]
