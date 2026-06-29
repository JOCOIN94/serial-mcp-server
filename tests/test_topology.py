"""topology.py — 분류·번호추출·SB 병합·로스터 배치(순수 로직).

픽스처는 2026-06-26 실장비 캡처(COM4=SSM-ESP, COM12=SB-STM BayID5, COM14=SB-ESP UnID5)에서
채취한 실제 로그 줄이다(scratchpad/topology-capture-2026-06-26.md).
"""

from serial_mcp.topology import (
    build_roster,
    classify_device,
    classify_lines,
    identify_port,
    parse_alias,
)
from serial_mcp.topology_routing import RoutingTable

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


def test_classify_sb_esp_via_info0_not_passive_signature():
    # SB-ESP 는 고유 수동 시그니처가 없다([Tx-my INFO]/[WiFi_Rx]/Save 가 전 리프 공통).
    # classify_lines(수동 시그니처)만으론 미상이고, 식별은 INFO[0]=4(classify_device)로만 한다.
    assert classify_lines(SB_ESP_LINES) == (None, None)
    d = classify_device(SB_ESP_LINES)
    assert d["type"] == "SB" and d["mcu"] == "ESP" and d["source"] == "info_json"


def test_classify_device_info_less_leaf_window_is_unknown_not_sb():
    # INFO 없는 윈도에 전 리프 공통 태그만 있으면 SB 로 단정하지 않는다(APU/REPEAT 오분류 방지).
    assert classify_device(['[WiFi_Rx] {"INFO":"REQ","UnID":5}'])["type"] is None
    assert classify_device(["Save a new Reved-Packet for me."])["type"] is None


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
    # SSM/REPEAT/APU/APU_C 는 단일 ESP — 별칭에 칩 표기가 없어도 내부 라벨용 mcu=ESP
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


# ---- Phase B: DeviceClassifier (INFO[0] 장비타입 enum 기반 4단계) ----
# 펌웨어 enum: dTSSM=1 dTAPU=2 dTAPU_C_SLIM=3 dTSBB=4 dTRPT=5 (SSM_esp32.h:468-472)
# 각 장비의 자기 보고 [Tx - my INFO] 의 INFO[0] 이 이 타입숫자다.

def test_classify_device_info0_maps_sb():
    # INFO[0]="4" = dTSBB → SB (강한 증거 info_json, conf 높음)
    d = classify_device(['[Tx - my INFO] {"UnID":5,"INFO":["4","SB260526-002",-22],"Unique":15}'])
    assert d["type"] == "SB"
    assert d["mcu"] == "ESP"
    assert d["source"] == "info_json"
    assert d["confidence"] >= 0.9


def test_classify_device_info0_maps_apu_and_apu_c():
    assert classify_device(['[Tx - my INFO] {"INFO":["2","X"],"Unique":1}'])["type"] == "APU"
    assert classify_device(['[Tx - my INFO] {"INFO":["3","X"],"Unique":1}'])["type"] == "APU_C"


def test_classify_device_info0_repeater_type5():
    # dTRPT=5 — simplevInfoBuffer 가 숫자 5로 흘리므로 "5"→REPEAT 매핑 필수
    d = classify_device(['[Tx - my INFO] {"INFO":["5","REP-1"],"Unique":1}'])
    assert d["type"] == "REPEAT"


def test_classify_device_info0_ignores_relayed_info_on_ssm():
    # 오인 방지: SSM [Proc-WiFiRx] 가 SB의 INFO를 중계 인용해도 SB로 오분류 금지.
    # INFO[0] 추출은 자기 보고 [Tx - my INFO] 일 때만 → SSM 은 시그니처로 SSM 분류.
    d = classify_device(SSM_LINES)
    assert d["type"] == "SSM"


def test_classify_device_ssm_table_and_signature():
    assert classify_device(["<< Information on the entire equipment >>"])["type"] == "SSM"
    assert classify_device(['[Proc-WebRTx] ["message",{}]'])["type"] == "SSM"


def test_classify_device_stm32_banner_is_sb_stm():
    # SSM 없이 SB 단독 연결(standalone): STM32 배너로 SB/STM 분류
    d = classify_device(["SmartBay FW v2.34", "BayID:5,"])
    assert d["type"] == "SB"
    assert d["mcu"] == "STM"
    assert d["source"] == "stm32_banner"


