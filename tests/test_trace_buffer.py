from __future__ import annotations

from pathlib import Path

from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter

SCHEMAS = Path(__file__).resolve().parents[1] / "contracts" / "schemas"


def test_aborted_case_leaves_no_events(tmp_path: Path) -> None:
    trace = TraceWriter(tmp_path / "trace.jsonl", Contracts(SCHEMAS))

    trace.begin_case()
    trace.emit(case_id="T_CASE_001", event_type="case_received", actor="coordinator")
    trace.discard_case()  # transport failure mid-case

    trace.begin_case()
    trace.emit(case_id="T_CASE_001", event_type="case_received", actor="coordinator")
    trace.emit(case_id="T_CASE_001", event_type="case_finalized", actor="coordinator")
    trace.commit_case()

    lines = (tmp_path / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert '"case_received"' in lines[0] and '"case_finalized"' in lines[1]
