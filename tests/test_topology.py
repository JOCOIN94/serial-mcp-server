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
# SB-STM 런타임(카드 동작) — 부팅/설정 시그니처(BayID 등) 없이 카드만 처리하는 윈도.
# 2026-06-30 라이브 캡처(COM13). 'Send state of STM32'=SB-SmartBay 전용, 'Released to touch
# Card'=STM main.c 전용(cbm 검증). 'Check the Card'는 APU/SSM 에도 있어 분류 토큰에서 제외.
SB_STM_RUNTIME_LINES = [
    "This Card is a Old card.",
    "Check the Card. - Our Card 1.",
    "Send state of STM32 : 0x0005",
    "Released to touch Card.",
    "Lower Disp. Step : 2",
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


def test_classify_sb_stm_from_runtime_card_logs():
    # 부팅/설정 시그니처 없이 카드 동작만 있는 런타임 윈도도 SB-STM 으로 잡아야 한다
    # (라이브서 COM13 SB-STM 이 unplaced 였던 결함). SB/STM 전용 토큰 기반.
    assert classify_lines(SB_STM_RUNTIME_LINES) == ("SB", "STM")
    d = classify_device(SB_STM_RUNTIME_LINES)
    assert d["type"] == "SB" and d["mcu"] == "STM"


def test_runtime_stm_tokens_do_not_misclassify_ssm():
    # SB/STM 전용 토큰은 SSM 윈도를 SB 로 끌어오면 안 된다(over-broad 금지).
    assert classify_lines(SSM_LINES) == ("SSM", "ESP")
    assert classify_device(SSM_LINES)["type"] == "SSM"


def test_classify_empty_is_unknown():
    assert classify_lines([]) == (None, None)
    assert classify_lines(["random noise 12345"]) == (None, None)


# ---- 정체 추정(별칭 우선 + 번호 보강) ----

def test_identify_uses_logs_when_no_alias():
    d = identify_port("COM14", None, SB_ESP_LINES, connected=True)
    assert d["type"] == "SB" and d["mcu"] == "ESP" and d["number"] == 5


def test_identify_sb_stm_number_from_logs_is_none():
    # STM 은 정상 로그에 BayID 를 안 흘려(펌웨어 검증) 로그로 번호를 못 얻는다 — 번호는 카드상관 전담.
    d = identify_port("COM12", None, SB_STM_LINES, connected=True)
    assert d["type"] == "SB" and d["mcu"] == "STM" and d["number"] is None


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


def test_roster_sb_esp_stm_merged_via_card_pairing():
    entries = _live_entries()
    # 페어링 전: STM 은 로그에 번호가 없어 ESP 와 안 합쳐짐 → 별도 무번호 SB 노드(2개).
    sb0 = [n for n in build_roster(entries)["groups"][0]["nodes"] if n["type"] == "SB"]
    assert len(sb0) == 2
    # 카드상관 페어링(COM12→bay5) → ESP(UnID5)와 한 노드로 병합.
    nodes = build_roster(entries, pairing={"COM12": 5})["groups"][0]["nodes"]
    sb = [n for n in nodes if n["type"] == "SB"]
    assert len(sb) == 1                       # ESP+STM → 한 노드
    ports = {p["mcu"]: p["port"] for p in sb[0]["ports"]}
    assert ports == {"ESP": "COM14", "STM": "COM12"}
    assert [p["mcu"] for p in sb[0]["ports"]] == ["ESP", "STM"]   # 발견순 무관, ESP→STM 고정
    assert sb[0]["label"] == "SB5"            # UnID/페어링 번호 5
    assert sb[0]["number_collision"] is False  # ESP+STM 같은 베이 → 충돌 아님


def test_roster_two_sb_esp_same_unid_not_merged():
    # 같은 BayID(UnID=5)인 서로 다른 베이의 SB-ESP 둘은 병합 금지(UnID=사용자설정 BayID 충돌).
    # 포트가 타이브레이커 → 별개 노드 2개, id 유일, number_collision 표시.
    entries = [
        {"port": "COM12", "alias": None, "lines": SB_ESP_LINES, "connected": True},
        {"port": "COM20", "alias": None, "lines": SB_ESP_LINES, "connected": True},
    ]
    sb = [n for n in build_roster(entries)["groups"][0]["nodes"] if n["type"] == "SB"]
    assert len(sb) == 2                              # 병합 금지
    assert len({n["id"] for n in sb}) == 2           # id 유일(포트로 유일화)
    assert all(n["number_collision"] for n in sb)    # 충돌 플래그
    assert all(len(n["ports"]) == 1 for n in sb)     # 각자 자기 포트만
    assert sorted(p["port"] for n in sb for p in n["ports"]) == ["COM12", "COM20"]


def test_roster_multi_ssm_places_leaf_by_membership():
    # 멀티-SSM: membership(SSM포트→leaf)로 각 leaf 를 자기 SSM 그룹에 배치(전부 첫 그룹에 몰지 않음).
    entries = [
        {"port": "COM4", "alias": "SSM", "lines": [], "connected": True},
        {"port": "COM9", "alias": "SSM", "lines": [], "connected": True},
        {"port": "COM12", "alias": "SB1-ESP", "lines": [], "connected": True},
        {"port": "COM20", "alias": "SB2-ESP", "lines": [], "connected": True},
    ]
    membership = {
        "COM4": {1: {"device_type": "4", "local_port": "COM12", "last_ts": 1.0}},
        "COM9": {2: {"device_type": "4", "local_port": "COM20", "last_ts": 1.0}},
    }
    r = build_roster(entries, membership=membership)
    assert len(r["groups"]) == 2
    by_ssm = {g["ssm_port"]: g for g in r["groups"]}
    com4_ports = {p["port"] for n in by_ssm["COM4"]["nodes"] for p in n["ports"]}
    com9_ports = {p["port"] for n in by_ssm["COM9"]["nodes"] for p in n["ports"]}
    assert "COM12" in com4_ports and "COM12" not in com9_ports
    assert "COM20" in com9_ports and "COM20" not in com4_ports


def test_roster_multi_ssm_without_membership_falls_back_first_group():
    # membership 없으면 현재 동작(첫 그룹 귀속) 폴백 — 하위호환.
    entries = [
        {"port": "COM4", "alias": "SSM", "lines": [], "connected": True},
        {"port": "COM9", "alias": "SSM", "lines": [], "connected": True},
        {"port": "COM12", "alias": "SB1-ESP", "lines": [], "connected": True},
    ]
    r = build_roster(entries)
    ports = {p["port"] for n in r["groups"][0]["nodes"] for p in n["ports"]}
    assert "COM12" in ports


def test_roster_membership_most_recent_ssm_wins():
    # leaf 가 두 SSM 에 들렸으면(시간차) last_ts 가 더 최근인 SSM 그룹에 배치.
    entries = [
        {"port": "COM4", "alias": "SSM", "lines": [], "connected": True},
        {"port": "COM9", "alias": "SSM", "lines": [], "connected": True},
        {"port": "COM12", "alias": "SB1-ESP", "lines": [], "connected": True},
    ]
    membership = {
        "COM4": {1: {"device_type": "4", "local_port": "COM12", "last_ts": 1.0}},
        "COM9": {1: {"device_type": "4", "local_port": "COM12", "last_ts": 9.0}},  # 더 최근
    }
    r = build_roster(entries, membership=membership)
    by_ssm = {g["ssm_port"]: g for g in r["groups"]}
    assert any(p["port"] == "COM12" for n in by_ssm["COM9"]["nodes"] for p in n["ports"])
    assert all(p["port"] != "COM12" for n in by_ssm["COM4"]["nodes"] for p in n["ports"])


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


def test_identify_sb_alias_without_chip_no_bayid_number():
    # 칩 미표기 SB 별칭 + BayID 로그: STM BayID 번호경로 제거로 번호 보강 안 됨(UnID 만 시도) → None.
    d = identify_port("COM12", "SB", ["BayID:5,"], True)
    assert d["type"] == "SB" and d["number"] is None


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


def test_roster_edges_from_membership_port_pairs():
    # 정적 링크선 = correlator 가 (UnID,Unique) TX↔RX 로 관측한 leaf↔SSM 포트쌍(멤버십).
    # REPRSSI 무선 이웃을 강제 링크로 긋지 않는다(plan §3, 사용자 강조: 링크 고정 강제 금지).
    membership = {"COM4": {5: {"device_type": "4", "local_port": "COM14", "last_ts": 1.0}}}
    r = build_roster(_live_entries(), membership=membership, now=2.0)
    edges = r["groups"][0]["edges"]
    assert len(edges) == 1
    e = edges[0]
    assert e["from"] == "COM14" and e["to"] == "COM4" and e["fresh"] is True


def test_roster_edges_stale_when_last_ts_old():
    # 오래된 관측(last_ts)은 fresh=False → 프론트가 옅게. 관측 이력은 유지하되 최신성 감쇠(고정 아님).
    membership = {"COM4": {5: {"device_type": "4", "local_port": "COM14", "last_ts": 1.0}}}
    e = build_roster(_live_entries(), membership=membership, now=100.0)["groups"][0]["edges"][0]
    assert e["fresh"] is False


def test_roster_edges_carry_rssi():
    # 멤버십 rssi → edge rssi(프론트 rssiColor 로 링크 품질색: 강=초록·약=빨강). None 이면 회색.
    membership = {"COM4": {5: {"device_type": "4", "local_port": "COM14", "last_ts": 1.0, "rssi": -42}}}
    e = build_roster(_live_entries(), membership=membership, now=2.0)["groups"][0]["edges"][0]
    assert e["rssi"] == -42


def test_roster_remote_node_from_passed_device():
    # SB5 직접연결 + REP1 은 [Passed Device] 로만 등장하는 원격 mesh 노드(직접 포트 없음).
    rt = RoutingTable()
    rt.observe(_ev(kind="rx", unid=5, mac="AA:BB", passed="(05-SB5)->(01-REP1)"))
    r = build_roster(_live_entries(), routing=rt, now=2.0)
    rep = [n for n in r["groups"][0]["nodes"] if n["type"] == "REPEAT"]
    assert len(rep) == 1
    assert rep[0]["ports"] == [] and rep[0]["status"] == "unknown"
    # 라벨은 메시 해소 이름(REP1) — type 정규화('REPEAT')가 아니라 hop/[Passed Device] 표기와 일치(P2-1).
    assert rep[0]["route_token"] == "01" and rep[0]["label"] == "REP1"
    assert rep[0]["type"] == "REPEAT"                   # type 은 정규화 enum(라벨과 별개)
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
    r = build_roster(_live_entries(), routing=rt, pairing={"COM12": 5}, now=2.0)
    sb = [n for n in r["groups"][0]["nodes"] if n["type"] == "SB"]
    assert len(sb) == 1 and sb[0]["ports"]      # 직접 SB5(ESP+STM 병합) 하나뿐(원격 중복 없음)


def test_roster_direct_node_enriched_mac_unit_token():
    rt = RoutingTable()
    rt.observe(_ev(kind="rx", unid=5, mac="AA:BB:CC", passed="(05-SB5)"))
    r = build_roster(_live_entries(), routing=rt, now=2.0)
    sb = [n for n in r["groups"][0]["nodes"] if n["type"] == "SB"][0]
    assert sb["unit_id"] == 5 and sb["route_token"] == "05" and sb["mac"] == "AA:BB:CC"


def test_roster_label_prefers_routing_resolved_mesh_name():
    # 메시가 UnID 5 를 'SB1' 로 부르면([Passed Device] 토큰 05 해소) 같은 장비를 SB5(직접)·SB1(원격)
    # 으로 중복 생성하지 않고, 살아남은 노드 라벨은 메시 이름(SB1)·UnID 는 unit_id 메타로 둔다.
    rt = RoutingTable()
    rt.observe(_ev(kind="rx", unid=5, mac="AA:BB", passed="(05-SB1)"))
    sb = [n for n in build_roster(_live_entries(), routing=rt, pairing={"COM12": 5}, now=2.0)["groups"][0]["nodes"]
          if n["type"] == "SB"]
    assert len(sb) == 1                      # 같은 토큰(05) → 직접·원격 중복 없음
    assert sb[0]["label"] == "SB1"           # 메시 해소 이름 우선
    assert sb[0]["unit_id"] == 5             # UnID 는 식별 권위가 아니라 메타로 보존


def test_roster_collision_label_disambiguated_by_port():
    # BayID 충돌 노드는 라벨도 포트로 구분(똑같은 'SB5' 둘이 안 보이게).
    entries = [
        {"port": "COM12", "alias": None, "lines": SB_ESP_LINES, "connected": True},
        {"port": "COM20", "alias": None, "lines": SB_ESP_LINES, "connected": True},
    ]
    sb = [n for n in build_roster(entries)["groups"][0]["nodes"] if n["type"] == "SB"]
    assert {n["label"] for n in sb} == {"SB5 (COM12)", "SB5 (COM20)"}


def test_roster_node_carries_type_confidence_source():
    sb = [n for n in build_roster(_live_entries())["groups"][0]["nodes"] if n["type"] == "SB"][0]
    assert sb["type_source"] == "info_json" and sb["type_confidence"] >= 0.9


def test_roster_standalone_group_has_empty_edges():
    # SSM 부재 standalone 그룹엔 링크선이 없다(멤버십 edge 는 SSM 그룹 한정 — ssm_port 가 None).
    membership = {"COM4": {5: {"local_port": "COM14", "last_ts": 1.0}}}   # SSM 노드 없어 매칭 안 됨
    entries = [{"port": "COM14", "alias": None, "lines": SB_ESP_LINES, "connected": True}]
    r = build_roster(entries, membership=membership, now=2.0)
    assert r["groups"][0]["kind"] == "standalone" and r["groups"][0]["edges"] == []
