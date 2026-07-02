"""topology_routing.py — 링크 그래프·토큰맵·RSSI ladder(순수 stateful, Phase B 모듈4).

펌웨어 §4·§13 근거를 합성/실측 픽스처로 고정한다:
- [Route] Link / REPRSSI 는 같은 방향(보고자→이웃)의 같은 간선 → (from,to) 키로 갱신(중복 금지).
  근거: RouteUpdateLinkFromReprssi(SSM_esp32.ino:6709)가 REPRSSI 를 RouteLinkMatrix[from][to] 적재 후
  '[Route] Link <fromMac> -> <toMac> rssi=' 출력. from=보고자(data.macAddress), to=이웃 mac.
- 토큰 = RouteTokenForInfoPos(:6299) '%02X'(UnitID&0xFF) → '05'=UnitID 5. [Passed Device] 가 토큰→이름 해소.
- RSSI 폴백 ladder: route_link > reprssi > info_rssi > info_table_rf > takentime > rs.
"""

from serial_mcp.topology_routing import (
    RoutingTable,
    pick_link_metric,
    _norm_token,
    _parse_passed_tokens,
)


def route_ev(from_mac, to_mac, rssi, ts=0.0, port="COM4"):
    """[Route] Link 이벤트(events kind='route')의 최소 형태."""
    return {"kind": "route", "port": port, "ts": ts,
            "route": {"from_mac": from_mac, "to_mac": to_mac, "rssi": rssi},
            "ids": {}, "hints": {}, "metrics": {}}


def webtx_ev(src_mac, reprssi, ts=0.0, port="COM4"):
    """[Proc-WebRTx] REPRSSI 이벤트. reprssi=[(mac,rssi),...]."""
    return {"kind": "webtx", "port": port, "ts": ts, "route": None,
            "ids": {"mac": src_mac}, "hints": {},
            "metrics": {"reprssi": [{"mac": m, "rssi": r, "snr": None} for m, r in reprssi]}}


def info_ev(unid, mac=None, passed=None, kind="rx", ts=0.0, port="COM4"):
    """UnID/Mac(+선택 [Passed Device]) 보유 RX/TX 이벤트의 최소 형태."""
    return {"kind": kind, "port": port, "ts": ts, "route": None,
            "ids": {"unid": unid, "mac": mac},
            "hints": {"passed": passed}, "metrics": {}}


# ---- 토큰 정규화·파싱 ----

def test_norm_token_2hex_upper():
    assert _norm_token("5") == "05"        # 단일 hex → zero-pad
    assert _norm_token("0a") == "0A"       # 소문자 → 대문자
    assert _norm_token("10") == "10"       # UnitID 16 의 hex
    assert _norm_token(None) is None
    assert _norm_token("zz") is None


def test_parse_passed_tokens_pairs():
    assert _parse_passed_tokens("(05-SB5)->(01-REP1)") == [("05", "SB5"), ("01", "REP1")]
    assert _parse_passed_tokens(" (0A-APU3) ") == [("0A", "APU3")]   # hex 토큰 정규화
    assert _parse_passed_tokens(None) == []
    assert _parse_passed_tokens("garbage") == []


# ---- 링크 그래프 ----

def test_route_link_event_makes_edge():
    rt = RoutingTable()
    rt.observe(route_ev("A0:85:E3:EA:5C:C4", "10:06:1C:16:97:AC", -41, ts=1.0))
    edges = rt.edges(now=2.0)
    assert len(edges) == 1
    e = edges[0]
    assert e["from"] == "A0:85:E3:EA:5C:C4" and e["to"] == "10:06:1C:16:97:AC"
    assert e["rssi"] == -41 and e["source"] == "route_link" and e["fresh"] is True


def test_reprssi_webtx_makes_edges_from_source():
    rt = RoutingTable()
    rt.observe(webtx_ev("30:AE:A4:4B:1A:0C",
                        [("A0:85:E3:EA:5C:C4", -22), ("10:06:1C:16:97:AC", -41)], ts=1.0))
    edges = {(e["from"], e["to"]): e for e in rt.edges(now=1.0)}
    assert len(edges) == 2
    e = edges[("30:AE:A4:4B:1A:0C", "A0:85:E3:EA:5C:C4")]
    assert e["rssi"] == -22 and e["source"] == "reprssi"


