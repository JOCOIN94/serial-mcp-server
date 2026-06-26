"""topology.py — 시리얼 포트 → 메시 토폴로지 로스터(순수 로직, I/O 비의존).

각 포트의 수신 로그(또는 별칭)로 장비를 식별(SSM/REP/APU/SB·ESP/STM)하고, SSM별
그룹·행(타입)·열(번호)로 배치한 로스터를 만든다. 웹 뷰어 좌측 토폴로지 그래프가 이
로스터(groups[].nodes[].{row,col,...})를 그대로 절대배치로 그린다 — UI는 배치를 추론하지
않는다.

설계 근거(SPEC §10 확장): 같은 종류 장비가 여러 개인 멀티홉 메시에서 로그 체인을
추적하려면 '어느 포트가 어느 장비인지·서로 어떻게 묶이는지'를 알아야 한다. 식별은
**로그 내용 자동발견**이며(명시 별칭이 있으면 우선), 패턴은 실측 캡처(2026-06-26)와
펌웨어 소스(firmware-src: ssm-esp32/SB_ESP32/Repeat/APU)에 근거한다. README 경고대로
'그 보드에서만·여러 상태에서' 나오는 고유 패턴만 등록해 상대 보드명 인용(SSM 로그 속
'SB1' 등) 오인을 줄인다.

불변식: 그룹↔SSM은 1:1 — 한 그룹에 SSM 2개 불가, SSM N개=그룹 N개.
시리얼 I/O·HTTP 비의존이라 단위 테스트가 쉽다(test_topology.py).
"""

from __future__ import annotations

import re
from typing import Optional

# 메시 계층 → 그래프 행. SSM(게이트웨이)=0, REP(리피터)=1, APU=2, SB(베이)=4.
# (디자인의 bands [[0],[1],[2,3],[4,5]] 와 호환 — 빈 행은 프론트 layout 이 접는다.)
ROW_BY_TYPE = {"SSM": 0, "REP": 1, "APU": 2, "SB": 4}
TYPE_RANK = {"SSM": 0, "REP": 1, "APU": 2, "SB": 3}   # 그룹 내 정렬용

# 로그 내용 식별 시그니처: (type, mcu, weight, regex). 점수 합산 — 단일 whitelist 아님.
# SSM/SB 는 실측 검증, REP/APU 는 실측 캡처 전이라 '명시 별칭 우선'으로 보수 운영.
_SIGNATURES = [
    # SSM-ESP(게이트웨이): Proc-* 는 WiFi 활성 시만 나오므로 Route/From SB/REPRSSI 도 함께.
    ("SSM", "ESP", 3, re.compile(r"\[Proc-WiFiRx\]|\[Proc-Raw Packet\]|\[Proc_WiFiTx\]|\[Proc-WebRTx\]")),
    ("SSM", "ESP", 3, re.compile(r"\[Route\] Link|<<<\s*From\s+SB|REPRSSI")),
    # SB-ESP: 자기 INFO 송신 + WiFi 수신.
    ("SB", "ESP", 3, re.compile(r"\[Tx - my INFO\]|\[WiFi_Rx\]|Save a new Reved-Packet")),
    # SB-STM: 베이 컨트롤러(카드·가격·베이설정).
    ("SB", "STM", 3, re.compile(r"\bBayID\s*:|<\s*MasterCard\s*>|BayConfig Info|minCoinSensingTime|Price1st")),
]

# 장비 자기 번호(BayID/UnID) 추출 — SB 의 ESP/STM 짝을 같은 번호로 묶는 데 쓴다.
_RE_BAYID = re.compile(r"\bBayID\s*:\s*(\d+)")          # SB-STM
_RE_UNID = re.compile(r'"UnID"\s*:\s*(\d+)')            # SB-ESP(자기 패킷의 UnID=자기 BayID)

# 별칭(SERIAL_NAMES/AUTONAME) 파싱: 'SB1-ESP'→type SB·num 1·mcu ESP, 'SSM'→type SSM.
_RE_ALIAS_UNIT = re.compile(r"^(SSM|SB|REP|APU)\s*0*(\d+)?$", re.IGNORECASE)


