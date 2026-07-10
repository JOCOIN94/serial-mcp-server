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


def test_server_runtime_is_the_single_owner_of_mutable_session_state():
    assert isinstance(srv._runtime, srv.ServerRuntime)
    assert {
        "monitors", "viewer", "lock_socket", "hotplug_thread",
        "topology_engine", "topology_thread", "topology_feed",
        "session_label", "chain_gate",
    } <= set(vars(srv._runtime))
    assert not {
        "_monitors", "_viewer", "_lock_socket", "_hotplug_thread",
        "_topology_engine", "_topology_thread", "_topology_feed",
        "_session_label", "_chain_gate",
    } & set(vars(srv))


def test_owner_compatibility_functions_delegate_to_runtime(monkeypatch):
    calls = []

    class Runtime:
        def acquire_owner_locked(self):
            calls.append("acquire")
            return {"status": "busy"}

        def release_owner_locked(self, reason):
            calls.append(("release", reason))

    monkeypatch.setattr(srv, "_runtime", Runtime())

    assert srv._acquire_owner_locked() == {"status": "busy"}
    srv._release_owner_locked("테스트")
    assert calls == ["acquire", ("release", "테스트")]


def test_status_lazily_acquires_owner_and_starts_readers(monkeypatch):
    monkeypatch.setattr(srv, "SerialReader", StubReader)
    monkeypatch.setattr(srv._runtime, "config", cfg())
    monkeypatch.setattr(srv._runtime, "monitors", {})
    monkeypatch.setattr(srv._runtime, "viewer", None)
    monkeypatch.setattr(srv._runtime, "owner_active", False, raising=False)
    monkeypatch.setattr(srv._runtime, "lock_socket", None, raising=False)

    out = srv.get_serial_status()

    try:
        assert out["status"] == "ok"
        assert out["viewer_url"].startswith("http://127.0.0.1:")
        assert list(srv._runtime.monitors) == ["COM_A"]
        assert srv._runtime.monitors["COM_A"].reader.started is True
    finally:
        srv._release_owner("test cleanup")


def test_owner_acquire_starts_topology_lifecycle_before_readers(monkeypatch):
    start_checks = []

    class TopologyAwareReader(StubReader):
        def start(self):
            start_checks.append(srv._runtime.topology_engine is not None)
            super().start()

    class DummyLockSocket:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    lock_socket = DummyLockSocket()
    monkeypatch.setattr(srv, "SerialReader", TopologyAwareReader)
    monkeypatch.setattr(srv, "_bind_lock_socket", lambda port: lock_socket)
    monkeypatch.setattr(srv._runtime, "config", cfg(web=0, web_ui=False))
    monkeypatch.setattr(srv._runtime, "monitors", {})
    monkeypatch.setattr(srv._runtime, "viewer", None)
    monkeypatch.setattr(srv._runtime, "owner_active", False, raising=False)
    monkeypatch.setattr(srv._runtime, "lock_socket", None, raising=False)
    monkeypatch.setattr(srv._runtime, "hotplug_thread", None, raising=False)

    err = srv._acquire_owner_locked()

    try:
        assert err is None
        assert start_checks == [True]
        assert isinstance(srv._runtime.topology_engine, srv.TopologyEngine)
        assert srv._runtime.topology_thread is not None
        assert srv._runtime.topology_thread.name == "topology-sweep"
        assert srv._runtime.topology_thread.is_alive()
        assert srv._runtime.topology_stop.is_set() is False
        assert isinstance(srv._runtime.topology_bootstrapped, set)
        assert srv._runtime.topology_owner_ts is not None
        assert srv._runtime.monitors["COM_A"].reader.started is True
    finally:
        srv._release_owner("test cleanup")

    assert lock_socket.closed is True


def test_topology_observe_publishes_hops_to_topology_feed(monkeypatch):
    hop = {"id": "observed-hop", "kind": "info", "path": ["node:SB5", "node:SSM"]}
    feed = srv.RawFeed()
    sub = feed.subscribe()

    class Engine:
        def observe(self, port, ts, text):
            assert port == "COM_A"
            assert text == "[Proc-WiFiRx] {}"
            return [hop]

    monkeypatch.setattr(srv._runtime, "topology_engine", Engine(), raising=False)
    monkeypatch.setattr(srv._runtime, "topology_feed", feed, raising=False)

    srv._topology_observe("COM_A", "[Proc-WiFiRx] {}")

    item = sub.get(timeout=1.0)
    assert item is not None
    assert item[1] == hop


