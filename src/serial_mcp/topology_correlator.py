"""topology_correlator.py — Event 스트림 → 멀티홉 Hop 상관(순수 stateful, Phase B 모듈3).

(UnID, Unique) 1차 키로 같은 패킷의 이벤트를 여러 포트에서 모은다. 키는 송신자가 발급하고
릴레이가 verbatim 보존하므로(펌웨어 §4) 소스 TX([Tx - my INFO])와 SSM RX([Proc-WiFiRx])가
같은 키로 묶인다. 단 INFO 요청(ReqInfoTo)엔 Unique 가 없어 (UnID,Unique)는 '장비 응답 TX ↔
SSM RX' leg + dedup 전용이다(plan §7-3 다중키: 라우팅=Rt, ACK=RS 등은 모듈4/후속).

설계 못:
- 윈도 클럭 = 서버 도착 ts(단조), 펌웨어 로그 RTC 아님(§4 C2).
- 포트내 dedup — 메시 브로드캐스트로 같은 패킷이 한 포트에 여러 번 수신된다.
- Unique 는 uint8 롤링이라 윈도 밖 같은 키는 별개 패킷(완료 키는 _recent 로 윈도만 차단).
- SSM RX 도착 → 성공 Hop(ok=True, path=[Passed Device], rtt=takentime) 즉시 방출(단측 관측 허용).
- 소스 TX 만 있고 윈도 내 SSM RX 없음 → sweep 방출: 그 장비(UnID)를 SSM 이 들은 적 있으면
  ok=False(실패 레이어), 없으면 unconfirmed(SSM 부재/다른 메시 standalone — 실패 단정 금지, §7-3).
  ※ UnID 단위 스코프라 SSM+standalone 공존을 전역 래치처럼 실패로 굳히지 않는다. 그룹/채널
    단위 정밀 스코프(다른 메시의 UnID 우연 충돌)는 컨텍스트를 아는 모듈5/6(roster/engine)로 위임.
- pending·_recent 상한 + drop-oldest(헤드리스 장시간 누수 방지).

I/O 비의존 — 단위 테스트 용이(test_topology_correlator.py). 구간 RSSI/링크 그래프 enrich 는 모듈4.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from typing import Optional

# '[Passed Device]' 의 '(05-SB5)->(01-REP1)' → 노드 이름(펌웨어가 토큰→이름 해소함).
_RE_PASSED_NODE = re.compile(r"\(\s*[0-9A-Fa-f]+\s*-\s*([^)]+?)\s*\)")


def _parse_passed(passed: Optional[str]) -> list:
    """'[Passed Device]' 경로 문자열 → 노드 이름 리스트. 없으면 []."""
    if not passed:
        return []
    return [m.group(1).strip() for m in _RE_PASSED_NODE.finditer(passed)]


class Correlator:
    """Event(rx/tx) 를 (UnID,Unique) 로 상관해 Hop 을 방출하는 순수 stateful 엔진."""

    def __init__(self, window_s: float = 15.0, max_flows: int = 2000,
                 max_recent: int = 4000) -> None:
        self._window = window_s
        self._max_flows = max_flows
        self._max_recent = max_recent
        self._flows: "OrderedDict[tuple, dict]" = OrderedDict()   # 미완 흐름(key→flow)
        self._recent: "OrderedDict[tuple, float]" = OrderedDict()  # 최근 완료 key→ts(잔향 차단)
        self._ssm_rx_unids: set = set()   # SSM RX 로 관측된 송신자 UnID(실패 vs unconfirmed 스코프)

    @staticmethod
    def _key(ev) -> Optional[tuple]:
        ids = ev.get("ids") or {}
        unid, unique = ids.get("unid"), ids.get("unique")
        if unid is None or unique is None:
            return None
        return (unid, unique)

    def observe(self, ev) -> list:
        """이벤트 1개 처리. 성공 Hop(보통 0~1개)을 반환. webtx/route 등은 모듈4 담당."""
        out: list = []
        if ev.get("kind") not in ("rx", "tx"):
            return out
        key = self._key(ev)
        if key is None:
            return out
        ts = ev.get("ts") or 0.0
        # 완료된 패킷의 브로드캐스트 잔향(윈도 내 같은 키)은 무시.
        last = self._recent.get(key)
        if last is not None and ts - last < self._window:
            return out

        flow = self._flows.get(key)
        if flow is None:
            flow = {"key": key, "first_ts": ts, "last_ts": ts, "ports": set(),
                    "seen": set(), "tx": False, "rx": False,
                    "path": [], "src_name": None, "device_type": None, "rtt_ms": None}
            self._flows[key] = flow
            self._evict(self._flows, self._max_flows)

        dk = (ev.get("port"), ev["kind"])
        if dk in flow["seen"]:                 # 포트내 같은 종류 중복 수신 → dedup
            return out
        flow["seen"].add(dk)

        flow["last_ts"] = ts
        flow["ports"].add(ev.get("port"))
        if ev["hints"].get("device_type"):
            flow["device_type"] = ev["hints"]["device_type"]

        if ev["kind"] == "tx":
            flow["tx"] = True
            return out

        # kind == "rx" (SSM 수신) → 성공 즉시 방출(단측 관측만으로 충분)
        self._ssm_rx_unids.add(key[0])
        flow["rx"] = True
        flow["src_name"] = ev["hints"].get("src_name") or flow["src_name"]
        if ev["metrics"].get("takentime_ms") is not None:
            flow["rtt_ms"] = ev["metrics"]["takentime_ms"]
        flow["path"] = _parse_passed(ev["hints"].get("passed"))
        out.append(self._emit(flow, ok=True, confidence="observed"))
        self._complete(key, ts)
        return out

    def sweep(self, now: float) -> list:
        """윈도 만료된 미완 흐름을 방출. TX-only → 실패(SSM 관측 시) 또는 unconfirmed."""
        out: list = []
        for key in list(self._flows.keys()):
            flow = self._flows[key]
            if now - flow["first_ts"] < self._window:
                continue
            self._flows.pop(key, None)
            if flow["rx"]:                      # 이론상 도달 안 함(rx 는 observe 에서 즉시 완료)
                continue
            if flow["key"][0] in self._ssm_rx_unids:      # 그 장비를 SSM 이 들은 적 있음
                out.append(self._emit(flow, ok=False, confidence="timeout"))   # 같은 장비 미도달=실패
            else:
                out.append(self._emit(flow, ok=None, confidence="unconfirmed"))  # SSM 이 모르는 장비/메시
            self._complete(key, flow["last_ts"])
        return out

    def _complete(self, key, ts) -> None:
        self._flows.pop(key, None)
        self._recent[key] = ts
        self._recent.move_to_end(key)          # 재완료 키를 최신으로 — drop-oldest 가 신선분 축출 방지
        self._evict(self._recent, self._max_recent)

    @staticmethod
    def _evict(od: "OrderedDict", cap: int) -> None:
        while len(od) > cap:
            od.popitem(last=False)             # drop-oldest

    @staticmethod
    def _emit(flow, ok, confidence) -> dict:
        path = list(flow["path"])
        if not path and flow.get("src_name"):
            path = [flow["src_name"]]          # [Passed Device] 없으면 <<<From 소스명으로 폴백
        return {
            "key": flow["key"], "ok": ok, "confidence": confidence,
            "path": path, "src_name": flow.get("src_name"),
            "device_type": flow.get("device_type"), "rtt_ms": flow.get("rtt_ms"),
            # 관측 포트(best-effort): RX-선행이면 소스 TX 포트가 빠질 수 있다(소스는 roster 가 path[0]로 해소).
            "ports": sorted(p for p in flow["ports"] if p is not None),
            "ts": flow.get("last_ts", flow["first_ts"]),
        }
