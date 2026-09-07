"""Agent package public surface.

The historical package-level ``extract_ifc_to_json`` helper is kept as a
lazy compatibility wrapper. IFC parsing now lives in
``backend.engines.ifc_parser``; importing any Agent must not eagerly import
the optional ``ifcopenshell`` dependency or open a model.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def safe_get_attr(entity: Any, attr_name: str) -> Any:
    """Return an IFC attribute when present, otherwise ``None``."""

    if entity is None:
        return None
    try:
        return getattr(entity, attr_name)
    except AttributeError:
        return None


def extract_ifc_to_json(ifc_file_path: str, output_json_path: str) -> None:
    """Export an IFC file through the canonical deterministic parser.

    The old implementation duplicated the parser and loaded ``ifcopenshell``
    during package import. Keep this synchronous API for compatibility while
    delegating the actual work to the engine implementation.
    """

    from backend.engines.ifc_parser import _sync_parse

    result = _sync_parse(str(ifc_file_path))
    output = Path(output_json_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )


__all__ = ["safe_get_attr", "extract_ifc_to_json"]
