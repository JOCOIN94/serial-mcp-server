"""topology_events.py — 포트별 줄 누산 → 논리 Event 조립(순수 로직, Phase B 모듈2).

픽스처는 2026-06-26 실장비 캡처(plan §14)에서 채취한 실제 SSM/SB 로그 줄이다.
한 RX 논리 이벤트가 여러 줄([Proc-WiFiRx]→<<<From→takentime→[Proc-WebRTx])에 흩어지는
펌웨어 동작을 EventAssembler 가 포트별 누산기로 조립하는지 검증한다.
"""

from serial_mcp.topology_events import EventAssembler, extract_json

# ---- 실측 멀티라인 블록 ----
SSM_RX_BLOCK = [
    '[Proc-WiFiRx] {"UnID":5,"INFO":["4","SB260526-002",-21],"Unique":25,"Rev":true,"Cidx":873}',
    ' -- Checking finished',
    '<<< From SB1.',
    ' -- takentime : 61',
    ' -- Normal Inspect cntRev[0] : 16, avrTakenTime : 121[ms]',
    '[Proc-WebRTx] ["message",{"now":3777495,"data":{"macAddress":"30,AE,A4,4B,1A,0C",'
    '"REPRSSI":[["A0,85,E3,EA,5C,C4",-22],["10,06,1C,16,97,AC",-41,31]],"Done":"OK"}}]',
]


def _run(lines, port="COM4"):
    """모든 줄을 순서대로 feed 한 뒤 flush — 방출된 Event 전부 반환."""
    a = EventAssembler(port)
    out = []
    for i, ln in enumerate(lines):
        out += a.feed(float(i), ln)
    out += a.flush()
    return out


# ---- extract_json: 태그 접두사 건너뛰고 첫 {/[ 부터 파싱 ----

def test_extract_json_skips_tag_prefix():
    assert extract_json('[Tx - my INFO] {"a":1,"b":2}') == {"a": 1, "b": 2}


def test_extract_json_nested_object_and_trailing():
    assert extract_json('[X] {"o":{"a":1},"INFO":["4"]} trailing junk') == {"o": {"a": 1}, "INFO": ["4"]}


def test_extract_json_webtx_socketio_array_payload():
    # [Proc-WebRTx] 의 Socket.IO 배열 — 첫 { 가 payload 객체(.data.REPRSSI 보유)
    obj = extract_json('[Proc-WebRTx] ["message",{"now":1,"data":{"REPRSSI":[["AA",-1]]}}]')
    assert obj["data"]["REPRSSI"] == [["AA", -1]]


def test_extract_json_none_for_non_json():
    assert extract_json("[Route] Link A0,85 -> 10,06 rssi=-41") is None
    assert extract_json(" -- takentime : 61") is None


# ---- EventAssembler: 멀티라인 RX 블록 조립 ----

def test_rx_block_assembled_into_one_event():
    evs = _run(SSM_RX_BLOCK)
    rx = [e for e in evs if e["kind"] == "rx"]
    assert len(rx) == 1
    e = rx[0]
    assert e["ids"]["unid"] == 5 and e["ids"]["unique"] == 25 and e["ids"]["cidx"] == 873
    assert e["hints"]["device_type"] == "4"          # INFO[0] = dTSBB
    assert e["hints"]["src_name"] == "SB1"           # <<< From SB1
    assert e["metrics"]["takentime_ms"] == 61
    assert e["metrics"]["avr_takentime_ms"] == 121.0
    assert e["metrics"]["rssi"] == -21               # INFO[2] = 장비 보고 이웃평균 RSSI(ladder 'info_rssi')


def test_webtx_is_separate_event_with_reprssi():
    evs = _run(SSM_RX_BLOCK)
    web = [e for e in evs if e["kind"] == "webtx"]
    assert len(web) == 1
    rep = web[0]["metrics"]["reprssi"]
    assert rep[0] == {"mac": "A0:85:E3:EA:5C:C4", "rssi": -22, "snr": None}
    assert rep[1] == {"mac": "10:06:1C:16:97:AC", "rssi": -41, "snr": 31}   # 3열(snr) 가변 수용
    assert web[0]["ids"]["mac"] == "30:AE:A4:4B:1A:0C"   # data.macAddress(소스 MAC)


def test_tx_info_event_carries_keys_and_type():
    evs = _run(['[Tx - my INFO] {"UnID":5,"INFO":["4","SB260526-002",-22],"Unique":15}'])
    tx = [e for e in evs if e["kind"] == "tx"]
    assert len(tx) == 1
    assert tx[0]["ids"]["unid"] == 5 and tx[0]["ids"]["unique"] == 15
    assert tx[0]["hints"]["device_type"] == "4"


def test_leaf_tx_variants_are_tx_events():
    lines = [
        '[Tx - resp for WHO] {"UnID":5,"Unique":16,"INFO":["4"]}',
        '[Tx_RESP] {"UnID":5,"Unique":17}',
        '[Tx_RSSI] {"UnID":5,"Unique":18}',
        '[Tx_REGMAC] {"UnID":5,"Unique":19}',
        '[Tx_REQRESP] {"UnID":5,"Unique":20}',
        '[WiFi_Tx-PendingRFTimeout] {"UnID":5,"Unique":21}',
        'ForceQuit_Tx {"UnID":5,"Unique":22}',
    ]
    evs = _run(lines)
    assert [e["kind"] for e in evs] == ["tx"] * len(lines)
    assert [e["ids"]["unique"] for e in evs] == [16, 17, 18, 19, 20, 21, 22]


