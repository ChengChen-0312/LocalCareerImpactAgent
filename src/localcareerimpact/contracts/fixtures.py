from __future__ import annotations

import hmac
import io
import json
import wave
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Mapping

from pydantic import Field, field_validator, model_validator

from .canonical import canonical_contract_hash, canonical_json_bytes, sha256_uri
from .factpack import FactPack, SnapshotRegistryView
from .query import (
    FrozenContract,
    QueryResultClosureV1,
    QueryResultDigestInput,
    QueryResultHashPreimage,
    SnapshotBoundQuery,
    _tupleize,
    validate_query_result_closure,
)

SHA256_PATTERN = r"^sha256:[0-9a-f]{64}$"


def _normalized_relative_path(value: str) -> str:
    if not value or "\\" in value:
        raise ValueError("fixture path must use normalized POSIX separators")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise ValueError("fixture path must be normalized and relative")
    if str(path) != value:
        raise ValueError("fixture path must be normalized POSIX form")
    return value


class NamedDigest(FrozenContract):
    name: str
    digest: str = Field(pattern=SHA256_PATTERN)


def _canonical_named(values: tuple[NamedDigest, ...], label: str) -> tuple[NamedDigest, ...]:
    names = [item.name for item in values]
    if len(set(names)) != len(names):
        raise ValueError(f"duplicate {label} names")
    return tuple(sorted(values, key=lambda item: item.name))


class SyntheticSnapshotRoot(FrozenContract):
    schema_version: Literal["synthetic-snapshot-root.v1"] = "synthetic-snapshot-root.v1"
    import_capsule_root: str = Field(pattern=SHA256_PATTERN)
    builder_source_hash: str = Field(pattern=SHA256_PATTERN)
    environment_lock_hashes: tuple[NamedDigest, ...]
    config_hashes: tuple[NamedDigest, ...]
    schema_digest: str = Field(pattern=SHA256_PATTERN)
    logical_export_digests: tuple[NamedDigest, ...]
    vector_tensor_hash: str = Field(pattern=SHA256_PATTERN)
    vector_metadata_digest: str = Field(pattern=SHA256_PATTERN)
    writer_settings_hash: str = Field(pattern=SHA256_PATTERN)
    locale: str
    timezone: str
    random_seed: int

    @model_validator(mode="after")
    def _canonical(self) -> SyntheticSnapshotRoot:
        for name in ("environment_lock_hashes", "config_hashes", "logical_export_digests"):
            ordered = _canonical_named(getattr(self, name), name)
            if ordered != getattr(self, name):
                object.__setattr__(self, name, ordered)
        return self


class QueryDigestFixture(FrozenContract):
    query_id: str
    bound_query: SnapshotBoundQuery
    result: QueryResultDigestInput

    @model_validator(mode="after")
    def _same_query(self) -> QueryDigestFixture:
        if self.query_id != self.result.query_id:
            raise ValueError("fixture query_id must equal result query_id")
        return self


class FixtureEntry(FrozenContract):
    relative_path: str
    content_hash: str = Field(pattern=SHA256_PATTERN)
    byte_size: int = Field(ge=0)

    _path = field_validator("relative_path")(_normalized_relative_path)


class DerivedAudioRecipe(FrozenContract):
    schema_version: Literal["derived-audio-recipe.v1"] = "derived-audio-recipe.v1"
    source_path: str
    source_hash: str = Field(pattern=SHA256_PATTERN)
    algorithm_version: str
    sample_rate: int = Field(gt=0)
    channels: int = Field(gt=0)
    sample_width_bytes: int = Field(gt=0)
    output_samples: int = Field(gt=0)
    output_hash: str = Field(pattern=SHA256_PATTERN)

    _path = field_validator("source_path")(_normalized_relative_path)


class FrozenAudioFixture(FrozenContract):
    schema_version: Literal["frozen-audio-fixture.v1"] = "frozen-audio-fixture.v1"
    relative_path: Literal["short_en.wav"]
    selected_voice: str
    macos_build: str
    sample_rate: int = Field(gt=0)
    channels: int = Field(gt=0)
    sample_width_bytes: int = Field(gt=0)
    frame_count: int = Field(gt=0)
    content_hash: str = Field(pattern=SHA256_PATTERN)


class HarnessCase(FrozenContract):
    schema_version: Literal["harness-case.v1"] = "harness-case.v1"
    case_id: str
    intent_id: str
    base_evidence_context_hash: str = Field(pattern=SHA256_PATTERN)
    filler_seed: int = Field(ge=0)
    main_total_target: int = Field(gt=0)
    main_output_cap: int = Field(gt=0)
    review_total_target: int = Field(gt=0)
    review_output_cap: int = Field(gt=0)
    passage_count: int = Field(ge=0)
    image_token_allowance: int = Field(ge=0)
    mode: Literal["small", "normal", "upper_semantic", "upper_capacity_forced"]

    @model_validator(mode="after")
    def _target_relationships(self) -> HarnessCase:
        if self.main_output_cap > self.main_total_target:
            raise ValueError("main output cap cannot exceed total target")
        if self.review_output_cap > self.review_total_target:
            raise ValueError("review output cap cannot exceed total target")
        return self


