"""_load_config()/_env_int() — 환경변수 계약 고정(SPEC §3/§4, README 환경변수표).

env를 인자로 주입하는 순수 함수라 os.environ 변경 없이 결정적으로 검증한다.
"""

import pytest

from serial_mcp.server import _env_int, _load_config


# ---- _env_int ----

def test_env_int_parses_valid():
    assert _env_int({"X": "9600"}, "X", 115200) == 9600


def test_env_int_missing_returns_default():
    assert _env_int({}, "X", 115200) == 115200


def test_env_int_blank_returns_default():
    assert _env_int({"X": "  "}, "X", 115200) == 115200


def test_env_int_invalid_returns_default():
    assert _env_int({"X": "fast"}, "X", 115200) == 115200


# ---- _load_config ----

def test_load_config_defaults_when_empty():
    assert _load_config({}) == {
        "port": "", "baud": 115200, "tee": None, "exclude": None,
        "include": None, "maxlen": 2000, "dedup": True, "web": 8743,
    }


def test_load_config_reads_all_vars():
    cfg = _load_config({
        "SERIAL_PORT": "COM4", "SERIAL_BAUD": "9600", "SERIAL_TEE": "log.txt",
        "SERIAL_EXCLUDE": "DEBUG", "SERIAL_INCLUDE": "ERROR",
        "SERIAL_BUFFER_LINES": "500", "SERIAL_DEDUP": "0", "SERIAL_WEB": "9000",
    })
    assert cfg == {
        "port": "COM4", "baud": 9600, "tee": "log.txt", "exclude": "DEBUG",
        "include": "ERROR", "maxlen": 500, "dedup": False, "web": 9000,
    }


def test_load_config_strips_port_whitespace():
    assert _load_config({"SERIAL_PORT": "  COM4  "})["port"] == "COM4"


@pytest.mark.parametrize(
    "val,expected",
    [
        ("0", False), ("false", False), ("FALSE", False), ("no", False),
        ("off", False), ("1", True), ("true", True), ("yes", True), ("", True),
    ],
)
def test_load_config_dedup_truthiness(val, expected):
    assert _load_config({"SERIAL_DEDUP": val})["dedup"] is expected


# ---- SERIAL_WEB (웹 뷰어 포트) ----

def test_load_config_web_default_on():
    assert _load_config({})["web"] == 8743


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "OFF"])
def test_load_config_web_disabled(val):
    assert _load_config({"SERIAL_WEB": val})["web"] is None


def test_load_config_web_custom_port():
    assert _load_config({"SERIAL_WEB": "9000"})["web"] == 9000


def test_load_config_web_invalid_falls_back_to_default():
    assert _load_config({"SERIAL_WEB": "abc"})["web"] == 8743
