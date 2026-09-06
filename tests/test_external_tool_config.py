"""外部 BIM/Revit 工具必须在缺失时给出明确配置错误。"""
import pytest

from backend.engines import adaptive_pipeline
from backend.mcp.revit_client import RevitMCPClient, RevitMCPError


def test_project_column_extractor_missing_path_is_actionable(monkeypatch, tmp_path):
    missing = str(tmp_path / "missing_extractor.py")
    monkeypatch.setattr(adaptive_pipeline, "_COLUMN_EXTRACTOR", missing)

    with pytest.raises(RuntimeError, match="项目内柱提取脚本"):
        adaptive_pipeline._run_extract("plan.dxf")


def test_revit_mcp_missing_paths_are_actionable(monkeypatch, tmp_path):
    import backend.mcp.revit_client as revit_client

    monkeypatch.setattr(revit_client, "_MCP_PYTHON", str(tmp_path / "missing.exe"))
    monkeypatch.setattr(revit_client, "_MCP_MAIN", str(tmp_path / "missing.py"))

    with pytest.raises(RevitMCPError, match="REVIT_MCP_PYTHON"):
        RevitMCPClient()._start_sync()
