# -*- coding: UTF-8 -*-
"""
Document Management Module for Revit MCP
Handles opening, closing, saving documents and syncing with central.
"""

from pyrevit import routes, revit, DB
from Autodesk.Revit.UI import RevitCommandId, PostableCommand
import json
import logging
import traceback

logger = logging.getLogger(__name__)


def register_document_routes(api):
    """Register all document management routes with the API."""

    @api.route("/open_document/", methods=["POST"])
    def open_document(uidoc, request):
        """
        Open a Revit document file.

        Expected payload:
        {
            "file_path": "C:\\path\\to\\file.rvt",
            "detach": false,
            "audit": false
        }
        """
        try:
            data = (
                json.loads(request.data)
                if isinstance(request.data, str)
                else request.data
            )
            file_path = data.get("file_path", "")
            detach = data.get("detach", False)
            audit = data.get("audit", False)

            if not file_path:
                return routes.make_response(
                    data={"error": "No file_path provided"}, status=400
                )

            # Get the UIApplication via pyRevit HOST_APP.  Revit can be open
            # on the start screen with no active document; in that state the
            # route must return an actionable dependency error instead of
            # leaking ``None.Application`` to the Bridge.
            host_app = getattr(revit, "HOST_APP", None)
            uiapp = getattr(host_app, "uiapp", None) if host_app is not None else None
            if uiapp is None:
                return routes.make_response(
                    data={
                        "error": (
                            "Revit UI application is unavailable. "
                            "Keep Revit open with pyRevit Routes enabled."
                        )
                    },
                    status=503,
                )
            app = getattr(uiapp, "Application", None)
            if app is None:
                return routes.make_response(
                    data={"error": "Revit application is unavailable"},
                    status=503,
                )

            # Convert string path to ModelPath
            model_path = DB.ModelPathUtils.ConvertUserVisiblePathToModelPath(
                file_path
            )

            # Build open options
            open_options = DB.OpenOptions()

            if detach:
                open_options.DetachFromCentralOption = (
                    DB.DetachFromCentralOption.DetachAndPreserveWorksets
                )

            if audit:
                open_options.Audit = True

            logger.info(
                "Opening document: {} (detach={}, audit={})".format(
                    file_path, detach, audit
                )
            )

            # Use OpenAndActivateDocument on UIApplication
            # This handles UI-thread marshalling and worksharing dialogs
            # Third param: False = don't close the existing active document
            uiapp.OpenAndActivateDocument(
                model_path, open_options, False
            )

            # After opening, get the now-active document
            new_doc = revit.doc
            if new_doc:
                result = {
                    "status": "success",
                    "document_title": new_doc.Title if new_doc.Title else "Untitled",
                    "file_path": file_path,
                    "is_workshared": new_doc.IsWorkshared,
                }

                if detach:
                    result["detached"] = True
                    result["message"] = (
                        "Document opened detached from central model."
                    )
                elif new_doc.IsWorkshared:
                    result["message"] = (
                        "Workshared document opened. "
                        "A local copy has been created."
                    )
                else:
                    result["message"] = "Document opened successfully."
            else:
                result = {
                    "status": "success",
                    "message": "Open command sent. Document may still be loading.",
                    "file_path": file_path,
                }

            return routes.make_response(data=result)

        except Exception as e:
            error_tb = traceback.format_exc()
            inner = getattr(e, 'InnerException', None)
            inner_msg = str(inner) if inner else "None"
            logger.error(
                "Failed to open document: {}\n"
                "Inner exception: {}\n{}".format(
                    str(e), inner_msg, error_tb
                )
            )
            return routes.make_response(
                data={
                    "error": "Failed to open document: {}".format(str(e)),
                    "inner_exception": inner_msg,
                    "traceback": error_tb,
                    "file_path": data.get("file_path", "") if data else "",
                },
                status=500,
            )

    @api.route("/close_document/", methods=["POST"])
    def close_document(doc, request):
        """
        Close the active Revit document.

        Expected payload:
        {
            "save": false
        }
        """
        try:
            data = (
                json.loads(request.data)
                if isinstance(request.data, str)
                else request.data
            )
            save = data.get("save", False)

            if not doc:
                return routes.make_response(
                    data={"error": "No active document to close"}, status=400
                )

            doc_title = doc.Title if doc.Title else "Untitled"

            logger.info(
                "Closing document: {} (save={})".format(doc_title, save)
            )

            if save:
                try:
                    doc.Save()
                except Exception as save_err:
                    logger.warning(
                        "Save before close failed: {}".format(str(save_err))
                    )

            # Use PostCommand to close the active document since
            # doc.Close() is not allowed on the active document from the API
            uiapp = revit.HOST_APP.uiapp
            close_cmd = RevitCommandId.LookupPostableCommandId(
                PostableCommand.Close
            )
            uiapp.PostCommand(close_cmd)

            return routes.make_response(
                data={
                    "status": "success",
                    "message": "Document '{}' close command sent{}.".format(
                        doc_title,
                        " (saved first)" if save else "",
                    ),
                    "document_title": doc_title,
                    "saved": save,
                    "note": "Revit may show a confirmation dialog if there "
                    "are unsaved changes.",
                }
            )

        except Exception as e:
            logger.error("Failed to close document: {}".format(str(e)))
            return routes.make_response(
                data={"error": "Failed to close document: {}".format(str(e))},
                status=500,
            )

    @api.route("/save_document/", methods=["POST"])
    def save_document(doc, request):
        """
        Save the active Revit document, optionally to a new path (Save As).

        Expected payload:
        {
            "file_path": null
        }

        If file_path is null/omitted, saves in place.
        If file_path is provided, performs Save As to that location.
        """
        try:
            data = (
                json.loads(request.data)
                if isinstance(request.data, str)
                else request.data
            )
            file_path = data.get("file_path", None)

            if not doc:
                return routes.make_response(
                    data={"error": "No active document to save"}, status=400
                )

            doc_title = doc.Title if doc.Title else "Untitled"

            if file_path:
                # Save As
                logger.info(
                    "Saving document '{}' as: {}".format(doc_title, file_path)
                )
                save_as_options = DB.SaveAsOptions()
                save_as_options.OverwriteExistingFile = True

                # Workshared/detached docs require SaveAsCentral
                if doc.IsWorkshared:
                    ws_options = DB.WorksharingSaveAsOptions()
                    ws_options.SaveAsCentral = True
                    save_as_options.SetWorksharingOptions(ws_options)

                doc.SaveAs(file_path, save_as_options)

                return routes.make_response(
                    data={
                        "status": "success",
                        "message": "Document saved as '{}'.".format(file_path),
                        "document_title": doc_title,
                        "saved_path": file_path,
                        "save_type": "save_as",
                    }
                )
            else:
                # Save in place
                logger.info("Saving document: {}".format(doc_title))
                doc.Save()

                return routes.make_response(
                    data={
                        "status": "success",
                        "message": "Document '{}' saved.".format(doc_title),
                        "document_title": doc_title,
                        "save_type": "save",
                    }
                )

        except Exception as e:
            logger.error("Failed to save document: {}".format(str(e)))
            return routes.make_response(
                data={"error": "Failed to save document: {}".format(str(e))},
                status=500,
            )

    @api.route("/sync_with_central/", methods=["POST"])
    def sync_with_central(doc, request):
        """
        Synchronize the active workshared document with central.

        Expected payload:
        {
            "comment": "",
            "compact": false,
            "relinquish_all": true
        }
        """
        try:
            data = (
                json.loads(request.data)
                if isinstance(request.data, str)
                else request.data
            )
            comment = data.get("comment", "")
            compact = data.get("compact", False)
            relinquish_all = data.get("relinquish_all", True)

            if not doc:
                return routes.make_response(
                    data={"error": "No active document"}, status=400
                )

            if not doc.IsWorkshared:
                return routes.make_response(
                    data={
                        "error": "Document is not workshared. "
                        "Use save_document instead."
                    },
                    status=400,
                )

            doc_title = doc.Title if doc.Title else "Untitled"

            logger.info(
                "Syncing '{}' with central (comment='{}', compact={})".format(
                    doc_title, comment, compact
                )
            )

            # Build transact options
            transact_options = DB.TransactWithCentralOptions()

            # Build sync options
            sync_options = DB.SynchronizeWithCentralOptions()
            sync_options.Comment = comment
            sync_options.Compact = compact

            if relinquish_all:
                relinquish_options = DB.RelinquishOptions(True)
                sync_options.SetRelinquishOptions(relinquish_options)

            # Perform sync
            doc.SynchronizeWithCentral(transact_options, sync_options)

            return routes.make_response(
                data={
                    "status": "success",
                    "message": "Document '{}' synchronized with central.".format(
                        doc_title
                    ),
                    "document_title": doc_title,
                    "comment": comment,
                    "compacted": compact,
                    "relinquished_all": relinquish_all,
                }
            )

        except Exception as e:
            logger.error(
                "Failed to sync with central: {}".format(str(e))
            )
            return routes.make_response(
                data={
                    "error": "Failed to sync with central: {}".format(str(e))
                },
                status=500,
            )

    logger.info("Document management routes registered successfully")
