"""ViewerServer — 다중 포트 라우팅 통합 테스트(임시 포트, 실제 HTTP 기동)."""

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
    feeds = {"COM_A": RawFeed(), "COM_B": RawFeed()}
    v = ViewerServer(
        ports_info=lambda: [{"port": "COM_A", "label": "SSM (COM_A)"},
                            {"port": "COM_B", "label": "COM_B"}],
        feed_for=lambda p: feeds.get(p),
        buffer_info=lambda p: {"status": "ok", "port": p, "entries": [], "capacity": 2000},
        status_info=lambda: {"ports": [
            {"port": "COM_A", "label": "SSM (COM_A)", "connected": True, "baud": 115200,
             "last_error": None, "buffer_entries": 3, "buffer_capacity": 2000},
            {"port": "COM_B", "label": "COM_B", "connected": False, "baud": 115200,
             "last_error": "busy", "buffer_entries": 0, "buffer_capacity": 2000},
        ]},
        port=0,   # 테스트는 임시 포트
    )
    v.start()
    assert v.url is not None
    yield v, feeds
    v.stop()


def _get_json(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.loads(r.read())


def test_root_serves_html(viewer):
    v, _ = viewer
    with urllib.request.urlopen(v.url + "/", timeout=5) as r:
        assert r.status == 200
        body = r.read().decode("utf-8")
    assert "serial-mcp" in body
    assert "psel" in body            # 포트 셀렉터 존재


def test_api_ports_lists_monitors(viewer):
    v, _ = viewer
    d = _get_json(v.url + "/api/ports")
    assert [p["label"] for p in d["ports"]] == ["SSM (COM_A)", "COM_B"]


def test_api_status_returns_port_array(viewer):
    v, _ = viewer
    d = _get_json(v.url + "/api/status")
    assert d["ports"][0]["connected"] is True
    assert d["ports"][1]["last_error"] == "busy"


def test_api_buffer_routes_by_port(viewer):
    v, _ = viewer
    assert _get_json(v.url + "/api/buffer?port=COM_B")["port"] == "COM_B"


def test_stream_sse_isolated_per_port(viewer):
    v, feeds = viewer
    resp = urllib.request.urlopen(v.url + "/api/stream?port=COM_A", timeout=5)
    try:
        assert "text/event-stream" in resp.headers["Content-Type"]
        feeds["COM_B"].publish(BASE, "B쪽 줄 — 받으면 안 됨")
        feeds["COM_A"].publish(BASE, "hello A")
        line = resp.readline().decode("utf-8")
        payload = json.loads(line[len("data: "):])
        assert payload == {"ts": "14:00:00.000", "text": "hello A"}   # A만 수신
    finally:
        resp.close()


def test_stream_unknown_port_404(viewer):
    v, _ = viewer
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(v.url + "/api/stream?port=NOPE", timeout=5)
    assert exc.value.code == 404


def test_unknown_path_returns_404(viewer):
    v, _ = viewer
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(v.url + "/nope", timeout=5)
    assert exc.value.code == 404


def test_port_fallback_when_preferred_busy():
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    busy_port = blocker.getsockname()[1]
    try:
        v = ViewerServer(ports_info=lambda: [], feed_for=lambda p: None,
                         buffer_info=lambda p: {}, status_info=lambda: {"ports": []},
                         port=busy_port)
        v.start()
        try:
            assert v.url is not None
            assert v.url != f"http://127.0.0.1:{busy_port}"
        finally:
            v.stop()
    finally:
        blocker.close()
