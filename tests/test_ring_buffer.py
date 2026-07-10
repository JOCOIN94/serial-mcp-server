"""LineBuffer/LogEntry/_fmt_ts 순수 로직 특성화 테스트.

타임스탬프를 명시적으로 주입(add(text, ts))해 결정적으로 검증한다.
SPEC §3(타임스탬프·ring), §4(dedup·필터), §2(동시성 Lock)을 고정한다.
"""

import re
import threading
from datetime import datetime

import pytest

from serial_mcp.ring_buffer import LineBuffer, LogEntry, _fmt_ts

BASE = datetime(2026, 6, 9, 14, 0, 0, 0)


# ---- _fmt_ts / LogEntry.render ----

def test_fmt_ts_millisecond_format():
    assert _fmt_ts(datetime(2026, 6, 9, 14, 2, 17, 123456)) == "14:02:17.123"


def test_logentry_render_single_has_no_repeat_suffix():
    e = LogEntry(text="hello", first_ts=BASE, last_ts=BASE)
    assert e.render() == "[14:00:00.000] hello"


def test_logentry_render_folded_shows_repeat_count_and_span():
    e = LogEntry(text="tick", first_ts=BASE, last_ts=datetime(2026, 6, 9, 14, 0, 5, 0), count=3)
    assert e.render() == "[14:00:00.000] tick  (3회 반복, 14:00:00~14:00:05)"


# ---- add / get_recent ----

def test_add_and_get_recent_returns_rendered_lines():
    buf = LineBuffer(maxlen=10, dedup=False)
    assert buf.add("boot ok", BASE) is True
    assert buf.get_recent(10) == ["[14:00:00.000] boot ok"]


def test_get_recent_zero_or_negative_returns_empty():
    buf = LineBuffer(maxlen=10, dedup=False)
    buf.add("x", BASE)
    assert buf.get_recent(0) == []
    assert buf.get_recent(-1) == []


def test_get_recent_returns_tail_in_chronological_order():
    buf = LineBuffer(maxlen=10, dedup=False)
    for i in range(5):
        buf.add(f"line{i}", BASE)
    assert buf.get_recent(2) == ["[14:00:00.000] line3", "[14:00:00.000] line4"]


# ---- dedup (SPEC §4.2) ----

def test_dedup_folds_consecutive_identical_lines():
    buf = LineBuffer(maxlen=10, dedup=True)
    assert buf.add("tick", BASE) is True
    assert buf.add("tick", datetime(2026, 6, 9, 14, 0, 1, 0)) is False
    assert buf.add("tick", datetime(2026, 6, 9, 14, 0, 2, 0)) is False
    lines = buf.get_recent(10)
    assert lines == ["[14:00:00.000] tick  (3회 반복, 14:00:00~14:00:02)"]
    info = buf.info()
    assert info["total_received"] == 3
    assert info["total_stored"] == 1


def test_dedup_breaks_on_different_line_then_starts_new_group():
    buf = LineBuffer(maxlen=10, dedup=True)
    buf.add("A", BASE)
    buf.add("A", BASE)   # 접힘
    buf.add("B", BASE)   # 묶음 종료
    buf.add("A", BASE)   # 간격 후 재등장 → 새 묶음(첫 묶음에 합쳐지지 않음)
    lines = buf.get_recent(10)
    assert len(lines) == 3
    assert "(2회 반복" in lines[0]
    assert lines[1].endswith("B")
    assert lines[2].endswith("A")
    assert "회 반복" not in lines[2]


def test_dedup_disabled_keeps_every_line():
    buf = LineBuffer(maxlen=10, dedup=False)
    for _ in range(3):
        assert buf.add("tick", BASE) is True
    assert len(buf.get_recent(10)) == 3


# ---- 수집 필터 (SPEC §4.1) ----

def test_exclude_filter_drops_matching_lines():
    buf = LineBuffer(maxlen=10, dedup=False, exclude=r"DEBUG")
    assert buf.add("INFO ok", BASE) is True
    assert buf.add("DEBUG noise", BASE) is False
    assert buf.get_recent(10) == ["[14:00:00.000] INFO ok"]


def test_include_filter_keeps_only_matching_lines():
    buf = LineBuffer(maxlen=10, dedup=False, include=r"ERROR")
    assert buf.add("INFO ok", BASE) is False
    assert buf.add("ERROR boom", BASE) is True
    assert buf.get_recent(10) == ["[14:00:00.000] ERROR boom"]


