import copy

import pytest

from backend.engines.wall_geometry import classify_dangling_endpoints


DRAWING_IDENTITY = "sha256:test-drawing"
STRUCTURAL_OCCURRENCE_ID = "structural_occurrence_test"


def _profile_wall(source_candidate_id="profile_approved"):
    return {
        "id": "profile_wall",
        "start": [0.0, 0.0],
        "end": [2.0, 0.0],
        "thickness": 100,
        "paired": True,
        "geometry_source": "DXF_CLOSED_WALL_STRIP",
        "source_candidate_id": source_candidate_id,
        "drawing_identity": DRAWING_IDENTITY,
        "source_occurrence_id": "source_occurrence_profile",
        "placed_entity_id": "placed_entity_profile",
        "structural_occurrence_id": STRUCTURAL_OCCURRENCE_ID,
    }


def _profile_end_cap(candidate_id, parent_profile_id, x):
    return {
        "candidate_id": candidate_id,
        "status": "RESOLVED_BY_PROFILE",
        "decision_reason": "approved_closed_wall_strip_end_cap",
        "profile_association_status": "EXACT",
        "geometry_source": "DXF_VECTOR",
        "identity_is_complete": True,
        "identity_limitations": [],
        "drawing_identity": DRAWING_IDENTITY,
        "source_occurrence_id": "source_occurrence_profile",
        "placed_entity_id": "placed_entity_profile",
        "structural_occurrence_id": STRUCTURAL_OCCURRENCE_ID,
        "source_segment_id": f"segment_{candidate_id}",
        "entity_handle": "PROFILE_HANDLE",
        "edge_role": "end_cap",
        "parent_profile_id": parent_profile_id,
        "start": [x, -0.05],
        "end": [x, 0.05],
        "unmapped_ranges": [{
            "start": [x, -0.05],
            "end": [x, 0.05],
            "length_m": 0.1,
            "opening_bridge_supported": False,
        }],
    }


def _strict_wall(*, paired=True):
    return {
        "id": "strict_wall",
        "start": [0.0, 0.0],
        "end": [2.0, 0.0],
        "thickness": 100,
        "paired": paired,
        "geometry_source": "DXF_VECTOR",
        "drawing_identity": DRAWING_IDENTITY,
        "structural_occurrence_id": STRUCTURAL_OCCURRENCE_ID,
        "source_segment_ids": ["face_a", "face_b"],
    }


def _strict_end_cap(*, identity_is_complete=True,
                    support_ids=("face_b", "face_a")):
    return {
        "candidate_id": "strict_cap",
        "status": "NEEDS_REVIEW",
        "decision_reason": "strict_topology_end_cap",
        "review_bucket": "STRICT_TOPOLOGY_END_CAP",
        "geometry_source": "DXF_VECTOR",
        "identity_is_complete": identity_is_complete,
        "identity_limitations": ([] if identity_is_complete else
                                 ["missing_identity"]),
        "drawing_identity": DRAWING_IDENTITY,
        "source_occurrence_id": "source_occurrence_cap",
        "placed_entity_id": "placed_entity_cap",
        "structural_occurrence_id": STRUCTURAL_OCCURRENCE_ID,
        "source_segment_id": "cap_segment",
        "entity_handle": "CAP_HANDLE",
        "start": [2.0, -0.05],
        "end": [2.0, 0.05],
        "unmapped_ranges": [{
            "start": [2.0, -0.05],
            "end": [2.0, 0.05],
            "length_m": 0.1,
            "opening_bridge_supported": False,
        }],
        "support_evidence": [
            {
                "drawing_identity": DRAWING_IDENTITY,
                "source_occurrence_id": f"source_occurrence_{source_id}",
                "placed_entity_id": f"placed_entity_{source_id}",
                "structural_occurrence_id": STRUCTURAL_OCCURRENCE_ID,
                "source_segment_id": source_id,
                "entity_handle": f"HANDLE_{source_id}",
            }
            for source_id in support_ids
        ],
        "strict_topology_evidence": {
            "support_count": len(support_ids),
        },
    }


def _coverage(*sources):
    return {
        "uncovered_source_segments": list(sources),
        "partially_mapped_source_segments": [],
    }


