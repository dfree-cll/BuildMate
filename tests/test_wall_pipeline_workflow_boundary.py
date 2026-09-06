import hashlib
from pathlib import Path

import pytest

from backend.application.wall_pipeline_workflow import (
    _require_local_bridge_artifact,
    _runtime_dir,
)
from backend.domain.errors import DependencyFailure


def test_bridge_artifact_boundary_accepts_only_readable_expected_suffix(tmp_path: Path):
    output = tmp_path / "working_copy.rvt"
    output.write_bytes(b"fixture-rvt")

    resolved = _require_local_bridge_artifact(
        str(output), suffix=".rvt", artifact_name="RVT working copy"
    )

    assert resolved == output.resolve()


def test_bridge_artifact_boundary_rejects_readable_path_outside_configured_root(
    tmp_path: Path,
):
    root = tmp_path / "bridge"
    root.mkdir()
    outside = tmp_path / "outside.rvt"
    outside.write_bytes(b"fixture")

    with pytest.raises(DependencyFailure, match="outside"):
        _require_local_bridge_artifact(
            str(outside),
            suffix=".rvt",
            artifact_name="RVT working copy",
            allowed_root=root,
        )


@pytest.mark.parametrize(
    ("value", "suffix", "artifact_name", "message"),
    [
        (None, ".rvt", "RVT working copy", "shared path"),
        ("working_copy.rvt", ".rvt", "RVT working copy", "relative"),
        ("Z:/bridge-host/working_copy.rvt", ".rvt", "RVT working copy", "host-local"),
        ("C:/bridge-host/actual.txt", ".png", "actual plan-view PNG", "suffix"),
    ],
)
def test_bridge_artifact_boundary_explains_unreadable_host_local_paths(
    value: str | None,
    suffix: str,
    artifact_name: str,
    message: str,
):
    with pytest.raises(DependencyFailure, match=message):
        _require_local_bridge_artifact(
            value, suffix=suffix, artifact_name=artifact_name
        )


def test_runtime_workspace_names_do_not_collide_after_sanitization():
    first = _runtime_dir("task/a")
    second = _runtime_dir("taska")

    assert first != second
    assert first.name.endswith("-" + hashlib.sha256(b"task/a").hexdigest()[:16])