def test_exclude_takes_precedence_over_include():
    buf = LineBuffer(maxlen=10, dedup=False, include=r"msg", exclude=r"secret")
    assert buf.add("secret msg", BASE) is False   # include 매칭이어도 exclude 우선
    assert buf.add("public msg", BASE) is True
    assert buf.get_recent(10) == ["[14:00:00.000] public msg"]


# ---- dedup 룩백 윈도 (SPEC §4.2 개정: 교차 반복 압축) ----

def test_lookback_folds_alternating_lines():
    # 실장비 패턴: A → B → A → B 교차 — 직전-줄 접기로는 못 잡던 케이스
    buf = LineBuffer(maxlen=10, dedup=5)
    buf.add("A", BASE)
    buf.add("B", BASE)
    assert buf.add("A", datetime(2026, 6, 9, 14, 0, 1, 0)) is False   # 룩백 접힘
    assert buf.add("B", datetime(2026, 6, 9, 14, 0, 2, 0)) is False
    snap = buf.snapshot()
    assert [e["text"] for e in snap] == ["A", "B"]          # 항목 위치 유지(first_ts 순서)
    assert snap[0]["count"] == 2 and snap[0]["last_ts"] == "14:00:01.000"
    assert snap[1]["count"] == 2 and snap[1]["last_ts"] == "14:00:02.000"


def test_lookback_window_limit():
    buf = LineBuffer(maxlen=20, dedup=2)
    buf.add("A", BASE)
    buf.add("B", BASE)
    buf.add("C", BASE)        # 이제 A는 윈도(끝 2개: B,C) 밖
    assert buf.add("A", BASE) is True    # 새 항목
    assert len(buf.snapshot()) == 4


def test_dedup_zero_disables_folding():
    buf = LineBuffer(maxlen=10, dedup=0)
    buf.add("x", BASE)
    assert buf.add("x", BASE) is True


def test_dedup_true_means_window_one():
    # 하위호환: 구버전 dedup=True(불리언)는 '직전 1줄만'과 동일
    buf = LineBuffer(maxlen=10, dedup=True)
    buf.add("A", BASE)
    buf.add("B", BASE)
    assert buf.add("A", BASE) is True    # 직전(B)만 비교 → 안 접힘


# ---- 빈 줄 저장 제외 (SPEC §4.3) ----

def test_blank_lines_are_not_stored():
    buf = LineBuffer(maxlen=10, dedup=True)
    assert buf.add("", BASE) is False
    assert buf.add("   ", BASE) is False   # 공백뿐인 줄도 제외
    info = buf.info()
    assert info["entries"] == 0
    assert info["total_received"] == 2
    assert info["total_stored"] == 0


def test_dedup_folds_repeats_interleaved_with_blank_lines():
    # 실장비 SSM 패턴: "" → 메시지 → "" → 메시지 … (SPEC §4.2 "실로그 확인 후 조정"의 근거)
    buf = LineBuffer(maxlen=10, dedup=True)
    buf.add("", BASE)
    assert buf.add("[IOc] Disconnected!", BASE) is True
    buf.add("", BASE)
    assert buf.add("[IOc] Disconnected!", datetime(2026, 6, 9, 14, 0, 1, 0)) is False   # 접힘
    lines = buf.get_recent(10)
    assert len(lines) == 1
    assert "(2회 반복" in lines[0]


# ---- ring eviction (SPEC §3) ----

def test_ring_buffer_evicts_oldest_beyond_maxlen():
    buf = LineBuffer(maxlen=3, dedup=False)
    for i in range(5):
        buf.add(f"line{i}", BASE)
    lines = buf.get_recent(100)
    assert len(lines) == 3
    assert lines[0].endswith("line2")
    assert lines[-1].endswith("line4")


# ---- query (SPEC §5) ----

def test_query_matches_regex_and_returns_chronological():
    buf = LineBuffer(maxlen=10, dedup=False)
    buf.add("ERROR one", BASE)
    buf.add("info", BASE)
    buf.add("ERROR two", BASE)
    assert buf.query(r"ERROR") == ["[14:00:00.000] ERROR one", "[14:00:00.000] ERROR two"]