def _norm_mcu(chip: Optional[str]) -> Optional[str]:
    """'ESP32-S3'·'esp'→'ESP', 'STM32F4'·'stm'→'STM', 그 외 None."""
    if not chip:
        return None
    c = chip.strip().upper()
    if c.startswith("ESP"):
        return "ESP"
    if c.startswith("STM"):
        return "STM"
    return None


def parse_alias(alias: Optional[str]) -> tuple[Optional[str], Optional[int], Optional[str]]:
    """별칭 → (type, number, mcu). 명시 식별이 자동발견보다 우선이다.

    'SB1-ESP'→('SB',1,'ESP'), 'SSM-ESP'→('SSM',None,'ESP'), 'SSM'→('SSM',None,None),
    'APU3'→('APU',3,None), 미인식/없음→(None,None,None).
    """
    if not alias:
        return (None, None, None)
    unit, _, chip = alias.strip().partition("-")
    mcu = _norm_mcu(chip)
    m = _RE_ALIAS_UNIT.match(unit.strip())
    if not m:
        return (None, None, mcu)
    typ = m.group(1).upper()
    num = int(m.group(2)) if m.group(2) else None
    return (typ, num, mcu)


def classify_lines(lines) -> tuple[Optional[str], Optional[str]]:
    """수신 로그로 (type, mcu) 추정. 신뢰 없으면 (None, None).

    여러 시그니처 점수를 합산해 최고 (type, mcu)를 고른다. 각 보드 고유 패턴만 등록해
    상대 보드명 인용(SSM 로그 속 'SB1' 등) 오인을 억제한다.
    """
    blob = "\n".join(lines) if isinstance(lines, (list, tuple)) else str(lines or "")
    scores: dict[tuple[str, str], int] = {}
    for typ, mcu, weight, rx in _SIGNATURES:
        n = len(rx.findall(blob))
        if n:
            scores[(typ, mcu)] = scores.get((typ, mcu), 0) + weight * min(n, 5)
    if not scores:
        return (None, None)
    return max(scores.items(), key=lambda kv: kv[1])[0]


def _number_from_lines(typ: Optional[str], mcu: Optional[str], lines) -> Optional[int]:
    """SB 의 자기 번호(BayID/UnID) 추출 — ESP/STM 짝을 묶는 키. SSM/REP/APU 는 None."""
    if typ != "SB":
        return None
    blob = "\n".join(lines) if isinstance(lines, (list, tuple)) else str(lines or "")
    rx = _RE_BAYID if mcu == "STM" else _RE_UNID
    m = rx.search(blob)
    return int(m.group(1)) if m else None


def identify_port(port: str, alias: Optional[str], lines, connected: bool = True) -> dict:
    """포트 1개의 정체 추정. 별칭 우선, 없으면 로그 자동발견.

    반환: {port, type, mcu, number, connected}. type 미상이면 type=None(미분류).
    """
    typ, num, mcu = parse_alias(alias)
    if typ is None:                                   # 별칭으로 못 정하면 로그로 발견
        typ, mcu = classify_lines(lines)
    if num is None:                                   # 번호는 로그(BayID/UnID)로 보강
        num = _number_from_lines(typ, mcu, lines)
    if mcu is None and typ in ("SSM", "REP", "APU"):  # 이들은 단일 ESP MCU(도메인 사실)
        mcu = "ESP"                                   # — 노드 내부 라벨용 기본값
    return {"port": port, "type": typ, "mcu": mcu, "number": num, "connected": bool(connected)}


def _status_of(connected: bool) -> str:
    """Phase A 상태: 연결=good, 미연결=stale. (Phase B 에서 라이브 메시 상태로 대체.)"""
    return "good" if connected else "stale"


