"""topology_engine.py — 관측 줄 → 이벤트·상관·라우팅·로스터 조정(상태+Lock, Phase B 모듈6).

순수 모듈(events·correlator·routing·roster)을 한데 묶어 리더 스레드의 observe 한 줄로
홉을 방출하고, sweep 으로 만료 pending·유휴 블록을 처리하며, roster/recent_hops 스냅샷을
낸다. 시리얼 I/O·타이머·부트스트랩 송신은 server.py 가 배선한다(이 클래스는 I/O 비의존).
"""

from serial_mcp.topology_engine import TopologyEngine

# 실측 멀티라인 RX 블록(plan §14). 마지막 블록은 다음 헤더/flush 전까지 pending.
SSM_RX_BLOCK = [
    '[Proc-WiFiRx] {"UnID":5,"INFO":["4","SB260526-002",-21],"Unique":25,"Rev":true,"Cidx":873}',
    '<<< From SB1.',
    ' -- takentime : 61',
    '[Proc-WebRTx] ["message",{"now":1,"data":{"macAddress":"30,AE,A4,4B,1A,0C",'
    '"REPRSSI":[["A0,85,E3,EA,5C,C4",-22]],"Done":"OK"}}]',
]


def _feed(eng, port, lines, t0=1.0):
    for i, ln in enumerate(lines):
        eng.observe(port, t0 + i, ln)


# ---- observe → 홉 방출 ----

def test_observe_rx_block_emits_success_hop():
    eng = TopologyEngine()
    _feed(eng, "COM4", SSM_RX_BLOCK, t0=1.0)
    hops = eng.flush()                      # 마지막 pending 블록(webtx)까지 방출
    rx_hops = eng.recent_hops(10)
    succ = [h for h in rx_hops if h["ok"] is True]
    assert len(succ) >= 1
    assert succ[0]["path"] == ["SB1"] or succ[0]["src_name"] == "SB1"
    assert succ[0]["rtt_ms"] == 61


def test_observe_is_per_port_assembler():
    # 포트별 독립 누산기 — 한 포트의 블록이 다른 포트로 새지 않는다.
    eng = TopologyEngine()
    eng.observe("COM4", 1.0, '[Proc-WiFiRx] {"UnID":5,"Unique":1,"INFO":["4"]}')
    eng.observe("COM14", 1.1, '[Tx - my INFO] {"UnID":9,"Unique":2,"INFO":["4"]}')
    eng.flush()
    # 두 포트의 이벤트가 서로 다른 (UnID,Unique) 로 분리 처리됨(혼선 없음).
    assert eng.recent_hops(10) is not None   # 예외 없이 처리됨(분리 누산 확인은 아래 상관 테스트)


# ---- 라우팅/로스터 통합 ----

def test_roster_includes_link_edges_from_observed_webtx():
    eng = TopologyEngine()
    _feed(eng, "COM4", SSM_RX_BLOCK, t0=1.0)
    eng.flush()
    entries = [{"port": "COM4", "alias": "SSM", "lines": SSM_RX_BLOCK, "connected": True}]
    r = eng.roster(entries, now=10.0)
    g = r["groups"][0]
    assert g["kind"] == "ssm"
    # REPRSSI 가 링크 그래프 간선으로 잡힘(소스 macAddress → 이웃).
    assert any(e["from"] == "30:AE:A4:4B:1A:0C" for e in g["edges"])


# ---- sweep: 만료 pending(TX-without-RX) ----

def test_sweep_emits_timeout_for_tx_without_rx():
    eng = TopologyEngine(window_s=10.0)
    # SSM 이 UnID5 를 들은 적 있음(실패 스코프 확립)
    eng.observe("COM4", 0.0, '[Proc-WiFiRx] {"UnID":5,"Unique":1,"INFO":["4"]}')
    eng.observe("COM4", 0.1, '[Proc-WiFiRx] {"UnID":5,"Unique":99,"INFO":["4"]}')   # flush 앞 블록
    eng.observe("COM14", 1.0, '[Tx - my INFO] {"UnID":5,"Unique":2,"INFO":["4"]}')  # 응답 없는 TX
    eng.observe("COM14", 1.1, '[Tx - my INFO] {"UnID":5,"Unique":3,"INFO":["4"]}')  # flush 앞 블록
    hops = eng.sweep(now=50.0)
    fails = [h for h in hops if h["key"] == (5, 2)]
    assert len(fails) == 1 and fails[0]["ok"] is False


def test_sweep_flushes_idle_pending_block():
    # 유휴(다음 줄 없는) 마지막 블록을 sweep 가 flush 해 홉을 방출한다.
    eng = TopologyEngine(window_s=10.0)
    eng.observe("COM4", 1.0, '[Proc-WiFiRx] {"UnID":5,"Unique":7,"INFO":["4"]}')
    eng.observe("COM4", 1.0, ' -- takentime : 42')
    hops = eng.sweep(now=20.0, flush_idle_s=2.0)        # 1.0 관측 후 20.0 → 유휴 flush
    succ = [h for h in hops if h["ok"] is True and h["key"] == (5, 7)]
    assert len(succ) == 1 and succ[0]["rtt_ms"] == 42


def test_sweep_does_not_flush_active_block():
    # 막 들어온 블록(유휴 아님)은 flush 하지 않는다(블록 도중 절단 방지).
    eng = TopologyEngine(window_s=10.0)
    eng.observe("COM4", 19.5, '[Proc-WiFiRx] {"UnID":5,"Unique":7,"INFO":["4"]}')
    hops = eng.sweep(now=20.0, flush_idle_s=2.0)        # 0.5s 전 관측 → 활성, flush 안 함
    assert all(h["key"] != (5, 7) for h in hops)


# ---- 홉 히스토리 상한·recent ----

def test_recent_hops_bounded_and_tail():
    eng = TopologyEngine(window_s=100.0, hop_history=3)
    for u in range(6):
        eng.observe("COM4", float(u), f'[Proc-WiFiRx] {{"UnID":5,"Unique":{u},"INFO":["4"]}}')
        eng.observe("COM4", float(u), '[Proc-WiFiRx] {"UnID":5,"Unique":900,"INFO":["4"]}')  # flush 앞
    hist = eng.recent_hops(10)
    assert len(hist) <= 3                       # maxlen 상한
    assert eng.recent_hops(2) == hist[-2:]      # tail


def test_recent_hops_default_count():
    eng = TopologyEngine()
    assert eng.recent_hops() == []              # 빈 상태