def test_query_max_results_keeps_most_recent_matches():
    buf = LineBuffer(maxlen=10, dedup=False)
    for i in range(5):
        buf.add(f"ERROR {i}", BASE)
    assert buf.query(r"ERROR", max_results=2) == ["[14:00:00.000] ERROR 3", "[14:00:00.000] ERROR 4"]


def test_query_invalid_regex_raises_re_error():
    buf = LineBuffer(maxlen=10, dedup=False)
    with pytest.raises(re.error):
        buf.query("[")


def test_query_searches_raw_text_not_rendered_form():
    buf = LineBuffer(maxlen=10, dedup=True)
    buf.add("tick", BASE)
    buf.add("tick", BASE)   # 접힘 → render에는 '(2회 반복…)'이 붙지만
    assert buf.query(r"반복") == []        # 검색 대상은 원문 text(접힘 표기 아님)
    assert buf.query(r"14:00:00") == []    # 타임스탬프도 검색 대상 아님
    got = buf.query(r"tick")
    assert len(got) == 1
    assert "(2회 반복" in got[0]           # 반환은 render된 형태


# ---- query context (v1.15.0) ----

def test_query_context_merges_overlapping_windows():
    buf = LineBuffer(maxlen=20, dedup=False)
    lines = ["boot", "wifi init", "connecting", "ERROR one", "retry1",
             "retry2", "ERROR two", "retry3", "connected", "ready"]
    for t in lines:
        buf.add(t, BASE)
    got = buf.query(r"ERROR", context=2)
    # 매칭 idx3([1,5])과 idx6([4,8])의 문맥 구간이 겹쳐 [1,8] 하나로 병합돼야 한다
    assert "---" not in got
    assert len(got) == 8
    assert got[0] == "[14:00:00.000] wifi init"
    assert got[2] == "▶ [14:00:00.000] ERROR one"
    assert got[5] == "▶ [14:00:00.000] ERROR two"
    assert got[-1] == "[14:00:00.000] connected"


def test_query_context_separates_distant_matches_with_separator():
    buf = LineBuffer(maxlen=30, dedup=False)
    texts = ["ERROR a" if i == 2 else ("ERROR b" if i == 17 else f"noise{i}")
             for i in range(20)]
    for t in texts:
        buf.add(t, BASE)
    got = buf.query(r"ERROR", context=2)
    assert got.count("---") == 1
    sep = got.index("---")
    first_block, second_block = got[:sep], got[sep + 1:]
    assert len(first_block) == 5   # idx 0..4
    assert len(second_block) == 5  # idx 15..19
    assert first_block[2] == "▶ [14:00:00.000] ERROR a"
    assert second_block[2] == "▶ [14:00:00.000] ERROR b"


def test_query_context_respects_max_results_on_match_count():
    buf = LineBuffer(maxlen=20, dedup=False)
    for i in range(5):
        buf.add(f"ERROR {i}", BASE)
    got = buf.query(r"ERROR", max_results=2, context=1)
    # 최근 매칭 2개(idx3,idx4)만 문맥 대상 — idx0,1,2는 애초에 매칭 후보에서 제외됨
    assert got == [
        "[14:00:00.000] ERROR 2",
        "▶ [14:00:00.000] ERROR 3",
        "▶ [14:00:00.000] ERROR 4",
    ]


def test_query_context_zero_is_unmarked_and_unchanged():
    buf = LineBuffer(maxlen=10, dedup=False)
    buf.add("ERROR one", BASE)
    assert buf.query(r"ERROR", context=0) == buf.query(r"ERROR")


def test_query_literal_matches_substring_with_regex_metacharacters():
    buf = LineBuffer(maxlen=10, dedup=False)
    text = '{"CHPLAN":[1,"00"],"Asn":58,"UnID":5}'
    buf.add(text, BASE)
    assert buf.query(text, literal=True) == [f"[14:00:00.000] {text}"]
    # literal=False(기본)면 '[' 가 정규식 문자 클래스로 해석돼 조용히 매칭 실패한다
    # (에러 없이 0건 — literal 스위치가 왜 필요한지 보여주는 대조군)
    assert buf.query(text) == []