def test_approved_profile_end_caps_require_exact_parent_and_five_mm_distance():
    wall = _profile_wall()
    original_wall = copy.deepcopy(wall)
    caps = [
        _profile_end_cap("profile_cap_start", "profile_approved", 0.0),
        _profile_end_cap("profile_cap_end", "profile_approved", 2.0),
    ]

    review = classify_dangling_endpoints(
        [{"id": "profile_wall", "ends": [False, False]}],
        [wall], source_coverage=_coverage(*caps))

    assert [item["status"] for item in review["candidates"]] == [
        "RESOLVED_INTENTIONAL_CAP", "RESOLVED_INTENTIONAL_CAP"]
    assert {item["decision_reason"] for item in review["candidates"]} == {
        "approved_closed_wall_strip_end_cap_endpoint"}
    assert all(item["blockers"] == [] for item in review["candidates"])
    assert wall == original_wall

    mismatched = classify_dangling_endpoints(
        [{"id": "profile_wall", "ends": [False, False]}],
        [_profile_wall(source_candidate_id="another_profile")],
        source_coverage=_coverage(*caps))
    assert all(item["status"] == "NEEDS_REVIEW"
               for item in mismatched["candidates"])

    far_caps = [
        _profile_end_cap("profile_cap_start", "profile_approved", -0.0051),
        _profile_end_cap("profile_cap_end", "profile_approved", 2.0051),
    ]
    too_far = classify_dangling_endpoints(
        [{"id": "profile_wall", "ends": [False, False]}],
        [_profile_wall()], source_coverage=_coverage(*far_caps))
    assert all(item["status"] == "NEEDS_REVIEW"
               for item in too_far["candidates"])


def test_paired_wall_strict_cap_resolves_with_exact_complete_support_set():
    wall = _strict_wall()
    original_wall = copy.deepcopy(wall)

    review = classify_dangling_endpoints(
        [{"id": "strict_wall", "ends": [True, False]}],
        [wall], source_coverage=_coverage(_strict_end_cap()))

    assert review["candidate_count"] == 1
    candidate = review["candidates"][0]
    assert candidate["status"] == "RESOLVED_INTENTIONAL_CAP"
    assert candidate["blockers"] == []
    assert wall == original_wall


@pytest.mark.parametrize(
    ("paired", "identity_is_complete", "support_ids"),
    [
        (False, True, ("face_a", "face_b")),
        (True, True, ("face_a",)),
        (True, True, ("face_a", "wrong_face")),
        (True, False, ("face_a", "face_b")),
    ],
)
def test_strict_cap_keeps_unsafe_evidence_in_review(
        paired, identity_is_complete, support_ids):
    wall = _strict_wall(paired=paired)
    original_wall = copy.deepcopy(wall)

    review = classify_dangling_endpoints(
        [{"id": "strict_wall", "ends": [True, False]}],
        [wall], source_coverage=_coverage(_strict_end_cap(
            identity_is_complete=identity_is_complete,
            support_ids=support_ids)))

    candidate = review["candidates"][0]
    assert candidate["status"] == "NEEDS_REVIEW"
    assert "human_review_required" in candidate["blockers"]
    assert wall == original_wall


@pytest.mark.parametrize("invalid_support", ["missing_identity", "wrong_occurrence"])
def test_strict_cap_keeps_invalid_support_identity_in_review(invalid_support):
    wall = _strict_wall()
    original_wall = copy.deepcopy(wall)
    cap = _strict_end_cap()
    if invalid_support == "missing_identity":
        cap["support_evidence"][0].pop("placed_entity_id")
    else:
        cap["support_evidence"][0]["structural_occurrence_id"] = (
            "structural_occurrence_other")

    review = classify_dangling_endpoints(
        [{"id": "strict_wall", "ends": [True, False]}],
        [wall], source_coverage=_coverage(cap))

    candidate = review["candidates"][0]
    assert candidate["status"] == "NEEDS_REVIEW"
    assert "human_review_required" in candidate["blockers"]
    assert wall == original_wall


def test_endpoint_review_counts_resolved_caps_without_dropping_raw_candidates():
    walls = [
        _profile_wall(),
        {
            "id": "unresolved_wall",
            "start": [10.0, 0.0],
            "end": [12.0, 0.0],
            "thickness": 100,
            "paired": False,
        },
    ]
    original_walls = copy.deepcopy(walls)
    caps = [
        _profile_end_cap("profile_cap_start", "profile_approved", 0.0),
        _profile_end_cap("profile_cap_end", "profile_approved", 2.0),
    ]

    review = classify_dangling_endpoints(
        [
            {"id": "profile_wall", "ends": [False, False]},
            {"id": "unresolved_wall", "ends": [True, False]},
        ],
        walls, source_coverage=_coverage(*caps))

    assert review["candidate_count"] == 3
    assert len(review["candidates"]) == 3
    assert review["resolved_intentional_cap_count"] == 2
    assert review["needs_review_count"] == 1
    assert sum(item["status"] == "RESOLVED_INTENTIONAL_CAP"
               for item in review["candidates"]) == 2
    assert sum(item["status"] == "NEEDS_REVIEW"
               for item in review["candidates"]) == 1
    assert walls == original_walls
