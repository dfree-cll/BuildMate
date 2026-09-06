from backend.main import app
from backend.api.v2.tasks import _WORKFLOWS


def test_v2_openapi_exposes_the_stable_vertical_slice():
    paths = app.openapi()["paths"]
    required = {
        "/api/v2/projects",
        "/api/v2/artifacts",
        "/api/v2/knowledge/documents/from-artifact",
        "/api/v2/knowledge/search",
        "/api/v2/workflows",
        "/api/v2/tasks/{task_id}/events",
        "/api/v2/model-ir",
        "/api/v2/reviews/{review_id}/decision",
        "/api/v2/builds/{build_id}/approve",
        "/api/v2/chat/sessions/{session_id}/events",
    }
    assert required <= set(paths)
    # BIM has one public workflow and the retired v1 compatibility adapter is
    # intentionally hidden from the OpenAPI contract.
    assert "drawing_review" not in _WORKFLOWS
    assert "/api/v1/bim/upload" not in paths
