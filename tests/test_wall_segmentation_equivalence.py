import math

import pytest

from backend.engines.wall_segmentation_equivalence import (
    prove_wall_segmentation_equivalence,
)


def _wall(start, end, thickness=300.0):
    return {
        "start": list(start),
        "end": list(end),
        "thickness": thickness,
    }


def _result(structure, architecture):
    return prove_wall_segmentation_equivalence(structure, architecture)


def test_proves_one_structure_to_many_architecture_split():
    result = _result(
        [_wall((0.0, 0.0), (10.0, 0.0))],
        [
            _wall((0.0, 0.0), (4.0, 0.0)),
            _wall((4.0, 0.0), (10.0, 0.0)),
        ],
    )

    assert result["unmatched_structure_indices"] == []
    assert result["unmatched_architecture_indices"] == []
    assert len(result["groups"]) == 1
    group = result["groups"][0]
    assert group["status"] == "PROVEN"
    assert group["relation"] == "ONE_STRUCTURE_TO_MANY_ARCHITECTURE"
    assert group["structure_indices"] == [0]
    assert group["architecture_indices"] == [0, 1]
    assert group["evidence"]["max_seam_gap_or_overlap_m"] == 0.0


def test_proves_many_structure_to_one_architecture_and_stable_id():
    structure = [
        _wall((0.0, 0.0), (3.0, 0.0)),
        _wall((3.0, 0.0), (10.0, 0.0)),
    ]
    architecture = [_wall((0.0, 0.0), (10.0, 0.0))]
    baseline = _result(structure, architecture)

    reordered_and_reversed = _result(
        [
            _wall((10.0, 0.0), (3.0, 0.0)),
            _wall((3.0, 0.0), (0.0, 0.0)),
        ],
        [_wall((10.0, 0.0), (0.0, 0.0))],
    )

    assert baseline["groups"][0]["relation"] == (
        "MANY_STRUCTURE_TO_ONE_ARCHITECTURE")
    assert baseline["groups"][0]["id"] == (
        reordered_and_reversed["groups"][0]["id"])


def test_accepts_submillimetre_normal_boundary_thickness_and_seam_overlap():
    result = _result(
        [_wall((0.0, 0.0), (10.0, 0.0), 300.0)],
        [
            _wall((0.0005, 0.0005), (5.0005, 0.0005), 300.8),
            _wall((4.9997, 0.0005), (9.9995, 0.0005), 300.8),
        ],
    )

    evidence = result["groups"][0]["evidence"]
    assert evidence["max_normal_residual_m"] == pytest.approx(0.0005)
    assert evidence["max_boundary_residual_m"] == pytest.approx(0.0005)
    assert evidence["max_seam_gap_or_overlap_m"] == pytest.approx(0.0008)
    assert evidence["max_thickness_delta_mm"] == pytest.approx(0.8)


def test_accepts_exact_one_millimetre_distance_and_thickness_limits():
    result = _result(
        [_wall((0.0, 0.0), (1.0, 0.0), 300.0)],
        [
            _wall((0.001, 0.001), (0.5, 0.001), 301.0),
            _wall((0.499, 0.001), (0.999, 0.001), 301.0),
        ],
    )

    evidence = result["groups"][0]["evidence"]
    assert evidence["max_normal_residual_m"] == pytest.approx(0.001)
    assert evidence["max_boundary_residual_m"] == pytest.approx(0.001)
    assert evidence["max_seam_gap_or_overlap_m"] == pytest.approx(0.001)
    assert evidence["max_thickness_delta_mm"] == pytest.approx(1.0)


