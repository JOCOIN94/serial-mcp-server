"""topology_engine.py — 관측 줄 → 이벤트·상관·라우팅·로스터 조정(상태+Lock, Phase B 모듈6).

순수 모듈(events·correlator·routing·roster)을 한데 묶어 리더 스레드의 observe 한 줄로
홉을 방출하고, sweep 으로 만료 pending·유휴 블록을 처리하며, roster/recent_hops 스냅샷을
낸다. 시리얼 I/O·타이머·부트스트랩 송신은 server.py 가 배선한다(이 클래스는 I/O 비의존).
"""

import serial_mcp.topology_engine as topology_engine
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


def _ssm_entries():
    return [{"port": "COM4", "alias": "SSM", "lines": SSM_RX_BLOCK, "connected": True}]


# ---- observe → 홉 방출 ----

def test_observe_rx_block_emits_success_hop():
    eng = TopologyEngine()
    _feed(eng, "COM4", SSM_RX_BLOCK, t0=1.0)
    eng.flush()                             # 마지막 pending 블록(webtx)까지 조립
    eng.sweep(now=100.0)                     # rx-only(소스 TX 미관측) → grace 후 sweep 방출
    succ = [h for h in eng.recent_hops(10) if h["ok"] is True]
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


# ---- membership 스냅샷(SSM포트별 그룹 귀속 기반) ----

def test_membership_snapshot_maps_ssm_port_to_leaf():
    # leaf TX(COM14) ↔ SSM RX(COM4) 같은 (UnID,Unique) 상관 → 멤버십에 SSM포트→{unid:{type,local_port}}.
    eng = TopologyEngine()
    eng.observe("COM14", 1.0, '[Tx - my INFO] {"UnID":5,"Unique":30,"INFO":["4","SB260526-002",-21]}')
    eng.observe("COM4", 1.2, '[Proc-WiFiRx] {"UnID":5,"Unique":30,"INFO":["4","SB260526-002",-21]}')
    eng.flush()                              # COM14 tx → COM4 rx 순으로 방출돼 상관 성립
    m = eng.membership_snapshot()
    assert "COM4" in m
    assert m["COM4"][5]["device_type"] == "4"
    assert m["COM4"][5]["local_port"] == "COM14"


def test_membership_snapshot_preserves_local_port_on_later_rx_only():
    # 나중에 같은 장비의 rx-only(소스 TX 미관측) 홉이 와도 이미 안 leaf 로컬포트를 지우지 않는다.
    eng = TopologyEngine()
    eng.observe("COM14", 1.0, '[Tx - my INFO] {"UnID":5,"Unique":30,"INFO":["4"]}')
    eng.observe("COM4", 1.2, '[Proc-WiFiRx] {"UnID":5,"Unique":30,"INFO":["4"]}')
    eng.flush()
    eng.observe("COM4", 5.0, '[Proc-WiFiRx] {"UnID":5,"Unique":40,"INFO":["4"]}')  # 소스 TX 없는 RX
    eng.observe("COM4", 5.1, '[Proc-WiFiRx] {"UnID":5,"Unique":41,"INFO":["4"]}')  # 앞 블록 flush
    m = eng.membership_snapshot()
    assert m["COM4"][5]["local_port"] == "COM14"   # 보존


def test_membership_local_port_filled_when_rx_precedes_tx():
    # 실장비: SSM RX(COM4)가 소스 TX(COM14)보다 먼저 관측돼도(펌웨어 출력순, plan §4) leaf 로컬포트가 채워진다.
    # 완성 우선 상관은 순서 무관 — src_port:null 버그(RX 즉시완료로 뒤늦은 TX 를 잔향 차단) 회귀 방지.
    eng = TopologyEngine()
    eng.observe("COM4", 1.0, '[Proc-WiFiRx] {"UnID":5,"Unique":30,"INFO":["4","SB260526-002",-21]}')   # RX 먼저
    eng.observe("COM14", 1.2, '[Tx - my INFO] {"UnID":5,"Unique":30,"INFO":["4","SB260526-002",-21]}')  # 뒤늦은 TX
    eng.flush()                              # COM4(rx) → COM14(tx) 방출: RX 선행 후 TX 로 완성
    m = eng.membership_snapshot()
    assert m["COM4"][5]["device_type"] == "4"
    assert m["COM4"][5]["local_port"] == "COM14"   # RX 선행이어도 leaf 포트 확보(버그 수정)


