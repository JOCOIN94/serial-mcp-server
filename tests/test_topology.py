"""topology.py — 분류·번호추출·SB 병합·로스터 배치(순수 로직).

픽스처는 2026-06-26 실장비 캡처(COM4=SSM-ESP, COM12=SB-STM BayID5, COM14=SB-ESP UnID5)에서
채취한 실제 로그 줄이다(scratchpad/topology-capture-2026-06-26.md).
"""

from serial_mcp.topology import (
    build_roster,
    classify_lines,
    identify_port,
    parse_alias,
)

# ---- 실측 로그 픽스처 ----
SSM_LINES = [
    '[Proc-WiFiRx] {"UnID":5,"INFO":["4","SB260526-002",-22,false,false,"0",false],"Unique":15,"Rev":true,"Cidx":861}',
    "<<< From SB1.",
    "[Route] Link A0,85,E3,EA,5C,C4 -> 10,06,1C,16,97,AC rssi=-41",
    ' -- Checking finished',
]
SB_ESP_LINES = [
    '[Tx - my INFO] {"UnID":5,"INFO":["4","SB260526-002",-22,false,false,"0",false],"Unique":15}',
    '[WiFi_Rx] {"RTC":[26,46,14,26,6,2026],"CHANNEL":"11","INFO":"REQ","UnID":5,"Cidx":475}',
    "Save a new Reved-Packet for me.",
]
SB_STM_LINES = [
    "BayID:5,",
    "< MasterCard >",
    "Price1st:3000,",
    "minCoinSensingTime:25,",
]


# ---- 별칭 파싱(명시 식별 우선) ----

def test_parse_alias_sb_with_chip_and_number():
    assert parse_alias("SB1-ESP") == ("SB", 1, "ESP")
    assert parse_alias("SB1-STM") == ("SB", 1, "STM")


def test_parse_alias_ssm_and_apu():
    assert parse_alias("SSM-ESP") == ("SSM", None, "ESP")
    assert parse_alias("SSM") == ("SSM", None, None)
    assert parse_alias("APU3") == ("APU", 3, None)


def test_parse_alias_unknown_or_empty():
    assert parse_alias("COM14") == (None, None, None)
    assert parse_alias("") == (None, None, None)
    assert parse_alias(None) == (None, None, None)


# ---- 로그 내용 자동발견 ----

def test_classify_ssm_from_logs():
    assert classify_lines(SSM_LINES) == ("SSM", "ESP")


def test_classify_sb_esp_from_logs():
    assert classify_lines(SB_ESP_LINES) == ("SB", "ESP")


def test_classify_sb_stm_from_logs():
    assert classify_lines(SB_STM_LINES) == ("SB", "STM")


def test_classify_empty_is_unknown():
    assert classify_lines([]) == (None, None)
    assert classify_lines(["random noise 12345"]) == (None, None)


# ---- 정체 추정(별칭 우선 + 번호 보강) ----

def test_identify_uses_logs_when_no_alias():
    d = identify_port("COM14", None, SB_ESP_LINES, connected=True)
    assert d["type"] == "SB" and d["mcu"] == "ESP" and d["number"] == 5


def test_identify_sb_stm_number_from_bayid():
    d = identify_port("COM12", None, SB_STM_LINES, connected=True)
    assert d["type"] == "SB" and d["mcu"] == "STM" and d["number"] == 5


def test_identify_alias_beats_logs():
    # 별칭이 명시되면 로그 자동발견보다 우선
    d = identify_port("COM9", "SB2-STM", SB_ESP_LINES, connected=True)
    assert d["type"] == "SB" and d["mcu"] == "STM" and d["number"] == 2


def test_ssm_number_stays_none():
    d = identify_port("COM4", None, SSM_LINES, connected=True)
    assert d["type"] == "SSM" and d["number"] is None


def test_single_mcu_defaults_esp():
    # SSM/REP/APU 는 단일 ESP — 별칭에 칩 표기가 없어도 내부 라벨용 mcu=ESP
    assert identify_port("COM20", "REP1", [], True)["mcu"] == "ESP"
    assert identify_port("COM4", None, SSM_LINES, True)["mcu"] == "ESP"


# ---- 로스터 배치(실측 구성: SSM 1 + SB(ESP+STM) 1) ----

def _live_entries():
    return [
        {"port": "COM4", "alias": None, "lines": SSM_LINES, "connected": True},
        {"port": "COM12", "alias": None, "lines": SB_STM_LINES, "connected": True},
        {"port": "COM14", "alias": None, "lines": SB_ESP_LINES, "connected": True},
    ]


def test_roster_single_group_one_ssm():
    r = build_roster(_live_entries())
    assert len(r["groups"]) == 1
    assert r["groups"][0]["ssm_port"] == "COM4"


def test_roster_sb_esp_stm_merged_into_one_node():
    r = build_roster(_live_entries())
    nodes = r["groups"][0]["nodes"]
    sb = [n for n in nodes if n["type"] == "SB"]
    assert len(sb) == 1                       # ESP+STM → 한 노드
    ports = {p["mcu"]: p["port"] for p in sb[0]["ports"]}
    assert ports == {"ESP": "COM14", "STM": "COM12"}
    assert [p["mcu"] for p in sb[0]["ports"]] == ["ESP", "STM"]   # 발견순 무관, ESP→STM 고정
    assert sb[0]["label"] == "SB5"            # BayID/UnID 5


def test_roster_rows_by_type():
    r = build_roster(_live_entries())
    by_type = {n["type"]: n for n in r["groups"][0]["nodes"]}
    assert by_type["SSM"]["row"] == 0
    assert by_type["SB"]["row"] == 4


def test_roster_unplaced_when_no_signature():
    entries = [{"port": "COM4", "alias": None, "lines": SSM_LINES, "connected": True},
               {"port": "COM99", "alias": None, "lines": ["garbage only"], "connected": True}]
    r = build_roster(entries)
    assert "COM99" in r["unplaced"]
