"""The supervisor drains the child's output to EOF before it exits (docs/how-it-works/llm.md)."""

from __future__ import annotations

import os
import sys
import time

from tests.test_llm_supervisor import WAIT_S, _start

_BIG_FINAL_SCRIPT = """
import sys
sys.stdout.write("x" * 3_000_000 + "\\nRESULT\\n")
sys.stdout.flush()
"""


def test_a_slow_reading_owner_still_gets_the_whole_output_after_the_child_exits(tmp_path):
    script = tmp_path / "big.py"
    script.write_text(_BIG_FINAL_SCRIPT)
    proc, write_fd = _start([sys.executable, str(script)])
    try:
        time.sleep(3.5)  # the child is long gone; the pipe to us is full
        out, _ = proc.communicate(timeout=WAIT_S)
    finally:
        os.close(write_fd)
    assert out.endswith(b"\nRESULT\n")
    assert len(out) == 3_000_000 + len(b"\nRESULT\n")
    assert proc.returncode == 0
