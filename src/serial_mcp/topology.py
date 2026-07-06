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

from .topology_routing import pick_link_metric

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
_MEMBERSHIP_FRESH_S = 30.0   # 멤버십 링크선 최신성 임계(초) — 넘으면 fresh=False(프론트 옅게). 실장비서 튜닝.

# 로그 내용 식별 시그니처(약한 폴백 증거, conf 0.6): (type, mcu, weight, regex). 점수 합산.
# 고유 수동(passive) 패턴이 있는 보드만 등록한다: SSM(Proc-*/Route)·SB-STM(BayID 등).
# 리프 ESP(SB/APU/APU_C/REPEAT)는 [Tx - my INFO]/[WiFi_Rx]/Save 등 태그를 전부 공유해(펌웨어
# 검증: Repeat/APU/APU_C 도 활성 출력) 수동 시그니처로 서로 못 가르므로, 타입 판별은
# classify_device ②단계 INFO[0] enum 이 전담한다(SB-ESP 도 INFO[0]=4 로만 식별). over-broad
# 패턴을 SB 로 등록하면 INFO 없는 윈도의 APU/REPEAT 를 SB 로 오분류하므로 두지 않는다(§7-1 미확정=UNKNOWN).
_SIGNATURES = [
    # SSM-ESP(게이트웨이): Proc-* 는 WiFi 활성 시만 나오므로 Route Link/From SB 도 함께.
    # REPRSSI 는 금지 — SSM 전용이 아니다(SB 자기 응답 [Tx_RSSI]·REP 중계 [BypassJson]에 상시,
    # 2026-07-06 실장비 재현: REP/SB 포트가 SSM(signature) 오분류 → 그룹 분열·타입 플래핑).
    ("SSM", "ESP", 3, re.compile(r"\[Proc-WiFiRx\]|\[Proc-Raw Packet\]|\[Proc_WiFiTx\]|\[Proc-WebRTx\]")),
    ("SSM", "ESP", 3, re.compile(r"\[Route\] Link|<<<\s*From\s+SB")),
    # SB-STM: 베이 컨트롤러(카드·가격·베이설정) — SB 고유. 부팅/설정 시점에 나옴.
    ("SB", "STM", 3, re.compile(r"\bBayID\s*:|<\s*MasterCard\s*>|BayConfig Info|minCoinSensingTime|Price1st")),
    # SB-STM 런타임(카드/상태 동작) — 부팅·설정 시그니처가 없는 카드처리 윈도를 보강한다(실장비서
    # COM13 SB-STM 이 카드만 처리해 unplaced 였던 결함). SB/STM **전용** 토큰만 등록한다(cbm 검증
    # 2026-06-30: 'Send state of STM32'=SB-SmartBay 전용, 'Released to touch Card'=STM main.c 전용).
    # 'Check the Card'/'Lower Disp. Step' 등은 APU/SSM 펌웨어에도 있어 over-broad 라 제외.
    ("SB", "STM", 3, re.compile(r"Send state of STM32|Released to touch Card")),
]

# ESP 자기 번호(UnID=자기 BayID) 추출 — ESP↔STM 병합 번호원. STM 번호는 카드상관(CardPairing) 전담.
# 자기 보고 [Tx - my INFO] 줄에서만 찾는다 — SB 포트 로그엔 [WiFi_Rx](수신 요청)·[Data_Pass](중계)
# 등 **남의 UnID** 가 섞이므로(펌웨어 확인 2026-07-02), 블롭 첫 매칭은 중계 구성에서 오귀속한다.
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

