"""Deterministic checks for the BuildMate Chinese BIM standards profile.

The national standards are information-management and delivery standards, not
an automatic geometry oracle.  This module therefore validates only facts the
pipeline can prove from its typed artifacts; project-specific design codes and
human interpretation remain review responsibilities.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from backend.engines.wall_pipeline.contracts import (
    BeamRecord,
    ColumnRecord,
    ConstructionInfo,
    LevelConfig,
    ModelingStandardConfig,
    WallRecord,
)


@dataclass(frozen=True)
class StandardViolation:
    code: str
    field: str
    message: str
    standard_ref: str
    entity_id: str | None = None
    severity: str = "error"

    def as_dict(self) -> dict[str, str]:
        value = {
            "code": self.code,
            "field": self.field,
            "message": self.message,
            "standard_ref": self.standard_ref,
            "severity": self.severity,
        }
        if self.entity_id:
            value["entity_id"] = self.entity_id
        return value


def _construction_violations(
    construction: ConstructionInfo | None,
    *,
    entity_id: str,
    category: str,
    standard: ModelingStandardConfig,
) -> list[StandardViolation]:
    if construction is None:
        if standard.require_instance_parameters:
            return [StandardViolation(
                code="missing_construction_info",
                field="construction",
                message=f"{category} {entity_id} has no typed construction metadata",
                standard_ref=standard.delivery_standard,
                entity_id=entity_id,
            )]
        return []
    violations: list[StandardViolation] = []
    if construction.category != category:
        violations.append(StandardViolation(
            code="construction_category_mismatch",
            field="construction.category",
            message=f"expected {category}, got {construction.category}",
            standard_ref=standard.delivery_standard,
            entity_id=entity_id,
        ))
    if standard.require_instance_parameters and not construction.type_name.strip():
        violations.append(StandardViolation(
            code="missing_type_name",
            field="construction.type_name",
            message="type_name is required for quantity and model handoff",
            standard_ref=standard.delivery_standard,
            entity_id=entity_id,
        ))
    if standard.require_classification_code and not construction.classification_code.strip():
        violations.append(StandardViolation(
            code="missing_classification_code",
            field="construction.classification_code",
            message="classification_code is required",
            standard_ref=standard.classification_system,
            entity_id=entity_id,
        ))
    if standard.require_material and not construction.material_name:
        violations.append(StandardViolation(
            code="missing_material",
            field="construction.material_name",
            message="material_name is required by the active profile",
            standard_ref=standard.delivery_standard,
            entity_id=entity_id,
        ))
    return violations


def validate_modeling_standard(
    *,
    standard: ModelingStandardConfig,
    units: str,
    coordinate_origin: str,
    transform_chain: list[dict],
    level: LevelConfig,
    walls: Iterable[WallRecord],
    columns: Iterable[ColumnRecord],
    beams: Iterable[BeamRecord] = (),
) -> list[StandardViolation]:
    """Return all deterministic profile violations; never silently downgrade."""

    wall_rows = list(walls)
    column_rows = list(columns)
    beam_rows = list(beams)
    violations: list[StandardViolation] = []
    if units != standard.units:
        violations.append(StandardViolation(
            code="units_must_be_m",
            field="units",
            message=f"model units must be {standard.units}, got {units!r}",
            standard_ref=standard.application_standard,
        ))
    if not coordinate_origin.strip():
        violations.append(StandardViolation(
            code="missing_coordinate_origin",
            field="coordinate_origin",
            message="coordinate origin is required for project-local delivery",
            standard_ref=standard.application_standard,
        ))
    if not transform_chain:
        violations.append(StandardViolation(
            code="missing_transform_chain",
            field="transform_chain",
            message="source-to-project transform must be recorded",
            standard_ref=standard.application_standard,
        ))
    if standard.require_level and not level.id.strip():
        violations.append(StandardViolation(
            code="missing_level",
            field="level.id",
            message="every model element must resolve to a level",
            standard_ref=standard.delivery_standard,
        ))

    seen_ids: set[str] = set()
    for wall in wall_rows:
        if wall.wall_id in seen_ids:
            violations.append(StandardViolation(
                code="duplicate_element_id",
                field="wall_id",
                message="element IDs must be unique across the delivered model",
                standard_ref=standard.delivery_standard,
                entity_id=wall.wall_id,
            ))
        seen_ids.add(wall.wall_id)
        if standard.require_source_refs and not wall.source_refs:
            violations.append(StandardViolation(
                code="missing_source_refs",
                field="source_refs",
                message="wall has no traceable source evidence",
                standard_ref=standard.delivery_standard,
                entity_id=wall.wall_id,
            ))
        if standard.require_level and wall.level_id != level.id:
            violations.append(StandardViolation(
                code="level_mismatch",
                field="level_id",
                message=f"expected level {level.id}, got {wall.level_id}",
                standard_ref=standard.delivery_standard,
                entity_id=wall.wall_id,
            ))
        if standard.require_quantities and wall.quantities is None:
            violations.append(StandardViolation(
                code="missing_quantities",
                field="quantities",
                message="deterministic quantity takeoff is required",
                standard_ref=standard.delivery_standard,
                entity_id=wall.wall_id,
            ))
        violations.extend(_construction_violations(
            wall.construction,
            entity_id=wall.wall_id,
            category="Wall",
            standard=standard,
        ))

    for column in column_rows:
        if column.column_id in seen_ids:
            violations.append(StandardViolation(
                code="duplicate_element_id",
                field="column_id",
                message="element IDs must be unique across the delivered model",
                standard_ref=standard.delivery_standard,
                entity_id=column.column_id,
            ))
        seen_ids.add(column.column_id)
        if standard.require_source_refs and not column.source_refs:
            violations.append(StandardViolation(
                code="missing_source_refs",
                field="source_refs",
                message="column has no traceable source evidence",
                standard_ref=standard.delivery_standard,
                entity_id=column.column_id,
            ))
        if standard.require_level and column.level_id != level.id:
            violations.append(StandardViolation(
                code="level_mismatch",
                field="level_id",
                message=f"expected level {level.id}, got {column.level_id}",
                standard_ref=standard.delivery_standard,
                entity_id=column.column_id,
            ))
        if standard.require_quantities and column.quantities is None:
            violations.append(StandardViolation(
                code="missing_quantities",
                field="quantities",
                message="deterministic quantity takeoff is required",
                standard_ref=standard.delivery_standard,
                entity_id=column.column_id,
            ))
        violations.extend(_construction_violations(
            column.construction,
            entity_id=column.column_id,
            category="Column",
            standard=standard,
        ))

    for beam in beam_rows:
        if beam.beam_id in seen_ids:
            violations.append(StandardViolation(
                code="duplicate_element_id",
                field="beam_id",
                message="element IDs must be unique across the delivered model",
                standard_ref=standard.delivery_standard,
                entity_id=beam.beam_id,
            ))
        seen_ids.add(beam.beam_id)
        if standard.require_source_refs and not beam.source_refs:
            violations.append(StandardViolation(
                code="missing_source_refs",
                field="source_refs",
                message="beam has no traceable source evidence",
                standard_ref=standard.delivery_standard,
                entity_id=beam.beam_id,
            ))
        if standard.require_level and beam.level_id != level.id:
            violations.append(StandardViolation(
                code="level_mismatch",
                field="level_id",
                message=f"expected level {level.id}, got {beam.level_id}",
                standard_ref=standard.delivery_standard,
                entity_id=beam.beam_id,
            ))
        if standard.require_quantities and beam.quantities is None:
            violations.append(StandardViolation(
                code="missing_quantities",
                field="quantities",
                message="deterministic quantity takeoff is required",
                standard_ref=standard.delivery_standard,
                entity_id=beam.beam_id,
            ))
        violations.extend(_construction_violations(
            beam.construction,
            entity_id=beam.beam_id,
            category="Beam",
            standard=standard,
        ))
    return violations


__all__ = ["StandardViolation", "validate_modeling_standard"]
