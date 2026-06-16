"""8743 bind 기반 whole-session 소유권 계약 테스트."""

import threading
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import serial_mcp.server as srv


class StubReader:
    """SerialReader 대역 — 포트 I/O 없이 기동/정지만 기록한다."""

    def __init__(self, port, baud, buffer, tee_path=None, feed=None, on_line=None, **kw):
        self.port = port
        self.baud = baud
        self.buffer = buffer
        self.tee_path = tee_path
        self.feed = feed
        self.on_line = on_line
        self.started = False
        self.stopped = False
        self.connected = False
        self.last_error = None
        self.opened_at = None

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True
        self.connected = False


class JoinableThread:
    def __init__(self):
        self.join_called = False
        self.timeout = None
        self._alive = True

    def join(self, timeout=None):
        self.join_called = True
        self.timeout = timeout
        self._alive = False

    def is_alive(self):
        return self._alive


def cfg(**overrides):
    base = {
        "ports": [("COM_A", None)],
        "names": {},
        "autoname": [],
        "baud": 115200,
        "tee": None,
        "exclude": None,
        "include": None,
        "maxlen": 2000,
        "dedup": 5,
        "web": 0,
        "web_ui": True,
        "hotplug": None,
        "write": True,
        "write_confirm": False,
        "char_delay": 0.0,
    }
    return {**base, **overrides}


def test_status_lazily_acquires_owner_and_starts_readers(monkeypatch):
    monkeypatch.setattr(srv, "SerialReader", StubReader)
    monkeypatch.setattr(srv, "_config", cfg())
    monkeypatch.setattr(srv, "_monitors", {})
    monkeypatch.setattr(srv, "_viewer", None)
    monkeypatch.setattr(srv, "_owner_active", False, raising=False)
    monkeypatch.setattr(srv, "_lock_socket", None, raising=False)

    out = srv.get_serial_status()

    try:
        assert out["status"] == "ok"
        assert out["viewer_url"].startswith("http://127.0.0.1:")
        assert list(srv._monitors) == ["COM_A"]
        assert srv._monitors["COM_A"].reader.started is True
    finally:
        srv._release_owner("test cleanup")


def test_occupied_lock_reports_busy_without_creating_monitors(monkeypatch):
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    busy_port = blocker.getsockname()[1]
    try:
        monkeypatch.setattr(srv, "SerialReader", StubReader)
        monkeypatch.setattr(srv, "_config", cfg(web=busy_port))
        monkeypatch.setattr(srv, "_monitors", {})
        monkeypatch.setattr(srv, "_viewer", None)
        monkeypatch.setattr(srv, "_owner_active", False, raising=False)
        monkeypatch.setattr(srv, "_lock_socket", None, raising=False)

        out = srv.get_recent_logs()

        assert out["status"] == "error"
        assert out["lines"] == []
        assert out["message"] == (
            "다른 serial-mcp 세션이 점유 중입니다. 먼저 해제하세요. "
            "owner 세션을 종료하거나 그 세션 뷰어에서 해제하세요."
        )
        assert out["owner_url"] == f"http://127.0.0.1:{busy_port}"
        assert "http://" not in out["message"]
        assert srv._monitors == {}
    finally:
        blocker.close()


def test_status_reports_occupied_lock_as_non_error(monkeypatch):
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    busy_port = blocker.getsockname()[1]
    try:
        monkeypatch.setattr(srv, "_config", cfg(web=busy_port))
        monkeypatch.setattr(srv, "_monitors", {})
        monkeypatch.setattr(srv, "_viewer", None)
        monkeypatch.setattr(srv, "_owner_active", False, raising=False)
        monkeypatch.setattr(srv, "_lock_socket", None, raising=False)

        out = srv.get_serial_status()

        assert out["status"] == "busy"
        assert out["message"] == (
            "다른 serial-mcp 세션이 점유 중입니다. 먼저 해제하세요. "
            "owner 세션을 종료하거나 그 세션 뷰어에서 해제하세요."
        )
        assert "http://" not in out["message"]
        assert out["connected"] is False
        assert out["ports"] == []
    finally:
        blocker.close()


def test_web_disabled_lock_only_busy_reports_error(monkeypatch):
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    busy_port = blocker.getsockname()[1]
    try:
        monkeypatch.setattr(srv, "SerialReader", StubReader)
        monkeypatch.setattr(srv, "_config", cfg(web=busy_port, web_ui=False))
        monkeypatch.setattr(srv, "_monitors", {})
        monkeypatch.setattr(srv, "_viewer", None)
        monkeypatch.setattr(srv, "_owner_active", False, raising=False)
        monkeypatch.setattr(srv, "_lock_socket", None, raising=False)

        out = srv.get_recent_logs()

        assert out["status"] == "error"
        assert out["message"] == (
            "다른 serial-mcp 세션이 점유 중입니다. 먼저 해제하세요. "
            "owner 세션을 종료하거나 그 세션 뷰어에서 해제하세요."
        )
        assert "http://" not in out["message"]
        assert srv._monitors == {}
        assert srv._viewer is None
    finally:
        blocker.close()


def test_busy_message_includes_viewer_link_when_owner_status_reachable(monkeypatch):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/api/status":
                self.send_response(404)
                self.end_headers()
                return
            body = b'{"session":"Codex"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setattr(srv, "_config", cfg(web=port))

        out = srv._owner_busy_result()

        assert out["status"] == "error"
        assert out["owner_session"] == "Codex"
        assert out["owner_url"] == f"http://127.0.0.1:{port}"
        assert f"웹뷰어: http://127.0.0.1:{port}" in out["message"]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=1.0)


