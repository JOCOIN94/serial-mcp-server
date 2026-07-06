"""topology_chains.py — Event/Hop 스트림 → 메시지 단위 체인 로그.

ChainLog 는 포트쌍 링크(PeerLinks)나 성공 판정(Correlator)을 다시 구현하지 않고,
같은 메시지 키로 관측된 tx/pass/rx/wifirx/wifitx 이벤트를 하나의 직렬화 가능한 로그 항목으로
병합한다. 서버 도착시각은 윈도 만료 판단에 쓰고, 공개 항목에는 첫 관측 시각(ts)만 싣는다 —
뷰어의 로그 점프가 Unique(1..99 롤링) 충돌 없이 같은 시점 라인을 찾는 시각 앵커다.
CHPLAN route_plan 은 intent 전용 필드이고, nodes 는 관측/추론 전달 노드만 싣는다(D1).
"""

from __future__ import annotations

import re
from collections import OrderedDict, deque
from typing import Optional

_KINDS = {"tx", "wifitx", "rx", "pass", "wifirx"}
_RESERVED_TOKENS = frozenset({"00", "FF"})
_RE_PASSED_PAIR = re.compile(r"\(\s*([0-9A-Fa-f]+)\s*-\s*([^)]+?)\s*\)")
_RE_NEEDLE_REV = re.compile(r',"Rev":true(?=[,}])')
_RE_NEEDLE_CIDX = re.compile(r',"Cidx":\d+(?=[,}])')


def _norm_token(tok) -> Optional[str]:
    if tok is None:
        return None
    try:
        return f"{int(str(tok), 16) & 0xFF:02X}"
    except (TypeError, ValueError):
        return None


def _norm_mac(value):
    if not isinstance(value, str):
        return value
    return value.strip().replace(",", ":").upper()


def _event_key(ev: dict) -> tuple[Optional[tuple], Optional[str]]:
    ids = ev.get("ids") or {}
    unique = ids.get("unique")
    if unique is not None:
        ident = ids.get("unid")
        if ident is None:
            ident = ids.get("mac")
        if ident is None:
            return None, None
        return ("u", ident, unique), "up"
    cidx = ids.get("cidx")
    if cidx is not None:
        ident = ids.get("unid")
        if ident is None:
            ident = ids.get("mac")
        return ("c", ident, cidx), "down"
    return None, None


def _jump_needle(ev: dict) -> Optional[str]:
    """관측 raw 라인에서 sendMessage 부착분(Rev/Cidx)만 벗긴 점프 니들.

    펌웨어 계약: 수신 라인 = 송신 콘솔 JSON + ,"Rev":true(비-SSM) + ,"Cidx":N append
    (공백 없는 serializeJson, REP 중계는 bypass 무변형). 벗긴 결과는 송신측 콘솔
    라인과 원문 일치하므로 뷰어가 송신측 버퍼에서 부분문자열 검색에 쓴다.
    """
    for line in (ev or {}).get("raw_lines") or []:
        if '"Cidx"' not in line:
            continue
        start = line.find("{")
        end = line.rfind("}")
        if start == -1 or end <= start:
            continue
        frag = _RE_NEEDLE_REV.sub("", line[start:end + 1])
        return _RE_NEEDLE_CIDX.sub("", frag)
    return None


def _dir_hint(ev: dict) -> Optional[str]:
    """관측 kind/json 마커 기반 방향 힌트. 키 종류 fallback 보다 우선한다."""
    kind = (ev or {}).get("kind")
    if kind == "rx":
        return "up"
    if kind == "wifitx":
        return "down"
    obj = ev.get("json")
    if isinstance(obj, dict):
        if obj.get("Rev") is True:
            return "up"
        if obj.get("INFO") == "REQ" or obj.get("REQRSSI") == "REQ" or "CHPLAN" in obj:
            return "down"
    return None


def _name_for_port(port: Optional[str], port_names: Optional[dict]) -> Optional[str]:
    if not port:
        return None
    return (port_names or {}).get(port) or port