# 분류 신뢰도(높을수록 강한 증거). route_name = SSM 이 해소한 [Passed Device] 토큰→이름(원격 mesh 노드).
_CONF = {"manual": 1.0, "info_json": 0.95, "stm32_banner": 0.9, "ssm_table": 0.9,
         "route_name": 0.9, "signature": 0.6}


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
    """SB 의 자기 번호 추출 — ESP 는 자기 UnID(=BayID). SB 외(SSM/REPEAT/APU/APU_C)는 None.

    STM 은 정상 운영 중 BayID 를 로그에 안 흘리므로(펌웨어 검증 2026-07-01) 여기선 번호를 안 뽑는다 —
    STM 번호는 카드상관 페어링(엔진 CardPairing → build_roster pairing)이 전담한다. mcu 미상 SB 도 UnID 만 시도.
    """
    if typ != "SB" or mcu == "STM":
        return None
    for line in _blob(lines).split("\n"):             # ESP·미상 SB: 자기 보고 줄의 UnID 만
        if "[Tx - my INFO]" not in line:              # [WiFi_Rx]/[Data_Pass] 등 남의 UnID 오귀속 방지
            continue
        m = _RE_UNID.search(line)
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


def _local_port_to_ssm(membership: Optional[dict]) -> dict:
    """멤버십 {ssm_port:{unid:{local_port,last_ts,..}}} → {local_port: ssm_port} 역인덱스.

    한 leaf 로컬포트가 두 SSM 에 들렸으면(시간차) last_ts 가 더 최근인 SSM 을 택한다(직접 USB
    leaf 는 보통 한 SSM 귀속이나 메시 오버히어 대비). local_port 없는 엔트리(원격·미상관)는 건너뜀.
    """
    best: dict = {}                     # local_port → (ssm_port, last_ts)
    for ssm_port, members in (membership or {}).items():
        for ent in members.values():
            lp = ent.get("local_port")
            if not lp:
                continue
            ts = ent.get("last_ts")
            cur = best.get(lp)
            if cur is None or (ts is not None and (cur[1] is None or ts >= cur[1])):
                best[lp] = (ssm_port, ts)
    return {lp: v[0] for lp, v in best.items()}


def _pair_group(groups: list[dict], d: dict) -> Optional[dict]:
    """번호 있는 SB 포트의 짝(같은 번호 SB)이 이미 배치된 그룹 — 없으면 None.

    STM 콘솔은 무선 상관이 불가능해 membership 에 절대 안 잡힌다 — 카드페어링/자체 번호로
    ESP 짝을 찾아 그 그룹을 따라간다(같은 그룹이어야 _merge_sb 가 한 베이 노드로 병합).
    """
    num = d.get("number")
    if d.get("type") != "SB" or num is None:
        return None
    for g in groups:
        if any(m.get("type") == "SB" and m.get("number") == num for m in g["_members"]):
            return g
    return None


def _apply_type_cache(ids: list, type_cache: Optional[dict]) -> list:
    """포트 타입 이력 고정 — 하드웨어는 안 바뀐다(사용자 원칙 2026-07-06).

    분류는 '최근 창'만 보므로 자기 보고([Tx - my INFO])가 창에서 밀리면 미상/약증거로
    강등돼 타입이 왔다갔다 한다(실장비 재현: 그룹 분열). 규칙: 새 판정의 신뢰도가 캐시
    이상일 때만 캐시를 교체하고, 약하거나 무증거면 캐시 판정을 재사용한다(connected 만
    현재값). 캐시 해제는 포트 disconnect(엔진 forget_port) 몫. type_cache=None 이면 무동작
    (하위호환 — 엔진 없는 호출부).
    """
    if type_cache is None:
        return ids
    out = []
    for d in ids:
        cached = type_cache.get(d["port"])
        conf = d.get("type_confidence") or 0.0
        if cached is not None and (d.get("type") is None or conf < cached["type_confidence"]):
            out.append({**cached, "port": d["port"], "connected": d.get("connected", True)})
            continue
        if d.get("type") is not None:
            keep = dict(d)
            if keep.get("number") is None and cached and cached.get("type") == keep.get("type"):
                keep["number"] = cached.get("number")     # 번호는 이전 창의 자기 보고에서 승계
            type_cache[d["port"]] = {k: keep.get(k) for k in
                                     ("type", "mcu", "number", "type_confidence", "type_source")}
            out.append(keep)
        else:
            out.append(d)
    return out


