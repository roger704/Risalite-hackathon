import json

import pytest

from app.application.runs.pipeline import _read_jsonl as pipeline_read
from app.evidence.factcheck import _read_jsonl as factcheck_read
from app.evidence.ledger import EvidenceLedger
from app.evidence.trace import TraceWriter
from app.export.run import _read_jsonl as export_read


@pytest.mark.parametrize("separator", ["\u0085", "\u2028", "\u2029"])
def test_source_text_unicode_separators_survive_every_jsonl_reader(tmp_path, separator):
    text = f"Published opening date{separator}2027"
    ledger = EvidenceLedger(tmp_path)
    ledger.append(
        {"evidence_id": "ev_page", "method": "tavily-extract", "passage": text, "quote": text}
    )
    ledger.set_status("ev_page", "verified")
    assert len(ledger.history()) == 2
    assert ledger.get("ev_page")["passage"] == text
    assert ledger.get("ev_page")["status"] == "verified"
    for reader in (pipeline_read, factcheck_read, export_read):
        assert [row["quote"] for row in reader(ledger.path)] == [text, text]

    trace = TraceWriter(tmp_path)
    trace.emit("search", "researcher", text, detail={"snippet": text})
    resumed = TraceWriter(tmp_path)
    event = resumed.emit("extract", "researcher", "Continue")
    assert event["seq"] == 1
    assert json.loads(trace.path.read_text().split("\n")[0])["why"] == text