def test_query_literal_with_context_returns_marked_window():
    buf = LineBuffer(maxlen=10, dedup=False)
    buf.add("before", BASE)
    buf.add('{"A":[1]}', BASE)
    buf.add("after", BASE)
    got = buf.query('{"A":[1]}', literal=True, context=1)
    assert got == [
        "[14:00:00.000] before",
        '▶ [14:00:00.000] {"A":[1]}',
        "[14:00:00.000] after",
    ]


# ---- info / clear ----

def test_info_reports_capacity_and_endpoints():
    buf = LineBuffer(maxlen=5, dedup=True)
    buf.add("first", BASE)
    buf.add("last", BASE)
    info = buf.info()
    assert info["entries"] == 2
    assert info["capacity"] == 5
    assert info["oldest"].endswith("first")
    assert info["newest"].endswith("last")
    assert info["dedup"] == 1


def test_info_empty_buffer_has_none_endpoints():
    info = LineBuffer(maxlen=5).info()
    assert info["entries"] == 0
    assert info["oldest"] is None
    assert info["newest"] is None


def test_clear_empties_and_returns_prior_count():
    buf = LineBuffer(maxlen=10, dedup=False)
    buf.add("a", BASE)
    buf.add("b", BASE)
    assert buf.clear() == 2
    assert buf.get_recent(10) == []
    assert buf.clear() == 0


# ---- entries_since (쓰기 도구 응답 회수용) ----

def test_entries_since_returns_only_after_ts():
    buf = LineBuffer(maxlen=10, dedup=False)
    t0 = datetime(2026, 6, 9, 14, 0, 1, 0)
    buf.add("before", BASE)
    buf.add("after", datetime(2026, 6, 9, 14, 0, 2, 0))

    assert buf.entries_since(t0) == ["[14:00:02.000] after"]


def test_entries_since_boundary_inclusive():
    buf = LineBuffer(maxlen=10, dedup=False)
    t0 = datetime(2026, 6, 9, 14, 0, 1, 0)
    buf.add("boundary", t0)

    assert buf.entries_since(t0) == ["[14:00:01.000] boundary"]


def test_entries_since_catches_folded_entry():
    buf = LineBuffer(maxlen=10, dedup=True)
    t0 = datetime(2026, 6, 9, 14, 0, 1, 0)
    buf.add("tick", BASE)
    buf.add("tick", datetime(2026, 6, 9, 14, 0, 2, 0))

    assert buf.entries_since(t0) == ["[14:00:00.000] tick  (2회 반복, 14:00:00~14:00:02)"]


def test_entries_since_empty_and_max_lines():
    buf = LineBuffer(maxlen=10, dedup=False)
    t0 = datetime(2026, 6, 9, 14, 0, 1, 0)
    assert buf.entries_since(t0) == []

    for i in range(4):
        buf.add(f"line{i}", datetime(2026, 6, 9, 14, 0, i + 1, 0))

    assert buf.entries_since(t0, max_lines=2) == [
        "[14:00:03.000] line2",
        "[14:00:04.000] line3",
    ]


# ---- 동시성 (SPEC §2) ----

def test_concurrent_adds_are_thread_safe():
    buf = LineBuffer(maxlen=100_000, dedup=False)

    def worker(tid: int) -> None:
        for i in range(1000):
            buf.add(f"t{tid}-{i}", BASE)   # 모두 고유 → dedup 무관, 전부 저장

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    info = buf.info()
    assert info["total_received"] == 8000   # Lock 하에 손실 없음
    assert info["total_stored"] == 8000
    assert info["entries"] == 8000


# ---- snapshot (웹 뷰어 버퍼 탭용 구조화 뷰) ----

def test_snapshot_empty_buffer():
    assert LineBuffer(maxlen=5).snapshot() == []


def test_snapshot_returns_structured_entries_with_fold():
    buf = LineBuffer(maxlen=10, dedup=True)
    buf.add("tick", BASE)
    buf.add("tick", datetime(2026, 6, 9, 14, 0, 5, 0))
    assert buf.snapshot() == [
        {"text": "tick", "first_ts": "14:00:00.000", "last_ts": "14:00:05.000", "count": 2}
    ]


def test_snapshot_chronological_order():
    buf = LineBuffer(maxlen=10, dedup=False)
    buf.add("first", BASE)
    buf.add("second", BASE)
    assert [e["text"] for e in buf.snapshot()] == ["first", "second"]


