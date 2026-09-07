"""Small helpers shared by Revit diagnostics.

Revit API objects do not all expose ``Name`` safely.  Reading it through this
helper keeps a diagnostic export from aborting because of one malformed or
partially loaded family/type.
"""


def safe_name(obj, fallback="unknown"):
    if obj is None:
        return fallback
    try:
        value = obj.get_Parameter(
            __import__("Autodesk.Revit.DB", fromlist=["BuiltInParameter"])
            .BuiltInParameter.SYMBOL_NAME_PARAM
        )
        if value:
            text = value.AsString()
            if text:
                return text
    except Exception:
        pass
    try:
        text = getattr(obj, "Name", None)
        if text:
            return text
    except Exception:
        pass
    return fallback