def test_membership_carries_rssi_from_hop():
    # rx INFO[2] RSSI 가 홉→멤버십으로 전파(링크 품질색 소스).
    eng = TopologyEngine()
    eng.observe("COM14", 1.0, '[Tx - my INFO] {"UnID":5,"Unique":30,"INFO":["4","M",-42]}')
    eng.observe("COM4", 1.2, '[Proc-WiFiRx] {"UnID":5,"Unique":30,"INFO":["4","M",-42]}')
    eng.flush()
    assert eng.membership_snapshot()["COM4"][5]["rssi"] == -42


def test_membership_via_mac_key_for_bayid_zero_device():
    # BayID=0 장비(payload 에 UnID 대신 Mac — 펌웨어 전 리프 공통)도 Mac 폴백 키로 멤버십에 든다.
    eng = TopologyEngine()
    eng.observe("COM12", 1.0, '[Tx - my INFO] {"Mac":"30,AE,A4,4B,1A,0C","Unique":8,"INFO":["5","R1",-30]}')
    eng.observe("COM4", 1.2, '[Proc-WiFiRx] {"Mac":"30,AE,A4,4B,1A,0C","Unique":8,"INFO":["5","R1",-30]}')
    eng.flush()
    m = eng.membership_snapshot()
    assert m["COM4"]["30:AE:A4:4B:1A:0C"]["local_port"] == "COM12"


def test_forget_port_purges_membership_local_port():
    # 유령 링크 방지: leaf 포트 재오픈(forget_port) 시 그 포트를 가리키던 멤버십 local_port 무효화.
    # SSM 이 무선으로 계속 들어도(rx-only 홉) 옛 포트로 fresh 링크선이 그려지면 안 된다.
    eng = TopologyEngine()
    eng.observe("COM14", 1.0, '[Tx - my INFO] {"UnID":5,"Unique":30,"INFO":["4"]}')
    eng.observe("COM4", 1.2, '[Proc-WiFiRx] {"UnID":5,"Unique":30,"INFO":["4"]}')
    eng.flush()
    assert eng.membership_snapshot()["COM4"][5]["local_port"] == "COM14"
    eng.forget_port("COM14")
    assert eng.membership_snapshot()["COM4"][5]["local_port"] is None   # 매핑만 무효화(관측 이력 유지)


def test_forget_port_drops_ssm_membership_group():
    # SSM 포트 재오픈 시 그 포트의 멤버십 그룹 자체를 제거(보드 교체 대비) — 재관측이 다시 채운다.
    eng = TopologyEngine()
    eng.observe("COM14", 1.0, '[Tx - my INFO] {"UnID":5,"Unique":30,"INFO":["4"]}')
    eng.observe("COM4", 1.2, '[Proc-WiFiRx] {"UnID":5,"Unique":30,"INFO":["4"]}')
    eng.flush()
    eng.forget_port("COM4")
    assert "COM4" not in eng.membership_snapshot()


# ---- 라우팅/로스터 통합 ----

