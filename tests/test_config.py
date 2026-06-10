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
        "ports": [], "names": {}, "baud": 115200, "tee": None, "exclude": None,
        "include": None, "maxlen": 2000, "dedup": 5, "web": 8743,
    }


def test_load_config_reads_all_vars():
    cfg = _load_config({
        "SERIAL_PORT": "COM4,COM13@9600", "SERIAL_NAMES": "COM4=SSM",
        "SERIAL_BAUD": "57600", "SERIAL_TEE": "log.txt",
        "SERIAL_EXCLUDE": "DEBUG", "SERIAL_INCLUDE": "ERROR",
        "SERIAL_BUFFER_LINES": "500", "SERIAL_DEDUP": "0", "SERIAL_WEB": "9000",
    })
    assert cfg == {
        "ports": [("COM4", None), ("COM13", 9600)], "names": {"COM4": "SSM"},
        "baud": 57600, "tee": "log.txt", "exclude": "DEBUG", "include": "ERROR",
        "maxlen": 500, "dedup": 0, "web": 9000,
    }


def test_load_config_strips_port_whitespace():
    assert _load_config({"SERIAL_PORT": "  COM4  "})["ports"] == [("COM4", None)]


@pytest.mark.parametrize(
    "env,expected",
    [
        ({}, 5),                              # 미설정 → 기본 룩백 5
        ({"SERIAL_DEDUP": "0"}, 0),
        ({"SERIAL_DEDUP": "false"}, 0),
        ({"SERIAL_DEDUP": "no"}, 0),
        ({"SERIAL_DEDUP": "off"}, 0),
        ({"SERIAL_DEDUP": "1"}, 1),           # 구버전: 직전 줄만
        ({"SERIAL_DEDUP": "true"}, 1),
        ({"SERIAL_DEDUP": "yes"}, 1),
        ({"SERIAL_DEDUP": "12"}, 12),
        ({"SERIAL_DEDUP": "abc"}, 5),         # 해석 실패 → 기본
        ({"SERIAL_DEDUP": "-3"}, 5),          # 음수 → 기본
    ],
)
def test_load_config_dedup_window(env, expected):
    assert _load_config(env)["dedup"] == expected


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
