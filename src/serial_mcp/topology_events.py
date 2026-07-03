"""topology_events.py — 포트별 수신 줄을 논리 Event 로 조립(순수 로직, Phase B 모듈2).

펌웨어 RX 흐름은 한 논리 이벤트가 여러 줄에 흩어진다(SSM_esp32.ino WiFi_rev_proc):
  [Proc-WiFiRx] {json}            ← 헤더(UnID/Unique/INFO/Rt)
  [Passed Device] (05-SB5)->...   ← 실제 통과 경로(Rt 토큰→이름, 펌웨어가 해소)
  <<< From SBn.                   ← 소스명
   -- takentime : 61              ← RTT
  [Proc-WebRTx] [...REPRSSI...]   ← 웹 전송(인접 링크 RSSI)
따라서 줄 단위 무상태 처리가 아니라 포트별 누산기(EventAssembler)로 헤더부터 블록을 조립한다.
JSON 은 줄 앞 태그를 건너뛰고 '첫 {/[ 부터' 파싱한다(plan §7-2). [Proc-WebRTx] 는 별도
이벤트(REPRSSI=링크품질)이고, 실제 경로는 RX 의 Rt/[Passed Device] 다(REPRSSI 아님).
[BypassJson] 은 relay 가 Rt 를 스탬프한 뒤 남기는 실행 증거라 pass 이벤트로 본다.
[Data_Pass] 중 같은 줄 JSON 이 없으면 송신 거부(pass_refused)로 누산해 다음 JSON 만 부착한다.

I/O·시리얼 비의존 — 단위 테스트가 쉽다(test_topology_events.py).
"""

from __future__ import annotations

import json
import re
from typing import Optional

# 헤더 태그(새 블록 시작): (kind, regex). 한 줄에 하나 — 위에서부터 첫 매칭.
_HEADERS = [
    ("rx",     re.compile(r"\[Proc-WiFiRx\]")),
    ("webtx",  re.compile(r"\[Proc-WebRTx\]")),
    ("wifitx", re.compile(r"\[Proc_WiFiTx\]|\[Proc_Alarm\]")),
    # Tx 뒤가 소문자만 아니면 태그로 본다 — 펌웨어에 [WiFi_Tx8]·[WiFi_Tx_CODE] 처럼
    # 숫자·_ 가 붙는 변형이 다수라 \b 로는 놓친다(2026-07-02 SB_ESP32.ino 전수 대조).
    ("tx",     re.compile(r"\[(?:WiFi_)?Tx(?![a-z])[^\]]*\]|ForceQuit_Tx")),
    ("wifirx", re.compile(r"\[WiFi_Rx\]")),
    ("pass",   re.compile(r"\[Data_Pass\]|\[BypassJson\]")),
    ("route",  re.compile(r"\[Route\]\s*Link\b")),
]

# 연속 줄(현재 블록에 부착) — 헤더가 아닌 보조 줄.
_RE_TAKEN = re.compile(r"\btakentime\s*:\s*(\d+)")          # ' -- takentime : 61' (소문자)
_RE_AVRTAKEN = re.compile(r"avrTakenTime\s*:\s*([\d.]+)")    # 'avrTakenTime : 121[ms]'
_RE_FROM = re.compile(r"<<<\s*From\s+([A-Za-z0-9_]+)")       # '<<< From SB1.'
_RE_PASSED = re.compile(r"\[Passed Device\]\s*(.+)")         # 실제 통과 경로 문자열
# [Route] Link <mac> -> <mac> rssi=<n>  (mac 은 콤마/콜론 16진)
_RE_ROUTE_LINK = re.compile(
    r"\[Route\]\s*Link\s+([0-9A-Fa-f,:]+)\s*->\s*([0-9A-Fa-f,:]+)\s*rssi\s*=\s*(-?\d+)")

# JSON 키 → Event ids 필드.
_ID_KEYS = (("UnID", "unid"), ("Unique", "unique"), ("Asn", "asn"), ("Cidx", "cidx"), ("Mac", "mac"))


def _norm_mac(s):
    """펌웨어 MAC('A0,85,E3,EA,5C,C4' 또는 'AA:BB:..')을 대문자 콜론 표기로 정규화."""
    if not isinstance(s, str):
        return s
    return s.strip().replace(",", ":").upper()