class FixtureManifest(FrozenContract):
    schema_version: Literal["phase0-fixture-manifest.v1"] = "phase0-fixture-manifest.v1"
    snapshot_id: str = Field(pattern=SHA256_PATTERN)
    entries: tuple[FixtureEntry, ...]
    query_digests_hash: str = Field(pattern=SHA256_PATTERN)
    short_audio_fixture: FrozenAudioFixture
    derived_audio_recipe: DerivedAudioRecipe

    @model_validator(mode="after")
    def _closed_manifest(self) -> FixtureManifest:
        paths = [entry.relative_path for entry in self.entries]
        if len(set(paths)) != len(paths):
            raise ValueError("duplicate fixture paths")
        folded = [path.casefold() for path in paths]
        if len(set(folded)) != len(folded):
            raise ValueError("case-fold colliding fixture paths")
        if "manifest.json" in paths:
            raise ValueError("FixtureManifest cannot contain a self-entry")
        entry_by_path = {entry.relative_path: entry for entry in self.entries}
        audio_entry = entry_by_path.get(self.short_audio_fixture.relative_path)
        if audio_entry is None or not hmac.compare_digest(audio_entry.content_hash, self.short_audio_fixture.content_hash):
            raise ValueError("short audio fixture must resolve to a matching manifest entry")
        if self.derived_audio_recipe.source_path != self.short_audio_fixture.relative_path:
            raise ValueError("derived audio recipe must name the short audio fixture")
        if not hmac.compare_digest(self.derived_audio_recipe.source_hash, self.short_audio_fixture.content_hash):
            raise ValueError("derived audio source hash mismatch")
        query_entry = entry_by_path.get("query_result_digests.json")
        if query_entry is None or not hmac.compare_digest(query_entry.content_hash, self.query_digests_hash):
            raise ValueError("query digest bytes must have a matching manifest entry")
        ordered = tuple(sorted(self.entries, key=lambda item: item.relative_path))
        if ordered != self.entries:
            object.__setattr__(self, "entries", ordered)
        return self


def compute_snapshot_id(root: SyntheticSnapshotRoot) -> str:
    if not isinstance(root, SyntheticSnapshotRoot):
        raise TypeError("root must be a parsed SyntheticSnapshotRoot")
    return sha256_uri(canonical_json_bytes(root))


def compute_fixture_manifest_hash(manifest: FixtureManifest) -> str:
    if not isinstance(manifest, FixtureManifest):
        raise TypeError("manifest must be a parsed FixtureManifest")
    return sha256_uri(canonical_json_bytes(manifest))


def compute_query_digests_hash(raw_bytes: bytes) -> str:
    if not isinstance(raw_bytes, bytes):
        raise TypeError("query digest preimage must be exact raw bytes")
    return sha256_uri(raw_bytes)


def parse_fixture_manifest(payload: Mapping[str, object]) -> FixtureManifest:
    if not isinstance(payload, Mapping):
        raise TypeError("FixtureManifest payload must be a mapping")
    return FixtureManifest.model_validate(_tupleize(dict(payload)))


def validate_query_digest_fixtures(
    fixtures: tuple[QueryDigestFixture, ...],
    pack: FactPack,
    closure: QueryResultClosureV1,
    snapshot_registry_view: SnapshotRegistryView,
) -> None:
    from .runbundle import validate_factpack_binding

    validate_factpack_binding(pack, closure, snapshot_registry_view)
    fixture_ids = [item.query_id for item in fixtures]
    if len(set(fixture_ids)) != len(fixture_ids):
        raise ValueError("duplicate query digest fixtures")
    query_by_id = {item.query_id: item for item in pack.queries}
    result_by_id = {item.query_id: item for item in closure.results}
    if set(fixture_ids) != set(query_by_id) or set(fixture_ids) != set(result_by_id):
        raise ValueError("query digest fixtures must mirror every FactPack/closure query")
    if fixture_ids != sorted(fixture_ids):
        raise ValueError("query digest fixtures must be sorted by query_id")
    validate_query_result_closure(closure, pack)
    registry_hash = sha256_uri(canonical_json_bytes(pack.registries))
    for fixture in fixtures:
        query = query_by_id[fixture.query_id]
        result = result_by_id[fixture.query_id]
        if fixture.bound_query.snapshot_id != pack.snapshot_id or fixture.bound_query.registry_bundle_hash != registry_hash:
            raise ValueError("fixture bound query snapshot/registry mismatch")
        if fixture.result.template_id != query.template_id:
            raise ValueError("fixture template mismatch")
        if canonical_json_bytes(fixture.bound_query.query) != canonical_json_bytes(query.normalized_params):
            raise ValueError("fixture normalized query mismatch")
        if canonical_json_bytes(fixture.result) != canonical_json_bytes(result):
            raise ValueError("fixture result is not a byte-equal closure mirror")
        expected = canonical_contract_hash(QueryResultHashPreimage(bound_query=fixture.bound_query, result=fixture.result))
        if not hmac.compare_digest(query.result_hash, expected):
            raise ValueError("fixture query result hash mismatch")