def build_roster(entries, routing=None, membership=None, pairing=None, now=None, peer_links=None,
                 type_cache=None) -> dict:
    """포트 목록(+선택 라우팅 상태·멤버십·카드페어링) → 토폴로지 로스터.

    entries: [{port, alias, lines, connected}] (lines 는 최근 수신 줄 list).
    routing: 선택 RoutingTable(모듈4) — 주면 링크그래프 edges·원격 mesh 노드·mac/토큰 enrich 를
      얹는다. 없으면(Phase A 호출부) edges=[]·원격노드 없음으로 하위호환 유지. now=fresh 판정 클럭.
    membership: 선택 {ssm_port:{unid:{device_type,local_port,last_ts}}}(엔진 모듈6) — 주면 각 leaf 를
      그 leaf 를 수신한 SSM 의 그룹에 배치한다(멀티-SSM 정확). None(Phase A)이면 첫 그룹 폴백,
      dict(엔진 가동)인데 매칭 안 되는 RF 콘솔은 미귀속 standalone 그룹으로 분리(STM 은 pair-follow).
    pairing: 선택 {port: bay}(엔진 CardPairing 스냅샷) — STM 은 정상 로그에 번호가 없으므로, 카드상관으로
      해소된 베이번호를 번호 미상 SB 포트에 채워 ESP 짝과 병합(_merge_sb)하게 한다. 없으면 번호 폴백 없음.
    peer_links: 선택 [{from,to,via,fresh}] — PeerLinks 가 관측한 범용 H.W↔H.W 포트쌍. 양끝이
      같은 그룹에 배치된 링크만 병합한다(프론트 canvas 가 그룹 단위라 cross-group v1 드랍).
    반환: {"groups": [{id, label, ssm_port, kind, nodes:[node...], edges:[...]}], "unplaced":[port...]}.
      kind = "ssm"(SSM 보유) | "standalone"(SSM 부재). edges = [{from,to,rssi,fresh,via}].
      node = {id, type, type_confidence, type_source, label, mac, unit_id, route_token,
              row, col, status, ports:[{mcu, port, connected}]}.
      - SB 의 ESP/STM(같은 번호)은 한 노드로 병합(ports 2개, 프론트가 [ESP|STM] 분할).
      - 원격 mesh 노드([Passed Device] 로만 등장, 직접 포트 없음)는 ports=[]·status="unknown".
      - row=타입 계층, col=같은 타입 내 번호/발견순.
    불변식: 그룹↔SSM 1:1. SSM 0개면 단일 standalone 그룹, 1개면 그 SSM 그룹, N개면 N그룹.
    예외: membership 가동 중 무관측 RF 콘솔이 있으면 끝에 미귀속 standalone 그룹("(SSM 미관측)")
    이 하나 더 붙을 수 있다(SSM 그룹 아님 — 그룹↔SSM 1:1 불변식은 kind="ssm" 그룹에만 적용).
    """
    ids = [identify_port(e["port"], e.get("alias"), e.get("lines"), e.get("connected", True))
           for e in entries]
    ids = _apply_type_cache(ids, type_cache)   # 타입 이력 고정(약증거 강등 금지) — 엔진이 캐시 소유
    # 카드페어링 번호 폴백: 번호 미상 SB 포트(주로 STM)를 카드상관 베이번호로 채워 ESP 짝과 병합.
    for d in ids:
        if d["type"] == "SB" and d.get("number") is None and pairing:
            bay = pairing.get(d["port"])
            if bay is not None:
                d["number"] = bay
    placed = [d for d in ids if d["type"]]
    unplaced = [d["port"] for d in ids if not d["type"]]

    ssms = [d for d in placed if d["type"] == "SSM"]
    others = [d for d in placed if d["type"] != "SSM"]

    # 그룹 골격(그룹↔SSM 1:1). SSM 없으면 단일 standalone 그룹(SSM group 아님, 실패 아님).
    if ssms:
        groups = [{"id": f"g{i+1}", "label": _label(s), "ssm_port": s["port"],
                   "kind": "ssm", "_members": [s]} for i, s in enumerate(ssms)]
    else:
        groups = [{"id": "g1", "label": "(SSM 미식별)", "ssm_port": None,
                   "kind": "standalone", "_members": []}]

    # 비-SSM 귀속: membership(SSM포트→leaf 로컬포트)이 있으면 각 leaf 를 그 leaf 를 수신한 SSM
    # 그룹에 배치한다(멀티-SSM 정확). membership=None(Phase A)이면 첫 그룹 폴백(단일 SSM 가정).
    # membership 가동 중(dict — 빈 dict 포함)이며 SSM 그룹이 있으면, 어느 SSM 에도 관측 귀속이
    # 없는 RF 콘솔은 첫 그룹에 붙이지 않고 미귀속 standalone 그룹으로 분리한다 — '단일 SSM 가정'
    # 폴백이 이웃 mesh 장비(예: 남의 SSM 의 REP)를 오배치해 체인로그 group 판정(같은 membership
    # 원천)과 그래프가 갈라졌던 2026-07-06 실장비 회귀. 단 STM 콘솔은 무선 상관이 원천 불가라
    # membership 부재가 증거가 아니다 — 같은 번호 SB 짝(ESP)이 배치된 그룹을 따라가고(pair-follow,
    # 번호는 자체/카드페어링), 짝이 없으면 기존 첫 그룹 폴백을 유지한다(베이 STM 찢김 방지).
    group_by_ssm = {g["ssm_port"]: g for g in groups if g.get("ssm_port")}
    local_to_ssm = _local_port_to_ssm(membership)
    strict = membership is not None and bool(ssms)
    deferred_stm: list = []
    orphans: list = []
    for d in others:
        target = group_by_ssm.get(local_to_ssm.get(d["port"]))
        if target is None:
            if d.get("mcu") == "STM":
                deferred_stm.append(d)      # ESP 짝 배치가 끝난 뒤 pair-follow
                continue
            if strict:
                orphans.append(d)
                continue
            target = groups[0]
        target["_members"].append(d)
    for d in deferred_stm:
        target = _pair_group(groups, d) or groups[0]
        target["_members"].append(d)
    if orphans:
        groups.append({"id": f"g{len(groups) + 1}", "label": "(SSM 미관측)",
                       "ssm_port": None, "kind": "standalone", "_members": orphans})

    token_map = routing.tokens() if routing is not None else {}
    unid_idx = _unid_index(token_map)                       # unid → (token, entry): mac/토큰 enrich
    # 링크 그래프(mac쌍)·INFO 테이블(mac 다리) — 멤버십 링크의 RSSI ladder 후보원(없으면 폴백 경로만).
    routing_edges = routing.edges(now) if routing is not None else []
    info = routing.info_table() if routing is not None and hasattr(routing, "info_table") else {}
    ssm_macs = info.get("ssm_mac") or {}

    for i, g in enumerate(groups):
        descriptors = _merge_sb(g["_members"])              # 직접연결(SB ESP/STM 병합)
        if i == 0 and token_map:                            # 원격 mesh 노드는 1차(주) 그룹에 귀속
            descriptors += _remote_descriptors(descriptors, token_map)
        g["nodes"] = _layout_group(descriptors, unid_idx)
        for n in g["nodes"]:                                # SSM 자신의 mac 은 INFO 테이블 자기 행에서
            if n["type"] == "SSM" and not n.get("mac"):
                n["mac"] = ssm_macs.get(g["ssm_port"])
        # 정적 링크선 = 멤버십 leaf↔SSM 포트쌍 + PeerLinks 범용 포트쌍. peer edge 는 양끝이
        # 이 그룹에 배치된 경우만 병합한다(cross-group 링크는 v1 프론트 canvas 에 그릴 곳이 없어 드랍).
        membership_edges = (_membership_edges(membership, g["ssm_port"], now,
                                              routing_edges=routing_edges, info=info)
                            if g["kind"] == "ssm" else [])
        g["edges"] = _merge_group_edges(membership_edges, peer_links, _ports_in_nodes(g["nodes"]))
        del g["_members"]
    return {"groups": groups, "unplaced": unplaced}


