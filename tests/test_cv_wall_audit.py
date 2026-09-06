import cv2
import numpy as np

from scripts.cv_wall_audit import audit, extract_wall_centerlines


def test_extracts_horizontal_wall_centerline():
    walls = extract_wall_centerlines([
        [10, 20, 110, 20],
        [10, 30, 110, 30],
    ])
    assert len(walls) == 1
    assert walls[0]["axis"] == "H"
    assert walls[0]["start_px"] == [10.0, 25.0]
    assert walls[0]["end_px"] == [110.0, 25.0]
    assert walls[0]["thickness_px"] == 10.0


def test_extracts_vertical_wall_centerline():
    walls = extract_wall_centerlines([
        [40, 5, 40, 105],
        [48, 5, 48, 105],
    ])
    assert len(walls) == 1
    assert walls[0]["axis"] == "V"
    assert walls[0]["start_px"] == [44.0, 5.0]
    assert walls[0]["end_px"] == [44.0, 105.0]
    assert walls[0]["thickness_px"] == 8.0


def test_rejects_unreasonably_wide_pair():
    walls = extract_wall_centerlines([
        [0, 10, 100, 10],
        [0, 80, 100, 80],
    ])
    assert walls == []


def test_audit_ignores_one_pixel_raster_staircase(tmp_path):
    image = np.zeros((160, 240, 3), dtype=np.uint8)
    cv2.line(image, (20, 80), (220, 81), (255, 255, 255), 1)
    source = tmp_path / "source.png"
    overlay = tmp_path / "overlay.png"
    cv2.imwrite(str(source), image)

    result = audit(source, overlay, min_line_length_px=50)

    assert result["skewed_count"] == 0


def test_audit_report_with_real_skew_is_json_serializable(tmp_path):
    image = np.zeros((160, 240, 3), dtype=np.uint8)
    cv2.line(image, (20, 80), (220, 90), (255, 255, 255), 1)
    source = tmp_path / "source.png"
    overlay = tmp_path / "overlay.png"
    cv2.imwrite(str(source), image)

    result = audit(source, overlay, min_line_length_px=50)

    assert result["skewed_count"] > 0
    __import__("json").dumps(result)
