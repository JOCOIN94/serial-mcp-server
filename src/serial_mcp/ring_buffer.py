"""시리얼 로그 라인 저장용 ring buffer.

수신 라인을 타임스탬프와 함께 보관하고, 근접 중복을 룩백 윈도로 접으며(dedup=N),
exclude/include 정규식으로 저장 시점에 거른다. 공백뿐인 줄은 저장하지
않는다(dedup 연속성·버퍼 밀도 확보). 스레드 안전(Lock).

이 모듈은 순수 로직만 담는다 — 시리얼 I/O나 MCP 의존성이 전혀 없어
단위 테스트가 쉽다.
"""

from __future__ import annotations

import re
import threading
from collections import deque
from itertools import islice
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


def _fmt_ts(ts: datetime) -> str:
    """[HH:MM:SS.mmm] 본문에 들어갈 'HH:MM:SS.mmm' 형식 문자열."""
    return ts.strftime("%H:%M:%S.") + f"{ts.microsecond // 1000:03d}"


@dataclass
class LogEntry:
    """버퍼에 저장되는 한 항목.

    근접 중복(룩백 윈도 내)이 접히면 count>1 이 되고, 첫/마지막 수신 시각을 함께 보존한다.
    """

    text: str            # 타임스탬프를 제외한 라인 내용(dedup 비교 기준)
    first_ts: datetime   # 이 묶음의 최초 수신 시각
    last_ts: datetime    # 이 묶음의 최종 수신 시각
    count: int = 1       # 접힌 반복 횟수

    def render(self) -> str:
        """조회용 문자열. 접힌 묶음은 '(N회 반복, HH:MM:SS~HH:MM:SS)'를 덧붙인다."""
        head = f"[{_fmt_ts(self.first_ts)}] {self.text}"
        if self.count > 1:
            t0 = self.first_ts.strftime("%H:%M:%S")
            t1 = self.last_ts.strftime("%H:%M:%S")
            head += f"  ({self.count}회 반복, {t0}~{t1})"
        return head