def port_labels(roster: dict) -> dict:
    """roster.groups[].nodes[].ports[].port → 그 노드 label 매핑."""
    out = {}
    for group in (roster or {}).get("groups") or []:
        for node in group.get("nodes") or []:
            label = node.get("label")
            if not label:
                continue
            for port in node.get("ports") or []:
                p = port.get("port")
                if p:
                    out[p] = label
    return out


def _unid_index(token_map: dict) -> dict:
    """토큰맵 {token:{name,mac,unid}} → unid 역인덱스 {unid:(token, entry)}. 직접노드 enrich 용."""
    idx = {}
    for token, ent in token_map.items():
        if ent.get("unid") is not None:
            idx[ent["unid"]] = (token, ent)
    return idx


def _membership_edges(membership, ssm_port, now, fresh_s=_MEMBERSHIP_FRESH_S,
                      routing_edges=None, info=None) -> list:
    """멤버십(ssm_port→leaf) → 정적 링크선 edges [{from:local_port, to:ssm_port, fresh, rssi, rssi_source}].

    correlator 가 (식별자,Unique) TX↔RX 로 관측한 leaf↔SSM 포트쌍만 그린다 — REPRSSI 같은 무선
    이웃 전부를 강제 링크로 긋지 않는다(plan §3, 사용자 강조: 링크 고정 강제 금지). last_ts 가
    fresh_s 넘게 오래되면 fresh=False(프론트가 옅게) — 관측 이력은 유지하되 최신성만 감쇠시켜
    '고정'이 아닌 '동적 관측'을 표현한다(관측 바뀌면 멤버십도 갱신).

    RSSI 품질은 ladder(pick_link_metric)로 고른다: **링크별**(route_link/reprssi — mac쌍, 링크
    그래프에서 leaf↔SSM 양방향 조회) 우선, 없으면 **장비 단위**(info_rssi=INFO[2], info_table_rf=
    INFO 테이블 RF열 — 둘 다 장비가 보고한 이웃 평균 avrRssi) 폴백. rssi_source 로 출처를 노출해
    "링크 품질"과 "장비 RF 건강도"의 혼동을 막는다(2026-07-02 재검토 — INFO[2] 직결 축 오류 수정).
    mac 끝점 해소: leaf = 멤버십 키(Mac 폴백 키면 그대로, UnID 키면 INFO 테이블 unid→mac 다리),
    SSM = INFO 테이블 자기 행. 다리가 없으면 장비 단위 폴백으로 자연 강등(우아한 열화).
    """
    if not membership or ssm_port is None:
        return []
    info = info or {}
    by_mac = info.get("by_mac") or {}
    ssm_mac = (info.get("ssm_mac") or {}).get(ssm_port)
    unid_to_mac = {e["unid"]: mac for mac, e in by_mac.items() if e.get("unid") is not None}
    links = {}
    for e in routing_edges or []:                     # 링크 그래프(mac쌍) — fresh 만(스테일 품질 방지)
        if e.get("fresh") is not False and e.get("rssi") is not None:
            links[(e.get("from"), e.get("to"))] = e
    out = []
    for ident, ent in membership.get(ssm_port, {}).items():
        lp = ent.get("local_port")
        if not lp:
            continue
        last_ts = ent.get("last_ts")
        fresh = now is None or last_ts is None or (now - last_ts) < fresh_s
        leaf_mac = ident if isinstance(ident, str) else unid_to_mac.get(ident)
        cand = {"info_rssi": ent.get("rssi")}
        if leaf_mac:
            tbl = by_mac.get(leaf_mac)
            if tbl and tbl.get("rf") is not None:
                cand["info_table_rf"] = tbl["rf"]
            if ssm_mac:
                link = links.get((leaf_mac, ssm_mac)) or links.get((ssm_mac, leaf_mac))
                if link:
                    cand[link.get("source") or "route_link"] = link["rssi"]
        picked = pick_link_metric(cand)
        out.append({"from": lp, "to": ssm_port, "fresh": bool(fresh),
                    "rssi": picked["value"], "rssi_source": picked["source"],
                    "via": "handled"})
    return out


