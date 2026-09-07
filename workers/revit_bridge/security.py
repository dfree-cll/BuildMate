"""Static safety gate for user-authored Revit scripts.

This gate is intentionally restrictive.  It is not a general Python sandbox;
the process must still run under a locked-down Windows account and on a copy of
the model.  Its purpose is to reject scripts that are outside BuildMate's Revit
editing contract before they reach pyRevit.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass


_ALLOWED_IMPORT_ROOTS = frozenset({"Autodesk", "math"})
_BLOCKED_NAMES = frozenset({
    "open", "eval", "exec", "compile", "__import__", "input", "breakpoint",
    "globals", "locals", "vars", "getattr", "setattr", "delattr",
    "uidoc", "revit",
})
_BLOCKED_ATTRIBUTES = frozenset({
    "system", "popen", "spawn", "fork", "remove", "unlink", "rmdir",
    "removedirs", "rename", "replace", "walk", "listdir", "scandir",
    "chdir", "chmod", "chown", "connect", "request", "urlopen", "kill",
    "terminate", "startfile", "environ", "__dict__", "__class__",
    "__subclasses__", "__globals__", "__code__",
    "application", "opendocumentfile", "openifcdocument", "addreference",
    "save", "saveas", "close", "export",
})
_BLOCKED_IMPORT_ROOTS = frozenset({
    "os", "sys", "subprocess", "shutil", "socket", "pathlib", "urllib",
    "requests", "httpx", "ftplib", "ctypes", "multiprocessing", "threading",
    "importlib", "pickle", "marshal", "tempfile", "winreg",
})


@dataclass(frozen=True)
class ScriptViolation:
    line: int
    code: str
    message: str


class RevitScriptValidator(ast.NodeVisitor):
    """Reject filesystem, process, network and dynamic-code capabilities."""

    def __init__(self) -> None:
        self.violations: list[ScriptViolation] = []

    def reject(self, node: ast.AST, code: str, message: str) -> None:
        self.violations.append(ScriptViolation(
            line=max(1, getattr(node, "lineno", 1)), code=code, message=message
        ))

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802
        for alias in node.names:
            root = alias.name.split(".", 1)[0]
            if root in _BLOCKED_IMPORT_ROOTS or root not in _ALLOWED_IMPORT_ROOTS:
                self.reject(node, "import_not_allowed", f"import {alias.name} is not allowed")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        root = (node.module or "").split(".", 1)[0]
        if root in _BLOCKED_IMPORT_ROOTS or root not in _ALLOWED_IMPORT_ROOTS:
            self.reject(node, "import_not_allowed", f"import from {node.module or ''} is not allowed")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:  # noqa: N802
        if node.id in _BLOCKED_NAMES or node.id.startswith("__"):
            self.reject(node, "name_not_allowed", f"name {node.id} is not allowed")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:  # noqa: N802
        attr = node.attr.lower()
        if attr in _BLOCKED_ATTRIBUTES or attr.startswith("__"):
            self.reject(node, "attribute_not_allowed", f"attribute {node.attr} is not allowed")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        # Model mutations must be visibly guarded by a Revit Transaction.
        # Actual transaction/rollback enforcement also happens in the Bridge executor.
        if isinstance(node.func, ast.Name) and node.func.id in _BLOCKED_NAMES:
            self.reject(node, "call_not_allowed", f"call {node.func.id} is not allowed")
        self.generic_visit(node)


def validate_revit_script(source: str, *, max_chars: int = 50_000) -> list[ScriptViolation]:
    if not source.strip():
        return [ScriptViolation(1, "empty_script", "script cannot be empty")]
    if len(source) > max_chars:
        return [ScriptViolation(1, "script_too_large", f"script exceeds {max_chars} characters")]
    try:
        tree = ast.parse(source, mode="exec")
    except SyntaxError as exc:
        return [ScriptViolation(exc.lineno or 1, "syntax_error", exc.msg)]
    validator = RevitScriptValidator()
    validator.visit(tree)
    return sorted(validator.violations, key=lambda item: (item.line, item.code))


def has_explicit_transaction(source: str) -> bool:
    """Require a visible DB.Transaction(...), Start and Commit/RollBack sequence."""
    try:
        tree = ast.parse(source, mode="exec")
    except SyntaxError:
        return False
    attrs = {
        node.attr.lower()
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
    }
    names = {
        node.id.lower()
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
    }
    return (
        "transaction" in attrs | names
        and "start" in attrs
        and bool({"commit", "rollback"} & attrs)
    )
