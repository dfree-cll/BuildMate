import ast
from pathlib import Path


def _geometry_namespace():
    source = Path(__file__).parents[1] / "scripts" / "revit_transient_wall_preview.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    definitions = [
        node for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.Assign, ast.FunctionDef))
    ]
    namespace = {}
    exec(compile(ast.Module(body=definitions, type_ignores=[]), str(source), "exec"), namespace)
    return namespace


GEOMETRY = _geometry_namespace()


def _wall(identifier, start, end, thickness=200):
    return {
        "id": identifier,
        "start": [float(start[0]), float(start[1]), 0.0],
        "end": [float(end[0]), float(end[1]), 0.0],
        "thickness": thickness,
    }


def test_opening_sized_gap_becomes_one_continuous_host_wall():
    cleaned = GEOMETRY["clean_wall_segments"]([
        _wall("left", (0, 0), (2000, 0)),
        _wall("right", (3000, 0), (5000, 0)),
    ])

    assert len(cleaned) == 1
    assert cleaned[0]["start"][:2] == [0.0, 0.0]
    assert cleaned[0]["end"][:2] == [5000.0, 0.0]


def test_t_and_l_endpoints_snap_to_exact_centerline_intersections():
    main = _wall("main", (0, 0), (2000, 0), 250)
    t_branch = _wall("t", (1000, 125), (1000, 1000), 250)
    l_branch = _wall("l", (2125, 0), (2125, 1000), 250)

    GEOMETRY["_snap_wall_junctions"]([main, t_branch, l_branch])

    assert t_branch["start"][:2] == [1000.0, 0.0]
    assert main["end"][:2] == [2125.0, 0.0]
    assert l_branch["start"][:2] == [2125.0, 0.0]


def test_structural_wall_replaces_only_its_architecture_overlap():
    architecture = [_wall("arch", (0, 0), (10000, 0), 200)]
    structural = [_wall("struct", (3000, 150), (7000, 150), 500)]

    residuals = GEOMETRY["_subtract_structural_overlaps"](
        architecture, structural)

    assert len(residuals) == 2
    assert residuals[0]["start"][:2] == [0.0, 0.0]
    assert residuals[0]["end"][:2] == [3000.0, 0.0]
    assert residuals[1]["start"][:2] == [7000.0, 0.0]
    assert residuals[1]["end"][:2] == [10000.0, 0.0]


def test_offset_centerline_duplicate_is_removed_by_solid_overlap():
    walls = [
        _wall("primary", (0, 0), (10000, 0), 250),
        _wall("duplicate", (3000, 125), (5000, 125), 100),
    ]

    resolved = GEOMETRY["_resolve_parallel_wall_overlaps"](walls)

    assert len(resolved) == 1
    assert resolved[0]["id"] == "primary"


def test_connected_near_vertical_chain_gets_one_exact_axis():
    lower = _wall("lower", (100.0, 0), (100.5, 1000), 250)
    upper = _wall("upper", (100.5, 1000), (101.0, 2000), 250)

    GEOMETRY["_flatten_connected_axis_chains"]([lower, upper])

    assert lower["start"][0] == lower["end"][0]
    assert upper["start"][0] == upper["end"][0]
    assert lower["start"][0] == upper["start"][0]


def test_parallel_offset_transition_disables_only_facing_join_ends():
    lower = _wall("lower", (0, 0), (0, 1000), 250)
    upper = _wall("upper", (25, 1200), (25, 3000), 300)

    GEOMETRY["_mark_parallel_transition_joins"]([lower, upper])

    assert lower["disallow_join_ends"] == [1]
    assert upper["disallow_join_ends"] == [0]


def test_pdf_primary_inputs_split_architecture_and_structure():
    result = GEOMETRY["_pdf_primary_wall_inputs"]({
        "grid": {"x_axes": [{"coord": 0}], "y_axes": [{"coord": 0}]},
        "model_elements": [
            {"type": "Wall", "id": "a", "start": [0, 0], "end": [1000, 0], "wall_group": "A"},
            {"type": "Wall", "id": "s", "start": [0, 0], "end": [1000, 0], "wall_group": "S"},
        ],
    })

    assert result is not None
    architecture, structural, grid = result
    assert [item["id"] for item in architecture] == ["a"]
    assert [item["id"] for item in structural] == ["s"]
    assert grid["x_axes"] == [{"coord": 0}]


def test_pdf_primary_inputs_reject_empty_reference():
    assert GEOMETRY["_pdf_primary_wall_inputs"]({"model_elements": []}) is None
