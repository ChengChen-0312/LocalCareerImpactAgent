import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

try:
    from localcareerimpact.contracts.report import ReportAST, ReportClaim, Uncertainty
except ModuleNotFoundError:
    pytest.fail("PHASE0_RED_T03_FACTPACK_CONTRACTS_ABSENT", pytrace=False)

from ._task3_helpers import digest


def claim(**updates):
    payload = {
        "claim_id": "C1",
        "template_id": "claim-v1",
        "subject": "occupation",
        "predicate": "has_metric",
        "value_ref": "F1",
        "unit": "ratio_0_1",
        "evidence_refs": ("D1",),
        "caveat_ids": (),
        "classification": "OSCA",
        "scope": {
            "classification": "OSCA",
            "classification_version": "v1",
            "occupation_codes": ("261313",),
            "geography_level": "AUS",
            "geography_code": "AUS",
            "grain": "occupation",
        },
    }
    payload.update(updates)
    return payload


def test_report_claim_rejects_free_numeric_value_and_invalid_fact_ref():
    with pytest.raises((ValidationError, ValueError)):
        ReportClaim(**claim(value="0.4"))
    with pytest.raises((ValidationError, ValueError)):
        ReportClaim(**claim(value_ref="fact one"))


def test_report_claim_scope_is_structured_not_free_text():
    with pytest.raises((ValidationError, ValueError)):
        ReportClaim(**claim(scope="national"))


def test_report_ast_is_closed_immutable_and_rejects_duplicate_local_ids():
    item = ReportClaim(**claim())
    uncertainty = Uncertainty(uncertainty_id="U1", claim_id="C1", code="bounded", explanation="Bounded uncertainty")
    report = ReportAST(run_id="run-1", snapshot_id=digest("snapshot"), factpack_hash=digest("pack"), claims=(item,), uncertainties=(uncertainty,))
    with pytest.raises((TypeError, ValidationError)):
        report.claims[0].subject = "changed"
    with pytest.raises((ValidationError, ValueError)):
        ReportAST(**{**report.model_dump(), "claims": (item, item)})
    with pytest.raises((ValidationError, ValueError)):
        ReportAST(**{**report.model_dump(), "uncertainties": (uncertainty, uncertainty)})