def test_topology_loop_publishes_swept_hops(monkeypatch):
    hop = {"id": "timeout-hop", "kind": "inferred_timeout", "ok": False}
    feed = srv.RawFeed()
    sub = feed.subscribe()

    class Engine:
        def sweep(self, now):
            return [hop]

    class StopAfterOneTick:
        def __init__(self):
            self.calls = 0

        def wait(self, interval):
            self.calls += 1
            return self.calls > 1

    monkeypatch.setattr(srv._runtime, "topology_engine", Engine(), raising=False)
    monkeypatch.setattr(srv._runtime, "topology_feed", feed, raising=False)

    srv._topology_loop(0.0, StopAfterOneTick(), bootstrap_enabled=False)

    item = sub.get(timeout=1.0)
    assert item is not None
    assert item[1] == hop


def test_occupied_lock_reports_busy_without_creating_monitors(monkeypatch):
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    busy_port = blocker.getsockname()[1]
    try:
        monkeypatch.setattr(srv, "SerialReader", StubReader)
        monkeypatch.setattr(srv._runtime, "config", cfg(web=busy_port))
        monkeypatch.setattr(srv._runtime, "monitors", {})
        monkeypatch.setattr(srv._runtime, "viewer", None)
        monkeypatch.setattr(srv._runtime, "owner_active", False, raising=False)
        monkeypatch.setattr(srv._runtime, "lock_socket", None, raising=False)

        out = srv.get_recent_logs()

        assert out["status"] == "error"
        assert out["lines"] == []
        assert out["message"] == (
            "다른 serial-mcp 세션이 점유 중입니다. 먼저 해제하세요. "
            "owner 세션을 종료하거나 그 세션 뷰어에서 해제하세요."
        )
        assert out["owner_url"] == f"http://127.0.0.1:{busy_port}"
        assert "http://" not in out["message"]
        assert srv._runtime.monitors == {}
    finally:
        blocker.close()


def test_status_reports_occupied_lock_as_non_error(monkeypatch):
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    busy_port = blocker.getsockname()[1]
    try:
        monkeypatch.setattr(srv._runtime, "config", cfg(web=busy_port))
        monkeypatch.setattr(srv._runtime, "monitors", {})
        monkeypatch.setattr(srv._runtime, "viewer", None)
        monkeypatch.setattr(srv._runtime, "owner_active", False, raising=False)
        monkeypatch.setattr(srv._runtime, "lock_socket", None, raising=False)

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
        monkeypatch.setattr(srv._runtime, "config", cfg(web=busy_port, web_ui=False))
        monkeypatch.setattr(srv._runtime, "monitors", {})
        monkeypatch.setattr(srv._runtime, "viewer", None)
        monkeypatch.setattr(srv._runtime, "owner_active", False, raising=False)
        monkeypatch.setattr(srv._runtime, "lock_socket", None, raising=False)

        out = srv.get_recent_logs()

        assert out["status"] == "error"
        assert out["message"] == (
            "다른 serial-mcp 세션이 점유 중입니다. 먼저 해제하세요. "
            "owner 세션을 종료하거나 그 세션 뷰어에서 해제하세요."
        )
        assert "http://" not in out["message"]
        assert srv._runtime.monitors == {}
        assert srv._runtime.viewer is None
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
        monkeypatch.setattr(srv._runtime, "config", cfg(web=port))

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
    monkeypatch.setattr(srv._runtime, "monitors", {"COM_A": mon})
    monkeypatch.setattr(srv._runtime, "viewer", viewer)
    monkeypatch.setattr(srv._runtime, "owner_active", True, raising=False)
    monkeypatch.setattr(srv._runtime, "lock_socket", None, raising=False)

    srv._release_owner("test release")

    assert reader.stopped is True
    assert viewer.stopped is True
    assert srv._runtime.monitors == {}
    assert srv._runtime.viewer is None
    assert srv._runtime.owner_active is False


def test_release_owner_joins_hotplug_thread_before_clearing(monkeypatch):
    reader = StubReader("COM_A", 115200, buffer=None)
    mon = SimpleNamespace(port="COM_A", reader=reader)
    hotplug = JoinableThread()
    stop = threading.Event()

    monkeypatch.setattr(srv._runtime, "monitors", {"COM_A": mon})
    monkeypatch.setattr(srv._runtime, "viewer", None)
    monkeypatch.setattr(srv._runtime, "owner_active", True, raising=False)
    monkeypatch.setattr(srv._runtime, "lock_socket", None, raising=False)
    monkeypatch.setattr(srv._runtime, "hotplug_thread", hotplug, raising=False)
    monkeypatch.setattr(srv._runtime, "hotplug_stop", stop, raising=False)

    srv._release_owner("test release")

    assert stop.is_set()
    assert hotplug.join_called is True
    assert hotplug.timeout is not None
    assert hotplug.is_alive() is False
    assert srv._runtime.hotplug_thread is None
    assert srv._runtime.monitors == {}