def test_reprssi_and_route_link_same_edge_idempotent():
    # REPRSSI 와 [Route] Link 는 펌웨어상 같은 방향의 같은 간선 → 키 1개로 합쳐진다.
    rt = RoutingTable()
    rt.observe(webtx_ev("AA:00", [("BB:11", -50)], ts=1.0))
    rt.observe(route_ev("AA:00", "BB:11", -48, ts=2.0))
    edges = rt.edges(now=2.0)
    assert len(edges) == 1 and edges[0]["rssi"] == -48   # last-writer-wins(같은 ts대)


def test_edge_freshness_window():
    rt = RoutingTable(fresh_window_s=10.0)
    rt.observe(route_ev("AA:00", "BB:11", -40, ts=0.0))
    assert rt.edges(now=5.0)[0]["fresh"] is True
    assert rt.edges(now=20.0)[0]["fresh"] is False
    assert rt.edges(now=None)[0]["fresh"] is None        # 클럭 모르면 미상


def test_route_event_missing_mac_ignored():
    rt = RoutingTable()
    rt.observe({"kind": "route", "ts": 1.0,
                "route": {"from_mac": None, "to_mac": "BB", "rssi": -1},
                "ids": {}, "hints": {}, "metrics": {}})
    assert rt.edges(now=1.0) == []


def test_links_bounded_drop_oldest():
    rt = RoutingTable(max_links=2)
    rt.observe(route_ev("A", "1", -1, ts=0.0))
    rt.observe(route_ev("B", "2", -2, ts=1.0))
    rt.observe(route_ev("C", "3", -3, ts=2.0))           # A->1 축출(누수 방지)
    keys = {(e["from"], e["to"]) for e in rt.edges(now=3.0)}
    assert keys == {("B", "2"), ("C", "3")}


def test_link_update_refreshes_recency():
    # 갱신된 간선은 최신으로 이동 — drop-oldest 가 신선분을 축출하지 않게.
    rt = RoutingTable(max_links=2)
    rt.observe(route_ev("A", "1", -1, ts=0.0))
    rt.observe(route_ev("B", "2", -2, ts=1.0))
    rt.observe(route_ev("A", "1", -9, ts=2.0))           # A->1 재갱신(최신화)
    rt.observe(route_ev("C", "3", -3, ts=3.0))           # B->2 축출(가장 오래됨)
    keys = {(e["from"], e["to"]) for e in rt.edges(now=4.0)}
    assert keys == {("A", "1"), ("C", "3")}


# ---- 토큰 맵 ----

def test_token_map_from_unid_and_mac():
    rt = RoutingTable()
    rt.observe(info_ev(unid=5, mac="AA:BB:CC"))
    ent = rt.resolve_token("05")
    assert ent["unid"] == 5 and ent["mac"] == "AA:BB:CC"


def test_token_map_from_passed_device_names():
    rt = RoutingTable()
    rt.observe(info_ev(unid=5, mac="AA", passed="(05-SB5)->(01-REP1)"))
    assert rt.resolve_token("05")["name"] == "SB5"
    assert rt.resolve_token("01")["name"] == "REP1"


def test_token_map_merges_unid_and_passed():
    rt = RoutingTable()
    rt.observe(info_ev(unid=5, mac="AA:BB"))                 # token 05: unid+mac
    rt.observe(info_ev(unid=99, passed="(05-SB5)"))          # token 05: name 추가(병합)
    ent = rt.resolve_token("05")
    assert ent["unid"] == 5 and ent["mac"] == "AA:BB" and ent["name"] == "SB5"


def test_unid_16_token_is_hex_10():
    rt = RoutingTable()
    rt.observe(info_ev(unid=16, mac="DD"))
    assert rt.resolve_token("10")["unid"] == 16   # %02X(16)=='10'


def test_resolve_unknown_token_none():
    assert RoutingTable().resolve_token("A3") is None   # 미등록(예약 아님) 토큰


# ---- 예약 토큰 가드(UnitID 0/255 ↔ ROUTE_DIRECT/EMPTY) ----

def test_token_of_unid_reserved_returns_none():
    from serial_mcp.topology_routing import _token_of_unid
    assert _token_of_unid(0) is None        # '00'=ROUTE_DIRECT_TOKEN(예약)
    assert _token_of_unid(255) is None      # 'FF'=ROUTE_EMPTY_TOKEN(예약)
    assert _token_of_unid(5) == "05"
    assert _token_of_unid(16) == "10"


