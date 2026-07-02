"""topology_chains.py — 메시지 단위 체인 로그 순수 로직."""

import pytest

from serial_mcp.topology_chains import ChainLog


def ev(kind, port="COM1", ts=1.0, unid=5, unique=9, cidx=None,
       passed=None, rt_tokens=None, src_name=None, rssi=None, ms=None):
    return {
        "port": port,
        "ts": ts,
        "kind": kind,
        "raw_lines": [],
        "json": None,
        "route": None,
        "ids": {
            "mac": None,
            "unid": unid,
            "unique": unique,
            "asn": None,
            "cidx": cidx,
            "rt_tokens": list(rt_tokens or []),
        },
        "hints": {
            "src_name": src_name,
            "dst_name": None,
            "device_type": None,
            "passed": passed,
        },
        "metrics": {
            "rssi": rssi,
            "takentime_ms": ms,
            "avr_takentime_ms": None,
            "reprssi": [],
            "rs": [],
        },
    }


class Resolver:
    def __init__(self, mapping):
        self.mapping = mapping

    def resolve_token(self, tok):
        ent = self.mapping.get(str(tok).upper().zfill(2))
        return dict(ent) if ent else None


def labels(entry):
    return [n.get("name") or n.get("port") or "?" for n in entry["nodes"]]


def assert_public(entry):
    assert "ts" not in entry
    assert "first_ts" not in entry
    assert "last_ts" not in entry
    assert all(not k.startswith("_") for k in entry)
    for node in entry["nodes"]:
        assert "ts" not in node
        assert all(not k.startswith("_") for k in node)


def test_up_direct_tx_rx_single_entry_with_src_rssi_and_dst_ms():
    log = ChainLog(window_s=10)
    names = {"COM1": "SB5", "COM4": "SSM"}

    first = log.observe(ev("tx", "COM1", ts=1.0, rssi=-71), port_names=names)[0]
    second = log.observe(ev("rx", "COM4", ts=1.2, rssi=-71, ms=61), port_names=names)[0]

    assert first["id"] == second["id"]
    assert labels(second) == ["SB5", "SSM"]
    assert second["nodes"][0]["role"] == "src"
    assert second["nodes"][0]["rssi"] == -71
    assert second["nodes"][1]["role"] == "dst"
    assert second["nodes"][1]["ms"] == 61
    assert second["ok"] is None
    assert log.recent(10) == [second]
    assert_public(second)


def test_passed_device_grows_existing_entry_without_new_history_item():
    log = ChainLog(window_s=10)
    names = {"COM1": "SB5", "COM4": "SSM"}
    created = log.observe(ev("tx", "COM1", ts=1.0), port_names=names)[0]

    grown = log.observe(
        ev("rx", "COM4", ts=1.2, passed="(05-SB5)->(01-REP1)", ms=44),
        port_names=names,
    )[0]

    assert grown["id"] == created["id"]
    assert len(log.recent(10)) == 1
    assert labels(grown) == ["SB5", "REP1", "SSM"]
    assert [n["role"] for n in grown["nodes"]] == ["src", "relay", "dst"]
    assert grown["ordered"] is True


def test_rt_tokens_skip_reserved_and_mark_unresolved_as_unknown():
    log = ChainLog(window_s=10)
    resolver = Resolver({"02": {"name": "REP2", "mac": "AA", "unid": 2}})

    entry = log.observe(
        ev("rx", "COM4", ts=1.0, rt_tokens=["00", "01", "FF", "02"], ms=10),
        resolver=resolver,
        port_names={"COM4": "SSM"},
    )[0]

    assert labels(entry) == ["?", "REP2", "SSM"]
    assert entry["nodes"][0]["resolved"] is False
    assert entry["nodes"][1]["resolved"] is True
    assert entry["nodes"][2]["role"] == "dst"


def test_pass_port_attaches_to_matching_skeleton_slot():
    log = ChainLog(window_s=10)
    names = {"COM1": "SB5", "COM2": "REP1", "COM4": "SSM"}

    log.observe(ev("tx", "COM1", ts=1.0), port_names=names)
    log.observe(ev("pass", "COM2", ts=1.1, rt_tokens=["05", "01"]), port_names=names)
    entry = log.observe(
        ev("rx", "COM4", ts=1.2, passed="(05-SB5)->(01-REP1)"),
        port_names=names,
    )[0]

    relay = entry["nodes"][1]
    assert relay["name"] == "REP1"
    assert relay["port"] == "COM2"
    assert relay["role"] == "relay"


def test_multiple_pass_without_skeleton_is_unordered():
    log = ChainLog(window_s=10)
    names = {"COM1": "SB5", "COM2": "REP1", "COM3": "REP2"}

    log.observe(ev("tx", "COM1", ts=1.0), port_names=names)
    log.observe(ev("pass", "COM2", ts=1.1), port_names=names)
    entry = log.observe(ev("pass", "COM3", ts=1.2), port_names=names)[0]

    assert entry["ordered"] is False
    assert labels(entry) == ["SB5", "REP1", "REP2"]