def test_classify_device_unknown_is_zero_confidence():
    d = classify_device(["random noise 12345"])
    assert d["type"] is None
    assert d["confidence"] == 0.0


def test_classify_device_alias_is_manual_highest_priority():
    # 명시 별칭 최우선(manual, conf 1.0), REP→REPEAT 정규화
    d = classify_device(['[Tx - my INFO] {"INFO":["4"],"Unique":1}'], alias="REP1")
    assert d["type"] == "REPEAT"
    assert d["source"] == "manual"
    assert d["confidence"] == 1.0


def test_identify_port_carries_confidence_and_source():
    d = identify_port("COM14", None, ['[Tx - my INFO] {"UnID":5,"INFO":["4","X"],"Unique":15}'], True)
    assert d["type"] == "SB" and d["number"] == 5
    assert d["type_source"] == "info_json"
    assert d["type_confidence"] >= 0.9


def test_classify_device_unknown_enum_not_mislabeled_sb():
    # 자기 보고했으나 미지/미래 enum(현 펌웨어 1~5 밖) → over-broad 시그니처로 SB 단정 금지, 미상.
    d = classify_device(['[Tx - my INFO] {"INFO":["6","X"],"Unique":1}'])
    assert d["type"] is None
    assert d["confidence"] == 0.0


def test_classify_device_info0_robust_to_nested_object_before_info():
    # INFO 키 앞에 중첩 객체가 와도 JSON 파싱으로 INFO[0]을 읽어 강증거(info_json) 유지.
    d = classify_device(['[Tx - my INFO] {"obj":{"a":1},"INFO":["4"],"Unique":1}'])
    assert d["type"] == "SB"
    assert d["source"] == "info_json"


def test_identify_sb_alias_without_chip_extracts_bayid():
    # 칩 미표기 SB 별칭 + STM 로그: mcu 미상이어도 BayID 로 번호 보강(둘 다 시도).
    d = identify_port("COM12", "SB", ["BayID:5,"], True)
    assert d["type"] == "SB" and d["number"] == 5


# ---- Phase B 모듈5: roster 확장(group kind · edges · 원격 mesh 노드 · 노드 enrich) ----

def _ev(kind="rx", ts=1.0, unid=None, unique=1, mac=None, passed=None, route=None, reprssi=None):
    """roster 테스트용 최소 Event(routing.observe 입력)."""
    return {"kind": kind, "ts": ts, "route": route,
            "ids": {"unid": unid, "unique": unique, "mac": mac},
            "hints": {"passed": passed},
            "metrics": {"reprssi": reprssi or []}}


def test_roster_group_kind_ssm():
    r = build_roster(_live_entries())
    assert r["groups"][0]["kind"] == "ssm"


def test_roster_group_kind_standalone_when_no_ssm():
    # SB 단독(SSM 부재) → standalone 그룹(실패 아님, SSM group 아님).
    entries = [{"port": "COM14", "alias": None, "lines": SB_ESP_LINES, "connected": True}]
    r = build_roster(entries)
    assert len(r["groups"]) == 1
    assert r["groups"][0]["kind"] == "standalone" and r["groups"][0]["ssm_port"] is None


def test_roster_no_routing_empty_edges_no_remote():
    # routing 미전달(Phase A 호출부) → edges 빈 리스트 + 원격 노드 없음(모두 직접연결 ports 보유).
    r = build_roster(_live_entries())
    g = r["groups"][0]
    assert g["edges"] == []
    assert all(n["ports"] for n in g["nodes"])


def test_roster_edges_from_routing_link_graph():
    rt = RoutingTable()
    rt.observe(_ev(kind="route", ts=1.0, route={"from_mac": "AA", "to_mac": "BB", "rssi": -41}))
    r = build_roster(_live_entries(), routing=rt, now=2.0)
    edges = r["groups"][0]["edges"]
    assert len(edges) == 1
    e = edges[0]
    assert e["from"] == "AA" and e["to"] == "BB" and e["rssi"] == -41 and e["fresh"] is True