def build_roster(entries) -> dict:
    """포트 목록 → 토폴로지 로스터.

    entries: [{port, alias, lines, connected}] (lines 는 최근 수신 줄 list).
    반환: {"groups": [{id, label, ssm_port, nodes:[node...]}], "unplaced": [port...]}.
      node = {id, type, label, row, col, status, ports:[{mcu, port, connected}]}.
      - SB 의 ESP/STM(같은 번호)은 한 노드로 병합(ports 2개, 프론트가 [ESP|STM] 분할).
      - row=타입 계층, col=같은 타입 내 번호/발견순.
    불변식: 그룹↔SSM 1:1. SSM 0개면 단일 기본 그룹, 1개면 그 SSM 그룹, N개면 N그룹.
    """
    ids = [identify_port(e["port"], e.get("alias"), e.get("lines"), e.get("connected", True))
           for e in entries]
    placed = [d for d in ids if d["type"]]
    unplaced = [d["port"] for d in ids if not d["type"]]

    ssms = [d for d in placed if d["type"] == "SSM"]
    others = [d for d in placed if d["type"] != "SSM"]

    # 그룹 골격(그룹↔SSM 1:1). SSM 없으면 단일 기본 그룹.
    if ssms:
        groups = [{"id": f"g{i+1}", "label": _label(s), "ssm_port": s["port"], "_members": [s]}
                  for i, s in enumerate(ssms)]
    else:
        groups = [{"id": "g1", "label": "(SSM 미식별)", "ssm_port": None, "_members": []}]

    # 비-SSM 귀속: Phase A 는 GID 미파싱이라 단일 SSM 가정(첫 그룹에 귀속). 멀티 SSM 의
    # 정확한 GID/채널 귀속은 Phase B. (실측 기준 구성은 SSM 1개라 정확.)
    for d in others:
        groups[0]["_members"].append(d)

    for g in groups:
        g["nodes"] = _layout_group(g["_members"])
        del g["_members"]
    return {"groups": groups, "unplaced": unplaced}


def _label(d: dict) -> str:
    """노드/그룹 표시 라벨: 'SSM', 'SB5', 'APU3' (번호 있으면 붙임)."""
    return d["type"] + (str(d["number"]) if d.get("number") is not None else "")


def _merge_sb(members: list[dict]) -> list[dict]:
    """SB 의 ESP/STM(같은 번호)을 한 논리 노드로 병합. 번호 없으면 포트별 개별 노드."""
    merged: dict = {}           # number -> 병합 노드
    singles: list[dict] = []
    for d in members:
        if d["type"] == "SB" and d.get("number") is not None:
            key = d["number"]
            node = merged.setdefault(key, {"type": "SB", "number": key, "ports": []})
            node["ports"].append({"mcu": d.get("mcu"), "port": d["port"], "connected": d["connected"]})
        else:
            singles.append({"type": d["type"], "number": d.get("number"),
                            "ports": [{"mcu": d.get("mcu"), "port": d["port"], "connected": d["connected"]}]})
    # SB 칩 순서를 발견순이 아니라 ESP→STM 으로 일관 고정(디자인 [ESP|STM]).
    for node in merged.values():
        node["ports"].sort(key=lambda p: 0 if p.get("mcu") == "ESP" else 1 if p.get("mcu") == "STM" else 2)
    return list(merged.values()) + singles


def _layout_group(members: list[dict]) -> list[dict]:
    """그룹 멤버 → 배치된 노드 목록(row=타입, col=같은 타입 내 번호/순서)."""
    nodes = _merge_sb(members)
    # 타입·번호로 안정 정렬(열 순서 결정)
    nodes.sort(key=lambda n: (TYPE_RANK.get(n["type"], 9),
                              n["number"] if n.get("number") is not None else 1 << 30))
    col_of: dict[str, int] = {}
    out = []
    for n in nodes:
        typ = n["type"]
        col = col_of.get(typ, 0)
        col_of[typ] = col + 1
        connected = any(p["connected"] for p in n["ports"])
        out.append({
            "id": f"{typ}-{n['number'] if n.get('number') is not None else n['ports'][0]['port']}",
            "type": typ,
            "label": _label(n),
            "row": ROW_BY_TYPE.get(typ, 5),
            "col": col,
            "status": _status_of(connected),
            "ports": n["ports"],
        })
    return out
