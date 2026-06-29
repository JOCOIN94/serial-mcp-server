"""topology_correlator.py — Event → Hop 상관(순수 stateful, Phase B 모듈3).

(UnID,Unique) 1차 키 매칭, 포트내 dedup, 윈도 스윕, SSM 부재 시 unconfirmed 를 검증한다.
Unique 는 uint8 롤링이라 윈도 밖 재사용은 별개 패킷으로 취급해야 한다(펌웨어 §4).
"""

from serial_mcp.topology_correlator import Correlator, _parse_passed


def ev(kind, unid=5, unique=1, port="COM4", ts=0.0, src=None, passed=None,
       takentime=None, dtype=None):
    """테스트용 최소 Event dict(topology_events 산출 형태의 부분집합)."""
    return {
        "kind": kind, "port": port, "ts": ts,
        "ids": {"unid": unid, "unique": unique, "rt_tokens": []},
        "hints": {"src_name": src, "passed": passed, "device_type": dtype},
        "metrics": {"takentime_ms": takentime, "reprssi": []},
    }


# ---- [Passed Device] 파싱 ----

def test_parse_passed_to_node_names():
    assert _parse_passed("(05-SB5)->(01-REP1)") == ["SB5", "REP1"]
    assert _parse_passed(" (05-SB5) ") == ["SB5"]
    assert _parse_passed(None) == []
    assert _parse_passed("garbage") == []


# ---- 성공 홉(SSM RX) ----

def test_rx_emits_success_hop_with_path_and_rtt():
    c = Correlator()
    hops = c.observe(ev("rx", port="COM4", ts=1.0, src="SB1",
                        passed="(05-SB5)->(01-REP1)", takentime=61, dtype="4"))
    assert len(hops) == 1
    h = hops[0]
    assert h["ok"] is True and h["confidence"] == "observed"
    assert h["path"] == ["SB5", "REP1"]
    assert h["rtt_ms"] == 61 and h["key"] == (5, 1)


def test_tx_then_rx_correlates_one_success():
    c = Correlator()
    assert c.observe(ev("tx", port="COM14", ts=1.0, dtype="4")) == []   # TX 만: pending
    hops = c.observe(ev("rx", port="COM4", ts=1.2, passed="(05-SB5)"))   # RX: 성공
    assert len(hops) == 1 and hops[0]["ok"] is True
    assert "COM14" in hops[0]["ports"] and "COM4" in hops[0]["ports"]


# ---- 포트내 dedup + 완료 후 브로드캐스트 잔향 ----

def test_port_internal_dedup_same_packet():
    c = Correlator()
    h1 = c.observe(ev("rx", port="COM4", ts=1.0, passed="(05-SB5)"))
    h2 = c.observe(ev("rx", port="COM4", ts=1.0, passed="(05-SB5)"))   # 같은 패킷 중복 수신
    assert len(h1) == 1 and h2 == []


# ---- 실패 레이어 vs unconfirmed ----

def test_tx_without_rx_is_failure_when_ssm_knows_device():
    # SSM 이 그 장비(UnID5)를 들은 적 있는데 후속 패킷이 안 오면 = 실패(도달 못 함).
    c = Correlator(window_s=10.0)
    c.observe(ev("rx", unid=5, unique=1, port="COM4", ts=0.0, passed="(05-SB5)"))
    c.observe(ev("tx", unid=5, unique=2, port="COM14", ts=1.0))
    fails = [h for h in c.sweep(now=20.0) if h["key"] == (5, 2)]
    assert len(fails) == 1 and fails[0]["ok"] is False and fails[0]["confidence"] == "timeout"


def test_tx_unconfirmed_when_ssm_never_heard_that_device():
    # SSM+standalone 공존: SSM 이 모르는 장비(다른 메시/채널)의 TX 는 실패로 단정 금지(§7-3).
    c = Correlator(window_s=10.0)
    c.observe(ev("rx", unid=5, unique=1, port="COM4", ts=0.0))      # SSM 은 UnID5 만 앎
    c.observe(ev("tx", unid=99, unique=2, port="COM20", ts=1.0))    # 다른 메시 standalone SB
    h = [x for x in c.sweep(now=20.0) if x["key"] == (99, 2)][0]
    assert h["ok"] is None and h["confidence"] == "unconfirmed"


def test_tx_without_rx_is_unconfirmed_when_no_ssm():
    c = Correlator(window_s=10.0)
    c.observe(ev("tx", port="COM14", ts=1.0, dtype="4"))          # SSM 한 번도 없음(standalone)
    hops = c.sweep(now=20.0)
    assert len(hops) == 1
    assert hops[0]["ok"] is None and hops[0]["confidence"] == "unconfirmed"


def test_sweep_keeps_pending_within_window():
    c = Correlator(window_s=10.0)
    c.observe(ev("tx", port="COM14", ts=5.0))
    assert c.sweep(now=10.0) == []        # 아직 윈도 안


# ---- Unique 롤링(윈도 밖 재사용=별개 패킷) ----

def test_key_reuse_after_window_is_new_packet():
    c = Correlator(window_s=10.0)
    c.observe(ev("tx", unid=5, unique=1, port="COM14", ts=0.0))
    c.sweep(now=20.0)                      # 첫 패킷 만료 처리
    hops = c.observe(ev("rx", unid=5, unique=1, port="COM4", ts=21.0, passed="(05-SB5)"))
    assert len(hops) == 1 and hops[0]["ok"] is True   # 같은 키지만 새 패킷


# ---- 키 없는 이벤트·비대상 kind ----

def test_missing_key_ignored():
    c = Correlator()
    assert c.observe(ev("rx", unid=None, unique=None)) == []


def test_non_rxtx_kind_ignored():
    c = Correlator()
    assert c.observe({"kind": "webtx", "port": "COM4", "ts": 1.0,
                      "ids": {"unid": 5, "unique": 1}, "hints": {}, "metrics": {}}) == []


# ---- pending 상한(drop-oldest) ----

def test_pending_bounded_drop_oldest():
    c = Correlator(window_s=100.0, max_flows=3)
    for u in range(6):
        c.observe(ev("tx", unid=1, unique=u, port="COM14", ts=float(u)))
    # 상한 3 — 오래된 키는 버려짐(누수 방지). 스윕은 살아남은 것만 방출.
    hops = c.sweep(now=1000.0)
    assert len(hops) == 3
