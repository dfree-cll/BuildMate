from backend.application.intent_router import classify_intent


def test_building_standard_query_routes_to_grounded_rag_without_confirmation():
    route = classify_intent("查询建筑规范中的墙厚要求", project_id="project-1")

    assert route.intent == "building_standard_query"
    assert route.route == "rag.answer"
    assert route.requires_confirmation is False
    assert route.evidence_sources == ["rag"]
    assert route.missing_parameters == []


def test_bid_query_requires_project_context():
    route = classify_intent("查询标书第三章的付款条件")

    assert route.intent == "bid_query"
    assert route.route == "clarify"
    assert route.missing_parameters == ["project_id"]


def test_contract_review_is_a_confirmed_workflow_and_requires_artifact():
    route = classify_intent("审核这份合同", project_id="project-1")

    assert route.intent == "contract_review"
    assert route.route == "clarify"
    assert route.requires_confirmation is True
    assert route.evidence_sources == ["rag", "structured", "rules"]
    assert route.missing_parameters == ["artifact_ids"]


def test_bim_command_is_not_executed_by_question_router():
    route = classify_intent(
        "把这份图纸建成 Revit 模型",
        project_id="project-1",
        artifact_ids=["artifact-1"],
    )

    assert route.intent == "bim_command"
    assert route.route == "workflow.wall_pipeline"
    assert route.requires_confirmation is True
    assert route.evidence_sources == ["bim", "rules"]


def test_unclassified_question_stays_in_evidence_first_rag():
    route = classify_intent("墙体施工前需要检查哪些内容？", project_id="project-1")

    assert route.intent == "general_question"
    assert route.route == "rag.answer"


def test_procurement_query_declares_document_and_structured_sources():
    route = classify_intent("核查供应商资质和采购报价", project_id="project-1")

    assert route.intent == "procurement_query"
    assert route.route == "workflow.procurement"
    assert route.evidence_sources == ["rag", "structured", "rules"]
