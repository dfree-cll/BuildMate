import json
from pathlib import Path

import pytest

from backend.engines.drawing_audit_cache import (
    audit_cache_key,
    build_audit_cache_identity,
    capture_environment,
    read_audit_cache,
    write_audit_cache,
)


def _identity(tmp_path: Path, **overrides):
    source = tmp_path / "hotel_b01.dxf"
    code = tmp_path / "audit.py"
    model = tmp_path / "walls.pt"
    if not source.exists():
        source.write_bytes(b"current hotel drawing")
    if not code.exists():
        code.write_text("AUDIT_VERSION = 1\n", encoding="utf-8")
    if not model.exists():
        model.write_bytes(b"current yolo weights")
    arguments = {
        "code_paths": [code],
        "yolo_model_path": model,
        "cv_params": {"threshold": 180, "tile": [512, 512]},
        "yolo_params": {"confidence": 0.4},
        "dependency_versions": {"opencv-python": "5.0", "ultralytics": "8.4"},
        "environment_config": {"DRAWING_FULL_CV_AUDIT": "1"},
    }
    arguments.update(overrides)
    return build_audit_cache_identity(source, **arguments)


def test_identity_covers_every_decision_input(tmp_path):
    original = _identity(tmp_path)
    source = original["source"]

    assert Path(source["path"]).is_absolute()
    assert len(source["sha256"]) == 64
    assert len(original["code"]["fingerprint"]) == 64
    assert len(original["yolo_model"]["sha256"]) == 64
    assert original["parameters"]["cv"]["threshold"] == 180
    assert original["dependencies"]["opencv-python"] == "5.0"
    assert original["environment"]["DRAWING_FULL_CV_AUDIT"] != "1"

    variants = [
        _identity(tmp_path, cv_params={"threshold": 181}),
        _identity(tmp_path, yolo_params={"confidence": 0.5}),
        _identity(tmp_path, dependency_versions={"opencv-python": "5.1"}),
        _identity(tmp_path, environment_config={"DRAWING_FULL_CV_AUDIT": "0"}),
    ]
    assert all(audit_cache_key(item) != audit_cache_key(original) for item in variants)


def test_code_source_and_model_content_change_identity(tmp_path):
    original = _identity(tmp_path)

    (tmp_path / "audit.py").write_text("AUDIT_VERSION = 2\n", encoding="utf-8")
    changed_code = _identity(tmp_path)
    (tmp_path / "hotel_b01.dxf").write_bytes(b"new drawing")
    changed_source = _identity(tmp_path)
    (tmp_path / "walls.pt").write_bytes(b"new weights")
    changed_model = _identity(tmp_path)

    keys = {
        audit_cache_key(original), audit_cache_key(changed_code),
        audit_cache_key(changed_source), audit_cache_key(changed_model),
    }
    assert len(keys) == 4


def test_atomic_round_trip_validates_artifact_content(tmp_path):
    identity = _identity(tmp_path)
    artifact = tmp_path / "wall_review.png"
    artifact.write_bytes(b"review pixels")
    cache_dir = tmp_path / "cache"

    cache_path = write_audit_cache(
        cache_dir,
        identity,
        {"status": "PASS", "wall_candidates": 109},
        {"wall_review": artifact},
    )

    assert cache_path is not None and cache_path.is_file()
    assert not list(cache_dir.glob("*.tmp"))
    hit = read_audit_cache(cache_dir, identity)
    assert hit["evidence"]["wall_candidates"] == 109
    assert hit["artifacts"]["wall_review"] == str(artifact.resolve())

    artifact.write_bytes(b"changed pixels")
    assert read_audit_cache(cache_dir, identity) is None


def test_corrupt_record_and_missing_artifact_are_cache_misses(tmp_path):
    identity = _identity(tmp_path)
    artifact = tmp_path / "review.json"
    artifact.write_text("{}", encoding="utf-8")
    cache_dir = tmp_path / "cache"
    cache_path = write_audit_cache(
        cache_dir, identity, {"status": "PASS"}, {"review": artifact})

    artifact.unlink()
    assert read_audit_cache(cache_dir, identity) is None

    cache_path.write_text("{broken", encoding="utf-8")
    assert read_audit_cache(cache_dir, identity) is None


def test_error_result_invalidates_existing_hit(tmp_path):
    identity = _identity(tmp_path)
    artifact = tmp_path / "review.png"
    artifact.write_bytes(b"ok")
    cache_dir = tmp_path / "cache"
    write_audit_cache(
        cache_dir, identity, {"inference_status": "OK"}, {"review": artifact})
    assert read_audit_cache(cache_dir, identity) is not None

    result = write_audit_cache(
        cache_dir,
        identity,
        {"inference_status": "ERROR", "message": "worker stopped"},
        {"review": artifact},
    )

    assert result is None
    assert read_audit_cache(cache_dir, identity) is None


@pytest.mark.parametrize("status", ["ERROR", "FAILED", "TIMEOUT"])
def test_incomplete_execution_status_is_always_a_miss(tmp_path, status):
    identity = _identity(tmp_path)
    artifact = tmp_path / "review.png"
    artifact.write_bytes(b"partial")

    result = write_audit_cache(
        tmp_path / "cache",
        identity,
        {"audit_status": status},
        {"review": artifact},
    )

    assert result is None
    assert read_audit_cache(tmp_path / "cache", identity) is None


@pytest.mark.parametrize(
    "evidence",
    [
        {"gate_approved": True},
        {"revit_execution": {"status": "OK"}},
        {"artifact": r"F:\\runtime\\revit\\result.json"},
        {"artifact": r"F:\\runtime\\model.json"},
    ],
)
def test_modeling_authorization_and_execution_are_never_cached(tmp_path, evidence):
    identity = _identity(tmp_path)
    artifact = tmp_path / "review.png"
    artifact.write_bytes(b"ok")

    with pytest.raises(ValueError):
        write_audit_cache(tmp_path / "cache", identity, evidence, {"review": artifact})


def test_model_json_and_revit_artifacts_are_rejected(tmp_path):
    identity = _identity(tmp_path)
    model_json = tmp_path / "model.json"
    model_json.write_text(json.dumps({"walls": []}), encoding="utf-8")

    with pytest.raises(ValueError):
        write_audit_cache(
            tmp_path / "cache", identity, {"status": "PASS"}, {"model": model_json})

    harmless_file = tmp_path / "result.json"
    harmless_file.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError):
        write_audit_cache(
            tmp_path / "cache",
            identity,
            {"status": "PASS"},
            {"revit_result": harmless_file},
        )


def test_environment_capture_distinguishes_unset_from_empty():
    captured = capture_environment(
        ["DRAWING_YOLO_REQUIRED", "DRAWING_FULL_CV_AUDIT"],
        {"DRAWING_YOLO_REQUIRED": ""},
    )

    assert captured == {
        "DRAWING_FULL_CV_AUDIT": None,
        "DRAWING_YOLO_REQUIRED": "",
    }
