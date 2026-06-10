"""포트 스캔 필터·SERIAL_PORT 목록 파싱·별칭(SERIAL_NAMES) — 순수 로직.

다중 포트 설계(docs/superpowers/specs/2026-06-10-multi-port-design.md §3·§4).
comports() 결과 같은 외부 입력은 인자로 주입받아, 시리얼 I/O 없이 단위 테스트한다.
"""

from __future__ import annotations

from typing import Iterable, Optional


def parse_port_list(raw: str) -> list[tuple[str, Optional[int]]]:
    """SERIAL_PORT 목록 파싱: "COM4,COM13@9600" → [("COM4", None), ("COM13", 9600)].

    빈 문자열이면 [] — 자동 스캔 모드를 뜻한다. '@' 뒤가 정수가 아니면 그 항목의
    보드레이트는 None(전역 SERIAL_BAUD 적용).
    """
    out: list[tuple[str, Optional[int]]] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        port, sep, baud_s = item.partition("@")
        baud: Optional[int] = None
        if sep:
            try:
                baud = int(baud_s)
            except ValueError:
                baud = None
        out.append((port.strip(), baud))
    return out


def auto_usb_ports(comports: Iterable) -> list[str]:
    """USB 시리얼만 골라 device 목록 반환 — VID 보유 = USB(CH343/CP210x/FTDI/Prolific…).

    블루투스 가상 포트 등 VID 없는 포트는 제외한다(열기 시도가 블록될 수 있음).
    """
    return [p.device for p in comports if getattr(p, "vid", None) is not None]


def parse_names(raw: str) -> dict[str, str]:
    """SERIAL_NAMES 파싱: "COM4=SSM,5909024173=SB1" → {"COM4": "SSM", "5909024173": "SB1"}.

    키는 포트명 또는 USB 시리얼넘버(대문자 정규화 — 포트 번호가 바뀌어도 시리얼넘버
    키는 따라간다). '='가 없거나 키/값이 비면 그 항목은 무시.
    """
    names: dict[str, str] = {}
    for item in raw.split(","):
        key, sep, val = item.partition("=")
        key, val = key.strip(), val.strip()
        if sep and key and val:
            names[key.upper()] = val
    return names


def name_for(port: str, serial_number: Optional[str], names: dict[str, str]) -> Optional[str]:
    """포트의 별칭 — 포트명 키 우선, 없으면 USB 시리얼넘버 키, 둘 다 없으면 None."""
    if port.upper() in names:
        return names[port.upper()]
    if serial_number and serial_number.upper() in names:
        return names[serial_number.upper()]
    return None


def label(port: str, name: Optional[str]) -> str:
    """표기 문자열 — 별칭 있으면 'SSM (COM4)', 없으면 'COM4'."""
    return f"{name} ({port})" if name else port