# ---- snapshot_delta (웹 뷰어 증분 동기화) ----

def test_snapshot_delta_full_then_unchanged_uses_stable_seq_revision():
    buf = LineBuffer(maxlen=10, dedup=False)
    buf.add("first", BASE)
    buf.add("second", datetime(2026, 6, 9, 14, 0, 1, 0))

    full = buf.snapshot_delta()
    assert full == {
        "revision": 2,
        "reset": True,
        "oldest_seq": 1,
        "entries": [
            {"seq": 1, "revision": 1, "text": "first",
             "first_ts": "14:00:00.000", "last_ts": "14:00:00.000", "count": 1},
            {"seq": 2, "revision": 2, "text": "second",
             "first_ts": "14:00:01.000", "last_ts": "14:00:01.000", "count": 1},
        ],
    }
    assert buf.snapshot_delta(since=2) == {
        "revision": 2, "reset": False, "oldest_seq": 1, "entries": [],
    }


def test_snapshot_delta_dedup_returns_same_seq_as_changed_entry():
    buf = LineBuffer(maxlen=10, dedup=5)
    buf.add("A", BASE)
    buf.add("B", BASE)
    assert buf.add("A", datetime(2026, 6, 9, 14, 0, 2, 0)) is False

    delta = buf.snapshot_delta(since=2)
    assert delta["revision"] == 3
    assert delta["reset"] is False
    assert delta["oldest_seq"] == 1
    assert delta["entries"] == [
        {"seq": 1, "revision": 3, "text": "A",
         "first_ts": "14:00:00.000", "last_ts": "14:00:02.000", "count": 2},
    ]


def test_snapshot_delta_eviction_reports_new_oldest_and_only_changes():
    buf = LineBuffer(maxlen=2, dedup=False)
    buf.add("A", BASE)
    buf.add("B", BASE)
    buf.add("C", BASE)  # seq=1 축출

    delta = buf.snapshot_delta(since=2)
    assert delta["reset"] is False
    assert delta["oldest_seq"] == 2
    assert [e["seq"] for e in delta["entries"]] == [3]
    assert delta["entries"][0]["text"] == "C"


def test_snapshot_delta_clear_and_restart_force_reset_without_data_loss():
    buf = LineBuffer(maxlen=10, dedup=False)
    buf.add("old", BASE)
    assert buf.clear() == 1
    cleared = buf.snapshot_delta(since=1)
    assert cleared == {
        "revision": 2, "reset": True, "oldest_seq": 2, "entries": [],
    }

    buf.add("new", BASE)
    recovered = buf.snapshot_delta(since=1)
    assert recovered["reset"] is True
    assert [e["text"] for e in recovered["entries"]] == ["new"]

    restarted = LineBuffer(maxlen=10, dedup=False).snapshot_delta(since=recovered["revision"])
    assert restarted == {
        "revision": 0, "reset": True, "oldest_seq": 1, "entries": [],
    }


def test_snapshot_delta_filtered_lines_do_not_advance_content_revision():
    buf = LineBuffer(maxlen=10, dedup=False, include="KEEP")
    assert buf.add("DROP", BASE) is False
    assert buf.snapshot_delta() == {
        "revision": 0, "reset": True, "oldest_seq": 1, "entries": [],
    }


# ---- contains_all (체인 점프 가능성 프로브 — 부분문자열 AND, 정규식 아님) ----

def test_contains_all_requires_every_needle_on_one_line():
    buf = LineBuffer(maxlen=10, dedup=False)
    buf.add('[Proc_WiFiTx] Ask Info : To. SB1, {"RTC":[1,2],"UnID":5}', BASE)
    buf.add('other {"UnID":7}', BASE)
    assert buf.contains_all(['"UnID":5', "Ask Info"]) is True
    assert buf.contains_all(['"UnID":5', '"UnID":7']) is False   # 서로 다른 줄 — AND 불성립
    assert buf.contains_all(['"Cidx":999']) is False


def test_contains_all_is_literal_not_regex():
    buf = LineBuffer(maxlen=10, dedup=False)
    buf.add('{"Rng":[0,4]}', BASE)
    assert buf.contains_all(['[0,4]']) is True     # 대괄호가 정규식으로 해석되면 실패한다
    assert buf.contains_all([]) is False           # 빈 니들 — 프로브 불가로 취급
