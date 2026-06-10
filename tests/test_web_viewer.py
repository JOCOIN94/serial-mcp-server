"""ViewerServer — 실제 HTTP 기동 통합 테스트(임시 포트). 외부 네트워크 불필요."""

import json
import socket
import urllib.error
import urllib.request
from datetime import datetime

import pytest

from serial_mcp.viewer_feed import RawFeed
from serial_mcp.web_viewer import ViewerServer

BASE = datetime(2026, 6, 9, 14, 0, 0, 0)


@pytest.fixture
def viewer():
    feed = RawFeed()
    v = ViewerServer(
        feed=feed,
        buffer_info=lambda: {"status": "ok", "entries": [], "capacity": 2000},
        status_info=lambda: {"connected": True, "port": "COM_TEST",
                             "baud": 115200, "last_error": None},
        port=0,   # 테스트는 임시 포트
    )
    v.start()
    assert v.url is not None
    yield v, feed
    v.stop()


def test_root_serves_html(viewer):
    v, _ = viewer
    with urllib.request.urlopen(v.url + "/", timeout=5) as r:
        assert r.status == 200
        assert "text/html" in r.headers["Content-Type"]
        assert "serial-mcp" in r.read().decode("utf-8")


def test_api_status_returns_injected_json(viewer):
    v, _ = viewer
    with urllib.request.urlopen(v.url + "/api/status", timeout=5) as r:
        d = json.loads(r.read())
    assert d["connected"] is True
    assert d["port"] == "COM_TEST"


def test_api_buffer_returns_injected_json(viewer):
    v, _ = viewer
    with urllib.request.urlopen(v.url + "/api/buffer", timeout=5) as r:
        d = json.loads(r.read())
    assert d["status"] == "ok"
    assert d["capacity"] == 2000


def test_unknown_path_returns_404(viewer):
    v, _ = viewer
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(v.url + "/nope", timeout=5)
    assert exc.value.code == 404


def test_stream_sse_delivers_published_line(viewer):
    v, feed = viewer
    resp = urllib.request.urlopen(v.url + "/api/stream", timeout=5)
    try:
        assert "text/event-stream" in resp.headers["Content-Type"]
        feed.publish(BASE, "hello sse")
        line = resp.readline().decode("utf-8")        # "data: {...}\n"
        assert line.startswith("data: ")
        payload = json.loads(line[len("data: "):])
        assert payload == {"ts": "14:00:00.000", "text": "hello sse"}
    finally:
        resp.close()


def test_port_fallback_when_preferred_busy():
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    busy_port = blocker.getsockname()[1]
    try:
        v = ViewerServer(feed=RawFeed(), buffer_info=lambda: {},
                         status_info=lambda: {}, port=busy_port)
        v.start()
        try:
            assert v.url is not None
            assert v.url != f"http://127.0.0.1:{busy_port}"   # 임시 포트로 폴백
        finally:
            v.stop()
    finally:
        blocker.close()