def _node(name=None, port=None, role="relay", resolved=True, rssi=None, ms=None,
          inferred=False) -> dict:
    return {
        "name": name,
        "port": port,
        "role": role,
        "rssi": rssi,
        "ms": ms,
        "resolved": bool(resolved),
        "inferred": bool(inferred),
    }


def _node_label(n: dict) -> Optional[str]:
    return n.get("name") or n.get("port")


def _parse_passed(passed: Optional[str]) -> list[dict]:
    if not passed:
        return []
    out = []
    for m in _RE_PASSED_PAIR.finditer(passed):
        out.append(_node(name=m.group(2).strip(), role="relay", resolved=True))
    return out


def _resolve_token(resolver, token) -> Optional[dict]:
    if resolver is None:
        return None
    if hasattr(resolver, "resolve_token"):
        return resolver.resolve_token(token)
    if callable(resolver):
        return resolver(token)
    return None


def _int_field(value) -> Optional[int]:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _parse_chplan(value, resolver) -> Optional[dict]:
    if not isinstance(value, list) or len(value) < 2:
        return None

    if isinstance(value[1], list):
        layout_version = 2
        raw_tokens = value[1]
        ttl = _int_field(value[2]) if len(value) > 2 else None
        expire_s = _int_field(value[3]) if len(value) > 3 else None
        pid = _int_field(value[4]) if len(value) > 4 else None
    else:
        layout_version = 1
        raw_tokens = [value[1]]
        if len(value) > 2:
            raw_tokens.append(value[2])
        ttl = _int_field(value[3]) if len(value) > 3 else None
        expire_s = _int_field(value[4]) if len(value) > 4 else None
        pid = None

    tokens = []
    for raw in raw_tokens:
        tok = _norm_token(raw)
        tokens.append(tok if tok is not None else str(raw))

    relays = []
    for tok in tokens:
        if tok in _RESERVED_TOKENS:
            continue
        hit = _resolve_token(resolver, tok)
        relays.append({
            "token": tok,
            "name": hit.get("name") if hit else None,
            "resolved": bool(hit),
        })

    return {
        "version": _int_field(value[0]) or layout_version,
        "tokens": tokens,
        "ttl": ttl,
        "expire_s": expire_s,
        "pid": pid,
        "relays": relays,
    }


def _skeleton_from_tokens(tokens, resolver) -> list[dict]:
    out = []
    for raw in tokens or []:
        tok = _norm_token(raw)
        if tok is None or tok in _RESERVED_TOKENS:
            continue
        ent = _resolve_token(resolver, tok)
        if ent:
            out.append(_node(name=ent.get("name"), role="relay", resolved=True))
        else:
            out.append(_node(name=None, role="relay", resolved=False))
    return out


