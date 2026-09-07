# -*- coding: utf-8 -*-
"""Routes probe: report document transaction state (debug only)."""
import json


def main():
    doc = __revit__.ActiveUIDocument.Document
    out = {"modifiable": bool(doc.IsModifiable), "title": doc.Title or ""}
    try:
        from Autodesk.Revit.DB import Transaction
        t = Transaction(doc, "probe")
        try:
            t.Start()
            st = "started"
            t.RollBack()
        except Exception as ex:
            st = "start_failed: " + str(ex)[:120]
        out["probe_txn"] = st
    except Exception as ex:
        out["probe_txn"] = "err: " + str(ex)[:120]
    print(json.dumps(out))
    return out