def test_proc_wifitx_stays_wifitx_not_leaf_tx():
    evs = _run(['[Proc_WiFiTx] {"UnID":5,"Cidx":475}'])
    assert len(evs) == 1
    assert evs[0]["kind"] == "wifitx"
    assert evs[0]["ids"]["cidx"] == 475


def test_data_pass_event_carries_ids_and_rt_tokens():
    evs = _run(['[Data_Pass] {"UnID":5,"Unique":23,"Rt":["05","01"],"Alive":"SSM"}'])
    assert len(evs) == 1
    assert evs[0]["kind"] == "pass"
    assert evs[0]["ids"]["unid"] == 5
    assert evs[0]["ids"]["unique"] == 23
    assert evs[0]["ids"]["rt_tokens"] == ["05", "01"]


def test_route_link_event_parsed():
    evs = _run(["[Route] Link A0,85,E3,EA,5C,C4 -> 10,06,1C,16,97,AC rssi=-41"])
    rt = [e for e in evs if e["kind"] == "route"]
    assert len(rt) == 1
    assert rt[0]["route"] == {"from_mac": "A0:85:E3:EA:5C:C4", "to_mac": "10:06:1C:16:97:AC", "rssi": -41}


def test_rt_tokens_from_proc_raw_packet_not_proc_wifirx():
    # 실제 SSM 은 [Proc-WiFiRx] 출력 전에 Rt 를 remove 한다 → 원시 Rt(실제 경로)는
    # [Proc-Raw Packet] 연속줄에만 남는다([Proc-WiFiRx] 인라인 Rt 는 펌웨어상 불가능).
    evs = _run([
        '[Proc-WiFiRx] {"UnID":5,"Unique":9,"INFO":["4"]}',
        '[Proc-Raw Packet] : {"UnID":5,"Unique":9,"Rt":["05","01"],"INFO":["4"]}',
    ])
    assert evs[0]["ids"]["rt_tokens"] == ["05", "01"]
    rx_only = _run(['[Proc-WiFiRx] {"UnID":5,"Unique":9,"INFO":["4"]}'])
    assert rx_only[0]["ids"]["rt_tokens"] == []


def test_passed_device_attached_to_rx():
    evs = _run([
        '[Proc-WiFiRx] {"UnID":5,"Unique":9,"INFO":["4"]}',
        '[Passed Device]  (05-SB5)->(01-REP1)',
    ])
    assert evs[0]["hints"]["passed"] == "(05-SB5)->(01-REP1)"


def test_tx_emitted_immediately_without_next_header_or_flush():
    # tx 는 헤더 한 줄에 페이로드 완결 → 즉시 방출. 버퍼링하면 리프 침묵 시 유휴 flush(2s+스윕
    # 위상)까지 밀려 correlator rx_grace(1s)가 먼저 만료돼 src_port 가 확률 유실된다(2026-07-02).
    a = EventAssembler("COM14")
    evs = a.feed(0.0, '[Tx - my INFO] {"UnID":5,"Unique":1,"INFO":["4"]}')
    assert len(evs) == 1 and evs[0]["kind"] == "tx" and evs[0]["ids"]["unique"] == 1
    assert a.flush() == []                    # 이미 방출됨 — flush 에 남지 않는다(이중 방출 금지)


def test_wifirx_and_data_pass_emit_immediately_without_flush():
    a = EventAssembler("COM14")
    rx = a.feed(0.0, '[WiFi_Rx] {"UnID":5,"Unique":1}')
    assert len(rx) == 1 and rx[0]["kind"] == "wifirx"
    ps = a.feed(0.1, '[Data_Pass] {"UnID":5,"Unique":2,"Rt":["05"]}')
    assert len(ps) == 1 and ps[0]["kind"] == "pass"
    assert a.flush() == []


def test_tx_header_also_completes_previous_block():
    # tx 헤더가 직전 블록(rx)을 닫으면서 자신도 즉시 방출 — 한 feed 가 2개 반환.
    a = EventAssembler("COM4")
    assert a.feed(0.0, '[Proc-WiFiRx] {"UnID":5,"Unique":9,"INFO":["4"]}') == []
    evs = a.feed(0.1, '[Tx - my INFO] {"UnID":7,"Unique":2,"INFO":["4"]}')
    assert [e["kind"] for e in evs] == ["rx", "tx"]


def test_two_headers_make_two_events():
    evs = _run([
        '[Tx - my INFO] {"UnID":5,"Unique":1,"INFO":["4"]}',
        '[Tx - my INFO] {"UnID":5,"Unique":2,"INFO":["4"]}',
    ])
    assert len(evs) == 2
    assert [e["ids"]["unique"] for e in evs] == [1, 2]


def test_continuation_without_header_is_ignored():
    # 헤더 없이 온 연속 줄은 버린다(현재 블록 없음).
    assert _run([" -- takentime : 61", "<<< From SB9."]) == []
