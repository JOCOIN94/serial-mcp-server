"""topology_routing.py — 링크 그래프·토큰맵·RSSI ladder(순수 stateful, Phase B 모듈4).

실제 경로(Rt/[Passed Device])와 **링크 그래프**(REPRSSI/[Route] Link)는 분리한다(plan §2·§7-4):
전자는 "패킷이 실제로 지나간 홉", 후자는 "들리는(가능한) 링크"다. 이 모듈은 후자(링크 그래프)와
경로 토큰 해소를 위한 토큰맵, 그리고 구간 품질 표시용 RSSI 폴백 ladder 를 소유한다.

펌웨어 근거(cbm 검증, plan §4·§13):
- **링크 간선** = RouteUpdateLinkFromReprssi(SSM_esp32.ino:6709): SSM 이 RX 패킷의 REPRSSI 를
  RouteLinkMatrix[fromPos][targetPos] 에 적재하고 `[Route] Link <fromMac> -> <targetMac> rssi=<n>` 출력.
  fromPos=보고자(data.macAddress), targetPos=이웃 mac(CheckInfoTable 미스면 skip=등록 노드 한정).
  → REPRSSI 원본과 [Route] Link 는 **같은 방향(보고자→이웃)의 같은 간선**이다([Route] Link 는
  등록노드로 필터된 REPRSSI 의 해소 출력). 따라서 둘 다 같은 (from,to) 키로 갱신한다(중복 간선 금지).
- **토큰** = RouteTokenForInfoPos(:6299): UnitID 있으면 `%02X`(UnitID&0xFF), 없으면 `%02X`(retEncrytion(mac)).
  → '05'=UnitID 5, '10'=UnitID 16(hex). [Passed Device] '(05-SB5)' 가 토큰→이름(SB5)을 이미 해소해 준다.

설계 못:
- 링크 신선도(fresh)는 **서버 도착 ts**(단조)로 판정한다(펌웨어 millis/lastUpdateMs 아님 — §4 C2 클럭).
- 토큰맵은 UnID→토큰(2-hex) 자기등록 + [Passed Device] 토큰→이름 해소를 같은 토큰 키로 병합한다.
  단 예약 토큰 '00'(ROUTE_DIRECT_TOKEN)·'FF'(ROUTE_EMPTY_TOKEN)는 노드가 아니므로 등록/해소하지 않는다.
  UnitID 0 은 retEncrytion(mac) 분기(토큰≠'00', 여기선 재현 불가)라 자기등록을 건너뛴다(§4·_RESERVED_TOKENS).
- 링크 상한 + drop-oldest(헤드리스 장시간 누수 방지). 갱신 간선은 최신화해 신선분 축출을 막는다.
- RSSI ladder 는 우선순위 picker(순수 함수): 강한 RSSI 부터 약한 프록시(takentime/RS)까지 폴백.
  per-packet RSSI(VEXTUNITINFO)는 상태 토글이라 자동 사용 금지 — ladder 는 이미 들어온 값만 고른다.

I/O 비의존 — 단위 테스트 용이(test_topology_routing.py). 토큰→노드 최종 귀속·구간 조립은 roster/engine.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from typing import Optional

# RSSI 폴백 ladder(강→약). plan §7-4: [Route] Link → REPRSSI → INFO[2] → INFO표 RF열 → takentime → RS.
# takentime/rs 는 RSSI 가 아니라 품질 프록시 — source 라벨로 출처를 노출해 혼동을 막는다.
RSSI_LADDER = ("route_link", "reprssi", "info_rssi", "info_table_rf", "takentime", "rs")

# 펌웨어 예약 토큰(노드로 등록/해소 금지). ROUTE_DIRECT_TOKEN '00'(직접/SSM 홉) · ROUTE_EMPTY_TOKEN
# 'FF'(빈 슬롯) — 둘 다 SSM_esp32.h:544-545(SB 보드 헤더도 동일). retEncrytion(SSM_esp32.ino:6243)이
# 0 을 절대 반환 않으므로(if(!dat) dat=0xFF) 실장비의 mac 파생 토큰도 '00' 이 될 수 없다. → 이 토큰들에
# 노드를 귀속하면 예약 센티넬을 오염시키고, UnitID 0(미할당) 장비들이 '00' 한 칸으로 충돌한다.
_RESERVED_TOKENS = frozenset({"00", "FF"})

# [Passed Device] '(05-SB5)->(01-REP1)' → (토큰, 이름) 쌍. 토큰=2-hex, 이름=unitName.
_RE_PASSED_PAIR = re.compile(r"\(\s*([0-9A-Fa-f]+)\s*-\s*([^)]+?)\s*\)")


def _norm_token(tok) -> Optional[str]:
    """토큰 문자열/숫자 → 2-hex 대문자('5'→'05','0a'→'0A'). hex 파싱 실패 시 None.

    RouteTokenForInfoPos 가 '%02X' 로 찍으므로 토큰은 본디 2-hex 대문자다. UnID 자기등록과
    [Passed Device] 해소가 같은 키를 갖도록, 양쪽 토큰을 동일 정규형으로 맞춘다.
    """
    if tok is None:
        return None
    try:
        return f"{int(str(tok), 16) & 0xFF:02X}"
    except (TypeError, ValueError):
        return None


def _token_of_unid(unid) -> Optional[str]:
    """UnitID(10진) → 라우팅 토큰(2-hex). RouteTokenForInfoPos: UnitID 있으면 '%02X'(UnitID & 0xFF).

    UnitID 0 은 펌웨어상 retEncrytion(mac) 분기라 토큰이 '00'(ROUTE_DIRECT_TOKEN)이 아니다 — 여기선
    retEncrytion 을 재현할 수 없으므로 None 을 돌려 자기등록을 건너뛴다(예약 센티넬 오염·'00' 충돌 방지).
    토큰이 예약값('00'/'FF')으로 떨어지면(UnitID 0/255) 노드 토큰이 아니므로 None 이다.
    """
    try:
        tok = f"{int(unid) & 0xFF:02X}"
    except (TypeError, ValueError):
        return None
    return None if tok in _RESERVED_TOKENS else tok


def _parse_passed_tokens(passed: Optional[str]) -> list:
    """'[Passed Device]' 문자열 → [(토큰, 이름), ...]. 없으면 []. 토큰은 정규형(2-hex)으로."""
    if not passed:
        return []
    out = []
    for m in _RE_PASSED_PAIR.finditer(passed):
        tok = _norm_token(m.group(1))
        if tok is not None:
            out.append((tok, m.group(2).strip()))
    return out


def pick_link_metric(candidates: dict) -> dict:
    """후보 metric dict → ladder 우선순위로 첫 유효값 선택. 반환 {value, source}.

    candidates: {source_name: value}. RSSI_LADDER 순서로 처음 None 아닌 값을 고른다(0 은 유효값).
    어떤 후보도 없으면 {value:None, source:None}. 후보 조립(INFO[2]·takentime 등 어디서 오는지)은
    엔진(모듈6) 책임 — 이 함수는 순위 결정만 한다(순수).
    """
    for src in RSSI_LADDER:
        v = candidates.get(src)
        if v is not None:
            return {"value": v, "source": src}
    return {"value": None, "source": None}


class RoutingTable:
    """링크 그래프(REPRSSI/[Route] Link) + 토큰맵을 유지하는 순수 stateful 엔진."""

    def __init__(self, fresh_window_s: float = 30.0, max_links: int = 4000) -> None:
        self._fresh = fresh_window_s
        self._max_links = max_links
        self._links: "OrderedDict[tuple, dict]" = OrderedDict()   # (from_mac,to_mac)→{rssi,ts,source}
        self._tokens: dict = {}                                   # token(2-hex)→{name,mac,unid}

    def observe(self, ev) -> None:
        """이벤트 1개로 링크 그래프·토큰맵을 갱신(방출 없음 — 스냅샷은 edges()/resolve_token())."""
        if not ev:
            return
        kind = ev.get("kind")
        ts = ev.get("ts") or 0.0
        if kind == "route":
            r = ev.get("route") or {}
            if r.get("from_mac") and r.get("to_mac"):
                self._add_link(r["from_mac"], r["to_mac"], r.get("rssi"), "route_link", ts)
        elif kind == "webtx":
            src = (ev.get("ids") or {}).get("mac")
            if src:
                for row in (ev.get("metrics") or {}).get("reprssi") or []:
                    if row.get("mac"):
                        self._add_link(src, row["mac"], row.get("rssi"), "reprssi", ts)
        self._register_tokens(ev)

    def edges(self, now: Optional[float] = None) -> list:
        """링크 그래프 스냅샷 → [{from,to,rssi,source,fresh}]. now 없으면 fresh=None(미상)."""
        out = []
        for (frm, to), v in self._links.items():
            fresh = None if now is None else (now - v["ts"]) < self._fresh
            out.append({"from": frm, "to": to, "rssi": v["rssi"],
                        "source": v["source"], "fresh": fresh})
        return out

    def resolve_token(self, token) -> Optional[dict]:
        """라우팅 토큰 → 노드 식별 {name,mac,unid} 사본. 미등록/예약 토큰이면 None."""
        tok = _norm_token(token)
        if tok is None or tok in _RESERVED_TOKENS:
            return None
        ent = self._tokens.get(tok)
        return dict(ent) if ent is not None else None

    def _register_tokens(self, ev) -> None:
        ids = ev.get("ids") or {}
        tok = _token_of_unid(ids.get("unid"))            # 예약/파싱불가 토큰이면 None → 자기등록 skip
        if tok is not None:
            ent = self._tokens.setdefault(tok, {"name": None, "mac": None, "unid": None})
            ent["unid"] = ids.get("unid")
            if ids.get("mac"):
                ent["mac"] = ids["mac"]
        for ptok, name in _parse_passed_tokens((ev.get("hints") or {}).get("passed")):
            if ptok in _RESERVED_TOKENS:                 # 예약 토큰은 이름도 등록 안 함
                continue
            ent = self._tokens.setdefault(ptok, {"name": None, "mac": None, "unid": None})
            ent["name"] = name

    def _add_link(self, frm, to, rssi, source, ts) -> None:
        key = (frm, to)
        self._links[key] = {"rssi": rssi, "ts": ts, "source": source}
        self._links.move_to_end(key)                 # 갱신 간선을 최신화(신선분 축출 방지)
        while len(self._links) > self._max_links:
            self._links.popitem(last=False)          # drop-oldest