def _norm_reprssi_row(row: list) -> dict:
    """REPRSSI 한 행 [mac,rssi] 또는 [mac,rssi,snr] → {mac,rssi,snr}. 열 개수 가변."""
    return {
        "mac": _norm_mac(row[0]),
        "rssi": row[1] if len(row) > 1 else None,
        "snr": row[2] if len(row) > 2 else None,
    }


def extract_json(text: str):
    """줄에서 첫 '{' 또는 '[' 부터 JSON 값 하나를 파싱(태그 접두사·후행 무시). 실패 None.

    [Proc-WebRTx] 의 Socket.IO 배열에선 '{' 가 먼저 payload 객체(.data.REPRSSI 보유)를 주므로
    '{' 를 우선 시도한다. raw_decode 라 첫 JSON 값만 읽고 뒤 텍스트는 무시한다.
    """
    for ch in ("{", "["):
        i = text.find(ch)
        if i < 0:
            continue
        try:
            obj, _ = json.JSONDecoder().raw_decode(text[i:])
            return obj
        except ValueError:
            continue
    return None


def _header_kind(text: str) -> Optional[str]:
    for kind, rx in _HEADERS:
        if rx.search(text):
            return kind
    return None


def _new_event(port: str, ts: float, kind: str, text: str) -> dict:
    ev = {
        "port": port, "ts": ts, "kind": kind, "raw_lines": [text], "json": None,
        "route": None,
        "ids": {"mac": None, "unid": None, "unique": None, "asn": None, "cidx": None,
                "rt_tokens": []},
        "hints": {"src_name": None, "dst_name": None, "device_type": None, "passed": None},
        "metrics": {"rssi": None, "takentime_ms": None, "avr_takentime_ms": None,
                    "reprssi": [], "rs": []},
    }
    if kind == "route":
        m = _RE_ROUTE_LINK.search(text)
        if m:
            ev["route"] = {"from_mac": _norm_mac(m.group(1)), "to_mac": _norm_mac(m.group(2)),
                           "rssi": int(m.group(3))}
        return ev
    obj = extract_json(text)
    if obj is not None:
        ev["json"] = obj
        _fill_from_json(ev, obj)
    return ev


def _fill_from_json(ev: dict, obj) -> None:
    if not isinstance(obj, dict):
        return
    ids, hints, met = ev["ids"], ev["hints"], ev["metrics"]
    for k_src, k_dst in _ID_KEYS:
        if obj.get(k_src) is not None:
            ids[k_dst] = _norm_mac(obj[k_src]) if k_dst == "mac" else obj[k_src]
    info = obj.get("INFO")
    if isinstance(info, list) and info:
        hints["device_type"] = str(info[0])            # INFO[0] = 장비타입 enum
        if len(info) > 2 and isinstance(info[2], (int, float)) and not isinstance(info[2], bool):
            # INFO[2] = 장비가 보고한 **주변 이웃 평균** RSSI(dBm, 펌웨어 avrRssi — 링크별 아님).
            # 링크별 품질이 아니라 장비 RF 건강도다 — ladder 'info_rssi' 폴백 후보(2026-07-02 확인).
            met["rssi"] = int(info[2])
    rt = obj.get("Rt")
    if isinstance(rt, list):
        ids["rt_tokens"] = [str(t) for t in rt if t is not None]
    # [Proc-WebRTx] payload: data.macAddress(소스) + data.REPRSSI(인접 링크)
    data = obj.get("data") if isinstance(obj.get("data"), dict) else None
    reprssi = obj.get("REPRSSI") or (data.get("REPRSSI") if data else None)
    if isinstance(reprssi, list):
        met["reprssi"] = [_norm_reprssi_row(r) for r in reprssi if isinstance(r, list) and r]
    if data and data.get("macAddress"):
        ids["mac"] = _norm_mac(data["macAddress"])


