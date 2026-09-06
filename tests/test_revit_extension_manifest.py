"""Static validation for the Revit 2020 pyRevit source bundle."""

from scripts.sync_revit_ext import _validate_manifest


def test_revit_manifest_validates_entrypoint_and_contract():
    manifest = _validate_manifest()

    assert manifest["schema_version"] == "buildmate.pyrevit/1"
    assert "2020" in manifest["revit_versions"]
    assert manifest["delivery_contract"] == "buildmate.wall-model/1.0"
