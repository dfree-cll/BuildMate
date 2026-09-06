# -*- coding: utf-8 -*-
"""Code execution tools for the MCP server."""

from mcp.server.fastmcp import Context
from .utils import format_response


def register_code_execution_tools(mcp, revit_get, revit_post, revit_image=None):
    """Register code execution tools with the MCP server."""
    # Note: revit_get and revit_image are unused but kept for interface consistency
    _ = revit_get, revit_image  # Acknowledge unused parameters

    @mcp.tool()
    async def execute_revit_code(
        code: str, description: str = "Code execution", ctx: Context = None
    ) -> str:
        """
        Execute IronPython code directly in Revit context.

        The code has access to:
        - doc: The active Revit document
        - uidoc: The active UIDocument (use for UI operations like switching the active view)
        - DB: Revit API Database namespace
        - revit: pyRevit module
        - print: Function to output text (returned in response)

        No transaction is opened automatically. Wrap model-modifying code yourself:
            t = DB.Transaction(doc, "My change")
            t.Start()
            # ... modify model ...
            t.Commit()

        For UI operations that cannot run inside a transaction (e.g. switching the active view):
            all_views = DB.FilteredElementCollector(doc).OfClass(DB.View).ToElements()
            target = next((v for v in all_views if v.Name == "Level 1"), None)
            if target:
                uidoc.ActiveView = target

        Tips:
        - Use getattr(element, 'Name', 'N/A') to safely access the Name property
        - Check elements exist before use: if element:
        - Use hasattr() for optional properties
        """
        try:
            payload = {"code": code, "description": description}

            if ctx:
                await ctx.info("Executing code: {}".format(description))

            response = await revit_post("/execute_code/", payload, ctx, timeout=600.0)
            return format_response(response)

        except (ConnectionError, ValueError, RuntimeError) as e:
            error_msg = "Error during code execution: {}".format(str(e))
            if ctx:
                await ctx.error(error_msg)
            return error_msg