def _ports_in_nodes(nodes: list[dict]) -> set:
    """배치된 노드 목록에서 이 그룹에 속한 실제 시리얼 포트 집합을 뽑는다."""
    ports = set()
    for n in nodes:
        for p in n.get("ports") or []:
            port = p.get("port")
            if port:
                ports.add(port)
    return ports


def _merge_group_edges(membership_edges: list, peer_links: Optional[list], group_ports: set) -> list:
    """멤버십 edge 와 peer edge 를 무방향 dedup 병합한다.

    멤버십 edge 는 RSSI ladder 를 보유하므로 우선한다. peer edge 는 양끝 포트가 같은 그룹에
    배치된 경우만 넣고, 순수 peer edge 는 RSSI 없이 via/fresh 만 싣는다.
    """
    merged: dict = {}
    order: list = []

    def add(edge: dict, prefer_existing: bool) -> None:
        src, dst = edge.get("from"), edge.get("to")
        if not src or not dst or src == dst:
            return
        key = frozenset((src, dst))
        cur = merged.get(key)
        if cur is None:
            merged[key] = dict(edge)
            order.append(key)
            return
        if prefer_existing:
            # peer 링크는 무방향 한 쌍이 양방향으로 온다(REP [Data_Pass]=handled,
            # SB [WiFi_Rx]=heard). 삽입 순서와 무관하게 handled 로 승격하고 fresh 를 OR 한다
            # — 안 그러면 heard·stale 가 먼저 들어와 실제 중계 링크가 점선으로 남는다
            # (2026-07-06 SB↔REP). 단 RSSI 보유 멤버십 edge 의 handled 는 절대 강등 안 함.
            if edge.get("via") == "handled" or (cur.get("via") is None and edge.get("via") is not None):
                cur["via"] = edge.get("via")
            if edge.get("fresh"):
                cur["fresh"] = True
            return
        if cur.get("rssi") is None and edge.get("rssi") is not None:
            cur["rssi"] = edge.get("rssi")
            cur["rssi_source"] = edge.get("rssi_source")
        if edge.get("via") == "handled" or cur.get("via") is None:
            cur["via"] = edge.get("via")
        cur["fresh"] = bool(cur.get("fresh")) or bool(edge.get("fresh"))

    for edge in membership_edges or []:
        add(edge, prefer_existing=False)
    for link in peer_links or []:
        src, dst = link.get("from"), link.get("to")
        if src not in group_ports or dst not in group_ports:
            continue
        add({"from": src, "to": dst, "fresh": bool(link.get("fresh")),
             "via": link.get("via"), "rssi": None, "rssi_source": None},
            prefer_existing=True)
    return [merged[key] for key in order]