@pytest.mark.parametrize(
    "architecture",
    [
        pytest.param(
            [
                _wall((0.0, 0.00101), (5.0, 0.00101)),
                _wall((5.0, 0.00101), (10.0, 0.00101)),
            ],
            id="normal-offset-over-1-mm",
        ),
        pytest.param(
            [
                _wall((0.0, 0.0), (5.0, 0.0), 301.01),
                _wall((5.0, 0.0), (10.0, 0.0), 301.01),
            ],
            id="thickness-difference-over-1-mm",
        ),
        pytest.param(
            [
                _wall((0.00101, 0.0), (5.0, 0.0)),
                _wall((5.0, 0.0), (10.0, 0.0)),
            ],
            id="boundary-residual-over-1-mm",
        ),
        pytest.param(
            [
                _wall((0.0, 0.0), (5.0, 0.0)),
                _wall((5.00101, 0.0), (10.0, 0.0)),
            ],
            id="seam-gap-over-1-mm",
        ),
        pytest.param(
            [
                _wall((0.0, 0.0), (5.00101, 0.0)),
                _wall((5.0, 0.0), (10.0, 0.0)),
            ],
            id="material-overlap-over-1-mm",
        ),
    ],
)
def test_rejects_distance_or_thickness_outside_tolerance(architecture):
    result = _result([_wall((0.0, 0.0), (10.0, 0.0))], architecture)

    assert result["groups"] == []
    assert result["unmatched_structure_indices"] == [0]
    assert result["unmatched_architecture_indices"] == [0, 1]


def test_rejects_angle_over_point_zero_one_degree():
    angle = math.radians(0.01001)

    def rise_at(x):
        return math.tan(angle) * x

    result = _result(
        [_wall((0.0, 0.0), (1.0, 0.0))],
        [
            _wall((0.0, 0.0), (0.5, rise_at(0.5))),
            _wall((0.5, rise_at(0.5)), (1.0, rise_at(1.0))),
        ],
    )

    assert result["groups"] == []
    assert result["unmatched_structure_indices"] == [0]
    assert result["unmatched_architecture_indices"] == [0, 1]


def test_rejects_member_without_more_than_one_millimetre_overlap():
    result = _result(
        [_wall((0.0, 0.0), (10.0, 0.0))],
        [
            _wall((0.0, 0.0), (0.001, 0.0)),
            _wall((0.001, 0.0), (10.0, 0.0)),
        ],
    )

    assert result["groups"] == []
    assert result["unmatched_structure_indices"] == [0]
    assert result["unmatched_architecture_indices"] == [0, 1]


def test_rejects_ambiguous_candidate_graph_t_branch():
    # Both structure members overlap architecture member 0.  The resulting
    # component is 2-to-2, not a unique one-to-many star.
    result = _result(
        [
            _wall((0.0, 0.0), (10.0, 0.0)),
            _wall((0.0, 0.0), (5.0, 0.0)),
        ],
        [
            _wall((0.0, 0.0), (5.0, 0.0)),
            _wall((5.0, 0.0), (10.0, 0.0)),
        ],
    )

    assert result["groups"] == []
    assert result["unmatched_structure_indices"] == [0, 1]
    assert result["unmatched_architecture_indices"] == [0, 1]


def test_rejects_duplicate_member_as_material_overlap():
    result = _result(
        [_wall((0.0, 0.0), (10.0, 0.0))],
        [
            _wall((0.0, 0.0), (5.0, 0.0)),
            _wall((5.0, 0.0), (10.0, 0.0)),
            _wall((2.0, 0.0), (3.0, 0.0)),
        ],
    )

    assert result["groups"] == []
    assert result["unmatched_structure_indices"] == [0]
    assert result["unmatched_architecture_indices"] == [0, 1, 2]


def test_leaves_one_to_one_match_unproven():
    result = _result(
        [_wall((0.0, 0.0), (10.0, 0.0))],
        [_wall((0.0, 0.0), (10.0, 0.0))],
    )

    assert result["groups"] == []
    assert result["unmatched_structure_indices"] == [0]
    assert result["unmatched_architecture_indices"] == [0]


@pytest.mark.parametrize(
    ("structure", "architecture"),
    [
        ([{"start": [0.0, 0.0], "end": [0.0, 0.0], "thickness": 300}], []),
        ([{"start": [0.0], "end": [1.0, 0.0], "thickness": 300}], []),
        ([{"start": [0.0, 0.0], "end": [1.0, 0.0], "thickness": 0}], []),
        ([{"start": [0.0, 0.0], "end": [1.0, float("nan")],
           "thickness": 300}], []),
        ("not-a-list", []),
    ],
)
def test_rejects_invalid_wall_views(structure, architecture):
    with pytest.raises(ValueError):
        _result(structure, architecture)
