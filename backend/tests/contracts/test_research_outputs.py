import pytest
from pydantic import ValidationError

from app.application.research_outputs import Challenge, Claim, GapQuery


def test_web_claim_requires_evidence_but_data_claim_does_not():
    assert Claim(text="Dataset value", kind="data")
    with pytest.raises(ValidationError):
        Claim(text="Web assertion", kind="web")


def test_challenge_constraints_and_extra_fields_are_enforced():
    values = {
        "claim": "Claim",
        "severity": "low",
        "adjustment_type": "none",
        "magnitude": 0.1,
        "is_assumption": False,
    }
    with pytest.raises(ValidationError):
        Challenge(**values)
    with pytest.raises(ValidationError):
        GapQuery(query="A valid query", rationale="Because", extra="rejected")