def _attach(ev: dict, text: str) -> None:
    """연속 줄을 현재 블록에 부착(보조 필드 추출). 매칭 없으면 raw_lines 보존만."""
    ev["raw_lines"].append(text)
    if ev.get("kind") == "pass_refused" and ev.get("json") is None:
        obj = extract_json(text)
        if obj is not None:
            ev["json"] = obj
            _fill_from_json(ev, obj)
    m = _RE_TAKEN.search(text)
    if m:
        ev["metrics"]["takentime_ms"] = int(m.group(1))
    m = _RE_AVRTAKEN.search(text)
    if m:
        ev["metrics"]["avr_takentime_ms"] = float(m.group(1))
    m = _RE_FROM.search(text)
    if m:
        ev["hints"]["src_name"] = m.group(1)
    m = _RE_PASSED.search(text)
    if m:
        ev["hints"]["passed"] = m.group(1).strip()
    # [Proc-Raw Packet](SSM)이 찍히면 Rt 제거 전 원시 패킷에서 실제 경로 토큰을 얻는다.
    # 기본 모드에서는 중복수신 검사 통과분만 찍히므로 Rt 출처로 항상 존재한다고 가정하지 않는다.
    if "[Proc-Raw Packet]" in text:
        obj = extract_json(text)
        if isinstance(obj, dict) and isinstance(obj.get("Rt"), list):
            ev["ids"]["rt_tokens"] = [str(t) for t in obj["Rt"] if t is not None]


class EventAssembler:
    """포트 1개의 수신 줄을 누산해 논리 Event 를 방출한다.

    헤더 줄에서 블록을 시작하고, 다음 헤더(또는 flush)에서 직전 블록을 완성 방출한다.
    헤더 사이의 연속 줄(takentime/<<<From/[Passed Device]/[Proc-Raw Packet] 등)은 현재 블록에
    부착된다. 한 이벤트 지연(다음 헤더가 와야 직전 이벤트 방출)이 있으나 윈도 상관에는 무해하다.

    예외: **tx/pass(w/JSON)/wifirx는 헤더 한 줄에 페이로드가 완결**(UnID/Mac·Unique/Cidx/Rt 전부
    인라인)이라 **즉시 방출**한다. Data_Pass 헤더에 JSON 이 없으면 송신 거부(pass_refused)로
    버퍼링해 다음 줄 JSON 을 한 번만 부착한다. tx 를 버퍼링하면 리프가
    전송 후 침묵할 때 다음 헤더/유휴 flush(2s+스윕 위상)까지 방출이 밀려, correlator 의
    rx_grace(1s)가 먼저 만료돼 src_port 가 확률적으로 유실된다(2026-07-02 재검토 — RX 선행
    수정 63495ed 의 잔여 경쟁). pass/wifirx 도 PeerLinks 포트쌍 상관에 즉시 필요하다.

    전제: SSM 기본 모드(fprintAllReceivedPackets=false, §14 실측 확증). VRXALLPKTS 토글로 true 면
    [Proc-Raw Packet]/[Passed Device] 가 [Proc-WiFiRx] 앞에 출력돼 순서가 달라진다(모듈3 시간창 결합으로 보강).
    """

    def __init__(self, port: str) -> None:
        self._port = port
        self._cur: Optional[dict] = None

    def feed(self, ts: float, text: str) -> list:
        """줄 1개 처리. 완성된 Event 목록을 반환(보통 0~1개, 즉시형은 직전 블록과 함께 최대 2개)."""
        out: list = []
        kind = _header_kind(text)
        if kind:
            if self._cur is not None:
                out.append(self._cur)
                self._cur = None
            ev = _new_event(self._port, ts, kind, text)
            if kind == "pass":
                if ev["json"] is not None:
                    out.append(ev)     # relay 실행 증거 — 헤더 한 줄에 완결
                else:
                    ev["kind"] = "pass_refused"
                    self._cur = ev     # 송신 거부 — 다음 줄 JSON 을 부착한 뒤 방출
            elif kind in ("tx", "wifirx"):
                out.append(ev)         # 헤더 한 줄에 완결 — 즉시 방출(클래스 docstring 예외)
            else:
                self._cur = ev
        elif self._cur is not None:
            _attach(self._cur, text)
        return out

    def flush(self) -> list:
        """누적 중인 마지막 블록을 방출(스트림 종료/타임아웃 시)."""
        if self._cur is not None:
            ev, self._cur = self._cur, None
            return [ev]
        return []