def test_downlink_wifitx_to_wifirx_accumulates_receivers():
    log = ChainLog(window_s=10)

    log.observe(ev("wifitx", "COM4", ts=1.0, unid=None, unique=None, cidx=77),
                scope={"COM4": "COM4"}, port_names={"COM4": "SSM"})
    first = log.observe(ev("wifirx", "COM1", ts=1.1, unid=None, unique=None, cidx=77),
                        scope={"COM1": "COM4"}, port_names={"COM1": "SB5"})[0]
    second = log.observe(ev("wifirx", "COM2", ts=1.2, unid=None, unique=None, cidx=77),
                         scope={"COM2": "COM4"}, port_names={"COM2": "SB6"})[0]

    assert second["id"] == first["id"]
    assert second["dir"] == "down"
    assert second["group"] == "COM4"
    assert labels(second) == ["SSM", "SB5", "SB6"]
    assert second["ok"] is True
    assert second["confidence"] == "observed"


def test_downlink_without_receiver_stays_pending_and_completes_on_sweep():
    log = ChainLog(window_s=1)
    entry = log.observe(ev("wifitx", "COM4", ts=1.0, unid=None, unique=None, cidx=88),
                        port_names={"COM4": "SSM"})[0]
    assert entry["ok"] is None

    done = log.sweep(2.0)[0]

    assert done["id"] == entry["id"]
    assert done["complete"] is True
    assert done["ok"] is None


def test_expired_same_key_creates_new_entry():
    log = ChainLog(window_s=1)
    first = log.observe(ev("tx", "COM1", ts=1.0))[0]
    log.sweep(2.1)

    second = log.observe(ev("tx", "COM1", ts=2.2))[0]

    assert second["id"] != first["id"]
    assert [e["id"] for e in log.recent(10)] == [first["id"], second["id"]]


def test_group_veto_discards_cross_group_observation_and_late_group_binds():
    log = ChainLog(window_s=10)
    first = log.observe(ev("tx", "COM1", ts=1.0), scope={}, port_names={"COM1": "SB5"})[0]
    bound = log.observe(ev("wifirx", "COM2", ts=1.1), scope={"COM2": "SSM_A"})[0]
    vetoed = log.observe(ev("rx", "COM9", ts=1.2), scope={"COM9": "SSM_B"})

    assert bound["id"] == first["id"]
    assert bound["group"] == "SSM_A"
    assert vetoed == []
    assert labels(log.recent(1)[0]) == ["SB5"]


def test_port_kind_dedup_returns_no_update():
    log = ChainLog(window_s=10)

    first = log.observe(ev("tx", "COM1", ts=1.0))[0]
    dup = log.observe(ev("tx", "COM1", ts=1.1))

    assert dup == []
    assert log.recent(1)[0]["id"] == first["id"]


def test_up_wifirx_is_heard_only_not_main_path():
    log = ChainLog(window_s=10)
    log.observe(ev("tx", "COM1", ts=1.0), port_names={"COM1": "SB5"})

    entry = log.observe(ev("wifirx", "COM5", ts=1.1), port_names={"COM5": "SB6"})[0]

    assert entry["heard"] == ["COM5"]
    assert labels(entry) == ["SB5"]


def test_apply_hop_backfills_status_path_and_completes_timeouts():
    log = ChainLog(window_s=10)
    log.observe(ev("tx", "COM1", ts=1.0), port_names={"COM1": "SB5"})

    observed = log.apply_hop({
        "key": (5, 9),
        "ok": True,
        "confidence": "observed",
        "path": ["SB5", "REP1"],
        "rtt_ms": 33,
        "rssi": -66,
        "rx_port": "COM4",
        "src_port": "COM1",
    })

    assert labels(observed) == ["SB5", "REP1", "COM4"]
    assert observed["ok"] is True
    assert observed["confidence"] == "observed"
    assert observed["rtt_ms"] == 33
    assert observed["nodes"][0]["rssi"] == -66
    assert observed["nodes"][-1]["ms"] == 33

    timed_out = log.apply_hop({"key": (5, 9), "ok": False, "confidence": "timeout"})
    assert timed_out["complete"] is True


def test_cap_evicts_old_history_and_forget_port_completes_active_entry():
    log = ChainLog(window_s=10, max_entries=2)
    log.observe(ev("tx", "COM1", ts=1.0, unique=1))
    log.observe(ev("tx", "COM2", ts=2.0, unique=2))
    last = log.observe(ev("tx", "COM3", ts=3.0, unique=3))[0]

    assert [e["id"] for e in log.recent(10)] == [2, 3]

    log.forget_port("COM3")
    entry = log.recent(1)[0]
    assert entry["id"] == last["id"]
    assert entry["complete"] is True
