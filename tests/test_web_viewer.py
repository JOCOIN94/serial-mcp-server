"""ViewerServer — 다중 포트 라우팅 통합 테스트(임시 포트, 실제 HTTP 기동)."""

import json
import socket
import urllib.error
import urllib.request
from datetime import datetime
from importlib import resources

import pytest

import serial_mcp.web_viewer as web_viewer
from serial_mcp.viewer_feed import RawFeed
from serial_mcp.web_viewer import ViewerServer

BASE = datetime(2026, 6, 9, 14, 0, 0, 0)


@pytest.fixture
def viewer():
    feeds = {"COM_A": RawFeed(), "COM_B": RawFeed()}
    release_calls = []

    def release_port(port):
        release_calls.append(port)
        if port == "COM_A":
            return {"status": "ok", "released": True}
        return {"status": "error", "message": "unknown port"}

    v = ViewerServer(
        ports_info=lambda: [{"port": "COM_A", "label": "SSM (COM_A)"},
                            {"port": "COM_B", "label": "COM_B"}],
        feed_for=lambda p: feeds.get(p),
        buffer_info=lambda p: {"status": "ok", "port": p, "entries": [], "capacity": 2000},
        status_info=lambda: {"session": "codex", "ports": [
            {"port": "COM_A", "label": "SSM (COM_A)", "connected": True, "baud": 115200,
             "last_error": None, "buffer_entries": 3, "buffer_capacity": 2000,
             "hw": "SSM", "board": None},
            {"port": "COM_B", "label": "COM_B", "connected": False, "baud": 115200,
             "last_error": "busy", "buffer_entries": 0, "buffer_capacity": 2000,
             "hw": None, "board": None},
        ]},
        release_port=release_port,
        port=0,   # 테스트는 임시 포트
    )
    v.start()
    assert v.url is not None
    yield v, feeds, release_calls
    v.stop()


