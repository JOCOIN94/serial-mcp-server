"""시리얼 로그 라인 저장용 ring buffer.

수신 라인을 타임스탬프와 함께 보관하고, 연속 중복을 접으며(dedup),
exclude/include 정규식으로 저장 시점에 거른다. 스레드 안전(Lock).

이 모듈은 순수 로직만 담는다 — 시리얼 I/O나 MCP 의존성이 전혀 없어
단위 테스트가 쉽다.
"""

from __future__ import annotations

import re
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


def _fmt_ts(ts: datetime) -> str:
    """[HH:MM:SS.mmm] 본문에 들어갈 'HH:MM:SS.mmm' 형식 문자열."""
    return ts.strftime("%H:%M:%S.") + f"{ts.microsecond // 1000:03d}"


@dataclass
class LogEntry:
    """버퍼에 저장되는 한 항목.

    연속 중복이 접히면 count>1 이 되고, 첫/마지막 수신 시각을 함께 보존한다.
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
    """라인 ring buffer + 연속 중복 접기 + 정규식 수집 필터. 스레드 안전."""

    def __init__(
        self,
        maxlen: int = 2000,
        dedup: bool = True,
        exclude: Optional[str] = None,
        include: Optional[str] = None,
    ) -> None:
        self._buf: deque[LogEntry] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._dedup = dedup
        self._exclude = re.compile(exclude) if exclude else None
        self._include = re.compile(include) if include else None
        self.maxlen = maxlen
        # 진단용 통계
        self.total_received = 0   # 수집 필터 통과 전, 들어온 라인 총수
        self.total_stored = 0     # 신규 항목으로 저장된 수(접힘/필터 제외)

    def add(self, text: str, ts: Optional[datetime] = None) -> bool:
        """라인 한 줄을 '수집 필터 → 연속 중복 접기' 순으로 처리해 버퍼에 반영.

        반환값: 새 항목으로 저장되면 True, 걸러지거나 직전 줄에 접히면 False.
        """
        if ts is None:
            ts = datetime.now()
        with self._lock:
            self.total_received += 1
            # 1) 수집 필터(저장 시점)
            if self._exclude is not None and self._exclude.search(text):
                return False
            if self._include is not None and not self._include.search(text):
                return False
            # 2) 연속 중복 접기: 직전 줄과 내용이 완전히 동일하면 마지막 항목 갱신
            if self._dedup and self._buf and self._buf[-1].text == text:
                last = self._buf[-1]
                last.count += 1
                last.last_ts = ts
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
                "dedup": self._dedup,
            }

    def clear(self) -> int:
        """버퍼를 비우고, 비우기 직전의 항목 수를 반환."""
        with self._lock:
            n = len(self._buf)
            self._buf.clear()
            return n