def test_roster_edge_per_link_rssi_end_to_end():
    # 관통: INFO 테이블(mac 다리) + [Route] Link(링크별 rssi) + TX↔RX 상관(멤버십) →
    # 링크선 edge 가 장비 평균(INFO[2]=-22)이 아니라 링크별 route_link(-48)를 물고 나온다.
    eng = TopologyEngine()
    eng.observe("COM4", 1.0, "  1  |                |         -          |    -    |           -           "
                             "|   SSM26-001 |  -3 | A0,85,E3,EA,5C,C4( -) |    SSM |   -    |  -   |     -    |    -    ")
    eng.observe("COM4", 1.1, " 2  |            SB1 |  94.1(00016/00017) |  61[ms] | S12:15:16 | R12:12:55 "
                             "| SB260526-002 |  -22 | 30,AE,A4,4B,1A,0C( 5) |     SB |   O    |  X   |    0     | Outdoor ")
    eng.observe("COM4", 1.5, "[Route] Link 30,AE,A4,4B,1A,0C -> A0,85,E3,EA,5C,C4 rssi=-48")
    eng.observe("COM14", 2.0, '[Tx - my INFO] {"UnID":5,"Unique":30,"INFO":["4","M",-22]}')
    eng.observe("COM4", 2.2, '[Proc-WiFiRx] {"UnID":5,"Unique":30,"INFO":["4","M",-22]}')
    eng.flush()
    entries = [{"port": "COM4", "alias": "SSM", "lines": [], "connected": True},
               {"port": "COM14", "alias": "SB1-ESP", "lines": [], "connected": True}]
    edge = next(e for e in eng.roster(entries, now=3.0)["groups"][0]["edges"]
                if e["from"] == "COM14")
    assert edge["rssi"] == -48 and edge["rssi_source"] == "route_link"


def test_roster_includes_link_edges_from_observed_membership():
    # 정적 링크선 = correlator 가 관측한 leaf↔SSM 포트쌍(멤버십). 소스 TX(COM14)+SSM RX(COM4) 완성 →
    # edge {from:COM14, to:COM4}. REPRSSI 무선이웃 강제 링크는 폐기(plan §3).
    eng = TopologyEngine()
    eng.observe("COM14", 1.0, '[Tx - my INFO] {"UnID":5,"Unique":25,"INFO":["4"]}')
    eng.observe("COM4", 1.2, '[Proc-WiFiRx] {"UnID":5,"Unique":25,"INFO":["4"]}')
    eng.flush()
    entries = [
        {"port": "COM4", "alias": "SSM", "lines": [], "connected": True},
        {"port": "COM14", "alias": "SB1-ESP", "lines": [], "connected": True},
    ]
    g = eng.roster(entries, now=2.0)["groups"][0]
    assert g["kind"] == "ssm"
    assert any(e["from"] == "COM14" and e["to"] == "COM4" for e in g["edges"])


def test_roster_and_recent_hops_returns_matching_snapshots():
    eng = TopologyEngine()
    _feed(eng, "COM4", SSM_RX_BLOCK, t0=1.0)
    eng.flush()
    entries = _ssm_entries()

    roster, hops = eng.roster_and_recent_hops(entries, now=10.0, n=10)

    assert roster == eng.roster(entries, now=10.0)
    assert hops == eng.recent_hops(10)


def test_roster_and_recent_hops_edge_and_hop_consistent():
    # 완성 홉(소스 TX+SSM RX)이 recent_hops 에 있고, 그 포트쌍(src_port↔rx_port)이 로스터 정적
    # 링크선 edge(from↔to)와 정합한다 — 같은 관측을 두 층(홉 펄스·링크선)이 공유(plan §3).
    eng = TopologyEngine()
    eng.observe("COM14", 1.0, '[Tx - my INFO] {"UnID":5,"Unique":25,"INFO":["4"]}')
    eng.observe("COM4", 1.2, '[Proc-WiFiRx] {"UnID":5,"Unique":25,"INFO":["4"]}')
    eng.flush()
    entries = [
        {"port": "COM4", "alias": "SSM", "lines": [], "connected": True},
        {"port": "COM14", "alias": "SB1-ESP", "lines": [], "connected": True},
    ]
    roster, hops = eng.roster_and_recent_hops(entries, now=2.0, n=10)

    h = next(h for h in hops if h["key"] == (5, 25))
    assert h["ok"] is True and h["src_port"] == "COM14" and h["rx_port"] == "COM4"
    assert any(e["from"] == "COM14" and e["to"] == "COM4" for e in roster["groups"][0]["edges"])


def test_roster_and_recent_hops_zero_or_negative_hops_empty():
    eng = TopologyEngine()
    _feed(eng, "COM4", SSM_RX_BLOCK, t0=1.0)

    _, zero_hops = eng.roster_and_recent_hops(_ssm_entries(), now=10.0, n=0)
    _, negative_hops = eng.roster_and_recent_hops(_ssm_entries(), now=10.0, n=-1)

    assert zero_hops == []
    assert negative_hops == []