def _safe_fixture_path(base_directory: Path, relative_path: str) -> Path:
    base = base_directory.resolve(strict=True)
    candidate = base / relative_path
    current = base
    for part in PurePosixPath(relative_path).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("fixture entries cannot traverse symlinks")
    resolved = candidate.resolve(strict=True)
    try:
        resolved.relative_to(base)
    except ValueError as exc:
        raise ValueError("fixture path escapes its base directory") from exc
    return resolved


def audit_fixture_manifest(
    manifest: FixtureManifest,
    base_directory: Path,
    *,
    pack: FactPack | None = None,
    closure: QueryResultClosureV1 | None = None,
    snapshot_registry_view: SnapshotRegistryView | None = None,
) -> None:
    if pack is None or closure is None or snapshot_registry_view is None:
        raise ValueError("fixture audit requires the paired FactPack, QueryResultClosure, and SnapshotRegistryView")
    if manifest.snapshot_id != pack.snapshot_id or closure.snapshot_id != pack.snapshot_id:
        raise ValueError("fixture manifest, closure, and FactPack snapshot mismatch")
    base = base_directory.resolve(strict=True)
    expected_paths = {entry.relative_path for entry in manifest.entries}
    actual_paths: set[str] = set()
    for path in base.rglob("*"):
        if path.is_symlink():
            raise ValueError("fixture directory cannot contain symlinks")
        if path.is_file():
            relative = path.relative_to(base).as_posix()
            if relative != "manifest.json":
                actual_paths.add(relative)
    if actual_paths != expected_paths:
        raise ValueError("FixtureManifest entries must exactly cover fixture artifacts")
    for entry in manifest.entries:
        path = _safe_fixture_path(base_directory, entry.relative_path)
        raw = path.read_bytes()
        if len(raw) != entry.byte_size or not hmac.compare_digest(sha256_uri(raw), entry.content_hash):
            raise ValueError(f"fixture bytes mismatch: {entry.relative_path}")
        if path.suffix == ".json":
            parsed = json.loads(raw)
            if canonical_json_bytes(parsed) != raw:
                raise ValueError(f"JSON fixture is not canonical: {entry.relative_path}")
    query_bytes = _safe_fixture_path(base_directory, "query_result_digests.json").read_bytes()
    if not hmac.compare_digest(compute_query_digests_hash(query_bytes), manifest.query_digests_hash):
        raise ValueError("query digest raw-byte hash mismatch")
    query_payload = json.loads(query_bytes)
    if not isinstance(query_payload, list):
        raise ValueError("query_result_digests.json top-level value must be an array")
    fixtures = tuple(QueryDigestFixture.model_validate(_tupleize(item)) for item in query_payload)
    validate_query_digest_fixtures(fixtures, pack, closure, snapshot_registry_view)
    audio_bytes = _safe_fixture_path(base_directory, manifest.short_audio_fixture.relative_path).read_bytes()
    with wave.open(io.BytesIO(audio_bytes), "rb") as wav:
        actual = (wav.getframerate(), wav.getnchannels(), wav.getsampwidth(), wav.getnframes())
    expected = (
        manifest.short_audio_fixture.sample_rate,
        manifest.short_audio_fixture.channels,
        manifest.short_audio_fixture.sample_width_bytes,
        manifest.short_audio_fixture.frame_count,
    )
    if actual != expected:
        raise ValueError("short audio format/frame count mismatch")


def validate_harness_case_context(case: HarnessCase, context: Any) -> None:
    from .runbundle import EvidenceContextV1

    if not isinstance(case, HarnessCase) or not isinstance(context, EvidenceContextV1):
        raise TypeError("case and context must be parsed contracts")
    expected = canonical_contract_hash(context)
    if not hmac.compare_digest(case.base_evidence_context_hash, expected):
        raise ValueError("HarnessCase base EvidenceContext hash mismatch")


__all__ = [
    "DerivedAudioRecipe", "FixtureEntry", "FixtureManifest", "FrozenAudioFixture", "HarnessCase", "NamedDigest",
    "QueryDigestFixture", "SyntheticSnapshotRoot", "audit_fixture_manifest", "compute_fixture_manifest_hash",
    "compute_query_digests_hash", "compute_snapshot_id", "parse_fixture_manifest", "validate_query_digest_fixtures",
    "validate_harness_case_context",
]
