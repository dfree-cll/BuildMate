from backend.engines.wall_iteration import inspect_wall_iteration


def _model():
    return {"model_elements": [{"type": "Wall", "id": "w1",
                                "start": [0, 0, 0], "end": [1000, 0, 0],
                                "thickness": 300, "confidence": 0.95}]}


def _build():
    return {"created": [{"kind": "Wall", "input_id": "w1",
                         "revit_element_id": 42}]}


def test_built_wall_needs_no_repair():
    dump = {"model_elements": [{"type": "Wall", "id": "wall_42",
                                "start": [0, 0, 0], "end": [1, 0, 0],
                                "thickness_m": 0.3}]}
    result = inspect_wall_iteration(_model(), _build(), dump)
    assert result["ready"] is True
    assert result["counts"] == {"built": 1}


def test_skewed_wall_generates_local_update():
    dump = {"model_elements": [{"type": "Wall", "id": "wall_42",
                                "start": [0, 0, 0], "end": [1, 0.1, 0],
                                "thickness_m": 0.3}]}
    result = inspect_wall_iteration(_model(), _build(), dump)
    assert result["counts"] == {"mismatch": 1}
    assert result["repairs"][0]["action"] == "update"
    assert "angle_mismatch" in result["repairs"][0]["reason"]


def test_missing_wall_generates_create():
    result = inspect_wall_iteration(_model(), _build(), {"model_elements": []})
    assert result["counts"] == {"missing": 1}
    assert result["repairs"][0]["action"] == "create"


def test_stale_revit_id_falls_back_to_geometry():
    dump = {"model_elements": [{"type": "Wall", "id": "wall_99",
                                "start": [0, 0, 0], "end": [1, 0, 0],
                                "thickness_m": 0.3}]}
    result = inspect_wall_iteration(_model(), _build(), dump)
    assert result["counts"] == {"built": 1}
    assert result["states"][0]["evidence"]["mapping_source"] == "geometry"
