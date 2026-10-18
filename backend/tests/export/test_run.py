import json

from app.export.run import load_run


def test_load_run_uses_latest_evidence_revision_in_first_seen_order(tmp_path):
    (tmp_path / "run.json").write_text(json.dumps({"run_id": "run-1"}), encoding="utf-8")
    (tmp_path / "cells.json").write_text(json.dumps({"cells": []}), encoding="utf-8")
    revisions = [
        {
            "evidence_id": "ev_first",
            "status": "unverified",
            "url": "https://example.org/old",
            "quote": "old quote",
        },
        {
            "evidence_id": "ev_second",
            "status": "verified",
            "url": "https://example.org/second",
            "quote": "second quote",
        },
        {
            "evidence_id": "ev_first",
            "status": "verified",
            "url": "https://example.org/latest",
            "quote": "latest quote",
        },
    ]
    (tmp_path / "evidence.jsonl").write_text(
        "\n".join(json.dumps(entry) for entry in revisions),
        encoding="utf-8",
    )

    bundle = load_run(tmp_path)

    assert [entry["evidence_id"] for entry in bundle.evidence] == ["ev_first", "ev_second"]
    assert bundle.evidence[0]["url"] == "https://example.org/latest"
    assert bundle.evidence[0]["quote"] == "latest quote"
    assert [entry["n"] for entry in bundle.evidence] == [1, 2]
