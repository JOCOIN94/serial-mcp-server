"""topology_peerlinks.py — 범용 H.W↔H.W 포트쌍 상관."""

from serial_mcp.topology_peerlinks import PeerLinks


def ev(port, kind, ts=1.0, unid=5, unique=1, cidx=None, mac=None):
    return {
        "port": port,
        "kind": kind,
        "ts": ts,
        "ids": {"unid": unid, "unique": unique, "cidx": cidx, "mac": mac},
    }


def links(pl, now=2.0):
    return pl.snapshot(now=now)


def test_uplink_tx_to_wifirx_records_heard_link():
    pl = PeerLinks()
    pl.observe(ev("COM12", "tx", ts=1.0, unid=7, unique=44), {})
    pl.observe(ev("COM14", "wifirx", ts=1.1, unid=7, unique=44), {})

    assert links(pl) == [{"from": "COM12", "to": "COM14", "via": "heard", "fresh": True}]


def test_downlink_cidx_wifitx_to_leaf_wifirx_records_heard_link():
    pl = PeerLinks()
    pl.observe(ev("COM4", "wifitx", ts=1.0, unique=None, cidx=475), {})
    pl.observe(ev("COM14", "wifirx", ts=1.1, unique=None, cidx=475), {})

    assert links(pl) == [{"from": "COM4", "to": "COM14", "via": "heard", "fresh": True}]


def test_rx_can_precede_tx_with_same_key():
    pl = PeerLinks()
    pl.observe(ev("COM14", "wifirx", ts=1.0, unid=7, unique=44), {})
    pl.observe(ev("COM12", "tx", ts=1.1, unid=7, unique=44), {})

    assert links(pl)[0]["from"] == "COM12"
    assert links(pl)[0]["to"] == "COM14"


def test_handled_observation_upgrades_heard_link():
    pl = PeerLinks()
    pl.observe(ev("COM12", "tx", ts=1.0, unid=7, unique=44), {})
    pl.observe(ev("COM14", "wifirx", ts=1.1, unid=7, unique=44), {})
    pl.observe(ev("COM14", "rx", ts=1.2, unid=7, unique=44), {})

    assert links(pl)[0]["via"] == "handled"


def test_data_pass_counts_as_handled_receiver():
    pl = PeerLinks()
    pl.observe(ev("COM12", "tx", ts=1.0, unid=7, unique=44), {})
    pl.observe(ev("COM20", "pass", ts=1.1, unid=7, unique=44), {})

    assert links(pl)[0] == {"from": "COM12", "to": "COM20", "via": "handled", "fresh": True}


def test_self_echo_is_not_a_link():
    pl = PeerLinks()
    pl.observe(ev("COM14", "tx", ts=1.0, unid=7, unique=44), {})
    pl.observe(ev("COM14", "wifirx", ts=1.1, unid=7, unique=44), {})

    assert links(pl) == []


def test_group_veto_rejects_ports_in_different_known_groups():
    pl = PeerLinks()
    scope = {"COM12": "COM4", "COM14": "COM9", "COM4": "COM4", "COM9": "COM9"}
    pl.observe(ev("COM12", "tx", ts=1.0, unid=7, unique=44), scope)
    pl.observe(ev("COM14", "wifirx", ts=1.1, unid=7, unique=44), scope)

    assert links(pl) == []


def test_group_veto_allows_unassigned_port_then_applies_after_assignment():
    pl = PeerLinks()
    pl.observe(ev("COM12", "tx", ts=0.0, unid=7, unique=1), {"COM12": "COM4"})
    pl.observe(ev("COM14", "wifirx", ts=0.1, unid=7, unique=1), {"COM12": "COM4"})
    assert links(pl, now=1.0)[0]["fresh"] is True

    scope = {"COM12": "COM4", "COM14": "COM9", "COM4": "COM4", "COM9": "COM9"}
    pl.observe(ev("COM12", "tx", ts=10.0, unid=7, unique=2), scope)
    pl.observe(ev("COM14", "wifirx", ts=10.1, unid=7, unique=2), scope)

    assert links(pl, now=31.0) == [{"from": "COM12", "to": "COM14", "via": "heard", "fresh": False}]


def test_window_expiry_drops_pending_flow():
    pl = PeerLinks(window_s=5.0)
    pl.observe(ev("COM14", "wifirx", ts=1.0, unid=7, unique=44), {})
    pl.observe(ev("COM12", "tx", ts=10.0, unid=7, unique=44), {})

    assert links(pl) == []


def test_pending_flows_are_bounded_drop_oldest():
    pl = PeerLinks(max_flows=1)
    pl.observe(ev("COM14", "wifirx", ts=1.0, unid=7, unique=1), {})
    pl.observe(ev("COM20", "wifirx", ts=1.1, unid=8, unique=2), {})
    pl.observe(ev("COM12", "tx", ts=1.2, unid=8, unique=2), {})
    assert links(pl)[0]["to"] == "COM20"
    pl.observe(ev("COM12", "tx", ts=1.3, unid=7, unique=1), {})
    assert len(links(pl)) == 1


def test_links_are_bounded_drop_oldest():
    pl = PeerLinks(max_links=1)
    pl.observe(ev("COM12", "tx", ts=1.0, unid=7, unique=1), {})
    pl.observe(ev("COM14", "wifirx", ts=1.1, unid=7, unique=1), {})
    pl.observe(ev("COM20", "tx", ts=2.0, unid=8, unique=2), {})
    pl.observe(ev("COM21", "wifirx", ts=2.1, unid=8, unique=2), {})

    assert links(pl) == [{"from": "COM20", "to": "COM21", "via": "heard", "fresh": True}]


def test_snapshot_fresh_decays_without_removing_link():
    pl = PeerLinks()
    pl.observe(ev("COM12", "tx", ts=1.0, unid=7, unique=44), {})
    pl.observe(ev("COM14", "wifirx", ts=1.1, unid=7, unique=44), {})

    assert links(pl, now=40.0) == [{"from": "COM12", "to": "COM14", "via": "heard", "fresh": False}]


def test_forget_port_removes_pending_and_links():
    pl = PeerLinks()
    pl.observe(ev("COM12", "tx", ts=1.0, unid=7, unique=44), {})
    pl.observe(ev("COM14", "wifirx", ts=1.1, unid=7, unique=44), {})
    pl.observe(ev("COM14", "wifirx", ts=1.2, unid=9, unique=55), {})
    pl.forget_port("COM14")
    pl.observe(ev("COM12", "tx", ts=1.3, unid=9, unique=55), {})

    assert links(pl) == []