def test_unid_0_not_registered_as_direct_token():
    # 펌웨어 RouteTokenForInfoPos: UnitID 0 은 retEncrytion(mac) 분기라 토큰이 '00' 아님.
    # retEncrytion 은 0 을 절대 반환 안 함(if(!dat) dat=0xFF) → 실토큰 '00' 불가. 자기등록 건너뜀.
    rt = RoutingTable()
    rt.observe(info_ev(unid=0, mac="AA"))
    rt.observe(info_ev(unid=0, mac="BB"))   # 둘째도 '00' 키 충돌(덮어씀) 없어야
    assert rt.resolve_token("00") is None   # 예약 토큰에 가짜 노드 미등록


def test_unid_255_not_registered_as_empty_token():
    rt = RoutingTable()
    rt.observe(info_ev(unid=255, mac="CC"))
    assert rt.resolve_token("FF") is None


def test_passed_device_reserved_token_skipped():
    rt = RoutingTable()
    rt.observe(info_ev(unid=5, mac="AA", passed="(00-DIRECT)->(05-SB5)"))
    assert rt.resolve_token("00") is None             # 예약 토큰은 이름도 등록 안 함
    assert rt.resolve_token("05")["name"] == "SB5"    # 정상 토큰은 그대로 해소


def test_resolve_reserved_token_none():
    assert RoutingTable().resolve_token("00") is None
    assert RoutingTable().resolve_token("FF") is None


# ---- webtx REPRSSI 결측 mac 가드(route 분기와 대칭) ----

def test_webtx_missing_mac_row_skipped():
    rt = RoutingTable()
    rt.observe({"kind": "webtx", "ts": 1.0, "route": None,
                "ids": {"mac": "30:AE:A4:4B:1A:0C"}, "hints": {},
                "metrics": {"reprssi": [{"mac": None, "rssi": -1, "snr": None},
                                        {"mac": "10:06:1C:16:97:AC", "rssi": -41, "snr": None}]}})
    keys = {(e["from"], e["to"]) for e in rt.edges(now=1.0)}
    assert keys == {("30:AE:A4:4B:1A:0C", "10:06:1C:16:97:AC")}   # None to 간선 없음


def test_webtx_missing_src_mac_no_edges():
    rt = RoutingTable()
    rt.observe({"kind": "webtx", "ts": 1.0, "route": None, "ids": {"mac": None}, "hints": {},
                "metrics": {"reprssi": [{"mac": "AA", "rssi": -1, "snr": None}]}})
    assert rt.edges(now=1.0) == []      # src 결측이면 모든 reprssi 간선 무시


def test_tokens_snapshot_is_copy():
    rt = RoutingTable()
    rt.observe(info_ev(unid=5, mac="AA"))
    snap = rt.tokens()
    assert snap["05"]["unid"] == 5 and snap["05"]["mac"] == "AA"
    snap["05"]["unid"] = 999
    assert rt.tokens()["05"]["unid"] == 5     # 내부 상태 불변(사본)


def test_resolve_returns_copy_not_internal():
    rt = RoutingTable()
    rt.observe(info_ev(unid=5, mac="AA"))
    ent = rt.resolve_token("05")
    ent["mac"] = "TAMPERED"
    assert rt.resolve_token("05")["mac"] == "AA"   # 내부 상태 불변


# ---- RSSI ladder ----

def test_rssi_ladder_priority_order():
    assert pick_link_metric({"route_link": -30, "reprssi": -41})["source"] == "route_link"
    assert pick_link_metric({"reprssi": -41, "info_rssi": -22})["source"] == "reprssi"
    assert pick_link_metric({"info_rssi": -22, "takentime": 61})["source"] == "info_rssi"
    assert pick_link_metric({"info_table_rf": -50, "takentime": 61})["source"] == "info_table_rf"
    assert pick_link_metric({"takentime": 61, "rs": 3}) == {"value": 61, "source": "takentime"}


def test_rssi_ladder_adjacent_info_ranks_and_full_chain():
    # info_rssi(per-packet, 신선) > info_table_rf(정적 표값) 인접 순위 고정(둘만 든 dict 로).
    assert pick_link_metric({"info_table_rf": -50, "info_rssi": -22})["source"] == "info_rssi"
    # 6개 소스가 전부 들어와도 route_link 가 전체를 이긴다(전체 사슬 고정).
    full = {"route_link": -30, "reprssi": -41, "info_rssi": -22,
            "info_table_rf": -50, "takentime": 61, "rs": 3}
    assert pick_link_metric(full) == {"value": -30, "source": "route_link"}


