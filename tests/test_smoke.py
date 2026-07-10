"""pytest 하네스·import 경로 검증용 최소 스모크."""


def test_package_imports():
    import serial_mcp

    assert serial_mcp.__version__


def test_core_modules_import():
    from serial_mcp.ring_buffer import LineBuffer  # noqa: F401
    from serial_mcp.server import get_recent_logs  # noqa: F401


def test_server_preserves_split_module_compatibility_aliases():
    import serial_mcp.config as config
    import serial_mcp.serial_reader as serial_reader
    import serial_mcp.server as server

    assert server.SerialReader is serial_reader.SerialReader
    assert server._env_int is config._env_int
    assert server._load_config is config._load_config


def test_diagnostics_log_uses_stderr_only(capsys):
    from serial_mcp.diagnostics import log

    log("smoke")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "[serial-mcp] smoke\n"
