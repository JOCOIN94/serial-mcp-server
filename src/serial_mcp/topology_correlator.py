"""topology_correlator.py — Event 스트림 → 멀티홉 Hop 상관(순수 stateful, Phase B 모듈3).

(UnID, Unique) 1차 키로 같은 패킷의 이벤트를 여러 포트에서 모은다. 키는 송신자가 발급하고
릴레이가 verbatim 보존하므로(펌웨어 §4) 소스 TX([Tx - my INFO])와 SSM RX([Proc-WiFiRx])가
같은 키로 묶인다. 단 INFO 요청(ReqInfoTo)엔 Unique 가 없어 (UnID,Unique)는 '장비 응답 TX ↔
SSM RX' leg + dedup 전용이다(plan §7-3 다중키: 라우팅=Rt, ACK=RS 등은 모듈4/후속).

설계 못:
- 윈도 클럭 = 서버 도착 ts(단조), 펌웨어 로그 RTC 아님(§4 C2).
- 포트내 dedup — 메시 브로드캐스트로 같은 패킷이 한 포트에 여러 번 수신된다.
- Unique 는 uint8 롤링이라 윈도 밖 같은 키는 별개 패킷(완료 키는 _recent 로 윈도만 차단).
- 완성(소스 TX + SSM RX, 순서 무관) → 성공 Hop(ok=True, path=[Passed Device], rtt=takentime) 즉시 방출.
  실장비는 펌웨어 출력순(sendMessage 후 [Tx])으로 SSM RX 가 소스 TX 보다 먼저 오므로(§4), RX 를 즉시
  완료하지 않고 둘째 이벤트에서 방출해 src_port(leaf)+rx_port(SSM) 를 함께 실어야 '포트간 링크'가 관측된다.
- SSM RX 만 있고 rx_grace 내 소스 TX 없음 → sweep 이 rx-only 로 방출(src_port=None, 원격/미연결 leaf).
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
                 max_recent: int = 4000, rx_grace_s: float = 1.0) -> None:
        self._window = window_s
        self._rx_grace = rx_grace_s      # rx-only 홉(소스 TX 미관측)을 sweep 방출 전 대기 — 뒤늦은 TX 흡수용
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
                    "tx_port": None, "rx_port": None, "rx_ts": None,
                    "path": [], "src_name": None, "device_type": None, "rtt_ms": None, "rssi": None}
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
            if flow["tx_port"] is None:            # leaf 발신 로컬 포트(첫 관측 보존)
                flow["tx_port"] = ev.get("port")
            # RX 가 이미 관측됐으면(실장비: 펌웨어 출력순으로 RX 선행) 완성 → src_port+rx_port 둘 다 실어 방출.
            if flow["rx"]:
                out.append(self._emit(flow, ok=True, confidence="observed"))
                self._complete(key, ts)
            return out

        # kind == "rx" (SSM 수신)
        self._ssm_rx_unids.add(key[0])
        flow["rx"] = True
        if flow["rx_port"] is None:                # SSM 수신 포트(그룹 귀속 키)
            flow["rx_port"] = ev.get("port")
        if flow["rx_ts"] is None:                  # rx-only sweep grace 기준 시각
            flow["rx_ts"] = ts
        flow["src_name"] = ev["hints"].get("src_name") or flow["src_name"]
        if ev["metrics"].get("takentime_ms") is not None:
            flow["rtt_ms"] = ev["metrics"]["takentime_ms"]
        if ev["metrics"].get("rssi") is not None:      # SB→SSM 수신 RSSI(INFO[2]) — 링크 품질색
            flow["rssi"] = ev["metrics"]["rssi"]
        flow["path"] = _parse_passed(ev["hints"].get("passed"))
        # 완성 우선: TX 를 이미 봤으면 즉시 방출(둘 다 채움). 아직이면 대기 — 뒤늦은 TX 를 흡수해
        # src_port 를 채우거나(포트간 링크), TX 가 안 오면 sweep 이 grace 후 rx-only 로 방출한다.
        if flow["tx"]:
            out.append(self._emit(flow, ok=True, confidence="observed"))
            self._complete(key, ts)
        return out

    def sweep(self, now: float) -> list:
        """미완 흐름을 방출.

        - rx-only(SSM 수신했으나 소스 TX 미관측): rx_grace 후 성공 방출(src_port=None, 원격/미연결 leaf).
        - tx-only(소스 TX 만): window 후 실패(SSM 이 그 장비를 들은 적 있음) 또는 unconfirmed(SSM 부재/타 메시).
        완성(tx+rx)은 observe 에서 이미 방출됐으므로 여기 도달하지 않는다.
        """
        out: list = []
        for key in list(self._flows.keys()):
            flow = self._flows[key]
            if flow["rx"]:                      # rx-only: 뒤늦은 TX 를 grace 만큼 기다렸다 없으면 rx_port 만으로 방출
                if now - (flow["rx_ts"] or flow["first_ts"]) < self._rx_grace:
                    continue
                self._flows.pop(key, None)
                out.append(self._emit(flow, ok=True, confidence="observed"))
                self._complete(key, flow["last_ts"])
                continue
            if now - flow["first_ts"] < self._window:    # tx-only: window 만료 대기
                continue
            self._flows.pop(key, None)
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
        # 홉에 시각(ts)을 싣지 않는다(의도적). 서버 도착시각(monotonic)은 포트 지터·펌웨어
        # 출력순(sendMessage 후 [Tx])으로 RX 가 TX 보다 먼저 관측될 수 있어 인과/순서 신호로
        # 부적합하다 — 상관은 (UnID,Unique) 키로 이미 끝났다. 윈도 만료 판정용 last_ts/first_ts 는
        # flow 내부에만 두고 노출하지 않아, 소비자(AI)가 순서로 추론하다 오판하는 것을 막는다.
        return {
            "key": flow["key"], "ok": ok, "confidence": confidence,
            "path": path, "src_name": flow.get("src_name"),
            "device_type": flow.get("device_type"), "rtt_ms": flow.get("rtt_ms"),
            "rssi": flow.get("rssi"),
            # rx_port=SSM 수신 포트(그룹 귀속), src_port=leaf 발신 로컬 포트. 한쪽만 관측되면 다른 쪽 None.
            "rx_port": flow.get("rx_port"), "src_port": flow.get("tx_port"),
            # 관측 포트(best-effort): RX-선행이면 소스 TX 포트가 빠질 수 있다(소스는 roster 가 path[0]로 해소).
            "ports": sorted(p for p in flow["ports"] if p is not None),
        }