def _get_json(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.loads(r.read())


def test_root_serves_html(viewer):
    v, _, _ = viewer
    with urllib.request.urlopen(v.url + "/", timeout=5) as r:
        assert r.status == 200
        body = r.read().decode("utf-8")
    assert "serial-mcp" in body
    assert "portboard" in body       # 좌측 포트 보드(board.js 렌더 타깃)
    assert "tabStream" in body       # 스트림/버퍼 탭 셸


def test_viewer_html_is_a_packaged_resource():
    asset = resources.files("serial_mcp").joinpath("viewer.html")
    assert asset.is_file()
    assert "VIEWER-PURE-START" in asset.read_text(encoding="utf-8")


def test_missing_viewer_asset_returns_500_without_stopping_api(monkeypatch, viewer):
    v, _, _ = viewer

    def missing():
        raise FileNotFoundError("viewer.html")

    monkeypatch.setattr(web_viewer, "_viewer_html_bytes", missing)

    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(v.url + "/", timeout=5)
    assert exc.value.code == 500
    assert _get_json(v.url + "/api/status")["session"] == "codex"


def test_api_ports_lists_monitors(viewer):
    v, _, _ = viewer
    d = _get_json(v.url + "/api/ports")
    assert [p["label"] for p in d["ports"]] == ["SSM (COM_A)", "COM_B"]


def test_api_status_returns_port_array(viewer):
    v, _, _ = viewer
    d = _get_json(v.url + "/api/status")
    assert d["session"] == "codex"
    assert d["ports"][0]["connected"] is True
    assert d["ports"][0]["hw"] == "SSM"
    assert d["ports"][0]["board"] is None
    assert d["ports"][1]["last_error"] == "busy"
    assert "released" not in d["ports"][0]
    assert "released" not in d["ports"][1]


def test_api_topology_returns_chains_key():
    v = ViewerServer(
        ports_info=lambda: [],
        feed_for=lambda p: None,
        buffer_info=lambda p: {},
        status_info=lambda: {"ports": []},
        topology_info=lambda: {"groups": [], "unplaced": [], "chains": [{"id": 1}]},
        port=0,
    )
    v.start()
    try:
        d = _get_json(v.url + "/api/topology")
    finally:
        v.stop()
    assert d["chains"] == [{"id": 1}]


def test_api_buffer_routes_by_port(viewer):
    v, _, _ = viewer
    assert _get_json(v.url + "/api/buffer?port=COM_B")["port"] == "COM_B"


def test_api_buffer_since_forwards_integer_revision():
    calls = []
    v = ViewerServer(
        ports_info=lambda: [],
        feed_for=lambda p: None,
        buffer_info=lambda p, since=None: calls.append((p, since)) or {
            "status": "ok", "port": p, "revision": since, "entries": [],
        },
        status_info=lambda: {"ports": []},
        port=0,
    )
    v.start()
    try:
        d = _get_json(v.url + "/api/buffer?port=COM_A&since=17")
    finally:
        v.stop()
    assert d["revision"] == 17
    assert calls == [("COM_A", 17)]


def test_api_release_routes_to_backend_callback(viewer):
    v, _, release_calls = viewer
    d = _get_json(v.url + "/api/release?port=COM_A")
    assert d == {"status": "ok", "released": True}
    assert release_calls == ["COM_A"]


def test_api_release_without_port_routes_to_backend_callback():
    calls = []
    v = ViewerServer(ports_info=lambda: [], feed_for=lambda p: None,
                     buffer_info=lambda p: {}, status_info=lambda: {"ports": []},
                     release_port=lambda p: calls.append(p) or {"status": "ok", "released": True},
                     port=0)
    v.start()
    try:
        d = _get_json(v.url + "/api/release")
    finally:
        v.stop()
    assert d == {"status": "ok", "released": True}
    assert calls == [""]


def test_api_release_unknown_port_returns_404(viewer):
    v, _, release_calls = viewer
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(v.url + "/api/release?port=COM_X", timeout=5)
    assert exc.value.code == 404
    assert release_calls == ["COM_X"]


def test_stream_sse_isolated_per_port(viewer):
    v, feeds, _ = viewer
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


def test_topology_stream_sse_publishes_hop():
    feed = RawFeed()
    v = ViewerServer(
        ports_info=lambda: [],
        feed_for=lambda p: None,
        buffer_info=lambda p: {},
        status_info=lambda: {"ports": []},
        topology_feed=feed,
        port=0,
    )
    v.start()
    try:
        resp = urllib.request.urlopen(v.url + "/api/topology/stream", timeout=5)
        try:
            assert "text/event-stream" in resp.headers["Content-Type"]
            hop = {
                "id": "hop-1",
                "ts": 12.5,
                "kind": "info",
                "path": ["node:SB5", "node:SSM"],
                "ok": True,
            }
            feed.publish(BASE, hop)
            line = resp.readline().decode("utf-8")
            payload = json.loads(line[len("data: "):])
            assert payload == hop
        finally:
            resp.close()
    finally:
        v.stop()


def test_topology_stream_sse_publishes_chain_payload():
    feed = RawFeed()
    v = ViewerServer(
        ports_info=lambda: [],
        feed_for=lambda p: None,
        buffer_info=lambda p: {},
        status_info=lambda: {"ports": []},
        topology_feed=feed,
        port=0,
    )
    v.start()
    try:
        resp = urllib.request.urlopen(v.url + "/api/topology/stream", timeout=5)
        try:
            chain = {"chain": {"id": 1, "key": ["u", 5, 9], "nodes": []}}
            feed.publish(BASE, chain)
            line = resp.readline().decode("utf-8")
            payload = json.loads(line[len("data: "):])
            assert payload == chain
        finally:
            resp.close()
    finally:
        v.stop()


def test_stream_unknown_port_404(viewer):
    v, _, _ = viewer
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(v.url + "/api/stream?port=NOPE", timeout=5)
    assert exc.value.code == 404


def test_unknown_path_returns_404(viewer):
    v, _, _ = viewer
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(v.url + "/nope", timeout=5)
    assert exc.value.code == 404


def test_no_port_fallback_when_preferred_busy():
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
            assert v.url is None
        finally:
            v.stop()
    finally:
        blocker.close()


def test_start_with_out_of_range_port_fails_closed_not_crash():
    # SERIAL_WEB 오타(99999)는 _load_config에서 걸러야 한다. ViewerServer 자체는 폴백하지 않는다.
    v = ViewerServer(ports_info=lambda: [], feed_for=lambda p: None,
                     buffer_info=lambda p: {}, status_info=lambda: {"ports": []},
                     port=99999)
    v.start()
    try:
        assert v.url is None
    finally:
        v.stop()