def _remote_descriptors(direct: list, token_map: dict) -> list:
    """토큰맵에서 직접연결 노드에 없는 원격 mesh 노드를 디스크립터로 만든다(ports:[]).

    [Passed Device] 가 해소한 토큰→이름(예 '(01-REP1)')만 노드로 쓴다. 토큰은 노드 1:1 식별자
    (RouteTokenForInfoPos '%02X'(UnitID))이므로 각 토큰=별개 노드다 — 토큰 단위로 순회만 해도
    원격 노드끼리는 중복되지 않는다(번호 없는 동일타입 이름 둘을 (type,None)로 잘못 합치지 않음).

    name 미해소(None) 엔트리는 parse_alias 로 type 을 도출할 수 없어 배치 불가라 건너뛴다
    (직접노드/원격 여부와 무관 — name=None 은 'UnID 는 봤으나 [Passed Device] 이름 미해소'다).
    type 은 [Passed Device] 경로 이름 해소분이므로 type_source="route_name"(SSM INFO 테이블 파싱과 구분).

    직접노드 dedup 은 **라우팅 토큰(UnitID 파생)** 기준이다 — 메시 이름의 번호(예 'SB1')가 직접
    노드의 UnID(예 5)와 달라도 같은 토큰('05')이면 같은 장비로 보고 원격 중복을 막는다((type,번호)
    기준이면 UnID≠이름번호 일 때 같은 장비가 SB5(직접)·SB1(원격)으로 둘이 된다). 번호 없는 직접
    비-SB 리프(REPEAT/APU: _number_from_lines 가 SB 한정이라 number=None)는 토큰을 못 구해 dedup
    에서 빠질 수 있다 — 직접 비-SB 리프의 UnID 추출은 모듈6(engine) 배선과 함께 보강(현재 잠복).
    """
    direct_tokens = {t for t in (_unid_token(n["number"]) for n in direct
                                 if n.get("number") is not None) if t}
    out = []
    for token, ent in token_map.items():
        name = ent.get("name")
        if not name:
            continue
        typ, num, _ = parse_alias(name)
        if not typ:
            continue
        if token in direct_tokens:
            continue                       # 같은 토큰(=같은 UnitID)의 직접노드 → 원격 중복 생성 금지
        out.append({"type": typ, "number": num, "ports": [], "mac": ent.get("mac"),
                    "unid": ent.get("unid"), "route_token": token, "name": name,
                    "type_confidence": _CONF["route_name"], "type_source": "route_name",
                    "remote": True})
    return out


