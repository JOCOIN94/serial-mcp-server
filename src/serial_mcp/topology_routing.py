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
# route_link/reprssi 만 **링크별**(mac쌍) 품질이다. info_rssi(INFO[2])와 info_table_rf(SSM INFO
# 테이블 RF열)는 같은 값의 두 경로 — 장비가 보고한 **주변 이웃 평균**(펌웨어 avrRssi, 2026-07-02
# 확인: 테이블 rf = jsonWiFiRxBuf["INFO"][2] 저장분)이라 링크가 아닌 장비 단위 RF 건강도다.
# takentime/rs 는 RSSI 가 아니라 품질 프록시 — source 라벨로 출처를 노출해 혼동을 막는다.
RSSI_LADDER = ("route_link", "reprssi", "info_rssi", "info_table_rf", "takentime", "rs")

# 펌웨어 예약 토큰(노드로 등록/해소 금지). ROUTE_DIRECT_TOKEN '00'(직접/SSM 홉) · ROUTE_EMPTY_TOKEN
# 'FF'(빈 슬롯) — 둘 다 SSM_esp32.h:544-545(SB 보드 헤더도 동일). retEncrytion(SSM_esp32.ino:6243)이
# 0 을 절대 반환 않으므로(if(!dat) dat=0xFF) 실장비의 mac 파생 토큰도 '00' 이 될 수 없다. → 이 토큰들에
# 노드를 귀속하면 예약 센티넬을 오염시키고, UnitID 0(미할당) 장비들이 '00' 한 칸으로 충돌한다.
_RESERVED_TOKENS = frozenset({"00", "FF"})

# [Passed Device] '(05-SB5)->(01-REP1)' → (토큰, 이름) 쌍. 토큰=2-hex, 이름=unitName.
_RE_PASSED_PAIR = re.compile(r"\(\s*([0-9A-Fa-f]+)\s*-\s*([^)]+?)\s*\)")

# SSM INFO 테이블('<< Information on the entire equipment >>') 행의 'Mac(ID)' 셀 —
# 'A0,85,E3,EA,5C,C4( 5)' / 자기 행은 '( -)'. 이 셀 구조(콤마 mac + 괄호 ID)와 파이프 6개
# 이상 조합은 테이블 행에만 나타난다([Route] Link·REPRSSI json 과 안 겹침) → 무상태 라인 매칭.
_RE_TABLE_MAC_CELL = re.compile(r"([0-9A-Fa-f]{2}(?:,[0-9A-Fa-f]{2}){5})\(\s*(-|\d+)\s*\)")


