"""pytest 하네스·import 경로 검증용 최소 스모크."""


def test_package_imports():
    import serial_mcp

    assert serial_mcp.__version__


def test_core_modules_import():
    from serial_mcp.ring_buffer import LineBuffer  # noqa: F401
    from serial_mcp.server import get_recent_logs  # noqa: F401
