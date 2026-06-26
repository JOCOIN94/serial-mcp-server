"""topology.py — 시리얼 포트 → 메시 토폴로지 로스터(순수 로직, I/O 비의존).

각 포트의 수신 로그(또는 별칭)로 장비를 식별(SSM/REPEAT/APU/APU_C/SB·ESP/STM)하고, SSM별
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

import json
import re
from typing import Optional

# 펌웨어 장비타입 enum(SSM_esp32.h:468-472·repeater 헤더): dTSSM=1·dTAPU=2·dTAPU_C_SLIM=3·
# dTSBB=4·dTRPT=5. 각 장비의 자기 보고 [Tx - my INFO] 의 INFO[0] 이 이 타입숫자다. 문자열
# 토큰(별칭·SSM INFO 테이블)도 함께 정규화한다. 주의: SSM simplevInfoBuffer 는 5(Repeater)를
# 문자열 변환 없이 숫자로 흘리므로 "5"→REPEAT 매핑이 반드시 있어야 한다.
DEVICE_TYPE = {
    "1": "SSM", "2": "APU", "3": "APU_C", "4": "SB", "5": "REPEAT",
    "SSM": "SSM", "APU": "APU", "APU_C": "APU_C", "APUC": "APU_C",
    "SB": "SB", "REPEAT": "REPEAT", "REP": "REPEAT",
}

# 메시 계층 → 그래프 행. SSM(게이트웨이)=0, REPEAT=1, APU=2, APU_C=3, SB(베이)=4.
# (빈 행은 프론트 layout 이 접는다.)
ROW_BY_TYPE = {"SSM": 0, "REPEAT": 1, "APU": 2, "APU_C": 3, "SB": 4}
TYPE_RANK = {"SSM": 0, "REPEAT": 1, "APU": 2, "APU_C": 3, "SB": 4}   # 그룹 내 정렬용

# 로그 내용 식별 시그니처(약한 폴백 증거, conf 0.6): (type, mcu, weight, regex). 점수 합산.
# 고유 수동(passive) 패턴이 있는 보드만 등록한다: SSM(Proc-*/Route)·SB-STM(BayID 등).
# 리프 ESP(SB/APU/APU_C/REPEAT)는 [Tx - my INFO]/[WiFi_Rx]/Save 등 태그를 전부 공유해(펌웨어
# 검증: Repeat/APU/APU_C 도 활성 출력) 수동 시그니처로 서로 못 가르므로, 타입 판별은
# classify_device ②단계 INFO[0] enum 이 전담한다(SB-ESP 도 INFO[0]=4 로만 식별). over-broad
# 패턴을 SB 로 등록하면 INFO 없는 윈도의 APU/REPEAT 를 SB 로 오분류하므로 두지 않는다(§7-1 미확정=UNKNOWN).
_SIGNATURES = [
    # SSM-ESP(게이트웨이): Proc-* 는 WiFi 활성 시만 나오므로 Route/From SB/REPRSSI 도 함께.
    ("SSM", "ESP", 3, re.compile(r"\[Proc-WiFiRx\]|\[Proc-Raw Packet\]|\[Proc_WiFiTx\]|\[Proc-WebRTx\]")),
    ("SSM", "ESP", 3, re.compile(r"\[Route\] Link|<<<\s*From\s+SB|REPRSSI")),
    # SB-STM: 베이 컨트롤러(카드·가격·베이설정) — SB 고유.
    ("SB", "STM", 3, re.compile(r"\bBayID\s*:|<\s*MasterCard\s*>|BayConfig Info|minCoinSensingTime|Price1st")),
]

# 장비 자기 번호(BayID/UnID) 추출 — SB 의 ESP/STM 짝을 같은 번호로 묶는 데 쓴다.
_RE_BAYID = re.compile(r"\bBayID\s*:\s*(\d+)")          # SB-STM
_RE_UNID = re.compile(r'"UnID"\s*:\s*(\d+)')            # SB-ESP(자기 패킷의 UnID=자기 BayID)

# 별칭(SERIAL_NAMES/AUTONAME) 파싱: 'SB1-ESP'→type SB·num 1·mcu ESP, 'SSM'→type SSM.
_RE_ALIAS_UNIT = re.compile(r"^(SSM|SB|REPEAT|REP|APU_C|APUC|APU)\s*0*(\d+)?$", re.IGNORECASE)

# 자기 보고 [Tx - my INFO] 의 INFO[0](장비타입 enum). '[Tx - my INFO]' 컨텍스트를 요구해
# SSM [Proc-WiFiRx] 가 남의 INFO를 중계 인용한 줄을 오인하지 않는다(README 경고).
_RE_INFO_TYPE = re.compile(r'\[Tx - my INFO\][^\n{]*\{[^\n}]*?"INFO"\s*:\s*\[\s*"?(\d+)"?')
# STM32(SB 베이 컨트롤러) 부팅 배너 — SSM 없이 SB 단독 연결(standalone) 시 분류 근거.
_RE_STM_BANNER = re.compile(r"SmartBay\s*FW", re.IGNORECASE)
# SSM INFO 명령(simplevInfoBuffer) 전체 장비 테이블 헤더.
_RE_SSM_TABLE = re.compile(r"Information on the entire equipment")

# 분류 신뢰도(높을수록 강한 증거).
_CONF = {"manual": 1.0, "info_json": 0.95, "stm32_banner": 0.9, "ssm_table": 0.9, "signature": 0.6}


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
    raw = m.group(1).upper()
    typ = DEVICE_TYPE.get(raw, raw)                    # REP→REPEAT, APUC→APU_C 정규화
    num = int(m.group(2)) if m.group(2) else None
    return (typ, num, mcu)


def classify_lines(lines) -> tuple[Optional[str], Optional[str]]:
    """수신 로그로 (type, mcu) 추정. 신뢰 없으면 (None, None).

    여러 시그니처 점수를 합산해 최고 (type, mcu)를 고른다. 각 보드 고유 패턴만 등록해
    상대 보드명 인용(SSM 로그 속 'SB1' 등) 오인을 억제한다.
    """
    blob = _blob(lines)
    scores: dict[tuple[str, str], int] = {}
    for typ, mcu, weight, rx in _SIGNATURES:
        n = len(rx.findall(blob))
        if n:
            scores[(typ, mcu)] = scores.get((typ, mcu), 0) + weight * min(n, 5)
    if not scores:
        return (None, None)
    return max(scores.items(), key=lambda kv: kv[1])[0]


def _blob(lines) -> str:
    """줄 리스트/문자열을 단일 문자열로 합친다(분류·추출용)."""
    return "\n".join(lines) if isinstance(lines, (list, tuple)) else str(lines or "")


def _extract_info_type(blob: str) -> Optional[str]:
    """자기 보고 '[Tx - my INFO]' 줄에서 INFO[0](장비타입 토큰)을 추출. 없으면 None.

    줄의 첫 '{' 부터 JSON 으로 파싱(필드 순서·중첩 객체 무관, plan §7-2 '첫 {/[ 부터 파싱'),
    실패 시 정규식 폴백. '[Tx - my INFO]' 태그를 요구해 SSM 의 중계 인용(남의 INFO)은 잡지
    않는다(README 경고). [Tx - my INFO] 는 전 리프(SB/APU/APU_C/REPEAT) 공통이라 타입 판별은
    INFO[0] enum 으로만 한다.
    """
    for line in blob.split("\n"):
        if "[Tx - my INFO]" not in line:
            continue
        i = line.find("{")
        if i >= 0:
            try:
                obj, _ = json.JSONDecoder().raw_decode(line[i:])   # 첫 JSON 값만, 후행 무시
                info = obj.get("INFO") if isinstance(obj, dict) else None
                if isinstance(info, list) and info:
                    return str(info[0])
            except (ValueError, TypeError):
                pass
        m = _RE_INFO_TYPE.search(line)     # JSON 파싱 실패 시 정규식 폴백
        if m:
            return m.group(1)
    return None


def classify_device(lines, alias: Optional[str] = None) -> dict:
    """포트 1개의 장비 정체를 추정. 반환 {type, mcu, number, confidence, source}.

    명시 별칭(manual)이 최우선, 없으면 4단계 로그 자동발견: ②자기 보고 [Tx - my INFO] 의
    INFO[0] 장비타입 enum(info_json) ③STM32 'SmartBay FW' 배너(stm32_banner — SSM 없는 SB
    단독 분류) ④SSM INFO 테이블 헤더(ssm_table) ⑤시그니처 점수 합산(signature, 약). 못 정하면
    type=None·confidence 0.0. INFO[0] 은 자기 보고 줄에서만 읽어 SSM 의 중계 인용(남의 INFO)을
    오분류하지 않고, 자기 보고했으나 미지 enum 이면 over-broad 시그니처로 SB 단정하지 않고 미상.
    number 는 별칭 번호만(로그 기반 번호 보강은 identify_port 가 담당).
    mcu 기본값('ESP') 부여는 이 함수가 단일 소유한다(identify_port 는 재보정하지 않음).
    """
    # ① 명시 별칭 — 최우선
    typ, num, mcu = parse_alias(alias)
    if typ:
        if mcu is None and typ != "SB":
            mcu = "ESP"                    # SSM/REPEAT/APU/APU_C 는 단일 ESP
        return {"type": typ, "mcu": mcu, "number": num, "confidence": _CONF["manual"], "source": "manual"}

    blob = _blob(lines)
    # ② 자기 보고 [Tx - my INFO] 의 INFO[0] = 장비타입 enum (강한 증거)
    info0 = _extract_info_type(blob)
    if info0 is not None:                  # 자기 보고함 → 리프 장비(SSM/SB-STM 아님)
        t = DEVICE_TYPE.get(info0)
        if t:
            return {"type": t, "mcu": "ESP", "number": None, "confidence": _CONF["info_json"], "source": "info_json"}
        # 미지/미래 enum 자기 보고 → over-broad 시그니처로 SB 단정 금지, 미상 처리
        return {"type": None, "mcu": None, "number": None, "confidence": 0.0, "source": None}
    # ③ STM32 배너 → SB/STM (standalone 포함)
    if _RE_STM_BANNER.search(blob):
        return {"type": "SB", "mcu": "STM", "number": None, "confidence": _CONF["stm32_banner"], "source": "stm32_banner"}
    # ④ SSM INFO 테이블 헤더
    if _RE_SSM_TABLE.search(blob):
        return {"type": "SSM", "mcu": "ESP", "number": None, "confidence": _CONF["ssm_table"], "source": "ssm_table"}
    # ⑤ 시그니처 점수(약한 증거) — Phase A 합산 재사용
    t, mc = classify_lines(lines)
    if t:
        if mc is None and t != "SB":
            mc = "ESP"
        return {"type": t, "mcu": mc, "number": None, "confidence": _CONF["signature"], "source": "signature"}
    # ⑥ 미상
    return {"type": None, "mcu": None, "number": None, "confidence": 0.0, "source": None}


def _number_from_lines(typ: Optional[str], mcu: Optional[str], lines) -> Optional[int]:
    """SB 의 자기 번호(BayID/UnID) 추출 — ESP/STM 짝을 묶는 키. SB 외(SSM/REPEAT/APU/APU_C)는 None.

    mcu=STM→BayID, ESP→UnID. mcu 미상(예: 칩 미표기 SB 별칭)이면 둘 다 시도한다 — 한 포트
    로그는 ESP/STM 한쪽 포맷이라 충돌하지 않는다.
    """
    if typ != "SB":
        return None
    blob = _blob(lines)
    if mcu == "STM":
        rxes = (_RE_BAYID,)
    elif mcu == "ESP":
        rxes = (_RE_UNID,)
    else:
        rxes = (_RE_BAYID, _RE_UNID)
    for rx in rxes:
        m = rx.search(blob)
        if m:
            return int(m.group(1))
    return None


def identify_port(port: str, alias: Optional[str], lines, connected: bool = True) -> dict:
    """포트 1개의 정체 추정. 별칭 우선, 없으면 로그 자동발견(classify_device 4단계).

    반환: {port, type, mcu, number, connected, type_confidence, type_source}.
    type 미상이면 type=None(미분류).
    """
    info = classify_device(lines, alias)              # 별칭 우선 → 로그 자동발견(mcu 기본값 포함)
    typ, mcu = info["type"], info["mcu"]
    num = info["number"]                              # 명시 별칭 번호(있으면)
    if num is None:                                   # 없으면 로그(BayID/UnID)로 보강
        num = _number_from_lines(typ, mcu, lines)
    return {"port": port, "type": typ, "mcu": mcu, "number": num, "connected": bool(connected),
            "type_confidence": info["confidence"], "type_source": info["source"]}


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
