from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator, model_validator

from .factpack import SHA256_PATTERN, Scope
from .query import FrozenContract


class ReportClaim(FrozenContract):
    claim_id: str
    template_id: str
    subject: str
    predicate: str
    value_ref: str | None = Field(pattern=r"^F[1-9][0-9]*$")
    unit: str | None
    evidence_refs: tuple[str, ...]
    caveat_ids: tuple[str, ...]
    classification: Literal["OSCA", "ANZSCO"]
    scope: Scope

    @field_validator("evidence_refs", "caveat_ids")
    @classmethod
    def _unique_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("duplicate report references")
        return tuple(sorted(value))


class Uncertainty(FrozenContract):
    uncertainty_id: str
    claim_id: str
    code: str
    explanation: str


class ReportAST(FrozenContract):
    schema_version: Literal["report-ast.v1"] = "report-ast.v1"
    run_id: str
    snapshot_id: str = Field(pattern=SHA256_PATTERN)
    factpack_hash: str = Field(pattern=SHA256_PATTERN)
    claims: tuple[ReportClaim, ...]
    uncertainties: tuple[Uncertainty, ...]

    @model_validator(mode="after")
    def _local_ids(self) -> ReportAST:
        claim_ids = [item.claim_id for item in self.claims]
        uncertainty_ids = [item.uncertainty_id for item in self.uncertainties]
        if len(set(claim_ids)) != len(claim_ids):
            raise ValueError("duplicate report claim IDs")
        if len(set(uncertainty_ids)) != len(uncertainty_ids):
            raise ValueError("duplicate uncertainty IDs")
        ordered_claims = tuple(sorted(self.claims, key=lambda item: item.claim_id))
        ordered_uncertainties = tuple(sorted(self.uncertainties, key=lambda item: item.uncertainty_id))
        updates = {}
        if ordered_claims != self.claims:
            updates["claims"] = ordered_claims
        if ordered_uncertainties != self.uncertainties:
            updates["uncertainties"] = ordered_uncertainties
        for name, value in updates.items():
            object.__setattr__(self, name, value)
        return self


__all__ = ["ReportAST", "ReportClaim", "Uncertainty"]