def test_release_owner_stops_readers_viewer_and_clears_session(monkeypatch):
    reader = StubReader("COM_A", 115200, buffer=None)
    viewer = SimpleNamespace(url="http://127.0.0.1:9999", stopped=False)

    def stop_viewer():
        viewer.stopped = True

    viewer.stop = stop_viewer
    mon = SimpleNamespace(port="COM_A", reader=reader)
    monkeypatch.setattr(srv, "_monitors", {"COM_A": mon})
    monkeypatch.setattr(srv, "_viewer", viewer)
    monkeypatch.setattr(srv, "_owner_active", True, raising=False)
    monkeypatch.setattr(srv, "_lock_socket", None, raising=False)

    srv._release_owner("test release")

    assert reader.stopped is True
    assert viewer.stopped is True
    assert srv._monitors == {}
    assert srv._viewer is None
    assert srv._owner_active is False


def test_release_owner_joins_hotplug_thread_before_clearing(monkeypatch):
    reader = StubReader("COM_A", 115200, buffer=None)
    mon = SimpleNamespace(port="COM_A", reader=reader)
    hotplug = JoinableThread()
    stop = threading.Event()

    monkeypatch.setattr(srv, "_monitors", {"COM_A": mon})
    monkeypatch.setattr(srv, "_viewer", None)
    monkeypatch.setattr(srv, "_owner_active", True, raising=False)
    monkeypatch.setattr(srv, "_lock_socket", None, raising=False)
    monkeypatch.setattr(srv, "_hotplug_thread", hotplug, raising=False)
    monkeypatch.setattr(srv, "_hotplug_stop", stop, raising=False)

    srv._release_owner("test release")

    assert stop.is_set()
    assert hotplug.join_called is True
    assert hotplug.timeout is not None
    assert hotplug.is_alive() is False
    assert srv._hotplug_thread is None
    assert srv._monitors == {}


def test_main_releases_owner_when_mcp_run_returns(monkeypatch):
    reader = StubReader("COM_A", 115200, buffer=None)
    mon = SimpleNamespace(port="COM_A", reader=reader)

    monkeypatch.setattr(srv, "_monitors", {"COM_A": mon})
    monkeypatch.setattr(srv, "_viewer", None)
    monkeypatch.setattr(srv, "_owner_active", True, raising=False)
    monkeypatch.setattr(srv, "_lock_socket", None, raising=False)
    monkeypatch.setattr(srv, "_hotplug_thread", None, raising=False)
    monkeypatch.setattr(srv, "_load_config", lambda env: cfg())
    monkeypatch.setattr(srv, "compile_autoname", lambda rules, log: [])
    monkeypatch.setattr(srv.mcp, "run", lambda: None)

    srv.main()

    assert reader.stopped is True
    assert srv._monitors == {}
    assert srv._viewer is None
    assert srv._owner_active is False


def test_main_releases_owner_when_mcp_run_raises(monkeypatch):
    class RunStopped(RuntimeError):
        pass

    reader = StubReader("COM_A", 115200, buffer=None)
    mon = SimpleNamespace(port="COM_A", reader=reader)

    monkeypatch.setattr(srv, "_monitors", {"COM_A": mon})
    monkeypatch.setattr(srv, "_viewer", None)
    monkeypatch.setattr(srv, "_owner_active", True, raising=False)
    monkeypatch.setattr(srv, "_lock_socket", None, raising=False)
    monkeypatch.setattr(srv, "_hotplug_thread", None, raising=False)
    monkeypatch.setattr(srv, "_load_config", lambda env: cfg())
    monkeypatch.setattr(srv, "compile_autoname", lambda rules, log: [])

    def raise_stop():
        raise RunStopped("stdio closed")

    monkeypatch.setattr(srv.mcp, "run", raise_stop)

    try:
        srv.main()
    except RunStopped:
        pass

    assert reader.stopped is True
    assert srv._monitors == {}
    assert srv._viewer is None
    assert srv._owner_active is False


def test_release_owner_is_idempotent(monkeypatch):
    reader = StubReader("COM_A", 115200, buffer=None)
    mon = SimpleNamespace(port="COM_A", reader=reader)

    monkeypatch.setattr(srv, "_monitors", {"COM_A": mon})
    monkeypatch.setattr(srv, "_viewer", None)
    monkeypatch.setattr(srv, "_owner_active", True, raising=False)
    monkeypatch.setattr(srv, "_lock_socket", None, raising=False)
    monkeypatch.setattr(srv, "_hotplug_thread", None, raising=False)

    srv._release_owner("first")
    srv._release_owner("second")

    assert reader.stopped is True
    assert srv._monitors == {}
    assert srv._viewer is None
    assert srv._owner_active is False


def test_main_eof_without_owner_is_noop(monkeypatch):
    monkeypatch.setattr(srv, "_monitors", {})
    monkeypatch.setattr(srv, "_viewer", None)
    monkeypatch.setattr(srv, "_owner_active", False, raising=False)
    monkeypatch.setattr(srv, "_lock_socket", None, raising=False)
    monkeypatch.setattr(srv, "_hotplug_thread", None, raising=False)
    monkeypatch.setattr(srv, "_load_config", lambda env: cfg())
    monkeypatch.setattr(srv, "compile_autoname", lambda rules, log: [])
    monkeypatch.setattr(srv.mcp, "run", lambda: None)

    srv.main()

    assert srv._monitors == {}
    assert srv._viewer is None
    assert srv._owner_active is False


def test_viewer_release_port_ignores_port_for_whole_session(monkeypatch):
    calls = []

    monkeypatch.setattr(srv, "_release_owner", lambda reason: calls.append(reason))

    out = srv._viewer_release_port("")

    assert out == {"status": "ok", "released": True}
    assert "release" in calls[0]