def test_roster_remote_node_from_passed_device():
    # SB5 직접연결 + REP1 은 [Passed Device] 로만 등장하는 원격 mesh 노드(직접 포트 없음).
    rt = RoutingTable()
    rt.observe(_ev(kind="rx", unid=5, mac="AA:BB", passed="(05-SB5)->(01-REP1)"))
    r = build_roster(_live_entries(), routing=rt, now=2.0)
    rep = [n for n in r["groups"][0]["nodes"] if n["type"] == "REPEAT"]
    assert len(rep) == 1
    assert rep[0]["ports"] == [] and rep[0]["status"] == "unknown"
    assert rep[0]["route_token"] == "01" and rep[0]["label"] == "REPEAT1"
    assert rep[0]["unit_id"] == 1                       # 토큰 entry unid 없으면 이름 번호로 폴백
    assert rep[0]["type_source"] == "route_name"        # 출처=[Passed Device] 이름 해소(ssm_table 아님)


def test_roster_remote_node_mac_from_token_entry():
    # 원격 REP1 이 자기 UnID/Mac 도 등록(SSM 이 중계 관측) → 원격노드에 mac·unit_id 전파.
    rt = RoutingTable()
    rt.observe(_ev(kind="rx", unid=5, mac="AA:BB", passed="(05-SB5)->(01-REP1)"))
    rt.observe(_ev(kind="rx", unid=1, mac="DD:EE:FF"))   # 토큰 '01' 에 unid+mac 병합
    rep = [n for n in build_roster(_live_entries(), routing=rt, now=2.0)["groups"][0]["nodes"]
           if n["type"] == "REPEAT"][0]
    assert rep["unit_id"] == 1 and rep["mac"] == "DD:EE:FF"


def test_roster_unnumbered_remote_same_type_not_collapsed():
    # 번호 없는 동일타입 원격 둘(토큰 01/02)은 별개 노드 — (type,None)로 합치지 말 것.
    rt = RoutingTable()
    rt.observe(_ev(kind="rx", unid=5, mac="AA:BB", passed="(01-REP)->(02-REP)"))
    rep = [n for n in build_roster(_live_entries(), routing=rt, now=2.0)["groups"][0]["nodes"]
           if n["type"] == "REPEAT"]
    assert {n["route_token"] for n in rep} == {"01", "02"}   # 둘 다 생존(붕괴 없음)


def test_roster_remote_deduped_against_direct_node():
    # 직접연결 SB5 가 [Passed Device] 에도 등장 → 원격 노드로 중복 생성 금지.
    rt = RoutingTable()
    rt.observe(_ev(kind="rx", unid=5, mac="AA:BB", passed="(05-SB5)"))
    r = build_roster(_live_entries(), routing=rt, now=2.0)
    sb = [n for n in r["groups"][0]["nodes"] if n["type"] == "SB"]
    assert len(sb) == 1 and sb[0]["ports"]      # 직접 SB5 하나뿐(원격 중복 없음)


def test_roster_direct_node_enriched_mac_unit_token():
    rt = RoutingTable()
    rt.observe(_ev(kind="rx", unid=5, mac="AA:BB:CC", passed="(05-SB5)"))
    r = build_roster(_live_entries(), routing=rt, now=2.0)
    sb = [n for n in r["groups"][0]["nodes"] if n["type"] == "SB"][0]
    assert sb["unit_id"] == 5 and sb["route_token"] == "05" and sb["mac"] == "AA:BB:CC"


def test_roster_node_carries_type_confidence_source():
    sb = [n for n in build_roster(_live_entries())["groups"][0]["nodes"] if n["type"] == "SB"][0]
    assert sb["type_source"] == "info_json" and sb["type_confidence"] >= 0.9


def test_roster_standalone_group_has_empty_edges():
    # SSM 부재 standalone 그룹엔 링크그래프 간선이 없다(REPRSSI/[Route] Link 는 SSM 발신).
    rt = RoutingTable()
    rt.observe(_ev(kind="route", ts=1.0, route={"from_mac": "AA", "to_mac": "BB", "rssi": -41}))
    entries = [{"port": "COM14", "alias": None, "lines": SB_ESP_LINES, "connected": True}]
    r = build_roster(entries, routing=rt, now=2.0)
    assert r["groups"][0]["kind"] == "standalone" and r["groups"][0]["edges"] == []
