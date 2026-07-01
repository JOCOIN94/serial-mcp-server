"""topology_pairing.py — SB 카드상관 페어링(순수 stateful, I/O 비의존).

STM 은 정상 운영 중 자기 베이번호(BayID)를 시리얼에 안 흘린다(펌웨어 검증 2026-07-01: BayID 는
DIP 설정모드/config 명령 때만·MAC 없음). 두 포트(ESP·STM)를 한 베이 노드로 병합할 공유 식별자가
없다. 유일한 신호는 **카드 태그**다:
  - ESP 는 카드를 SSM 으로 포워딩할 때 `{"sCuID":..,"UnID":N,..}` 로 **sCuID+UnID 를 함께** 로깅.
  - STM 은 같은 카드의 `{"sCuID":..,"Amnt":..}` 를(UnID 없이) 로깅.

핵심 규칙: **어떤 포트든 자기가 찍은 sCuID 가 UnID-태그된 sCuID 와 일치하면 그 베이(UnID) 소속.**
한 베이의 ESP·STM 은 같은 카드 sCuID 를 둘 다 찍으므로 둘 다 그 베이로 귀속 → roster(_merge_sb)가
병합한다. 포트가 ESP/STM 인지 미리 알 필요 없다(ESP 는 자기 UnID 로 이미 번호가 있어 멱등).

설계 못:
- **라이브 스트림 포착**: 엔진 observe 가 매 줄 흘려주므로, 카드 줄이 링버퍼 밖으로 밀려나도 매핑이
  남는다(스냅샷 재계산이 버퍼에 의존하지 않음 — 롤오프 문제 해소).
- **휘발성**: 세션 인메모리. forget_port 로 포트 disconnect/재오픈 시 무효화(재꽂/보드교체 시 스테일
  매핑 방지). 최신 sCuID 가 이김(port_card 덮어씀 + card_bay move_to_end) — 리케이블 자기교정.
- **누수 방지**: card_bay 는 OrderedDict + 상한 drop-oldest(correlator `_recent` 패턴). port_card 는
  포트 수만큼이라 유한.
- 사전검사: `"sCuID"` 미포함 줄은 정규식 전에 값싸게 무시(대부분의 줄).

mesh `EventAssembler` 는 헤더(`[Proc-WiFiRx]` 등) 기반이라 카드 줄을 이벤트로 안 만든다 — 그래서
카드상관은 별도 경량 모듈로 분리한다(관심사 분리). 단위 테스트 용이.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from typing import Optional

# 카드 줄에서 sCuID(16진 카드 식별자)와, 같은 줄에 있으면 UnID(=베이번호)를 뽑는다.
_RE_SCUID = re.compile(r'"sCuID"\s*:\s*"([0-9A-Fa-f]+)"')
_RE_UNID = re.compile(r'"UnID"\s*:\s*(\d+)')


class CardPairing:
    """카드 sCuID 상관으로 포트→베이(UnID) 매핑을 유지하는 순수 stateful 엔진."""

    def __init__(self, max_cards: int = 512) -> None:
        self._max_cards = max_cards
        self._card_bay: "OrderedDict[str, int]" = OrderedDict()   # sCuID → bay(UnID). ESP forward 줄 출처
        self._port_card: dict[str, str] = {}                      # port → 마지막 본 sCuID

    def observe(self, port: str, ts: float, text: str) -> None:
        """수신 줄 1개 처리. 'sCuID' 없으면 값싸게 무시(대부분의 줄). ts 는 인터페이스 대칭용(현재 미사용)."""
        if "sCuID" not in text:
            return
        m = _RE_SCUID.search(text)
        if not m:
            return
        scuid = m.group(1)
        u = _RE_UNID.search(text)
        if u is not None:                          # sCuID+UnID → 베이번호 출처(ESP forward 줄)
            self._card_bay[scuid] = int(u.group(1))
            self._card_bay.move_to_end(scuid)      # 최신화(신선분 축출 방지)
            while len(self._card_bay) > self._max_cards:
                self._card_bay.popitem(last=False)  # drop-oldest
        self._port_card[port] = scuid              # 이 포트가 마지막 본 카드(최신 우선 — 덮어씀)

    def forget_port(self, port: str) -> None:
        """포트 disconnect/재오픈 시 그 포트 페어링 흔적 제거(휘발 무효화). card_bay 는 카드 키라 그대로(자동 축출)."""
        self._port_card.pop(port, None)

    def snapshot(self) -> dict:
        """{port: bay} 사본 — 포트의 마지막 카드 sCuID 가 베이번호로 해소되면 매핑. build_roster(모듈5)가 STM 번호 폴백에 쓴다.

        ESP 는 bare `{sCuID}` 를 UnID-태그 줄보다 ms 먼저 찍지만, 스냅샷 시점에 card_bay 를 조회하므로
        도착 순서 무관하다. UnID-태그를 못 본 카드(예: ESP 미연결)는 해소 안 돼 포함되지 않는다.
        """
        out: dict = {}
        for port, scuid in self._port_card.items():
            bay = self._card_bay.get(scuid)
            if bay is not None:
                out[port] = bay
        return out
