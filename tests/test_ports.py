"""ports.py — USB 포트 스캔 필터·SERIAL_PORT/SERIAL_NAMES 파싱·별칭(순수 로직)."""

from types import SimpleNamespace

from serial_mcp.ports import auto_usb_ports, label, name_for, parse_names, parse_port_list


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