def _label(d: dict) -> str:
    """그룹 표시 라벨: 'SSM', 'SB5', 'APU3' (번호 있으면 붙임). 그룹(SSM)용 — 노드는 _node_label."""
    return d["type"] + (str(d["number"]) if d.get("number") is not None else "")


def _unid_token(num) -> Optional[str]:
    """UnitID(10진) → 라우팅 토큰(2-hex). 예약 토큰('00'/'FF')은 노드 토큰 아님 → None.

    routing._token_of_unid 과 같은 규약. 직접노드↔원격노드를 '같은 장비'로 묶는 dedup 키다 —
    토큰은 UnitID 파생이라, 메시 이름의 번호(예 SB1)가 UnID(예 5)와 달라도 같은 토큰('05')으로
    중복을 제거한다((type,번호) 기준 dedup 은 UnID≠이름번호 일 때 같은 장비를 둘로 본다).
    """
    try:
        tok = f"{int(num) & 0xFF:02X}"
    except (TypeError, ValueError):
        return None
    return None if tok in ("00", "FF") else tok


def _node_label(typ: str, number, resolved_name: Optional[str] = None,
                collision: bool = False, port: Optional[str] = None) -> str:
    """노드 표시 라벨. 우선순위: 라우팅 해소 이름(메시 네이밍) > type+번호 > type.

    UnID(번호)는 사용자설정 BayID 라 식별 권위가 아니다(표시 메타) — 표시는 메시가 해소한 이름을
    우선한다(roster↔hop 네이밍 단일 소스). BayID 충돌(number_collision) 노드는 포트로 라벨을
    구분한다(똑같은 'SB5' 둘 방지).
    """
    base = resolved_name or (f"{typ}{number}" if number is not None else typ)
    if collision and port:
        return f"{base} ({port})"
    return base


