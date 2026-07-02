"""topology_peerlinks.py — 범용 포트쌍 링크 상관(PeerLinks, 순수 stateful).

펌웨어 근거(2026-07-02 소스 조사):
- 리프 TX 태그는 [Tx - my INFO]뿐 아니라 [Tx_RESP], [Tx_RSSI], [Tx_REGMAC],
  [Tx_REQRESP], [WiFi_Tx...], ForceQuit_Tx 등으로 넓고, INFO 외 상태/응답 패킷도
  Unique(1..99 롤링, 0 금지)를 싣는다.
- SSM TX([Proc_WiFiTx], [Proc_Alarm], [Tx])는 Unique 대신 Cidx(SSM 송신 카운터)를
  싣고, 리프 [WiFi_Rx]는 수신 원본을 중복제거 전에 출력해 Cidx/Unique를 보존한다.
- 리프 [WiFi_Rx]는 채널/hidden ID 일치 패킷이면 오버히어와 자기 에코도 찍을 수 있다.
  따라서 src_port == dst_port는 링크로 저장하지 않는다.
- [Data_Pass]는 Alive=="SSM" 패킷 중계 시 원본 전체(Rt 포함)를 출력하지만, 중계 재송신에는
  별도 TX 태그가 없다. v1은 원 송신자 키로 하류 수신과 직접 링크처럼 관측하고, 실제 물리
  경유는 [Passed Device]/Rt 경로가 따로 보여준다는 한계를 수용한다.

멀티-SSM 스코프 규칙:
- 매칭 시점에 두 포트의 그룹이 둘 다 확정됐고 서로 다르면 상관을 거부한다.
- 같은 그룹이거나 한쪽이라도 미귀속이면 허용한다. SSM 없는 A<->B 관측을 살리기 위함이다.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Optional

_TX_KINDS = {"tx", "wifitx"}
_RX_VIA = {"rx": "handled", "pass": "handled", "wifirx": "heard"}
_VIA_RANK = {"heard": 1, "handled": 2}


class PeerLinks:
    """Event 송수신 키를 포트쌍 링크 상태로 상관한다."""

    def __init__(self, window_s: float = 15.0, max_flows: int = 2000,
                 max_links: int = 4000) -> None:
        self._window = window_s
        self._max_flows = max_flows
        self._max_links = max_links
        self._flows: "OrderedDict[tuple, dict]" = OrderedDict()
        self._links: "OrderedDict[tuple[str, str], dict]" = OrderedDict()

    @staticmethod
    def _key(ev: dict) -> Optional[tuple]:
        ids = ev.get("ids") or {}
        unique = ids.get("unique")
        if unique is not None:
            ident = ids.get("unid")
            if ident is None:
                ident = ids.get("mac")
            if ident is None:
                return None
            return ("u", ident, unique)
        cidx = ids.get("cidx")
        if cidx is not None:
            return ("c", cidx)
        return None

    def observe(self, ev: dict, scope: Optional[dict] = None) -> None:
        """Event 1개를 관측해 가능한 송신포트->수신포트 링크를 갱신한다."""
        kind = ev.get("kind")
        if kind not in _TX_KINDS and kind not in _RX_VIA:
            return
        key = self._key(ev)
        if key is None:
            return
        port = ev.get("port")
        if not port:
            return
        ts = ev.get("ts") or 0.0
        self._expire(ts)

        flow = self._flows.get(key)
        if flow is None:
            flow = {"first_ts": ts, "last_ts": ts, "seen": set(),
                    "tx_ports": set(), "rx_ports": {}}
            self._flows[key] = flow
            self._evict(self._flows, self._max_flows)
        else:
            self._flows.move_to_end(key)

        dk = (port, kind)
        if dk in flow["seen"]:
            return
        flow["seen"].add(dk)
        flow["last_ts"] = ts

        if kind in _TX_KINDS:
            flow["tx_ports"].add(port)
            for rx_port, via in list(flow["rx_ports"].items()):
                self._record(port, rx_port, ts, via, scope)
            return

        via = _RX_VIA[kind]
        prev_via = flow["rx_ports"].get(port)
        if prev_via is None or _VIA_RANK[via] > _VIA_RANK[prev_via]:
            flow["rx_ports"][port] = via
        else:
            via = prev_via
        for tx_port in list(flow["tx_ports"]):
            self._record(tx_port, port, ts, via, scope)

    def snapshot(self, now: Optional[float] = None, fresh_s: float = 30.0) -> list:
        """링크 상태 사본 [{from,to,via,fresh}]을 반환한다."""
        out = []
        for (src, dst), ent in self._links.items():
            last_ts = ent.get("last_ts")
            fresh = now is None or last_ts is None or (now - last_ts) < fresh_s
            out.append({"from": src, "to": dst, "via": ent.get("via"), "fresh": bool(fresh)})
        return out

    def forget_port(self, port: str) -> None:
        """해당 포트가 낀 pending flow와 링크를 제거한다."""
        for key in list(self._flows.keys()):
            flow = self._flows[key]
            if port in flow["tx_ports"] or port in flow["rx_ports"] or any(p == port for p, _ in flow["seen"]):
                self._flows.pop(key, None)
        for pair in list(self._links.keys()):
            if port in pair:
                self._links.pop(pair, None)

    def _record(self, src_port: str, dst_port: str, ts: float, via: str,
                scope: Optional[dict]) -> None:
        if src_port == dst_port:
            return
        if self._veto(src_port, dst_port, scope):
            return
        pair = (src_port, dst_port)
        ent = self._links.get(pair)
        if ent is None:
            self._links[pair] = {"last_ts": ts, "via": via}
            self._evict(self._links, self._max_links)
            return
        ent["last_ts"] = ts
        if _VIA_RANK[via] > _VIA_RANK.get(ent.get("via"), 0):
            ent["via"] = via
        self._links.move_to_end(pair)

    @staticmethod
    def _veto(src_port: str, dst_port: str, scope: Optional[dict]) -> bool:
        if not scope:
            return False
        a = scope.get(src_port)
        b = scope.get(dst_port)
        return a is not None and b is not None and a != b

    def _expire(self, now: float) -> None:
        for key in list(self._flows.keys()):
            flow = self._flows[key]
            if now - flow["last_ts"] >= self._window:
                self._flows.pop(key, None)

    @staticmethod
    def _evict(od: "OrderedDict", cap: int) -> None:
        while len(od) > cap:
            od.popitem(last=False)