class ChainLog:
    """메시지 키별 체인 로그를 유지하는 순수 stateful 엔진."""

    def __init__(self, window_s: float = 15.0, max_entries: int = 300,
                 max_active: int = 500, epoch_of=None) -> None:
        self._window = window_s
        self._max_entries = max_entries
        self._max_active = max_active
        # 공개 ts 변환기(단조시각→epoch s). 서버가 observe ts 로 time.monotonic 을 주입하므로
        # 뷰어 점프 앵커(버퍼 wall-clock HH:MM:SS 와 30s 근접 비교) 계약을 지키려면 변환이 필요.
        # None 이면 무변환(테스트·epoch 주입 호출자). 내부 윈도 클럭(_first_ts/_last_ts)은 원본 유지.
        self._epoch_of = epoch_of
        self._next_id = 1
        self._active: "OrderedDict[tuple, dict]" = OrderedDict()
        self._entries: deque = deque()
        self._route_tx: deque = deque(maxlen=32)

    def observe(self, ev: dict, scope: Optional[dict] = None,
                resolver=None, port_names: Optional[dict] = None,
                port_idents: Optional[dict] = None) -> list:
        """Event 1개를 반영하고 변경된 공개 항목 사본을 반환한다."""
        kind = (ev or {}).get("kind")
        if kind == "routetx":
            return self._observe_route_tx(ev, resolver, port_names)
        if kind not in _KINDS:
            return []
        key, fallback_dir = _event_key(ev)
        direction = _dir_hint(ev) or fallback_dir
        port = ev.get("port")
        if key is None or not port:
            return []
        ts = ev.get("ts") or 0.0
        changed = self._expire(ts)

        ent = self._active.get(key)
        if ent is None or ent.get("complete"):
            ent = self._new_entry(key, direction, (scope or {}).get(port), ts)
        else:
            self._active.move_to_end(key)
            group = (scope or {}).get(port)
            if ent.get("group") is not None and group is not None and ent.get("group") != group:
                return changed
            if ent.get("group") is None and group is not None:
                ent["group"] = group
            if direction and direction != ent.get("dir"):
                self._correct_direction(ent, direction)

        seen_key = (port, kind)
        if seen_key in ent["_seen"]:
            return changed
        ent["_seen"].add(seen_key)
        ent["_last_ts"] = ts
        if ent.get("_needle") is None:
            # 송신측 점프 니들(첫 성공 관측 고정). "c" 전용이었으나 "u" 로 확장(2026-07-03) —
            # REQRSSI 하행("u" 키)은 SSM 콘솔에 TX 미출력이라, 키 조각(Unique 1..99 롤링)만으론
            # 게이트 프로브가 상행 에코와 무시간 충돌해 죽은 ▸ 가 발행됐다.
            ent["_needle"] = _jump_needle(ev)

        before = self._public(ent)
        obj = ev.get("json")
        if ent.get("route_plan") is None and isinstance(obj, dict) and "CHPLAN" in obj:
            rp = _parse_chplan(obj.get("CHPLAN"), resolver)
            if rp is not None:
                ent["route_plan"] = rp
                self._attach_buffered_route_tx(ent, resolver, port_names)

        if kind == "tx":
            self._observe_tx(ent, ev, port_names)
        elif kind == "rx":
            self._observe_rx(ent, ev, resolver, port_names)
        elif kind == "pass":
            self._observe_pass(ent, ev, resolver, port_names)
        elif kind == "wifitx":
            self._observe_wifitx(ent, ev, port_names)
        elif kind == "wifirx":
            self._observe_wifirx(ent, ev, port_names, port_idents, resolver)
        self._attach_src_port(ent, port_idents)

        after = self._public(ent)
        if after != before:
            changed.append(after)
        return changed

    def apply_hop(self, hop: dict, port_idents: Optional[dict] = None) -> Optional[dict]:
        """Correlator Hop 을 같은 상행 키 항목에 접목한다."""
        if not hop:
            return None
        raw_key = hop.get("key")
        if not raw_key or len(raw_key) < 2:
            return None
        key = ("u", raw_key[0], raw_key[1])
        ent = self._active.get(key) or self._find_recent_by_key(key)
        if ent is None:
            return None

        if hop.get("ok") is not None:
            ent["ok"] = hop.get("ok")
        if hop.get("confidence") is not None:
            ent["confidence"] = hop.get("confidence")
        if hop.get("rtt_ms") is not None:
            ent["rtt_ms"] = hop.get("rtt_ms")
        if ent.get("group") is None and hop.get("rx_port"):
            ent["group"] = hop.get("rx_port")
        if hop.get("rssi") is not None:
            self._ensure_src(ent, hop.get("src_port"), hop.get("src_name"), hop.get("rssi"))
        if hop.get("src_port"):
            self._ensure_src(ent, hop.get("src_port"), hop.get("src_name"), None)
        if hop.get("path"):
            self._merge_hop_path(ent, hop)
        elif hop.get("rx_port"):
            self._ensure_dst(ent, hop.get("rx_port"), hop.get("rx_port"), hop.get("rtt_ms"))

        self._attach_src_port(ent, port_idents)
        if hop.get("confidence") in ("timeout", "unconfirmed"):
            self._complete(ent)
        return self._public(ent)

    def sweep(self, now: float) -> list:
        """윈도 만료 활성 항목을 complete 로 전환하고 변경 사본을 반환한다."""
        return self._expire(now)

    def recent(self, n: int = 30) -> list:
        """id 오름차순 tail. n<=0 이면 빈 리스트."""
        if n <= 0:
            return []
        return [self._public(ent) for ent in list(self._entries)[-n:]]

    def forget_port(self, port: str) -> None:
        """포트가 낀 활성 항목을 완료 처리한다. 히스토리는 보존한다."""
        for ent in list(self._active.values()):
            if self._entry_mentions_port(ent, port):
                self._complete(ent)

    def _new_entry(self, key: tuple, direction: str, group, ts: float) -> dict:
        ent = {
            "id": self._next_id,
            "key": key,
            "dir": direction,
            "group": group,
            "ordered": True,
            "nodes": [],
            "heard": [],
            "ok": None,
            "confidence": None,
            "rtt_ms": None,
            "route_plan": None,
            "complete": False,
            "_seen": set(),
            "_needle": None,
            "_first_ts": ts,
            "_last_ts": ts,
        }
        self._next_id += 1
        self._active[key] = ent
        self._active.move_to_end(key)
        self._entries.append(ent)
        self._evict()
        return ent

    def _observe_tx(self, ent: dict, ev: dict, port_names: Optional[dict]) -> None:
        port = ev.get("port")
        rssi = (ev.get("metrics") or {}).get("rssi")
        self._ensure_src(ent, port, _name_for_port(port, port_names), rssi)

    def _observe_route_tx(self, ev: dict, resolver, port_names: Optional[dict]) -> list:
        ts = ev.get("ts") or 0.0
        changed = self._expire(ts)
        info = ev.get("route_plan_tx") or {}
        tokens = [(_norm_token(tok) or str(tok)) for tok in info.get("tokens") or []]
        port = ev.get("port")
        if not port or not tokens:
            return changed
        raw = ((ev.get("raw_lines") or [""])[0] or "").strip()
        tx = {
            "port": port,
            "ts": ts,
            "target": info.get("target"),
            "tokens": tokens,
            "raw": raw,
        }
        self._route_tx.append(tx)
        ent = self._attach_route_tx_to_one_entry(tx, resolver, port_names)
        if ent is not None:
            self._remove_route_tx(tx)
            changed.append(self._public(ent))
        return changed

    def _attach_route_tx_to_one_entry(self, tx: dict, resolver, port_names: Optional[dict]) -> Optional[dict]:
        candidates = [
            ent for ent in self._active.values()
            if self._route_tx_matches(ent, tx, resolver)
        ]
        if len(candidates) != 1:
            return None
        self._attach_route_tx(candidates[0], tx, port_names)
        return candidates[0]

    def _attach_buffered_route_tx(self, ent: dict, resolver, port_names: Optional[dict]) -> bool:
        candidates = [
            tx for tx in list(self._route_tx)
            if self._route_tx_matches(ent, tx, resolver)
        ]
        if len(candidates) != 1:
            return False
        tx = candidates[0]
        self._attach_route_tx(ent, tx, port_names)
        self._remove_route_tx(tx)
        return True

    def _route_tx_matches(self, ent: dict, tx: dict, resolver) -> bool:
        if ent.get("dir") != "down" or ent.get("complete"):
            return False
        if (tx.get("port"), "routetx") in ent.get("_seen", set()):
            return False
        rp = ent.get("route_plan")
        if not rp or not rp.get("tokens") or rp.get("tokens") != tx.get("tokens"):
            return False
        if abs((ent.get("_last_ts") or 0.0) - (tx.get("ts") or 0.0)) > self._window:
            return False
        group = ent.get("group")
        if group is not None and group != tx.get("port"):
            return False
        return self._route_tx_ident_matches(ent, tx, resolver)

    def _route_tx_ident_matches(self, ent: dict, tx: dict, resolver) -> bool:
        ident = self._entry_ident(ent)
        target = tx.get("target")
        if ident is None or target is None:
            return True
        if isinstance(ident, str):
            return _norm_mac(ident) == _norm_mac(target)
        if isinstance(ident, int) and not isinstance(ident, bool):
            hit = _resolve_token(resolver, f"{ident & 0xFF:02X}")
            mac = hit.get("mac") if hit else None
            if mac:
                return _norm_mac(mac) == _norm_mac(target)
        return True

    def _attach_route_tx(self, ent: dict, tx: dict, port_names: Optional[dict]) -> None:
        src = self._ensure_src(ent, tx.get("port"), _name_for_port(tx.get("port"), port_names), None)
        src["inferred"] = False
        src["resolved"] = True
        if ent.get("group") is None:
            ent["group"] = tx.get("port")
        # CHPLAN 수신 JSON needle 은 SSM 송신 콘솔에 실재하지 않는다. routetx 관측 시
        # 송신측에 실제 존재하는 원문 라인으로 needle 계약을 복구하는 명시 예외다.
        ent["_needle"] = tx.get("raw")
        ent["_seen"].add((tx.get("port"), "routetx"))

    def _remove_route_tx(self, tx: dict) -> None:
        try:
            self._route_tx.remove(tx)
        except ValueError:
            pass

    def _observe_rx(self, ent: dict, ev: dict, resolver, port_names: Optional[dict]) -> None:
        port = ev.get("port")
        hints = ev.get("hints") or {}
        metrics = ev.get("metrics") or {}
        skeleton = _parse_passed(hints.get("passed"))
        if not skeleton:
            skeleton = _skeleton_from_tokens((ev.get("ids") or {}).get("rt_tokens"), resolver)
        if skeleton:
            self._rebuild_with_skeleton(ent, skeleton, port, port_names, metrics)
        else:
            if not self._src(ent) and hints.get("src_name"):
                self._ensure_src(ent, None, hints.get("src_name"), metrics.get("rssi"))
            elif metrics.get("rssi") is not None:
                self._ensure_src(ent, None, None, metrics.get("rssi"))
            self._ensure_dst(ent, port, _name_for_port(port, port_names), metrics.get("takentime_ms"))
        if metrics.get("takentime_ms") is not None:
            ent["rtt_ms"] = metrics.get("takentime_ms")
        if (ent.get("key") or (None,))[0] == "c":
            # Cidx 키(Unique 없음, ACK류)는 correlator 상관 밖 — RX 관측 자체가 도착 증거다.
            # 홉이 영영 안 오므로 ok=None(미확정) 고정 대신 여기서 관측 성공으로 표기한다.
            ent["ok"] = True
            ent["confidence"] = "observed"

    def _observe_pass(self, ent: dict, ev: dict, resolver, port_names: Optional[dict]) -> None:
        port = ev.get("port")
        name = _name_for_port(port, port_names)
        skeleton = _skeleton_from_tokens((ev.get("ids") or {}).get("rt_tokens"), resolver)
        node = self._match_relay(ent, name)
        if node is None and skeleton:
            node = self._match_skeleton_slot(ent, skeleton, name)
        if node is None:
            node = _node(name=name, port=port, role="relay", resolved=True)
            self._insert_before_dst(ent, node)
        else:
            node["port"] = node.get("port") or port
            if not node.get("name"):
                node["name"] = name
            if node.get("role") != "src":
                node["role"] = "relay"
        if not skeleton and len([n for n in ent["nodes"] if n.get("role") == "relay"]) >= 2:
            ent["ordered"] = False

    def _observe_wifitx(self, ent: dict, ev: dict, port_names: Optional[dict]) -> None:
        if ent.get("dir") != "down":
            return
        port = ev.get("port")
        self._ensure_src(ent, port, _name_for_port(port, port_names), None)

    def _observe_wifirx(self, ent: dict, ev: dict, port_names: Optional[dict],
                        port_idents: Optional[dict] = None, resolver=None) -> None:
        port = ev.get("port")
        if ent.get("dir") == "up":
            ident = self._event_ident(ev)
            if ident is not None and (port_idents or {}).get(port) == ident:
                self._ensure_src(ent, port, _name_for_port(port, port_names), None)
            else:
                if port not in ent["heard"]:
                    ent["heard"].append(port)
                # 수신 포트는 청취자지 발신자가 아니다 — 발신자는 페이로드/키 ident 가 안다.
                # "발신 미상" 대신 ident 를 해소해 추론 src 로 표시한다(포트는 _attach_src_port 몫).
                self._ensure_ident_src(ent, resolver)
            return
        ent_ident = self._entry_ident(ent)
        port_ident = (port_idents or {}).get(port)
        if ent_ident is not None and port_ident is not None and not self._same_ident(ent_ident, port_ident):
            if port not in ent["heard"]:
                ent["heard"].append(port)
            self._ensure_ident_rx(ent, resolver)
            return
        if self._find_by_port(ent, port) is None:
            ent["nodes"].append(_node(name=_name_for_port(port, port_names), port=port,
                                      role="rx", resolved=True))
        ent["ok"] = True
        ent["confidence"] = "observed"

    @staticmethod
    def _event_ident(ev: dict):
        ids = (ev or {}).get("ids") or {}
        if ids.get("unid") is not None:
            return ids.get("unid")
        return ids.get("mac")

    def _ensure_ident_src(self, ent: dict, resolver) -> None:
        """src 미관측 상행 항목에 키 ident 기반 추론 src 를 만든다(빈 '발신 미상' 방지).

        ident 가 mac(BayID=0 장비)이면 mac 그대로(표시 축약은 뷰어), unid 면 토큰맵으로
        이름 해소 — 실패 시 "UnID n" 폴백(resolved=False). 추론이므로 inferred=True.
        """
        if ent.get("key", (None,))[0] != "u" or self._src(ent) is not None:
            return
        ident = ent["key"][1]
        if isinstance(ident, str):                       # mac ident
            name, resolved = ident, True
        else:
            try:                                          # 토큰 = '%02X'(UnID) — 펌웨어 규칙(십진→hex)
                hit = _resolve_token(resolver, f"{int(ident) & 0xFF:02X}")
            except (TypeError, ValueError):
                hit = None
            if hit and hit.get("name"):
                name, resolved = hit["name"], True
            else:
                name, resolved = f"UnID {ident}", False
        ent["nodes"].insert(0, _node(name=name, role="src", resolved=resolved, inferred=True))

    def _ensure_ident_rx(self, ent: dict, resolver) -> None:
        if any(n.get("role") in ("rx", "dst") for n in ent.get("nodes", [])):
            return
        ident = self._entry_ident(ent)
        if ident is None:
            return
        if isinstance(ident, str):
            name, resolved = ident, True
        else:
            try:
                hit = _resolve_token(resolver, f"{int(ident) & 0xFF:02X}")
            except (TypeError, ValueError):
                hit = None
            if hit and hit.get("name"):
                name, resolved = hit["name"], True
            else:
                name, resolved = f"UnID {ident}", False
        ent["nodes"].append(_node(name=name, role="rx", resolved=resolved, inferred=True))

    @staticmethod
    def _same_ident(a, b) -> bool:
        if isinstance(a, str) or isinstance(b, str):
            return _norm_mac(a) == _norm_mac(b)
        return a == b

    def _attach_src_port(self, ent: dict, port_idents: Optional[dict]) -> None:
        """상행 키 ident 가 어느 로컬 포트 장비인지 알면(membership) 포트 없는 src 에 부착.

        리프 TX 태그가 없는 메시지는 src 가 <<<From 이름만으로 만들어져 로스터 라벨
        (포트 기반)을 못 받는다 — 발신자 ident=key[1] 은 membership 이 포트를 아는
        관측 사실이므로 부착해도 '관측만 그린다' 원칙과 어긋나지 않는다.
        """
        ident = self._entry_ident(ent)
        if not port_idents or ident is None:
            return
        src = self._src(ent)
        if src is None or src.get("port"):
            return
        for port, pid in port_idents.items():
            if pid == ident:
                src["port"] = port
                return

    @staticmethod
    def _entry_ident(ent: dict):
        """항목의 발신자 ident — "u"/"c" 모두 key[1] (D1: 두 키가 동형)."""
        key = ent.get("key") or (None,)
        if key[0] in ("u", "c"):
            return key[1]
        return None

    @staticmethod
    def _correct_direction(ent: dict, direction: str) -> None:
        ent["dir"] = direction
        ent["ordered"] = True
        ent["nodes"] = []
        ent["heard"] = []
        ent["ok"] = None
        ent["confidence"] = None
        ent["rtt_ms"] = None

    def _rebuild_with_skeleton(self, ent: dict, skeleton: list[dict], dst_port: str,
                               port_names: Optional[dict], metrics: dict) -> None:
        old_nodes = list(ent["nodes"])
        old_src = self._src(ent)
        old_dst = self._dst(ent)
        old_relays = [n for n in old_nodes if n.get("role") == "relay"]

        first = dict(skeleton[0])
        first["role"] = "src"
        if old_src:
            first["port"] = old_src.get("port") or first.get("port")
            first["name"] = first.get("name") or old_src.get("name")
            first["rssi"] = old_src.get("rssi")
            first["resolved"] = first.get("resolved", old_src.get("resolved", True))
        if metrics.get("rssi") is not None and first.get("rssi") is None:
            first["rssi"] = metrics.get("rssi")

        new_nodes = [first]
        relays = []
        for raw in skeleton[1:]:
            relay = dict(raw)
            relay["role"] = "relay"
            matched = self._pop_matching_relay(old_relays, relay.get("name"))
            if matched:
                relay["port"] = matched.get("port") or relay.get("port")
                relay["rssi"] = matched.get("rssi")
                relay["ms"] = matched.get("ms")
            relays.append(relay)

        empty = [r for r in relays if r.get("port") is None]
        unmatched_with_port = [r for r in old_relays if r.get("port")]
        if len(empty) == 1 and len(unmatched_with_port) == 1:
            empty[0]["port"] = unmatched_with_port[0].get("port")
            if not empty[0].get("name"):
                empty[0]["name"] = unmatched_with_port[0].get("name")
            old_relays.remove(unmatched_with_port[0])

        new_nodes.extend(relays)
        for leftover in old_relays:
            if leftover.get("port") and not self._node_already_present(new_nodes, leftover):
                new_nodes.append(leftover)

        dst = _node(name=_name_for_port(dst_port, port_names), port=dst_port, role="dst",
                    resolved=True, ms=metrics.get("takentime_ms"))
        if old_dst:
            dst["name"] = dst.get("name") or old_dst.get("name")
            dst["ms"] = metrics.get("takentime_ms") if metrics.get("takentime_ms") is not None else old_dst.get("ms")
        new_nodes.append(dst)

        ent["nodes"] = new_nodes
        ent["ordered"] = True

    def _merge_hop_path(self, ent: dict, hop: dict) -> None:
        metrics_ms = hop.get("rtt_ms")
        skeleton = [_node(name=name, role="relay", resolved=True) for name in hop.get("path") or []]
        if skeleton:
            self._rebuild_with_skeleton(
                ent, skeleton, hop.get("rx_port"), None,
                {"rssi": hop.get("rssi"), "takentime_ms": metrics_ms},
            )
        elif hop.get("rx_port"):
            self._ensure_dst(ent, hop.get("rx_port"), hop.get("rx_port"), metrics_ms)

    def _ensure_src(self, ent: dict, port=None, name=None, rssi=None) -> dict:
        src = self._src(ent)
        if src is None:
            src = _node(name=name, port=port, role="src", resolved=True, rssi=rssi)
            ent["nodes"].insert(0, src)
        else:
            if port and not src.get("port"):
                src["port"] = port
            if name and (not src.get("name") or src.get("name") == src.get("port")):
                src["name"] = name
            if rssi is not None and src.get("rssi") is None:
                src["rssi"] = rssi
        return src

    def _ensure_dst(self, ent: dict, port=None, name=None, ms=None) -> dict:
        dst = self._dst(ent)
        if dst is None:
            dst = _node(name=name, port=port, role="dst", resolved=True, ms=ms)
            ent["nodes"].append(dst)
        else:
            if port and not dst.get("port"):
                dst["port"] = port
            if name and not dst.get("name"):
                dst["name"] = name
            if ms is not None and dst.get("ms") is None:
                dst["ms"] = ms
        return dst

    def _src(self, ent: dict) -> Optional[dict]:
        return next((n for n in ent["nodes"] if n.get("role") == "src"), None)

    def _dst(self, ent: dict) -> Optional[dict]:
        return next((n for n in ent["nodes"] if n.get("role") == "dst"), None)

    def _find_by_port(self, ent: dict, port: str) -> Optional[dict]:
        return next((n for n in ent["nodes"] if n.get("port") == port), None)

    def _match_relay(self, ent: dict, name: Optional[str]) -> Optional[dict]:
        if not name:
            return None
        return next((n for n in ent["nodes"]
                     if n.get("role") == "relay" and _node_label(n) == name), None)

    def _match_skeleton_slot(self, ent: dict, skeleton: list[dict], name: Optional[str]) -> Optional[dict]:
        if not name:
            return None
        for n in ent["nodes"]:
            if n.get("role") == "relay" and _node_label(n) == name:
                return n
        return None

    @staticmethod
    def _pop_matching_relay(relays: list, name: Optional[str]) -> Optional[dict]:
        if not name:
            return None
        for i, relay in enumerate(relays):
            if _node_label(relay) == name:
                return relays.pop(i)
        return None

    @staticmethod
    def _node_already_present(nodes: list, candidate: dict) -> bool:
        c_label = _node_label(candidate)
        c_port = candidate.get("port")
        for node in nodes:
            if c_port and node.get("port") == c_port:
                return True
            if c_label and _node_label(node) == c_label:
                return True
        return False

    @staticmethod
    def _insert_before_dst(ent: dict, node: dict) -> None:
        for i, existing in enumerate(ent["nodes"]):
            if existing.get("role") in ("dst", "rx"):
                ent["nodes"].insert(i, node)
                return
        ent["nodes"].append(node)

    def _expire(self, now: float) -> list:
        changed = []
        for key, ent in list(self._active.items()):
            if ent.get("complete"):
                self._active.pop(key, None)
                continue
            if now - ent.get("_last_ts", now) >= self._window:
                self._complete(ent)
                changed.append(self._public(ent))
        return changed

    def _complete(self, ent: dict) -> None:
        ent["complete"] = True
        self._active.pop(ent["key"], None)

    def _find_recent_by_key(self, key: tuple) -> Optional[dict]:
        for ent in reversed(self._entries):
            if ent.get("key") == key:
                return ent
        return None

    @staticmethod
    def _entry_mentions_port(ent: dict, port: str) -> bool:
        if port in ent.get("heard", []):
            return True
        if any(p == port for p, _kind in ent.get("_seen", set())):
            return True
        return any(n.get("port") == port for n in ent.get("nodes", []))

    def _evict(self) -> None:
        while len(self._active) > self._max_active:
            _key, ent = self._active.popitem(last=False)
            ent["complete"] = True
        while len(self._entries) > self._max_entries:
            old = self._entries.popleft()
            if self._active.get(old.get("key")) is old:
                self._active.pop(old.get("key"), None)

    def _public(self, ent: dict) -> dict:
        nodes = [dict(n) for n in ent.get("nodes", [])]
        for node in nodes:
            node["inferred"] = bool(node.get("inferred"))
        if ent.get("dir") == "down" and not any(n.get("role") == "src" for n in nodes):
            group = ent.get("group")
            nodes.insert(0, _node(name=None, port=group, role="src",
                                  resolved=group is not None, inferred=True))
        ts = ent.get("_first_ts")
        if self._epoch_of is not None and ts is not None:
            ts = self._epoch_of(ts)       # 단조시각→epoch s (서버 주입 변환기)
        return {
            "id": ent["id"],
            "key": list(ent["key"]),
            "dir": ent["dir"],
            "group": ent.get("group"),
            "ordered": bool(ent.get("ordered")),
            "nodes": nodes,
            "heard": list(ent.get("heard", [])),
            "ok": ent.get("ok"),
            "confidence": ent.get("confidence"),
            "rtt_ms": ent.get("rtt_ms"),
            "route_plan": ent.get("route_plan"),
            "complete": bool(ent.get("complete")),
            "ts": ts,                     # 첫 관측 시각(epoch s) — 뷰어 점프의 시각 앵커
            "needle": ent.get("_needle"),  # "c" 체인 송신측 점프 폴백(§D2) — "u" 는 None
        }
