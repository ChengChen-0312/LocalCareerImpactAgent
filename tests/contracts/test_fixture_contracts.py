import io
import sys
import wave
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

try:
    from localcareerimpact.contracts.canonical import canonical_json_bytes, sha256_uri
    from localcareerimpact.contracts.fixtures import (
        DerivedAudioRecipe,
        FixtureEntry,
        FixtureManifest,
        FrozenAudioFixture,
        HarnessCase,
        NamedDigest,
        QueryDigestFixture,
        SyntheticSnapshotRoot,
        audit_fixture_manifest,
        compute_snapshot_id,
        validate_harness_case_context,
        validate_query_digest_fixtures,
    )
    from localcareerimpact.contracts.query import SnapshotBoundQuery
except ModuleNotFoundError:
    pytest.fail("PHASE0_RED_T03_FACTPACK_CONTRACTS_ABSENT", pytrace=False)

from ._task3_helpers import build_valid_graph, digest, registry_view


def wav_bytes() -> bytes:
    stream = io.BytesIO()
    with wave.open(stream, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x00" * 16000)
    return stream.getvalue()


def valid_manifest(tmp_path):
    pack, closure, _ = build_valid_graph()
    query = pack.queries[0]
    bound = SnapshotBoundQuery(snapshot_id=pack.snapshot_id, registry_bundle_hash=closure.registry_bundle_hash, query=query.normalized_params)
    fixture = QueryDigestFixture(query_id=query.query_id, bound_query=bound, result=closure.results[0])
    raw_wav = wav_bytes()
    raw_queries = canonical_json_bytes((fixture.model_dump(mode="json"),))
    (tmp_path / "short_en.wav").write_bytes(raw_wav)
    (tmp_path / "query_result_digests.json").write_bytes(raw_queries)
    audio = FrozenAudioFixture(
        relative_path="short_en.wav", selected_voice="Samantha", macos_build="fixed",
        sample_rate=16000, channels=1, sample_width_bytes=2, frame_count=16000,
        content_hash=sha256_uri(raw_wav),
    )
    recipe = DerivedAudioRecipe(
        source_path="short_en.wav", source_hash=audio.content_hash, algorithm_version="repeat-v1",
        sample_rate=16000, channels=1, sample_width_bytes=2, output_samples=14400000,
        output_hash=digest("derived"),
    )
    entries = (
        FixtureEntry(relative_path="short_en.wav", content_hash=audio.content_hash, byte_size=len(raw_wav)),
        FixtureEntry(relative_path="query_result_digests.json", content_hash=sha256_uri(raw_queries), byte_size=len(raw_queries)),
    )
    manifest = FixtureManifest(snapshot_id=pack.snapshot_id, entries=entries, query_digests_hash=sha256_uri(raw_queries), short_audio_fixture=audio, derived_audio_recipe=recipe)
    return manifest, pack, closure


def test_synthetic_root_has_one_acyclic_snapshot_digest():
    root = SyntheticSnapshotRoot(
        import_capsule_root=digest("capsule"),
        builder_source_hash=digest("builder"),
        environment_lock_hashes=(),
        config_hashes=(),
        schema_digest=digest("schema"),
        logical_export_digests=(),
        vector_tensor_hash=digest("tensor"),
        vector_metadata_digest=digest("metadata"),
        writer_settings_hash=digest("writer"),
        locale="C",
        timezone="UTC",
        random_seed=7,
    )
    assert compute_snapshot_id(root) == sha256_uri(canonical_json_bytes(root))
    for extra in ("snapshot_id", "snapshot_root_hash", "manifest_hash", "receipt_hash"):
        with pytest.raises((ValidationError, ValueError)):
            SyntheticSnapshotRoot(**{**root.model_dump(), extra: digest(extra)})


@pytest.mark.parametrize("path", ["/abs.json", "../escape.json", "a/../b.json", "a\\b.json", "./x.json"])
def test_fixture_paths_are_normalized_posix_relative(path):
    with pytest.raises((ValidationError, ValueError)):
        FixtureEntry(relative_path=path, content_hash=digest("content"), byte_size=1)


def test_manifest_rejects_duplicate_and_casefold_colliding_paths():
    first = FixtureEntry(relative_path="A.json", content_hash=digest("a"), byte_size=1)
    second = FixtureEntry(relative_path="a.json", content_hash=digest("b"), byte_size=1)
    audio = FrozenAudioFixture(
        relative_path="short_en.wav", selected_voice="Samantha", macos_build="fixed",
        sample_rate=16000, channels=1, sample_width_bytes=2, frame_count=16000,
        content_hash=digest("wav"),
    )
    recipe = DerivedAudioRecipe(
        source_path="short_en.wav", source_hash=audio.content_hash, algorithm_version="repeat-v1",
        sample_rate=16000, channels=1, sample_width_bytes=2, output_samples=14400000,
        output_hash=digest("derived"),
    )
    entries = (
        first, second,
        FixtureEntry(relative_path="short_en.wav", content_hash=audio.content_hash, byte_size=1),
        FixtureEntry(relative_path="query_result_digests.json", content_hash=digest("q"), byte_size=1),
    )
    with pytest.raises(ValueError, match="case-fold"):
        FixtureManifest(snapshot_id=digest("snapshot"), entries=entries, query_digests_hash=digest("q"), short_audio_fixture=audio, derived_audio_recipe=recipe)


def test_query_digest_fixture_is_exact_closure_mirror():
    pack, closure, _ = build_valid_graph()
    query = pack.queries[0]
    bound = SnapshotBoundQuery(snapshot_id=pack.snapshot_id, registry_bundle_hash=closure.registry_bundle_hash, query=query.normalized_params)
    fixture = QueryDigestFixture(query_id="Q1", bound_query=bound, result=closure.results[0])
    validate_query_digest_fixtures((fixture,), pack, closure, registry_view())
    with pytest.raises((ValidationError, ValueError)):
        QueryDigestFixture(query_id="other", bound_query=bound, result=closure.results[0])


def test_fixture_contracts_reject_float_and_extra_fields():
    with pytest.raises((ValidationError, ValueError, TypeError)):
        HarnessCase(
            case_id="small",
            intent_id="intent-small",
            base_evidence_context_hash=digest("context"),
            filler_seed=1,
            main_total_target=1.5,
            main_output_cap=100,
            review_total_target=100,
            review_output_cap=50,
            passage_count=1,
            image_token_allowance=0,
            mode="small",
        )


def test_named_digest_arrays_sort_and_reject_duplicates():
    values = (NamedDigest(name="z", digest=digest("z")), NamedDigest(name="a", digest=digest("a")))
    root = SyntheticSnapshotRoot(
        import_capsule_root=digest("capsule"), builder_source_hash=digest("builder"),
        environment_lock_hashes=values, config_hashes=(), schema_digest=digest("schema"),
        logical_export_digests=(), vector_tensor_hash=digest("tensor"),
        vector_metadata_digest=digest("meta"), writer_settings_hash=digest("writer"),
        locale="C", timezone="UTC", random_seed=0,
    )
    assert [item.name for item in root.environment_lock_hashes] == ["a", "z"]
    with pytest.raises((ValidationError, ValueError)):
        SyntheticSnapshotRoot(**{**root.model_dump(), "environment_lock_hashes": (values[0], values[0])})


def test_frozen_audio_contract_has_fixed_path_and_integer_rational_duration_inputs():
    audio = FrozenAudioFixture(
        relative_path="short_en.wav", selected_voice="Samantha", macos_build="fixed",
        sample_rate=16000, channels=1, sample_width_bytes=2, frame_count=16000,
        content_hash=digest("wav"),
    )
    assert audio.frame_count / audio.sample_rate == 1
    with pytest.raises((ValidationError, ValueError)):
        FrozenAudioFixture(**{**audio.model_dump(), "relative_path": "other.wav"})


def test_manifest_audit_recomputes_raw_byte_hashes_and_wav_shape(tmp_path):
    manifest, pack, closure = valid_manifest(tmp_path)
    with pytest.raises(ValueError):
        audit_fixture_manifest(manifest, tmp_path)
    with pytest.raises(ValueError):
        audit_fixture_manifest(manifest, tmp_path, pack=pack, closure=closure)
    audit_fixture_manifest(manifest, tmp_path, pack=pack, closure=closure, snapshot_registry_view=registry_view())
    (tmp_path / "short_en.wav").write_bytes(b"tampered")
    with pytest.raises(ValueError):
        audit_fixture_manifest(manifest, tmp_path, pack=pack, closure=closure, snapshot_registry_view=registry_view())


def test_manifest_audit_rejects_unlisted_artifacts(tmp_path):
    manifest, pack, closure = valid_manifest(tmp_path)
    (tmp_path / "extra.txt").write_text("extra")
    with pytest.raises(ValueError):
        audit_fixture_manifest(manifest, tmp_path, pack=pack, closure=closure, snapshot_registry_view=registry_view())


def test_harness_case_context_hash_is_recomputed_from_context():
    _, _, context = build_valid_graph()
    case = HarnessCase(
        case_id="small", intent_id="intent-small", base_evidence_context_hash=digest("wrong"),
        filler_seed=1, main_total_target=100, main_output_cap=20,
        review_total_target=50, review_output_cap=10, passage_count=1,
        image_token_allowance=0, mode="small",
    )
    with pytest.raises(ValueError):
        validate_harness_case_context(case, context)
    with pytest.raises((ValidationError, ValueError)):
        DerivedAudioRecipe(
            source_path="short_en.wav",
            source_hash=digest("source"),
            algorithm_version="v1",
            sample_rate=16000,
            channels=1,
            sample_width_bytes=2,
            output_samples=14400000,
            output_hash=digest("out"),
            duration=900,
        )