def test_roster_matches_atomic_roster_part_after_delegation():
    eng = TopologyEngine()
    _feed(eng, "COM4", SSM_RX_BLOCK, t0=1.0)
    eng.flush()
    entries = _ssm_entries()

    atomic_roster, hops = eng.roster_and_recent_hops(entries, now=10.0, n=0)

    assert hops == []
    assert eng.roster(entries, now=10.0) == atomic_roster


def test_roster_and_recent_hops_builds_roster_outside_engine_lock(monkeypatch):
    eng = TopologyEngine()
    _feed(eng, "COM4", SSM_RX_BLOCK, t0=1.0)
    eng.flush()
    locked_during_build = []

    def fake_build_roster(entries, routing=None, membership=None, pairing=None, now=None):
        locked_during_build.append(eng._lock.locked())
        return {"groups": [{"id": "g", "edges": routing.edges(now)}], "unplaced": []}

    monkeypatch.setattr(topology_engine, "build_roster", fake_build_roster)

    roster, hops = eng.roster_and_recent_hops(_ssm_entries(), now=10.0, n=10)

    assert locked_during_build == [False]
    assert roster["groups"][0]["edges"]
    assert hops == eng.recent_hops(10)


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


def test_sweep_idle_flush_not_double_recorded_in_history():
    # 회귀: sweep 의 유휴 flush 홉이 recent_hops 히스토리에 1번만 적재돼야(이중 적재 금지).
    eng = TopologyEngine(window_s=10.0)
    eng.observe("COM4", 1.0, '[Proc-WiFiRx] {"UnID":5,"Unique":7,"INFO":["4"]}')
    eng.observe("COM4", 1.0, ' -- takentime : 42')
    eng.sweep(now=20.0, flush_idle_s=2.0)
    keys = [h["key"] for h in eng.recent_hops(10)]
    assert keys.count((5, 7)) == 1                       # 중복 없음


def test_sweep_mixed_flush_and_timeout_history_no_duplicates():
    # 유휴 flush(성공) + correlator 만료(실패)가 섞여도 히스토리는 각 1회.
    eng = TopologyEngine(window_s=10.0)
    eng.observe("COM4", 0.0, '[Proc-WiFiRx] {"UnID":5,"Unique":1,"INFO":["4"]}')  # SSM 이 UnID5 들음
    eng.observe("COM4", 0.1, ' -- takentime : 5')
    eng.observe("COM14", 1.0, '[Tx - my INFO] {"UnID":5,"Unique":2,"INFO":["4"]}')  # 응답없는 TX
    eng.observe("COM14", 1.1, ' -- noise')
    eng.sweep(now=50.0, flush_idle_s=2.0)
    keys = [h["key"] for h in eng.recent_hops(20)]
    assert keys.count((5, 1)) == 1 and keys.count((5, 2)) == 1


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
    eng.sweep(now=1000.0)                       # rx-only 홉들을 grace 후 방출(히스토리 적재)
    hist = eng.recent_hops(10)
    assert len(hist) <= 3                       # maxlen 상한
    assert eng.recent_hops(2) == hist[-2:]      # tail


def test_recent_hops_default_count():
    eng = TopologyEngine()
    assert eng.recent_hops() == []              # 빈 상태


def test_recent_hops_zero_returns_empty():
    # n<=0 은 빈 리스트(전체 반환 아님 — 슬라이스 [-0:] 함정 방지).
    eng = TopologyEngine(window_s=100.0)
    eng.observe("COM4", 1.0, '[Proc-WiFiRx] {"UnID":5,"Unique":1,"INFO":["4"]}')
    eng.observe("COM4", 1.0, '[Proc-WiFiRx] {"UnID":5,"Unique":2,"INFO":["4"]}')   # 앞 블록 flush
    assert eng.recent_hops(0) == []
    assert eng.recent_hops(-1) == []
