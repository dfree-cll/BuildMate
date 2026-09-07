# Backend 约束

- 新业务进入 `domain → application → ports → adapters → api`；兼容入口只保留存量能力，禁止新增功能。
- 路由只做认证、参数校验、上下文构造和用例调用，不直接写 SQL。
- 领域合同使用 Pydantic 严格校验；错误使用 `backend.domain.errors` 分型。
- 所有 Repository 查询都必须包含租户条件，项目资源还必须包含项目条件。
- RAG 按数据、解析、chunk、embedding、retrieval、rerank、context、generation 分层定位问题。
- Reranker/LLM/向量库不可用必须在结果或错误中明确表达，不得假装成功。
- 几何与 Model IR 输出必须有 `source_refs`；相同输入哈希与版本应可复现。
- 墙体主链仅为 `ODA/ezdxf/PyMuPDF → NumPy/Shapely → Pydantic → Revit`；IFC、OCR、YOLO 不得成为 PDF/DWG 墙坐标事实源。
- `wall_model.json` 未通过确定性 Gate 或未持久化人工批准时，任何 Revit adapter 都必须 fail closed。