def _norm_mac(s: str) -> str:
    """테이블 mac('A0,85,..')을 이벤트 mac 정규형(대문자 콜론)으로 — topology_events 와 동일 규약."""
    return s.strip().replace(",", ":").upper()


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

    def __init__(self, fresh_window_s: float = 30.0, max_links: int = 4000,
                 max_table: int = 512) -> None:
        self._fresh = fresh_window_s
        self._max_links = max_links
        self._max_table = max_table
        self._links: "OrderedDict[tuple, dict]" = OrderedDict()   # (from_mac,to_mac)→{rssi,ts,source}
        self._tokens: dict = {}                                   # token(2-hex)→{name,mac,unid}
        # SSM INFO 테이블 관측(mac↔UnID↔이름 다리 + 장비 RF). BayID 장비는 mesh payload 에 Mac 을
        # 숨기므로(펌웨어 if(BayID) UnID else Mac) 이 테이블이 mac 공간↔논리 노드 공간의 유일한 다리다.
        self._table: "OrderedDict[str, dict]" = OrderedDict()     # mac→{name,unid,rf,ts}
        self._ssm_mac: dict = {}                                  # port→그 포트 SSM 자신의 mac(자기 행)
        # 이름 해소 원천(_tokens·_table·_ssm_mac) 변경 카운터 — 소비자(엔진 _names 캐시)가
        # 매 이벤트 재빌드 대신 version 비교로 무효화한다. 값이 같으면 이름 원천 불변이 보장된다.
        self._version = 0

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

    def observe_table_line(self, port: str, ts: float, text: str) -> None:
        """SSM INFO 테이블 행 1줄 관측(무상태 라인 매칭). 엔진이 매 줄 흘려준다(카드페어링과 동형).

        행 파싱은 'Mac(ID)' 셀을 앵커로 한다 — LastComTime 셀 안에 '|' 가 들어가는 행이 있어
        (S..:.. | R..:..) 고정 인덱스 split 이 불가하므로, mac 셀 위치 기준 상대 참조:
        unitName=셀[1], RF=mac 직전 셀, 장비타입=mac 직후 셀. 타입 'SSM'(자기 행, ID='-')은
        그 포트 SSM 자신의 mac 으로 저장한다(멤버십 링크 끝점 해소용). (ID)>0 이면 토큰맵에도
        mac/unid 를 등록해 직접 노드 mac enrich([Passed Device] 이름 해소와 같은 InfoListArr 원천).
        """
        if text.count("|") < 6:
            return
        m = _RE_TABLE_MAC_CELL.search(text)
        if not m:
            return
        cells = [c.strip() for c in text.split("|")]
        idx = next((i for i, c in enumerate(cells) if _RE_TABLE_MAC_CELL.search(c)), None)
        if idx is None or idx < 2:
            return
        mac = _norm_mac(m.group(1))
        unid = None if m.group(2) == "-" else int(m.group(2))
        if unid == 0:
            unid = None                       # UnitID 0 = 미할당(BayID=0 장비) — 다리 키 아님
        name = cells[1] or None
        try:
            rf = int(cells[idx - 1])
        except ValueError:
            rf = None                         # 자기 행 '-' 등
        typ = cells[idx + 1].upper() if idx + 1 < len(cells) else ""
        if typ == "SSM":
            self._ssm_mac[port] = mac
        ent = self._table.setdefault(mac, {"name": None, "unid": None, "rf": None, "ts": None})
        ent.update({"name": name or ent["name"], "unid": unid if unid is not None else ent["unid"],
                    "rf": rf if rf is not None else ent["rf"], "ts": ts})
        self._version += 1                    # 테이블 행 = 이름 원천 갱신(주기 출력이라 저빈도)
        self._table.move_to_end(mac)
        while len(self._table) > self._max_table:
            self._table.popitem(last=False)   # drop-oldest
        tok = _token_of_unid(unid)
        if tok is not None:
            t = self._tokens.setdefault(tok, {"name": None, "mac": None, "unid": None})
            t["unid"], t["mac"] = unid, mac
            if name:
                t["name"] = name              # unitName — [Passed Device] 해소와 같은 원천이라 덮어써도 동치

    def info_table(self) -> dict:
        """INFO 테이블 스냅샷(사본) {by_mac: {mac:{name,unid,rf,ts}}, ssm_mac: {port: mac}}.

        build_roster(모듈5)가 멤버십 링크의 mac 끝점 해소(unid→mac, ssm_port→mac)와
        info_table_rf 폴백(장비 단위 RF)에 쓴다.
        """
        return {"by_mac": {mac: dict(e) for mac, e in self._table.items()},
                "ssm_mac": dict(self._ssm_mac)}

    def edges(self, now: Optional[float] = None) -> list:
        """링크 그래프 스냅샷 → [{from,to,rssi,source,fresh}]. now 없으면 fresh=None(미상)."""
        out = []
        for (frm, to), v in self._links.items():
            fresh = None if now is None else (now - v["ts"]) < self._fresh
            out.append({"from": frm, "to": to, "rssi": v["rssi"],
                        "source": v["source"], "fresh": fresh})
        return out

    def tokens(self) -> dict:
        """토큰맵 스냅샷(사본) {token: {name,mac,unid}}. roster(모듈5)가 원격 노드·mac 해소에 쓴다."""
        return {tok: dict(ent) for tok, ent in self._tokens.items()}

    def resolve_token(self, token) -> Optional[dict]:
        """라우팅 토큰 → 노드 식별 {name,mac,unid} 사본. 미등록/예약 토큰이면 None."""
        tok = _norm_token(token)
        if tok is None or tok in _RESERVED_TOKENS:
            return None
        ent = self._tokens.get(tok)
        return dict(ent) if ent is not None else None

    def version(self) -> int:
        """이름 원천(_tokens·_table·_ssm_mac) 변경 카운터. 같으면 이름 캐시 재사용 가능."""
        return self._version

    def _register_tokens(self, ev) -> None:
        ids = ev.get("ids") or {}
        tok = _token_of_unid(ids.get("unid"))            # 예약/파싱불가 토큰이면 None → 자기등록 skip
        if tok is not None:
            ent = self._tokens.setdefault(tok, {"name": None, "mac": None, "unid": None})
            if ent["unid"] != ids.get("unid"):           # 실제 변경 시에만 version 증가(캐시 유효성 유지)
                ent["unid"] = ids.get("unid")
                self._version += 1
            if ids.get("mac") and ent["mac"] != ids["mac"]:
                ent["mac"] = ids["mac"]
                self._version += 1
        for ptok, name in _parse_passed_tokens((ev.get("hints") or {}).get("passed")):
            if ptok in _RESERVED_TOKENS:                 # 예약 토큰은 이름도 등록 안 함
                continue
            ent = self._tokens.setdefault(ptok, {"name": None, "mac": None, "unid": None})
            if ent["name"] != name:
                ent["name"] = name
                self._version += 1

    def _add_link(self, frm, to, rssi, source, ts) -> None:
        key = (frm, to)
        self._links[key] = {"rssi": rssi, "ts": ts, "source": source}
        self._links.move_to_end(key)                 # 갱신 간선을 최신화(신선분 축출 방지)
        while len(self._links) > self._max_links:
            self._links.popitem(last=False)          # drop-oldest
