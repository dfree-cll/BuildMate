import hashlib

import ezdxf

from scripts.bim_pipeline import pipeline_std
from scripts.bim_pipeline.pipeline_std import step0_clean as base_step0_clean
from scripts.geometry_first_clean import drawing_identity, walk_wall_evidence


DRAWING_ID = "sha256:" + "a" * 64


def _duplicate_wall_plan():
    document = ezdxf.new()
    wall_unit = document.blocks.new("WALL_UNIT")
    wall_unit.add_line((0, 0), (2000, 0),
                       dxfattribs={"layer": "A-WALL"})
    plan = document.blocks.new("B1层平面图")
    plan.add_blockref("WALL_UNIT", (1000, 2000))
    plan.add_blockref("WALL_UNIT", (1000, 2000))
    root = document.modelspace().add_blockref(
        "B1层平面图", (10000, 20000))
    return document, root


def test_duplicate_insert_paths_have_distinct_stable_placed_ids():
    _document, root = _duplicate_wall_plan()

    first = list(walk_wall_evidence([root], drawing_id=DRAWING_ID))
    second = list(walk_wall_evidence([root], drawing_id=DRAWING_ID))

    assert len(first) == 2
    assert [item[2] for item in first] == [item[2] for item in second]
    assert {
        (item[0].dxf.start.x, item[0].dxf.start.y,
         item[0].dxf.end.x, item[0].dxf.end.y)
        for item in first
    } == {(11000.0, 22000.0, 13000.0, 22000.0)}

    provenance = [item[2] for item in first]
    assert len({item["source_entity_handle"] for item in provenance}) == 1
    assert len({item["source_occurrence_id"] for item in provenance}) == 2
    assert len({item["placed_entity_id"] for item in provenance}) == 2
    assert len({item["segment_id"] for item in provenance}) == 2
    assert all(item["drawing_identity"] == DRAWING_ID for item in provenance)
    assert all(len(item["placement_path"]) == 2 for item in provenance)
    assert all(
        step["insert_handle"] and step["block_name"] and
        step["cumulative_transform"]["affine_2d"]
        for item in provenance for step in item["placement_path"]
    )
    assert provenance[0]["placement_path"][0]["insert_handle"] == (
        provenance[1]["placement_path"][0]["insert_handle"])
    assert provenance[0]["placement_path"][1]["insert_handle"] != (
        provenance[1]["placement_path"][1]["insert_handle"])


def test_multi_insert_path_keeps_row_column_identity():
    document = ezdxf.new()
    wall_unit = document.blocks.new("WALL_UNIT")
    wall_unit.add_line((0, 0), (1000, 0),
                       dxfattribs={"layer": "A-WALL"})
    plan = document.blocks.new("B1层平面图")
    plan.add_blockref("WALL_UNIT", (1000, 2000)).grid(
        size=(2, 2), spacing=(3000, 4000))
    root = document.modelspace().add_blockref("B1层平面图", (0, 0))

    records = list(walk_wall_evidence([root], drawing_id=DRAWING_ID))

    assert len(records) == 4
    array_indices = [item[2]["placement_path"][1]["array_index"]
                     for item in records]
    assert [(item["row"], item["column"], item["linear"])
            for item in array_indices] == [
                (0, 0, 0), (0, 1, 1), (1, 0, 2), (1, 1, 3)]
    assert len({item[2]["source_occurrence_id"] for item in records}) == 4
    assert len({item[2]["placed_entity_id"] for item in records}) == 4
    # One MINSERT entity handle is sufficient because the array index is part
    # of the ordered physical path.
    assert len({item[2]["placement_path"][1]["insert_handle"]
                for item in records}) == 1


def test_step0_separates_full_placement_evidence_from_modeling_geometry():
    document, _root = _duplicate_wall_plan()

    cleaned = pipeline_std.step0_clean(document)

    assert len(cleaned["placed_wall_source_records"]) == 2
    assert len(cleaned["wall_source_records"]) == 1
    survivor = cleaned["wall_source_records"][0][2]
    assert len(survivor["equivalent_placements"]) == 1
    placed_ids = {
        record[2]["placed_entity_id"]
        for record in cleaned["placed_wall_source_records"]
    }
    assert placed_ids == {
        survivor["placed_entity_id"],
        survivor["equivalent_placements"][0]["placed_entity_id"],
    }


def test_step0_accepts_structural_plan_and_excludes_unplaced_wall_lines():
    document = ezdxf.new()
    plan = document.blocks.new("B1层墙柱(-6.5~-0.1m)")
    plan.add_line((0, 1000), (4000, 1000),
                  dxfattribs={"layer": "S-WALL"})
    plan.add_line((0, 1300), (4000, 1300),
                  dxfattribs={"layer": "S-WALL"})
    root = document.modelspace().add_blockref(
        "B1层墙柱(-6.5~-0.1m)", (10000, 20000))
    # Direct modelspace wall lines do not belong to the selected placed plan
    # and must not be mixed into its modeling evidence.
    document.modelspace().add_line(
        (90000, 90000), (94000, 90000),
        dxfattribs={"layer": "S-WALL"})
    document.modelspace().add_line(
        (90000, 90300), (94000, 90300),
        dxfattribs={"layer": "S-WALL"})

    cleaned = base_step0_clean(document)

    assert cleaned["plan_selection"]["selection_mode"] == (
        "structural_wall_column_block")
    assert cleaned["plan_selection"]["selected"]["insert_handle"] == (
        root.dxf.handle)
    assert cleaned["structural_source_entities"] == 2
    assert len(cleaned["placed_wall_source_records"]) == 2
    assert len(cleaned["wall_source_records"]) == 2
    provenances = [record[2] for record in cleaned["wall_source_records"]]
    occurrence_ids = {
        provenance["structural_occurrence_id"] for provenance in provenances}
    assert len(occurrence_ids) == 1
    assert None not in occurrence_ids
    assert all(
        provenance["root_plan"] == "B1层墙柱(-6.5~-0.1m)" and
        provenance["source_occurrence_id"] and
        provenance["placed_entity_id"] and
        provenance["segment_ids"] and
        len(provenance["placement_path"]) == 1 and
        provenance["placement_path"][0]["insert_handle"] == root.dxf.handle
        for provenance in provenances
    )


def test_file_backed_drawing_identity_uses_current_file_sha256(tmp_path):
    document, _root = _duplicate_wall_plan()
    source = tmp_path / "drawing.dxf"
    document.saveas(source)
    expected = hashlib.sha256(source.read_bytes()).hexdigest()
    reloaded = ezdxf.readfile(source)

    assert drawing_identity(reloaded) == f"sha256:{expected}"