def _merge_sb(members: list[dict]) -> list[dict]:
    """SB 의 ESP/STM(같은 번호·다른 MCU)을 한 논리 노드로 병합. 번호 없으면 포트별 개별 노드.

    병합은 **번호가 같고 MCU 가 서로 다를 때만** 한다(한 베이 = ESP 1 + STM 1). 같은 번호라도
    같은 MCU 가 둘 이상이면(예: SB-ESP 둘이 BayID 5 공유 — UnID=사용자설정 BayID 충돌이라
    서로 다른 베이) 한 노드로 묶지 않고 별개 노드로 남긴다. 포트가 타이브레이커다. 이렇게 충돌로
    갈라진 노드들은 number_collision=True 로 표시해 식별 모호를 신호한다(_layout_group 이 id 도 포트로 유일화).
    각 디스크립터의 type_confidence/type_source 는 보존하며, 병합 노드는 더 강한 증거(confidence 최대)를 채택한다.
    """
    by_number: dict = {}        # number -> [bay 노드, ...] (각 bay 는 MCU 당 포트 1개)
    singles: list[dict] = []
    for d in members:
        port_entry = {"mcu": d.get("mcu"), "port": d["port"], "connected": d["connected"]}
        if d["type"] == "SB" and d.get("number") is not None:
            bays = by_number.setdefault(d["number"], [])
            mcu = d.get("mcu")
            # 이 MCU 슬롯이 빈 기존 bay 에 합류, 없으면(같은 MCU 충돌) 새 bay 생성.
            node = next((b for b in bays if all(p.get("mcu") != mcu for p in b["ports"])), None)
            if node is None:
                node = {"type": "SB", "number": d["number"], "ports": [],
                        "type_confidence": 0.0, "type_source": None, "remote": False}
                bays.append(node)
            node["ports"].append(port_entry)
            if d["type_confidence"] >= node["type_confidence"]:   # 더 강한 증거 채택
                node["type_confidence"] = d["type_confidence"]
                node["type_source"] = d["type_source"]
        else:
            singles.append({"type": d["type"], "number": d.get("number"), "ports": [port_entry],
                            "type_confidence": d["type_confidence"], "type_source": d["type_source"],
                            "remote": False, "number_collision": False})
    merged_nodes: list[dict] = []
    for bays in by_number.values():
        collision = len(bays) > 1                  # 같은 번호가 여러 bay → BayID 충돌
        for node in bays:
            node["number_collision"] = collision
            # SB 칩 순서를 발견순이 아니라 ESP→STM 으로 일관 고정(디자인 [ESP|STM]).
            node["ports"].sort(key=lambda p: 0 if p.get("mcu") == "ESP" else 1 if p.get("mcu") == "STM" else 2)
            merged_nodes.append(node)
    return merged_nodes + singles


def _layout_group(nodes: list[dict], unid_idx: dict) -> list[dict]:
    """디스크립터 목록 → 배치된 노드(row=타입, col=같은 타입 내 번호/순서). 직접·원격 공통.

    직접노드는 unid 역인덱스(unid_idx)로 mac/route_token 을 enrich 한다. 원격노드는 디스크립터에
    이미 mac/unid/route_token 이 실려 있고 status="unknown"(직접 관측 연결 없음).
    """
    nodes.sort(key=lambda n: (TYPE_RANK.get(n["type"], 9),
                              n["number"] if n.get("number") is not None else 1 << 30))
    col_of: dict[str, int] = {}
    out = []
    for n in nodes:
        typ = n["type"]
        col = col_of.get(typ, 0)
        col_of[typ] = col + 1
        remote = n.get("remote", False)
        if remote:
            token, mac, unit_id = n.get("route_token"), n.get("mac"), n.get("unid")
            resolved_name = n.get("name")              # 원격: [Passed Device] 메시 이름
        else:                                          # 직접노드: 번호로 라우팅 관측치 enrich
            unit_id, token, mac, resolved_name = n.get("number"), None, None, None
            hit = unid_idx.get(unit_id) if unit_id is not None else None
            if hit:
                token, ent = hit
                mac = ent.get("mac")
                resolved_name = ent.get("name")        # 메시가 이 UnID 를 부르는 이름(있으면 라벨 우선)
        if unit_id is None:
            unit_id = n.get("number")                  # 원격 토큰 entry 에 unid 없으면 이름 번호로
        connected = (not remote) and any(p["connected"] for p in n["ports"])
        num = n.get("number")
        fallback = (n["ports"][0]["port"] if n["ports"] else token or typ)
        node_id = f"{typ}-{num if num is not None else fallback}"
        # BayID 충돌로 같은 (type,number) 노드가 둘 이상이면 id 가 겹치므로 포트로 유일화.
        collision = bool(n.get("number_collision"))
        first_port = n["ports"][0]["port"] if n.get("ports") else None
        if collision and first_port:
            node_id = f"{node_id}@{first_port}"
        out.append({
            "id": node_id,
            "type": typ,
            "type_confidence": n.get("type_confidence"),
            "type_source": n.get("type_source"),
            "label": _node_label(typ, num, resolved_name, collision, first_port),
            "mac": mac,
            "unit_id": unit_id,
            "route_token": token,
            "row": ROW_BY_TYPE.get(typ, 5),
            "col": col,
            "status": "unknown" if remote else _status_of(connected),
            "number_collision": bool(n.get("number_collision")),
            "ports": n["ports"],
        })
    return out