def test_rssi_ladder_skips_none_and_keeps_zero():
    assert pick_link_metric({"route_link": None, "reprssi": -41})["source"] == "reprssi"
    assert pick_link_metric({"reprssi": 0}) == {"value": 0, "source": "reprssi"}   # 0 은 유효값
    assert pick_link_metric({}) == {"value": None, "source": None}
    assert pick_link_metric({"unknown_src": -1}) == {"value": None, "source": None}


# ---- SSM INFO 테이블 파싱(mac↔UnID↔이름 다리 + 장비 RF) ----

INFO_TABLE_SELF_ROW = ("  1  |                |         -          |    -    |           -           "
                       "|   SSM26-001 |  -3 | A0,85,E3,EA,5C,C4( -) |    SSM |   -    |  -   |     -    |    -    ")
INFO_TABLE_DEV_ROW = (" 2  |            SB1 |  94.1(00016/00017) |  61[ms] | S12:15:16 | R12:12:55 "
                      "| SB260526-002 |  -22 | 30,AE,A4,4B,1A,0C( 5) |     SB |   O    |  X   |    0     | Outdoor ")


def test_info_table_device_row_parsed_to_mac_bridge():
    # 'Mac(ID)' 셀 앵커 상대 참조 — LastComTime 셀 안의 '|'(S..|R..) 때문에 고정 인덱스 불가.
    rt = RoutingTable()
    rt.observe_table_line("COM4", 1.0, INFO_TABLE_DEV_ROW)
    ent = rt.info_table()["by_mac"]["30:AE:A4:4B:1A:0C"]
    assert ent["unid"] == 5 and ent["name"] == "SB1" and ent["rf"] == -22


def test_info_table_self_row_sets_ssm_mac():
    rt = RoutingTable()
    rt.observe_table_line("COM4", 1.0, INFO_TABLE_SELF_ROW)
    t = rt.info_table()
    assert t["ssm_mac"]["COM4"] == "A0:85:E3:EA:5C:C4"
    assert t["by_mac"]["A0:85:E3:EA:5C:C4"]["rf"] == -3      # 자기 행 RF(자기 이웃 평균)도 보존


def test_info_table_row_enriches_token_map():
    # (ID)>0 행은 토큰맵에 mac/unid/이름 등록 — [Passed Device] 해소와 같은 InfoListArr 원천.
    rt = RoutingTable()
    rt.observe_table_line("COM4", 1.0, INFO_TABLE_DEV_ROW)
    assert rt.resolve_token("05") == {"name": "SB1", "mac": "30:AE:A4:4B:1A:0C", "unid": 5}


def test_info_table_ignores_non_table_lines():
    # [Route] Link(파이프 없음)·json REPRSSI·연속줄은 테이블 행이 아니다 — 무상태 매칭 오탐 금지.
    rt = RoutingTable()
    rt.observe_table_line("COM4", 1.0, "[Route] Link A0,85,E3,EA,5C,C4 -> 10,06,1C,16,97,AC rssi=-41")
    rt.observe_table_line("COM4", 1.0, " -- takentime : 61")
    rt.observe_table_line("COM4", 1.0, '[Proc-WebRTx] ["message",{"data":{"REPRSSI":[["A0,85,E3,EA,5C,C4",-22]]}}]')
    assert rt.info_table() == {"by_mac": {}, "ssm_mac": {}}


def test_info_table_unitid_zero_not_bridged():
    # UnitID 0 = 미할당(BayID=0 장비) — unid 다리 키가 아니며 토큰맵('00' 예약)도 오염하지 않는다.
    row = (" 3  |                |  90.0(00009/00010) |  70[ms] | S12:15:16 | R12:12:55 "
           "| R26-001 |  -35 | 10,06,1C,16,97,AC( 0) |      5 |   -    |  -   |    0     |    -    ")
    rt = RoutingTable()
    rt.observe_table_line("COM4", 1.0, row)
    ent = rt.info_table()["by_mac"]["10:06:1C:16:97:AC"]
    assert ent["unid"] is None and ent["rf"] == -35
    assert rt.resolve_token("00") is None
