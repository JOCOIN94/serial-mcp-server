"""topology_chains.py — Event/Hop 스트림 → 메시지 단위 체인 로그.

ChainLog 는 포트쌍 링크(PeerLinks)나 성공 판정(Correlator)을 다시 구현하지 않고,
같은 메시지 키로 관측된 tx/pass/rx/wifirx/wifitx 이벤트를 하나의 직렬화 가능한 로그 항목으로
병합한다. 서버 도착시각은 윈도 만료 판단에만 쓰고 공개 항목에는 싣지 않는다.
"""

from __future__ import annotations

import re
from collections import OrderedDict, deque
from typing import Optional

_KINDS = {"tx", "wifitx", "rx", "pass", "wifirx"}
_RESERVED_TOKENS = frozenset({"00", "FF"})
_RE_PASSED_PAIR = re.compile(r"\(\s*([0-9A-Fa-f]+)\s*-\s*([^)]+?)\s*\)")


def _norm_token(tok) -> Optional[str]:
    if tok is None:
        return None
    try:
        return f"{int(str(tok), 16) & 0xFF:02X}"
    except (TypeError, ValueError):
        return None


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
        return ("c", cidx), "down"
    return None, None


def _name_for_port(port: Optional[str], port_names: Optional[dict]) -> Optional[str]:
    if not port:
        return None
    return (port_names or {}).get(port) or port


def _node(name=None, port=None, role="relay", resolved=True, rssi=None, ms=None) -> dict:
    return {
        "name": name,
        "port": port,
        "role": role,
        "rssi": rssi,
        "ms": ms,
        "resolved": bool(resolved),
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
                 max_active: int = 500) -> None:
        self._window = window_s
        self._max_entries = max_entries
        self._max_active = max_active
        self._next_id = 1
        self._active: "OrderedDict[tuple, dict]" = OrderedDict()
        self._entries: deque = deque()

    def observe(self, ev: dict, scope: Optional[dict] = None,
                resolver=None, port_names: Optional[dict] = None) -> list:
        """Event 1개를 반영하고 변경된 공개 항목 사본을 반환한다."""
        kind = (ev or {}).get("kind")
        if kind not in _KINDS:
            return []
        key, direction = _event_key(ev)
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

        seen_key = (port, kind)
        if seen_key in ent["_seen"]:
            return changed
        ent["_seen"].add(seen_key)
        ent["_last_ts"] = ts

        before = self._public(ent)
        if kind == "tx":
            self._observe_tx(ent, ev, port_names)
        elif kind == "rx":
            self._observe_rx(ent, ev, resolver, port_names)
        elif kind == "pass":
            self._observe_pass(ent, ev, resolver, port_names)
        elif kind == "wifitx":
            self._observe_wifitx(ent, ev, port_names)
        elif kind == "wifirx":
            self._observe_wifirx(ent, ev, port_names)

        after = self._public(ent)
        if after != before:
            changed.append(after)
        return changed

    def apply_hop(self, hop: dict) -> Optional[dict]:
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
            "complete": False,
            "_seen": set(),
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

    def _observe_wifirx(self, ent: dict, ev: dict, port_names: Optional[dict]) -> None:
        port = ev.get("port")
        if ent.get("dir") == "up":
            if port not in ent["heard"]:
                ent["heard"].append(port)
            return
        if self._find_by_port(ent, port) is None:
            ent["nodes"].append(_node(name=_name_for_port(port, port_names), port=port,
                                      role="rx", resolved=True))
        ent["ok"] = True
        ent["confidence"] = "observed"

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
            if existing.get("role") == "dst":
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

    @staticmethod
    def _public(ent: dict) -> dict:
        return {
            "id": ent["id"],
            "key": list(ent["key"]),
            "dir": ent["dir"],
            "group": ent.get("group"),
            "ordered": bool(ent.get("ordered")),
            "nodes": [dict(n) for n in ent.get("nodes", [])],
            "heard": list(ent.get("heard", [])),
            "ok": ent.get("ok"),
            "confidence": ent.get("confidence"),
            "rtt_ms": ent.get("rtt_ms"),
            "complete": bool(ent.get("complete")),
        }
