"""웹 뷰어 순수 렌더 로직(SViewer) 동작 테스트.

viewer.html의 VIEWER-PURE 블록(DOM 비의존 순수 함수)을 추출해
node 로 실제 실행·검증한다. 샘플 암기가 아니라 '일반 패턴'(ANSI/JSON/kv/분류/반복/
noise/네트워크 토큰 등)을 검증한다 — 새 펌웨어·태그·키가 와도 안전하도록.

node 가 없으면 skip 한다(Python 전용 `uv run pytest` 가 깨지지 않게).
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "serial_mcp" / "viewer.html"
HARNESS = Path(__file__).with_name("viewer_logic_harness.cjs")

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node 미설치 — JS 순수 로직 검증 skip")


def _extract_pure_block() -> str:
    """viewer.html의 VIEWER-PURE-START ~ END 사이 순수 JS(IIFE)를 추출한다."""
    src = WEB.read_text(encoding="utf-8")
    m1 = src.index("VIEWER-PURE-START")
    code_start = src.index("*/", m1) + 2          # START 주석을 닫는 */ 이후
    m2 = src.index("VIEWER-PURE-END")
    code_end = src.rindex("/*", code_start, m2)    # END 주석을 여는 /* 이전
    code = src[code_start:code_end].strip()
    assert "module.exports" in code, "순수 블록에 node export 가 있어야 한다"
    assert "document" not in code and "window.SViewer" in code, "순수 블록은 DOM 비의존이어야 한다"
    return code


def test_viewer_pure_logic_passes_node_harness(tmp_path):
    code = _extract_pure_block()
    mod = tmp_path / "viewer_pure.cjs"
    mod.write_text(code, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(HARNESS), str(mod)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    # 실패 시 node 가 출력한 어떤 단언이 깨졌는지 그대로 보여준다.
    assert proc.returncode == 0, f"\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    assert "all viewer-logic assertions passed" in proc.stdout


def test_pure_block_is_dom_free_and_single():
    """sentinel 추출이 유일하고, DOM API 를 실수로 끌고 오지 않는지 가드."""
    src = WEB.read_text(encoding="utf-8")
    assert src.count("VIEWER-PURE-START") == 1
    assert src.count("VIEWER-PURE-END") == 1
    code = _extract_pure_block()
    # 순수 블록 안에서는 document / addEventListener 등을 쓰지 않는다(테스트 가능성 보장).
    for forbidden in ("document.", "addEventListener", "localStorage", "EventSource"):
        assert forbidden not in code, f"순수 블록에 DOM 의존 {forbidden!r} 가 있으면 안 됨"


def test_buffer_delta_dom_path_updates_rows_without_full_rebuild():
    """정상 revision delta는 전체 buffer DOM을 비우지 않고 행 단위로 갱신해야 한다."""
    src = WEB.read_text(encoding="utf-8")
    start = src.index("function applyBufferDeltaDom")
    end = src.index("async function refreshBuffer", start)
    block = src[start:end]
    assert "innerHTML" not in block and "replaceChildren" not in block
    assert "replaceBufferRow" in block
    assert "appendBufferEntry" in block
    assert "data-buffer-seq" in src