def test_release_owner_stops_topology_thread_and_clears_engine(monkeypatch):
    reader = StubReader("COM_A", 115200, buffer=None)
    mon = SimpleNamespace(port="COM_A", reader=reader)
    topology_thread = JoinableThread()
    topology_stop = threading.Event()
    engine = object()

    monkeypatch.setattr(srv._runtime, "monitors", {"COM_A": mon})
    monkeypatch.setattr(srv._runtime, "viewer", None)
    monkeypatch.setattr(srv._runtime, "owner_active", True, raising=False)
    monkeypatch.setattr(srv._runtime, "lock_socket", None, raising=False)
    monkeypatch.setattr(srv._runtime, "hotplug_thread", None, raising=False)
    monkeypatch.setattr(srv._runtime, "topology_thread", topology_thread, raising=False)
    monkeypatch.setattr(srv._runtime, "topology_stop", topology_stop, raising=False)
    monkeypatch.setattr(srv._runtime, "topology_engine", engine, raising=False)

    srv._release_owner("test release")

    assert topology_stop.is_set()
    assert topology_thread.join_called is True
    assert topology_thread.timeout is not None
    assert topology_thread.is_alive() is False
    assert srv._runtime.topology_thread is None
    assert srv._runtime.topology_engine is None
    assert reader.stopped is True
    assert srv._runtime.monitors == {}


def test_main_releases_owner_when_mcp_run_returns(monkeypatch):
    reader = StubReader("COM_A", 115200, buffer=None)
    mon = SimpleNamespace(port="COM_A", reader=reader)

    monkeypatch.setattr(srv._runtime, "monitors", {"COM_A": mon})
    monkeypatch.setattr(srv._runtime, "viewer", None)
    monkeypatch.setattr(srv._runtime, "owner_active", True, raising=False)
    monkeypatch.setattr(srv._runtime, "lock_socket", None, raising=False)
    monkeypatch.setattr(srv._runtime, "hotplug_thread", None, raising=False)
    monkeypatch.setattr(srv, "_load_config", lambda env: cfg())
    monkeypatch.setattr(srv, "compile_autoname", lambda rules, log: [])
    monkeypatch.setattr(srv.mcp, "run", lambda: None)

    srv.main()

    assert reader.stopped is True
    assert srv._runtime.monitors == {}
    assert srv._runtime.viewer is None
    assert srv._runtime.owner_active is False


def test_main_releases_owner_when_mcp_run_raises(monkeypatch):
    class RunStopped(RuntimeError):
        pass

    reader = StubReader("COM_A", 115200, buffer=None)
    mon = SimpleNamespace(port="COM_A", reader=reader)

    monkeypatch.setattr(srv._runtime, "monitors", {"COM_A": mon})
    monkeypatch.setattr(srv._runtime, "viewer", None)
    monkeypatch.setattr(srv._runtime, "owner_active", True, raising=False)
    monkeypatch.setattr(srv._runtime, "lock_socket", None, raising=False)
    monkeypatch.setattr(srv._runtime, "hotplug_thread", None, raising=False)
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
    assert srv._runtime.monitors == {}
    assert srv._runtime.viewer is None
    assert srv._runtime.owner_active is False


def test_release_owner_is_idempotent(monkeypatch):
    reader = StubReader("COM_A", 115200, buffer=None)
    mon = SimpleNamespace(port="COM_A", reader=reader)

    monkeypatch.setattr(srv._runtime, "monitors", {"COM_A": mon})
    monkeypatch.setattr(srv._runtime, "viewer", None)
    monkeypatch.setattr(srv._runtime, "owner_active", True, raising=False)
    monkeypatch.setattr(srv._runtime, "lock_socket", None, raising=False)
    monkeypatch.setattr(srv._runtime, "hotplug_thread", None, raising=False)

    srv._release_owner("first")
    srv._release_owner("second")

    assert reader.stopped is True
    assert srv._runtime.monitors == {}
    assert srv._runtime.viewer is None
    assert srv._runtime.owner_active is False


def test_main_eof_without_owner_is_noop(monkeypatch):
    monkeypatch.setattr(srv._runtime, "monitors", {})
    monkeypatch.setattr(srv._runtime, "viewer", None)
    monkeypatch.setattr(srv._runtime, "owner_active", False, raising=False)
    monkeypatch.setattr(srv._runtime, "lock_socket", None, raising=False)
    monkeypatch.setattr(srv._runtime, "hotplug_thread", None, raising=False)
    monkeypatch.setattr(srv, "_load_config", lambda env: cfg())
    monkeypatch.setattr(srv, "compile_autoname", lambda rules, log: [])
    monkeypatch.setattr(srv.mcp, "run", lambda: None)

    srv.main()

    assert srv._runtime.monitors == {}
    assert srv._runtime.viewer is None
    assert srv._runtime.owner_active is False


def test_viewer_release_port_ignores_port_for_whole_session(monkeypatch):
    calls = []

    monkeypatch.setattr(srv, "_release_owner", lambda reason: calls.append(reason))

    out = srv._viewer_release_port("")

    assert out == {"status": "ok", "released": True}
    assert "release" in calls[0]
