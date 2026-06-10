"""ports.py — USB 포트 스캔 필터·SERIAL_PORT/SERIAL_NAMES 파싱·별칭(순수 로직)."""

from types import SimpleNamespace

from serial_mcp.ports import (
    auto_usb_ports,
    compile_autoname,
    first_autoname_match,
    label,
    name_for,
    parse_autoname,
    parse_names,
    parse_port_list,
)


def test_parse_port_list_single():
    assert parse_port_list("COM4") == [("COM4", None)]


def test_parse_port_list_multi_with_baud():
    assert parse_port_list("COM4, COM13@9600") == [("COM4", None), ("COM13", 9600)]


def test_parse_port_list_empty_means_auto():
    assert parse_port_list("") == []


def test_parse_port_list_bad_baud_becomes_none():
    assert parse_port_list("COM4@fast") == [("COM4", None)]


def test_auto_usb_ports_filters_by_vid():
    ports = [
        SimpleNamespace(device="COM4", vid=0x1A86),
        SimpleNamespace(device="COM5", vid=None),      # 블루투스 가상 — 제외
        SimpleNamespace(device="COM13", vid=0x067B),
    ]
    assert auto_usb_ports(ports) == ["COM4", "COM13"]


def test_parse_names_port_and_serial_keys():
    # 키는 대문자 정규화, '=' 없는 항목은 무시
    assert parse_names("com4=SSM, 5909024173=SSM2,bad") == {"COM4": "SSM", "5909024173": "SSM2"}


def test_name_for_prefers_port_key_then_serial_number():
    names = {"COM4": "SSM", "5909024173": "BYSERIAL"}
    assert name_for("com4", "5909024173", names) == "SSM"     # 포트명 키 우선
    assert name_for("COM9", "5909024173", names) == "BYSERIAL"
    assert name_for("COM9", None, names) is None


def test_label_formats():
    assert label("COM4", "SSM") == "SSM (COM4)"
    assert label("COM13", None) == "COM13"


# ---- SERIAL_AUTONAME (로그 내용 기반 보드 자동 식별) ----

def test_parse_autoname_semicolon_separated_rules():
    # 구분자는 세미콜론 — 정규식 안에 쉼표({1,3} 등)가 올 수 있어서
    assert parse_autoname(r"SSM=\[Proc-; SB1=Send to the STM32|x{1,3}") == [
        ("SSM", r"\[Proc-"),
        ("SB1", r"Send to the STM32|x{1,3}"),
    ]


def test_parse_autoname_skips_malformed_items():
    assert parse_autoname("SSM=; =pat; bad; SB=ok") == [("SB", "ok")]


def test_parse_autoname_empty():
    assert parse_autoname("") == []


def test_compile_autoname_skips_invalid_regex():
    logged = []
    rules = compile_autoname([("A", "good"), ("B", "[")], log=logged.append)
    assert [name for name, _ in rules] == ["A"]
    assert len(logged) == 1 and "B" in logged[0]


def test_first_autoname_match_order_and_none():
    rules = compile_autoname([("SSM", r"\[Proc-"), ("SB1", r"STM32")])
    assert first_autoname_match("[Proc-WiFiRx] {...}", rules) == "SSM"
    assert first_autoname_match("***Send to the STM32 to request.", rules) == "SB1"
    assert first_autoname_match("...", rules) is None
