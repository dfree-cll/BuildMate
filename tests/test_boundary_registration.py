from backend.engines.boundary_columns import register_profiles_to_walls


def test_registers_profile_to_nearest_matching_wall_corner():
    walls = [
        {"start": [1000, 1000], "end": [3000, 1000], "thickness": 200},
        {"start": [1000, 1000], "end": [1000, 3000], "thickness": 200},
    ]
    columns = [{
        "id": "GBZ1", "x": 5000, "y": 5000,
        "profile": {"kind": "poly", "points": [
            [-100, -100], [2000, -100], [2000, 100],
            [100, 100], [100, 2000], [-100, 2000],
        ]},
    }]

    result = register_profiles_to_walls(
        columns, walls, resolution_mm=25, max_shift_mm=5000,
        min_overlap=0.8)

    assert len(result) == 1
    assert abs(result[0]["x"] - 1000) <= 25
    assert abs(result[0]["y"] - 1000) <= 25
    assert result[0]["registration"]["overlap"] >= 0.8


def test_rejects_profile_without_wall_shape_match():
    columns = [{
        "id": "GBZ1", "x": 0, "y": 0,
        "profile": {"kind": "poly", "points": [
            [-500, -500], [500, -500], [500, 500], [-500, 500],
        ]},
    }]
    walls = [{"start": [5000, 0], "end": [7000, 0], "thickness": 100}]

    assert register_profiles_to_walls(
        columns, walls, max_shift_mm=1000, min_overlap=0.8) == []


def test_profiles_in_one_detail_keep_their_relative_spacing():
    walls = [
        {"start": [1000, 1000], "end": [3000, 1000], "thickness": 200},
        {"start": [6000, 1000], "end": [8000, 1000], "thickness": 200},
    ]
    shape = {"kind": "poly", "points": [
        [-100, -100], [2000, -100], [2000, 100], [-100, 100],
    ]}
    columns = [
        {"id": "a", "x": 1000, "y": 5000, "profile": shape},
        {"id": "b", "x": 6000, "y": 5000, "profile": shape},
    ]

    result = register_profiles_to_walls(
        columns, walls, resolution_mm=25, max_shift_mm=5000,
        min_overlap=0.8)

    assert len(result) == 2
    by_id = {item["id"]: item for item in result}
    assert by_id["b"]["x"] - by_id["a"]["x"] == 5000
    assert by_id["a"]["registration"]["offset_mm"] == \
        by_id["b"]["registration"]["offset_mm"]