class LineBuffer:
    """라인 ring buffer + 근접 중복 접기(룩백) + 정규식 수집 필터. 스레드 안전."""

    def __init__(
        self,
        maxlen: int = 2000,
        dedup: int = 5,
        exclude: Optional[str] = None,
        include: Optional[str] = None,
    ) -> None:
        self._buf: deque[LogEntry] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        # 룩백 윈도: 버퍼 끝 N개 안에 같은 줄이 있으면 접는다.
        # 0=끔, 1=직전 줄만(구버전 동작), bool 입력도 int()로 자연 호환(True→1).
        self._dedup_window = int(dedup)
        self._exclude = re.compile(exclude) if exclude else None
        self._include = re.compile(include) if include else None
        self.maxlen = maxlen
        # 진단용 통계
        self.total_received = 0   # 수집 필터 통과 전, 들어온 라인 총수
        self.total_stored = 0     # 신규 항목으로 저장된 수(접힘/필터 제외)

    def add(self, text: str, ts: Optional[datetime] = None) -> bool:
        """라인 한 줄을 '수집 필터 → 근접 중복 접기(룩백)' 순으로 처리해 버퍼에 반영.

        반환값: 새 항목으로 저장되면 True, 걸러지거나 직전 줄에 접히면 False.
        """
        if ts is None:
            ts = datetime.now()
        with self._lock:
            self.total_received += 1
            # 1) 수집 필터(저장 시점)
            # 공백뿐인 줄은 저장하지 않는다(SPEC §4.3) — 임베디드 로그가 메시지 사이에
            # 빈 줄을 끼워 보내 연속 중복 접기(§4.2)가 무력화되는 것을 막는다.
            if not text.strip():
                return False
            if self._exclude is not None and self._exclude.search(text):
                return False
            if self._include is not None and not self._include.search(text):
                return False
            # 2) 룩백 접기: 버퍼 끝 N개 안에 같은 줄이 있으면 그 항목에 접는다
            #    (항목 위치 유지 — first_ts 순서 보존. 교차 반복도 압축, SPEC §4.2)
            if self._dedup_window:
                for e in islice(reversed(self._buf), self._dedup_window):
                    if e.text == text:
                        e.count += 1
                        e.last_ts = ts
                        return False
            # 3) 신규 항목(가득 차면 deque가 가장 오래된 것을 자동으로 밀어냄)
            self._buf.append(LogEntry(text=text, first_ts=ts, last_ts=ts))
            self.total_stored += 1
            return True

    def get_recent(self, lines: int = 200) -> list[str]:
        """최근 N개 항목을 render된 문자열 리스트로 반환(시간 오름차순)."""
        if lines <= 0:
            return []
        with self._lock:
            items = list(self._buf)[-lines:]
        return [e.render() for e in items]

    def contains_all(self, needles: list) -> bool:
        """모든 needle 이 '한 항목의 원문'에 함께 포함된 항목이 있는지(리터럴 부분문자열 AND).

        체인 점프 가능성 프로브(뷰어 ▸ 사전 비활성 판정)용 — 최신부터 역방향 조기 종료.
        정규식이 아니라 리터럴 비교다(니들에 대괄호 등 메타문자가 그대로 들어온다).
        """
        if not needles:
            return False
        with self._lock:
            snapshot = list(self._buf)
        for e in reversed(snapshot):
            text = e.text
            if all(n in text for n in needles):
                return True
        return False

    def query(self, pattern: str, max_results: int = 100) -> list[str]:
        """정규식으로 버퍼를 검색해 매칭 항목을 render해 반환.

        최신 항목부터 훑어 max_results개를 모은 뒤, 시간 오름차순으로 돌려준다.
        잘못된 정규식이면 re.error 를 그대로 올린다(호출부에서 처리).
        """
        rx = re.compile(pattern)
        with self._lock:
            snapshot = list(self._buf)
        out: list[str] = []
        for e in reversed(snapshot):
            if rx.search(e.text):
                out.append(e.render())
                if len(out) >= max_results:
                    break
        out.reverse()
        return out

    def info(self) -> dict:
        """버퍼 크기와 최신·최오래 항목 등 진단 정보."""
        with self._lock:
            n = len(self._buf)
            oldest = self._buf[0].render() if n else None
            newest = self._buf[-1].render() if n else None
            return {
                "entries": n,
                "capacity": self.maxlen,
                "oldest": oldest,
                "newest": newest,
                "total_received": self.total_received,
                "total_stored": self.total_stored,
                "dedup": self._dedup_window,
            }

    def clear(self) -> int:
        """버퍼를 비우고, 비우기 직전의 항목 수를 반환."""
        with self._lock:
            n = len(self._buf)
            self._buf.clear()
            return n

    def entries_since(self, ts: datetime, max_lines: int = 200) -> list[str]:
        """ts 이후(last_ts >= ts) 활동한 항목을 render해 반환(시간 오름차순).

        쓰기 도구의 응답 자동 회수용이다. 전송 직전 시각을 기록해 두고 그 이후
        수신분만 가져온다. dedup으로 기존 항목에 접힌 응답도 last_ts가 갱신되므로
        잡힌다. 접힌 항목은 기존 render()의 반복 표기로 반환된다.
        """
        if max_lines <= 0:
            return []
        with self._lock:
            items = [e for e in self._buf if e.last_ts >= ts]
        return [e.render() for e in items[-max_lines:]]

    def snapshot(self) -> list[dict]:
        """웹 뷰어 버퍼 탭용 구조화 뷰(시간 오름차순).

        render() 문자열 대신 본문/시각/반복수를 분리해 반환한다 — 클라이언트가
        메타(타임스탬프·반복 표기)를 본문과 다른 색으로 칠할 수 있게.
        """
        with self._lock:
            items = list(self._buf)
        return [
            {
                "text": e.text,
                "first_ts": _fmt_ts(e.first_ts),
                "last_ts": _fmt_ts(e.last_ts),
                "count": e.count,
            }
            for e in items
        ]
