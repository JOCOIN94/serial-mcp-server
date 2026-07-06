"""topology_chains.py — 메시지 단위 체인 로그 순수 로직."""

import pytest

from serial_mcp.topology_chains import ChainLog, annotate_chain_groups


def ev(kind, port="COM1", ts=1.0, unid=5, unique=9, cidx=None,
       passed=None, rt_tokens=None, src_name=None, rssi=None, ms=None,
       json_obj=None, mac=None, raw_lines=None):
    return {
        "port": port,
        "ts": ts,
        "kind": kind,
        "raw_lines": list(raw_lines or []),
        "json": json_obj,
        "route": None,
        "ids": {
            "mac": mac,
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
    # ts = 첫 관측 시각(epoch s) — 뷰어 점프의 시각 앵커로 유일하게 공개하는 시각 필드.
    assert isinstance(entry.get("ts"), (int, float))
    assert "first_ts" not in entry
    assert "last_ts" not in entry
    assert all(not k.startswith("_") for k in entry)
    for node in entry["nodes"]:
        assert "ts" not in node
        assert all(not k.startswith("_") for k in node)
        assert "inferred" in node


def test_heard_only_entry_resolves_ident_src_name_and_mac():
    # 수신만 관측된 상행 — 발신자 ident 를 토큰맵으로 이름 해소해 추론 src 로 표시.
    log = ChainLog(window_s=10)
    resolver = Resolver({"05": {"name": "SB1", "mac": None, "unid": 5}})

    entry = log.observe(ev("wifirx", "COM2", ts=1.0, unid=5, unique=44), resolver=resolver)[0]
    src = entry["nodes"][0]
    assert src["name"] == "SB1" and src["resolved"] is True and src["inferred"] is True

    # BayID=0 장비 — ident 가 mac 이면 mac 그대로(표시 축약은 뷰어 몫).
    m = ChainLog(window_s=10).observe(
        ev("wifirx", "COM2", ts=1.0, unid=None, unique=44, mac="A0:85:E3:EA:5C:C4"))[0]
    assert m["nodes"][0]["name"] == "A0:85:E3:EA:5C:C4"


def test_src_without_tx_gets_port_from_ident_map():
    # 리프 TX 태그가 없는 메시지 — src 가 <<<From 이름만으로 만들어질 때, 발신자
    # ident(key)가 membership(port_idents)으로 포트를 알면 src 에 부착한다(로스터 라벨 대상 —
    # 안 하면 같은 장비가 체인마다 mesh 이름/로스터 라벨로 다르게 표기됨).
    log = ChainLog(window_s=10)

    entry = log.observe(ev("rx", "COM4", ts=1.0, unid=5, unique=44, src_name="SB1", ms=61),
                        port_idents={"COM12": 5})[0]

    src = entry["nodes"][0]
    assert src["role"] == "src"
    assert src["port"] == "COM12"
    assert src["name"] == "SB1"          # mesh 이름은 보존(라벨 우선순위·툴팁용)


def test_apply_hop_attaches_src_port_from_ident_map():
    log = ChainLog(window_s=10)
    log.observe(ev("rx", "COM4", ts=1.0, unid=7, unique=9, src_name="REP1"))

    out = log.apply_hop({"key": (7, 9), "ok": True, "confidence": "observed",
                         "rssi": -50, "rx_port": "COM4"}, port_idents={"COM9": 7})

    src = out["nodes"][0]
    assert src["role"] == "src" and src["port"] == "COM9"


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


def test_wifirx_reqrsssi_request_is_downlink_receiver_not_heard():
    log = ChainLog(window_s=10)

    entry = log.observe(
        ev("wifirx", "COM12", ts=1.0, unique=23, cidx=226,
           json_obj={"UnID": 5, "REQRSSI": "REQ", "Unique": 23, "Cidx": 226}),
        scope={"COM12": "COM4"},
        port_names={"COM12": "SB5"},
    )[0]

    assert entry["dir"] == "down"
    assert entry["group"] == "COM4"
    assert entry["heard"] == []
    assert labels(entry) == ["COM4", "SB5"]
    assert entry["nodes"][0]["role"] == "src"
    assert entry["nodes"][0]["inferred"] is True
    assert entry["nodes"][1]["role"] == "rx"
    assert entry["ok"] is True


def test_rev_true_wifirx_response_stays_up():
    log = ChainLog(window_s=10)

    entry = log.observe(
        ev("wifirx", "COM12", ts=1.0,
           json_obj={"UnID": 5, "Unique": 80, "Rev": True, "Cidx": 925}),
        scope={"COM12": "COM4"},
        port_names={"COM12": "SB5"},
    )[0]

    assert entry["dir"] == "up"
    assert entry["heard"] == ["COM12"]
    # 수신만 관측돼도 발신자는 키 ident 로 안다 — 빈 "발신 미상" 대신 추론 src 표시.
    assert labels(entry) == ["UnID 5"]
    assert entry["nodes"][0]["inferred"] is True and entry["nodes"][0]["resolved"] is False


def test_uplink_cidx_ack_rx_marks_observed_success():
    # Unique 없이 Cidx만 실린 ACK 상행({"Stat":"OK","Rev":true,"Cidx":N})은 correlator
    # 상관 밖(키=(ident,Unique) 필수)이다 — SSM RX 관측 자체가 도착 증거이므로
    # ok=None(미확정) 고정 대신 관측 성공으로 표기한다.
    log = ChainLog(window_s=10)

    entry = log.observe(
        ev("rx", "COM4", ts=1.0, unid=5, unique=None, cidx=3028, src_name="SB1",
           json_obj={"UnID": 5, "Stat": "OK", "Asn": 58, "Rev": True, "Cidx": 3028}),
        scope={"COM4": "COM4"},
        port_names={"COM4": "SSM"},
    )[0]

    assert entry["dir"] == "up"
    assert entry["ok"] is True
    assert entry["confidence"] == "observed"


def test_uplink_cidx_ack_attaches_src_port_via_event_ident():
    # Cidx 키("c") 항목은 키에 ident 가 없지만 이벤트 ids.unid 는 있다 — membership
    # (port_idents) 역해소로 src 포트를 부착해야 뷰어가 로스터 라벨(SB5)로 표기한다.
    # 미부착이면 SSM 접두 이름(SB1)이 노출돼 같은 장비가 SB1/SB5 로 혼재한다.
    log = ChainLog(window_s=10)

    entry = log.observe(
        ev("rx", "COM4", ts=1.0, unid=5, unique=None, cidx=3033, src_name="SB1",
           json_obj={"UnID": 5, "Stat": "OK", "Asn": 60, "Rev": True, "Cidx": 3033}),
        scope={"COM4": "COM4"},
        port_names={"COM4": "SSM"},
        port_idents={"COM12": 5},
    )[0]

    src = entry["nodes"][0]
    assert src["role"] == "src"
    assert src["port"] == "COM12"


def test_cidx_key_carries_ident_and_separates_same_cidx_senders():
    # F2: Cidx 는 장비별 카운터라 값 충돌이 가능 — ident 를 키에 넣어 다른 장비의
    # 같은 Cidx 가 한 체인으로 오병합되지 않아야 한다(P2).
    log = ChainLog(window_s=10)
    e1 = log.observe(ev("wifirx", "COM12", ts=1.0, unid=5, unique=None, cidx=100,
                        json_obj={"UnID": 5, "Asn": 1, "Cidx": 100}))[0]
    e2 = log.observe(ev("wifirx", "COM13", ts=1.5, unid=7, unique=None, cidx=100,
                        json_obj={"UnID": 7, "Asn": 2, "Cidx": 100}))[0]
    assert e1["key"] == ["c", 5, 100]
    assert e2["key"] == ["c", 7, 100]
    assert e1["id"] != e2["id"]


def test_cidx_key_without_ident_still_chains():
    # UnID/Mac 없는 브로드캐스트 — ident=None 폴백으로 기존 동작 유지.
    log = ChainLog(window_s=10)
    entry = log.observe(ev("wifirx", "COM12", ts=1.0, unid=None, unique=None, cidx=200))[0]
    assert entry["key"] == ["c", None, 200]


def test_public_needle_strips_rev_and_cidx():
    # D2: 수신 raw 에서 sendMessage 부착분만 벗기면 송신측 콘솔 라인과 원문 일치(F3).
    raw = '[WiFi_Rx] {"UnID":5,"Stat":"OK","Asn":58,"Rev":true,"Cidx":4520}'
    log = ChainLog(window_s=10)
    entry = log.observe(ev("wifirx", "COM4", ts=1.0, unid=5, unique=None, cidx=4520,
                           raw_lines=[raw]))[0]
    assert entry["needle"] == '{"UnID":5,"Stat":"OK","Asn":58}'


def test_public_needle_without_rev_ssm_sender():
    # SSM 펌웨어는 Rev 없이 Cidx 만 부착 — Rev 제거가 선택적이어야 한다.
    raw = '[Proc-WiFiRx] {"CHPLAN":[1,"00"],"Asn":58,"UnID":5,"Cidx":4704}'
    log = ChainLog(window_s=10)
    entry = log.observe(ev("rx", "COM12", ts=1.0, unid=5, unique=None, cidx=4704,
                           raw_lines=[raw]))[0]
    assert entry["needle"] == '{"CHPLAN":[1,"00"],"Asn":58,"UnID":5}'


def test_needle_fixed_on_first_observation():
    # 니들은 첫 성공 관측으로 고정 — 이후 관측(Rev 포함본)이 와도 안 바뀐다(안정 앵커).
    log = ChainLog(window_s=10)
    log.observe(ev("wifirx", "COM4", ts=1.0, unid=5, unique=None, cidx=300,
                   raw_lines=['[WiFi_Rx] {"UnID":5,"A":1,"Cidx":300}']))
    log.observe(ev("rx", "COM13", ts=1.2, unid=5, unique=None, cidx=300,
                   raw_lines=['[Proc-WiFiRx] {"UnID":5,"A":1,"Rev":true,"Cidx":300}']))
    assert log.recent(5)[-1]["needle"] == '{"UnID":5,"A":1}'


def test_u_key_needle_captured_from_cidx_line():
    # "u" 키도 needle 캡처 — REQRSSI 하행("u" 키)은 SSM 콘솔에 TX 가 안 찍혀서, 키 조각
    # (Unique+UnID)만으론 게이트 프로브가 SB 상행 에코와 무시간 충돌해 위양성이 난다
    # (2026-07-03 실장비 재현). 원문 needle 이 정확한 판정 근거다.
    raw = '[WiFi_Rx] {"UnID":5,"REQRSSI":"REQ","Rng":[0,4],"Unique":98,"Cidx":1059}'
    log = ChainLog(window_s=10)
    entry = log.observe(ev("wifirx", "COM12", ts=1.0, unid=5, unique=98, cidx=1059,
                           raw_lines=[raw]))[0]
    assert entry["needle"] == '{"UnID":5,"REQRSSI":"REQ","Rng":[0,4],"Unique":98}'


def test_u_key_needle_none_without_cidx_line():
    # Cidx 실린 관측 줄이 없으면 needle 없음 — 상행 TX 콘솔 라인은 키 조각으로 충분.
    log = ChainLog(window_s=10)
    entry = log.observe(ev("tx", "COM12", ts=1.0), port_names={"COM12": "SB5"})[0]
    assert entry["needle"] is None


def test_public_entry_carries_first_observation_ts():
    # 뷰어 점프의 시각 앵커 — Unique(1..99 롤링) 재사용 충돌을 시각 근접 매칭으로 푼다.
    log = ChainLog(window_s=10)
    entry = log.observe(ev("tx", "COM12", ts=42.5), port_names={"COM12": "SB5"})[0]
    assert entry["ts"] == 42.5


def test_public_ts_converted_by_epoch_of():
    # 서버 관측 ts 는 단조시각(time.monotonic)인데 뷰어는 공개 ts 를 epoch 초로 믿고
    # 버퍼 라인 HH:MM:SS 와 30s 근접 비교한다(점프 시각 앵커) — 공개 시점에 epoch_of 로
    # 변환한다. 내부 윈도 클럭(_first_ts/_last_ts·만료 판정)은 단조시각을 유지한다.
    log = ChainLog(window_s=10, epoch_of=lambda mono: mono + 1_000_000.0)
    entry = log.observe(ev("tx", "COM12", ts=42.5), port_names={"COM12": "SB5"})[0]
    assert entry["ts"] == pytest.approx(1_000_042.5)


def test_rx_observation_corrects_active_down_misclassification():
    log = ChainLog(window_s=10)
    log.observe(
        ev("wifirx", "COM12", ts=1.0, unique=23, cidx=226,
           json_obj={"UnID": 5, "REQRSSI": "REQ", "Unique": 23, "Cidx": 226}),
        scope={"COM12": "COM4"},
        port_names={"COM12": "SB5"},
    )

    corrected = log.observe(
        ev("rx", "COM4", ts=1.1, unique=23, cidx=226, src_name="SB1", ms=52,
           json_obj={"UnID": 5, "Unique": 23, "Rev": True, "Cidx": 226}),
        scope={"COM4": "COM4"},
        port_names={"COM4": "SSM"},
    )[0]

    assert corrected["dir"] == "up"
    assert labels(corrected) == ["SB1", "SSM"]
    assert [n["role"] for n in corrected["nodes"]] == ["src", "dst"]


def test_two_ended_chain_survives_late_opposite_direction_hint():
    # 2026-07-06 실장비 리셋 사고 회귀: tx+rx 양단이 실포트 관측으로 채워진 체인은 방향이
    # 기하로 확정된 것 — 같은 키를 실은 후행 반대 힌트 한 줄(콘솔 interleave 오염으로
    # wifitx 분류된 수신 JSON 등)로 nodes/ok 를 리셋하지 않는다.
    log = ChainLog(window_s=15)
    log.observe(
        ev("tx", "COM12", ts=1.0, unid=5, unique=23,
           json_obj={"UnID": 5, "Unique": 23}),
        scope={"COM12": "COM4"}, port_names={"COM12": "SB5"},
    )
    log.observe(
        ev("rx", "COM4", ts=1.1, unid=5, unique=23, src_name="SB1", ms=61,
           json_obj={"UnID": 5, "Unique": 23, "Rev": True, "Cidx": 1192}),
        scope={"COM4": "COM4"}, port_names={"COM4": "SSM"},
    )

    # 오염 줄: '[Proc_WiFiTx] … To. SB1, {수신 JSON}' 병합 → kind wifitx(하행 힌트) + 같은 키.
    log.observe(
        ev("wifitx", "COM4", ts=2.0, unid=5, unique=23,
           json_obj={"UnID": 5, "Unique": 23, "Rev": True, "Cidx": 1192}),
        scope={"COM4": "COM4"}, port_names={"COM4": "SSM"},
    )

    entry = log.recent(1)[0]
    assert entry["dir"] == "up"
    assert [n["role"] for n in entry["nodes"]] == ["src", "dst"]
    assert labels(entry) == ["SB5", "SSM"]


def test_relay_chain_with_portless_src_survives_late_opposite_hint():
    # 엣지 장비가 콘솔 미연결인 3노드 relay 체인(실배치 기본형) — src 는 포트 없는
    # 스켈레톤 노드지만 relay+dst 실포트 관측 2개가 이미 방향을 증명한다.
    # 오염 줄(wifitx 분류·같은 키)로 리셋되면 안 된다.
    log = ChainLog(window_s=15)
    log.observe(
        ev("pass", "COMR", ts=1.0, unid=1, unique=5, cidx=996, rt_tokens=["7C"],
           json_obj={"UnID": 1, "Unique": 5, "Rev": True, "Cidx": 996, "Rt": ["7C"]}),
        port_names={"COMR": "REP"},
    )
    log.observe(
        ev("rx", "COMS", ts=1.1, unid=1, unique=5, cidx=996, rt_tokens=["7C"],
           json_obj={"UnID": 1, "Unique": 5, "Rev": True, "Cidx": 996}),
        port_names={"COMS": "SSM"},
    )
    before = log.recent(1)[0]
    assert [n["role"] for n in before["nodes"]] == ["src", "relay", "dst"]

    log.observe(
        ev("wifitx", "COMS", ts=2.0, unid=1, unique=5, cidx=996,
           json_obj={"UnID": 1, "Unique": 5, "Rev": True, "Cidx": 996}),
        port_names={"COMS": "SSM"},
    )
    entry = log.recent(1)[0]
    assert entry["dir"] == "up"
    assert [n["role"] for n in entry["nodes"]] == ["src", "relay", "dst"]


def test_one_ended_entry_still_accepts_direction_correction():
    # 가드의 경계: 한쪽 끝만 있는 항목(수신 관측 1개)은 여전히 교정 가능해야 한다 —
    # down 오분류를 rx 관측이 up 으로 바로잡는 기존 계약(위 테스트)과 동일 원리.
    log = ChainLog(window_s=15)
    log.observe(
        ev("wifirx", "COM12", ts=1.0, unid=5, unique=31, cidx=300,
           json_obj={"UnID": 5, "REQRSSI": "REQ", "Unique": 31, "Cidx": 300}),
        scope={"COM12": "COM4"}, port_names={"COM12": "SB5"},
    )
    corrected = log.observe(
        ev("rx", "COM4", ts=1.1, unid=5, unique=31, cidx=300, src_name="SB1",
           json_obj={"UnID": 5, "Unique": 31, "Rev": True, "Cidx": 300}),
        scope={"COM4": "COM4"}, port_names={"COM4": "SSM"},
    )[0]
    assert corrected["dir"] == "up"


def test_downlink_inferred_src_is_public_only_and_handles_unknown_group():
    log = ChainLog(window_s=10)
    entry = log.observe(
        ev("wifirx", "COM12", ts=1.0, unique=23,
           json_obj={"UnID": 5, "INFO": "REQ", "Unique": 23}),
        scope={"COM12": "COM4"},
        port_names={"COM12": "SB5"},
    )[0]

    assert labels(entry) == ["COM4", "SB5"]
    assert log._entries[-1]["nodes"][0]["port"] == "COM12"  # inferred src is not stored internally
    assert labels(log.recent(1)[0]) == ["COM4", "SB5"]

    unknown = ChainLog(window_s=10).observe(
        ev("wifirx", "COM12", ts=1.0, unique=24,
           json_obj={"UnID": 5, "CHPLAN": [1, 2], "Unique": 24}),
        port_names={"COM12": "SB5"},
    )[0]
    assert labels(unknown) == ["?", "SB5"]
    assert unknown["nodes"][0]["resolved"] is False
    assert unknown["nodes"][0]["inferred"] is True


def test_up_wifirx_self_echo_promotes_to_src_when_port_ident_matches():
    log = ChainLog(window_s=10)

    entry = log.observe(
        ev("wifirx", "COM12", ts=1.0, unid=5, unique=91,
           json_obj={"UnID": 5, "Unique": 91, "Rev": True}),
        port_names={"COM12": "SB5"},
        port_idents={"COM12": 5},
    )[0]

    assert entry["heard"] == []
    assert labels(entry) == ["SB5"]
    assert entry["nodes"][0]["role"] == "src"

    heard = ChainLog(window_s=10).observe(
        ev("wifirx", "COM12", ts=1.0, unid=5, unique=92,
           json_obj={"UnID": 5, "Unique": 92, "Rev": True}),
        port_names={"COM12": "SB5"},
        port_idents={"COM12": 9},
    )[0]
    assert heard["heard"] == ["COM12"]
    assert labels(heard) == ["UnID 5"]           # 오버히어 — 발신자 ident 추론 src(포트 불일치라 미부착)
    assert heard["nodes"][0]["port"] is None


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


def test_wifitx_kind_forces_down_direction_even_with_unique_key():
    log = ChainLog(window_s=10)

    entry = log.observe(ev("wifitx", "COM4", ts=1.0), port_names={"COM4": "SSM"})[0]

    assert entry["dir"] == "down"
    assert labels(entry) == ["SSM"]


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


CHPLAN_V2_RX = '[WiFi_Rx] {"CHPLAN":[2,["7C","02"],4,30,0],"Asn":6,"UnID":1,"Cidx":297}'
CHPLAN_V2_OBJ = {"CHPLAN": [2, ["7C", "02"], 4, 30, 0], "Asn": 6, "UnID": 1, "Cidx": 297}
CHPLAN_V1_RX = '[WiFi_Rx] {"CHPLAN":[1,"00","FF",4,120],"Asn":70,"UnID":5,"Cidx":898}'
CHPLAN_V1_OBJ = {"CHPLAN": [1, "00", "FF", 4, 120], "Asn": 70, "UnID": 5, "Cidx": 898}
ROUTE_TX_LINE = "[Route] CHPLAN to 80,7D,3A,82,5A,AC A=7C B=02"


def route_tx(port="COM4", ts=1.0, line=ROUTE_TX_LINE, target="80:7D:3A:82:5A:AC",
             tokens=None):
    # 합성 — SSM_esp32.ino:6499 printf 형식 유래, 배포 세대 문구 실측 미확인.
    return {
        "port": port,
        "ts": ts,
        "kind": "routetx",
        "raw_lines": [line],
        "json": None,
        "route": None,
        "route_plan_tx": {"target": target, "tokens": list(tokens or ["7C", "02"])},
        "ids": {"mac": None, "unid": None, "unique": None, "asn": None, "cidx": None,
                "rt_tokens": []},
        "hints": {"src_name": None, "dst_name": None, "device_type": None, "passed": None},
        "metrics": {"rssi": None, "takentime_ms": None, "avr_takentime_ms": None,
                    "reprssi": [], "rs": []},
    }


def chplan_wifirx(port="COM12", ts=1.0, unid=1, cidx=297, json_obj=None, raw=None):
    obj = CHPLAN_V2_OBJ if json_obj is None else json_obj
    return ev("wifirx", port, ts=ts, unid=unid, unique=None, cidx=cidx,
              json_obj=obj, raw_lines=[raw or CHPLAN_V2_RX])


def test_chplan_v2_route_plan_is_intent_only_and_does_not_add_relay_nodes():
    log = ChainLog(window_s=10)

    entry = log.observe(chplan_wifirx(), scope={"COM12": "COM4"},
                        port_names={"COM12": "SB2"})[0]

    assert entry["route_plan"] == {
        "version": 2,
        "tokens": ["7C", "02"],
        "ttl": 4,
        "expire_s": 30,
        "pid": 0,
        "relays": [
            {"token": "7C", "name": None, "resolved": False},
            {"token": "02", "name": None, "resolved": False},
        ],
    }
    node_text = " ".join(str(n.get("name") or n.get("port") or "") for n in entry["nodes"])
    assert "7C" not in node_text
    assert "02" not in node_text


def test_chplan_v1_route_plan_excludes_reserved_tokens_from_relays():
    log = ChainLog(window_s=10)

    entry = log.observe(
        chplan_wifirx(port="COM12", ts=1.0, unid=5, cidx=898,
                      json_obj=CHPLAN_V1_OBJ, raw=CHPLAN_V1_RX),
        scope={"COM12": "COM4"},
    )[0]

    assert entry["route_plan"] == {
        "version": 1,
        "tokens": ["00", "FF"],
        "ttl": 4,
        "expire_s": 120,
        "pid": None,
        "relays": [],
    }


def test_chplan_route_plan_relays_resolve_tokens():
    log = ChainLog(window_s=10)
    resolver = Resolver({"7C": {"name": "REPEATOR", "mac": "AA", "unid": 124}})

    entry = log.observe(chplan_wifirx(), resolver=resolver)[0]

    assert entry["route_plan"]["relays"][0] == {
        "token": "7C",
        "name": "REPEATOR",
        "resolved": True,
    }


def test_routetx_before_chplan_rx_promotes_observed_src_and_replaces_needle():
    log = ChainLog(window_s=10)

    assert log.observe(route_tx(ts=1.0), scope={"COM4": "COM4"},
                       port_names={"COM4": "SSM"}) == []
    entry = log.observe(chplan_wifirx(ts=1.2), scope={"COM12": "COM4"},
                        port_names={"COM4": "SSM", "COM12": "SB2"})[0]

    src = next(n for n in entry["nodes"] if n["role"] == "src")
    assert src["port"] == "COM4"
    assert src["inferred"] is False
    assert entry["needle"] == ROUTE_TX_LINE


def test_chplan_rx_before_routetx_promotes_observed_src_and_replaces_needle():
    log = ChainLog(window_s=10)

    first = log.observe(chplan_wifirx(ts=1.0), scope={"COM12": "COM4"},
                        port_names={"COM12": "SB2"})[0]
    assert first["needle"] != ROUTE_TX_LINE

    entry = log.observe(route_tx(ts=1.2), scope={"COM4": "COM4"},
                        port_names={"COM4": "SSM"})[0]

    src = next(n for n in entry["nodes"] if n["role"] == "src")
    assert src["port"] == "COM4"
    assert src["inferred"] is False
    assert entry["needle"] == ROUTE_TX_LINE


def test_routetx_skips_ambiguous_same_token_downlink_candidates():
    log = ChainLog(window_s=10)
    log.observe(chplan_wifirx(port="COM12", ts=1.0, unid=1, cidx=297),
                scope={"COM12": "COM4"})
    log.observe(chplan_wifirx(port="COM13", ts=1.1, unid=2, cidx=298),
                scope={"COM13": "COM4"})

    assert log.observe(route_tx(ts=1.2), scope={"COM4": "COM4"}) == []

    for entry in log.recent(10):
        src = next(n for n in entry["nodes"] if n["role"] == "src")
        assert src["inferred"] is True


def test_routetx_group_veto_prevents_cross_group_attach():
    log = ChainLog(window_s=10)
    log.observe(chplan_wifirx(ts=1.0), scope={"COM12": "COM9"})

    assert log.observe(route_tx(ts=1.2), scope={"COM4": "COM4"}) == []

    src = next(n for n in log.recent(1)[0]["nodes"] if n["role"] == "src")
    assert src["inferred"] is True


def test_routetx_ident_mismatch_prevents_attach_when_mac_is_known():
    log = ChainLog(window_s=10)
    log.observe(
        ev("wifirx", "COM12", ts=1.0, unid=None, unique=None, cidx=297,
           mac="30:AE:A4:4C:94:20",
           json_obj={**CHPLAN_V2_OBJ, "Mac": "30,AE,A4,4C,94,20"},
           raw_lines=['[WiFi_Rx] {"CHPLAN":[2,["7C","02"],4,30,0],"Asn":6,'
                      '"Mac":"30,AE,A4,4C,94,20","Cidx":297}']),
        scope={"COM12": "COM4"},
    )

    assert log.observe(route_tx(ts=1.2), scope={"COM4": "COM4"}) == []

    src = next(n for n in log.recent(1)[0]["nodes"] if n["role"] == "src")
    assert src["inferred"] is True


def test_downlink_wifirx_overhear_uses_heard_and_inferred_target_rx_when_ident_differs():
    log = ChainLog(window_s=10)

    heard = log.observe(chplan_wifirx(port="COM13", ts=1.0, unid=1, cidx=297),
                        port_idents={"COM13": 2})[0]

    assert heard["heard"] == ["COM13"]
    assert not any(n.get("role") == "rx" and n.get("port") == "COM13"
                   for n in heard["nodes"])
    rx = next(n for n in heard["nodes"] if n["role"] == "rx")
    assert rx["name"] == "UnID 1"
    assert rx["inferred"] is True
    assert heard["ok"] is None

    legacy = ChainLog(window_s=10).observe(
        chplan_wifirx(port="COM13", ts=1.0, unid=1, cidx=297),
        port_names={"COM13": "SB2"},
    )[0]
    assert legacy["heard"] == []
    assert any(n.get("role") == "rx" and n.get("port") == "COM13"
               for n in legacy["nodes"])
    assert legacy["ok"] is True


def test_downlink_pass_relay_inserts_before_rx_terminal():
    log = ChainLog(window_s=10)
    log.observe(chplan_wifirx(ts=1.0), scope={"COM12": "COM4"},
                port_names={"COM12": "SB1"})

    # 합성 — 2_bay:218 원문 JSON + A형 태그, REP 하행 중계 시나리오.
    entry = log.observe(
        ev("pass", "COMR", ts=1.1, unid=1, unique=None, cidx=297,
           json_obj=CHPLAN_V2_OBJ,
           raw_lines=['[Data_Pass] {"CHPLAN":[2,["7C","02"],4,30,0],"Asn":6,'
                      '"UnID":1,"Cidx":297}']),
        scope={"COMR": "COM4"},
        port_names={"COMR": "REP1"},
    )[0]

    roles = [n["role"] for n in entry["nodes"]]
    assert roles.index("relay") < roles.index("rx")


# ---- 2026-07-06 실장비 회귀: 같은 포트 청취+중계 이중 노드(? → REPEAT → REPEAT) ----

def test_downlink_listen_then_pass_merges_same_port_and_defers_arrival():
    # 하행에서 같은 포트가 [WiFi_Rx] 청취(rx)와 [Data_Pass] 중계(relay)로 두 번 그려졌다
    # (? → REPEAT → REPEAT). 청취-후-중계는 한 노드(relay)로 병합하고, 목적지는 키 ident 로
    # 추론 rx 를 붙이며, 도착은 미확정(ok=None, 주황)이어야 한다 — 중계 청취는 도착 증거가 아니다.
    log = ChainLog(window_s=10)
    log.observe(ev("wifirx", "COMR", ts=1.0, unid=1, unique=None, cidx=463,
                   json_obj={"UnID": 1, "Stat": "OK", "Asn": 6, "Cidx": 463}))
    entry = log.observe(ev("pass", "COMR", ts=1.1, unid=1, unique=None, cidx=463,
                           json_obj={"UnID": 1, "Stat": "OK", "Asn": 6, "Cidx": 463}))[0]

    com_r = [n for n in entry["nodes"] if n.get("port") == "COMR"]
    assert len(com_r) == 1 and com_r[0]["role"] == "relay"
    rx = [n for n in entry["nodes"] if n.get("role") == "rx"]
    assert len(rx) == 1
    assert rx[0]["inferred"] is True and rx[0]["port"] is None
    assert rx[0]["name"] == "UnID 1" and rx[0]["resolved"] is False
    assert [n["role"] for n in entry["nodes"]] == ["src", "relay", "rx"]
    assert entry["ok"] is None and entry["confidence"] is None
    assert_public(entry)


def test_downlink_pass_then_listen_does_not_confirm_arrival():
    # 역순(중계가 먼저, 청취가 나중) — 같은 포트 relay 를 재사용하고 도착 확정을 만들지 않는다.
    log = ChainLog(window_s=10)
    log.observe(ev("pass", "COMR", ts=1.0, unid=1, unique=None, cidx=463,
                   json_obj={"UnID": 1, "Stat": "OK", "Asn": 6, "Cidx": 463}))
    entry = log.observe(ev("wifirx", "COMR", ts=1.1, unid=1, unique=None, cidx=463,
                           json_obj={"UnID": 1, "Stat": "OK", "Asn": 6, "Cidx": 463}))[0]

    com_r = [n for n in entry["nodes"] if n.get("port") == "COMR"]
    assert len(com_r) == 1 and com_r[0]["role"] == "relay"
    rx = [n for n in entry["nodes"] if n.get("role") == "rx"]
    assert len(rx) == 1 and rx[0]["inferred"] is True
    assert entry["ok"] is None and entry["confidence"] is None


def test_downlink_observed_rx_merges_into_inferred_rx_node():
    # 2026-07-06 실장비 회귀(SSM ▸ SB1 SB1): 제3 포트 청취(heard)가 추론 rx 를 먼저 만들고,
    # 그 뒤 진짜 목적지 포트의 [WiFi_Rx] 관측이 오면 같은 메시지의 목적지 노드를 새로
    # append 하지 않고 추론 rx 를 관측본으로 승격해야 한다(rx 노드는 항상 1개).
    log = ChainLog(window_s=10)
    resolver = Resolver({"05": {"name": "SB1", "mac": None, "unid": 5}})
    idents = {"COM9": "10:06:1C:16:97:AC", "COM13": 5}
    obj = {"UnID": 5, "Stat": "OK", "Asn": 27, "Cidx": 544}
    log.observe(ev("wifirx", "COM9", ts=1.0, unid=5, unique=None, cidx=544, json_obj=obj),
                port_idents=idents, resolver=resolver)
    entry = log.observe(ev("wifirx", "COM13", ts=1.1, unid=5, unique=None, cidx=544,
                           json_obj=obj),
                        port_idents=idents, port_names={"COM13": "SB1"}, resolver=resolver)[0]

    rx = [n for n in entry["nodes"] if n.get("role") == "rx"]
    assert len(rx) == 1
    assert rx[0]["port"] == "COM13" and rx[0]["inferred"] is False
    assert rx[0]["name"] == "SB1" and rx[0]["ident_only"] is False
    assert entry["heard"] == ["COM9"]
    assert entry["ok"] is True and entry["confidence"] == "observed"
    assert_public(entry)


def test_downlink_observed_rx_merge_replaces_raw_mac_ident_display():
    # 2026-07-06 실장비 회귀(SSM ▸ …97:AC REPEAT): mac ident 추론 rx(raw mac 표기) 뒤에
    # 그 장비 포트의 관측이 오면 병합되어 mac 텍스트 노드가 남지 않아야 한다.
    log = ChainLog(window_s=10)
    idents = {"COM9": "10:06:1C:16:97:AC", "COM13": 5}
    obj = {"RTC": [4, 23, 17, 6, 7, 2026], "CHANNEL": "11", "INFO": "REQ",
           "Mac": "10,06,1C,16,97,AC"}
    log.observe(ev("wifirx", "COM13", ts=1.0, unid=None, unique=None, cidx=322,
                   mac="10:06:1C:16:97:AC", json_obj=obj), port_idents=idents)
    entry = log.observe(ev("wifirx", "COM9", ts=1.1, unid=None, unique=None, cidx=322,
                           mac="10:06:1C:16:97:AC", json_obj=obj), port_idents=idents)[0]

    rx = [n for n in entry["nodes"] if n.get("role") == "rx"]
    assert len(rx) == 1
    assert rx[0]["port"] == "COM9" and rx[0]["inferred"] is False
    assert rx[0]["ident_only"] is False
    assert entry["heard"] == ["COM13"]
    assert entry["ok"] is True and entry["confidence"] == "observed"


def test_downlink_inferred_rx_resolves_name_from_token_map():
    # 추론 목적지 ident 가 토큰맵으로 해소되면 이름을 붙인다(여전히 inferred=True — dim 표시).
    log = ChainLog(window_s=10)
    resolver = Resolver({"01": {"name": "SB1", "mac": None, "unid": 1}})
    log.observe(ev("wifirx", "COMR", ts=1.0, unid=1, unique=None, cidx=463,
                   json_obj={"UnID": 1, "Stat": "OK", "Asn": 6, "Cidx": 463}), resolver=resolver)
    entry = log.observe(ev("pass", "COMR", ts=1.1, unid=1, unique=None, cidx=463,
                           json_obj={"UnID": 1, "Stat": "OK", "Asn": 6, "Cidx": 463}),
                        resolver=resolver)[0]

    rx = [n for n in entry["nodes"] if n.get("role") == "rx"]
    assert len(rx) == 1
    assert rx[0]["name"] == "SB1" and rx[0]["resolved"] is True and rx[0]["inferred"] is True


# ---- 2026-07-06 사용자 원칙: 체인로그↔그래프 동일 정보(그룹 판정 단일 원천·raw ident 숨김) ----

def test_inferred_ident_nodes_carry_ident_only_flag():
    # 추론 노드의 raw ident("UnID n"·mac)는 장비명이 아니라 표기 가치가 없다 — ident_only=True 로
    # 표시해 뷰어가 "?" 칩으로 그린다. 토큰맵으로 장비명이 해소된 경우만 ident_only=False(이름 표기).
    log = ChainLog(window_s=10)
    log.observe(ev("wifirx", "COMR", ts=1.0, unid=1, unique=None, cidx=463,
                   json_obj={"UnID": 1, "Stat": "OK", "Asn": 6, "Cidx": 463}))
    entry = log.observe(ev("pass", "COMR", ts=1.1, unid=1, unique=None, cidx=463,
                           json_obj={"UnID": 1, "Stat": "OK", "Asn": 6, "Cidx": 463}))[0]
    rx = [n for n in entry["nodes"] if n.get("role") == "rx"][0]
    assert rx["name"] == "UnID 1" and rx["ident_only"] is True

    resolver = Resolver({"01": {"name": "SB1", "mac": None, "unid": 1}})
    log2 = ChainLog(window_s=10)
    log2.observe(ev("wifirx", "COMR", ts=1.0, unid=1, unique=None, cidx=463,
                    json_obj={"UnID": 1, "Stat": "OK", "Asn": 6, "Cidx": 463}), resolver=resolver)
    entry2 = log2.observe(ev("pass", "COMR", ts=1.1, unid=1, unique=None, cidx=463,
                             json_obj={"UnID": 1, "Stat": "OK", "Asn": 6, "Cidx": 463}),
                          resolver=resolver)[0]
    rx2 = [n for n in entry2["nodes"] if n.get("role") == "rx"][0]
    assert rx2["name"] == "SB1" and rx2["ident_only"] is False

    # 상행 heard-only 의 mac ident 추론 src 도 raw ident — ident_only=True.
    m = ChainLog(window_s=10).observe(
        ev("wifirx", "COM2", ts=1.0, unid=None, unique=44, mac="A0:85:E3:EA:5C:C4"))[0]
    src = m["nodes"][0]
    assert src["name"] == "A0:85:E3:EA:5C:C4" and src["ident_only"] is True


def test_annotate_chain_groups_matches_roster_judgment():
    # 체인 group 배지는 로스터(그래프) 그룹 판정과 같은 원천이어야 한다 — 사람(뷰어)과
    # AI(get_topology)가 같은 것을 봐야 도구 오용·백엔드 오제공을 발견할 수 있다.
    # 규칙: 노드 실포트가 전부 한 그룹이면 그 그룹(SSM=ssm_port, 미귀속=그룹 id), 아니면 None.
    roster = {"groups": [
        {"id": "g1", "ssm_port": "COM4",
         "nodes": [{"ports": [{"port": "COM4"}]}, {"ports": [{"port": "COM13"}]}]},
        {"id": "g2", "ssm_port": None, "nodes": [{"ports": [{"port": "COM9"}]}]},
    ]}
    chains = [
        {"id": 1, "group": None, "nodes": [{"port": "COM9"}, {"port": None}]},
        {"id": 2, "group": None, "nodes": [{"port": "COMX"}]},
        {"id": 3, "group": "COM4", "nodes": [{"port": "COM13"}]},
        {"id": 4, "group": None, "nodes": [{"port": "COM9"}, {"port": "COM4"}]},
        {"id": 5, "group": None, "nodes": [{"port": None}]},
    ]
    out = annotate_chain_groups(chains, roster)
    assert out[0]["group"] == "g2"      # 미귀속 그룹 — 그룹 id 로 부여
    assert out[1]["group"] is None      # 로스터 밖 포트 — 판정 불가 유지
    assert out[2]["group"] == "COM4"    # 기존 판정 보존
    assert out[3]["group"] is None      # 그룹 걸침 — 모호
    assert out[4]["group"] is None      # 실포트 없음
    assert chains[0]["group"] is None   # 원본 불변(사본 반환)
