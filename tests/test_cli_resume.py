from __future__ import annotations

import json
from pathlib import Path

from student_agent.cli import _resumable_cases


def _event(case_id: str, event_type: str) -> str:
    return json.dumps({"case_id": case_id, "event_type": event_type}) + "\n"


def test_resume_keeps_only_finalized_staged_cases(tmp_path: Path) -> None:
    trace = tmp_path / "trace.jsonl"
    trace.write_text(
        _event("C1", "case_received")
        + _event("C1", "case_finalized")
        + _event("C2", "case_received")
        + _event("C2", "case_finalized")  # finalized but its output was never staged
        + '{"case_id": "C3", "event_ty',  # torn line from the interruption
        encoding="utf-8",
    )
    (tmp_path / "C1.json").write_text("{}", encoding="utf-8")

    assert _resumable_cases(tmp_path, trace) == {"C1"}
    kept = [json.loads(line)["case_id"] for line in trace.read_text().splitlines()]
    assert kept == ["C1", "C1"]
