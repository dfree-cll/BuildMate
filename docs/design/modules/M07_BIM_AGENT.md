# M07 BIM Agent：图纸到模型交付设计

## 功能描述

BIM Agent 是一个独立的用户入口和业务 Workflow，面向用户完成“PDF/DWG/DXF 图纸 → 轴网/墙柱/构件信息 → Revit 2020 工作副本 → 独立叠图验收 → 交付”。B1～B5 是内部执行阶段，不是独立 Agent 或独立用户任务。问答入口识别到 `bim_command` 时，只能跳转此入口并预填上下文，不能绕过其配置、门禁和审批。

建模标准执行 [BIM_MODELING_STANDARD.md](../../BIM_MODELING_STANDARD.md) 的 `cn_gb_bim_delivery_v1` profile，默认引用 GB/T 51212-2016、GB/T 51269-2017、GB/T 51301-2018 和 GB/T 51235-2017。

## 功能实现流程

1. 前端选择项目、楼层、比例、坐标原点、墙柱材料和 Revit 工作模型策略。
2. 创建输入 Artifact 和 BIM Agent WorkflowRun。
3. **B1 来源适配**：PDF/PyMuPDF；DWG/ODA→DXF；DXF/ezdxf；生成 SourceManifest/Entities。
4. **B2 几何识别**：清洗、单位、旋转、轴网、双线墙、矩形/异形柱、去重和 WallEvidence。
5. **B3 Model IR/门禁**：墙厚、拓扑、断墙、碰撞、参数、坐标和轴网检查，生成 WallModel。
6. **B4 审查/审批**：硬规则 + RAG 软审查，审核员/管理员审核 WallModel。
7. **B5 Dry-run/写入**：检查 Level/族/参数/数量后，第二次审批调用 Revit Bridge。
8. Bridge 按轴网→墙体→柱→构件参数顺序写入隔离副本，Transaction 失败 Rollback。
9. 读回元素、参数和数量，导出实际视图，与独立源图渲染叠图。
10. 仅当实际 RVT、读回、参数和独立指标全部通过时交付成功。

性能约束：SourceEntities 解析按输入文件哈希、解析配置和缓存版本复用；缓存命中只跳过 PDF/DWG 解析，WallEvidence、WallModel、门禁、审批、Dry-run、Revit 写入和独立叠图仍完整执行。缓存损坏或版本变化按未命中处理，不阻断正确性。

## 业务规则

- PDF/DWG 只在来源适配层分叉；下游 JSON 合同统一。
- 不按图号、固定文件路径、固定图框、固定轴距或项目特有墙层名写分支。
- OCR/YOLO/颜色识别不能直接决定墙体最终坐标。
- 墙、柱、异形柱和连接必须有 source_refs/evidence/confidence。
- 轴网先于墙柱；墙柱碰撞为 0 或有人工处置记录。
- 图纸图例/表格参数写入 Revit 构件信息，不只生成明细表。
- 按国标 profile 生成统一米制、项目坐标、分类系统、类型命名和 `BM_*` 构件参数；profile 与标准引用随 WallModel 一起交付。
- Revit 2020 使用兼容共享参数格式；原始 RVT 不覆盖。
- 独立 edge IoU、源覆盖率、Revit 精确率和相似度均 ≥0.95 才能 `delivery_complete`。

## 使用角色

- `project`：上传图纸、填写配置、查看任务和下载结果。
- `reviewer/admin`：审核 WallModel 和 Revit 写入。
- `bim`：仅执行已批准工作副本写入和回传。
- `user`：按权限查看结果。

## 界面设计要求

- 页面只显示一个 BIM Agent 任务，内部 B1～B5 用步骤条展示。
- 上传、配置、识别摘要、门禁、两次审批、交付物和独立叠图在同一任务详情中呈现。
- 显示墙、柱、异形柱、轴网、连接点、碰撞、参数写入数和洞口待处理数。
- 审批页先显示可读摘要，JSON/证据/坐标可展开。
- 独立叠图明确标出原始图纸和实际 Revit 视图来源及四项指标。

## API 与数据

- `POST /api/v2/workflows`，`workflow=wall_pipeline`。
- `GET /api/v2/tasks/{id}`、`GET /tasks/{id}/events`、`POST /tasks/{id}/resume`。
- 产物：`source_manifest.json`、`source_entities.json`、`wall_evidence.json`、`wall_model.json`、`revit_result.json`、`audit_report.json`、原图/实际视图/叠图。
- 表：`model_ir_versions`、`build_runs`、`approvals`，以及通用任务表。

## 异常与验收

- ODA 未配置、PDF 实体超限、未识别轴网、零墙/零柱、缺 Level、共享参数缺失、Bridge 503、碰撞或叠图低于阈值均明确失败。
- 不能将只生成 JSON、Dry-run 或审批成功标记为最终交付。
- 换另一张 PDF/DWG 只改输入配置即可运行。
