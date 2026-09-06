# M13 证据、报告与审计设计

## 功能描述

提供统一证据引用、报告生成、任务事件回放和操作审计，让用户知道结论来源、模型变化和批准责任。

## 功能实现流程

1. 各 Agent/引擎产生 EvidenceRef、source_refs 和产物哈希。
2. 应用层校验证据属于当前租户/项目和当前输入。
3. 汇总为 Review Report、RAG Answer、Model Diff、Revit Result 和 Audit Report。
4. 记录 TaskEvent、Approval 和 AuditEvent。
5. 前端以摘要、证据卡、时间线和 JSON 展开呈现；管理员可导出。

## 业务规则

- 引用不存在、跨项目或不属于本次检索的证据视为非法。
- 审计事件只追加不更新。
- 报告必须包含版本、来源哈希、生成时间和错误列表。
- 独立叠图必须标明源图与实际 Revit 视图来源，禁止 WallModel 自证。

## 使用角色

业务用户查看所属项目；审核员审核证据；管理员查询和导出租户审计。

## 界面设计要求

- 结果页面先给结论、风险和指标，再给证据详情。
- 时间线支持按步骤过滤、展开输入/输出摘要和错误。
- 哈希、Trace ID、坐标和 JSON 使用等宽字体并可复制。
- 报告支持下载 PDF/JSON/图片，下载也要做权限校验。

## API 与数据

- 任务事件、Review、Build、RetrievalRun 和 Artifact 查询 API。
- 表：`task_events`、`audit_events`、`artifact_derivatives`、`review_findings`。

## 异常与验收

- 缺证据、哈希不匹配、报告生成失败和下载权限不足明确显示。
- 任一成功结论可回溯到输入 Artifact、版本、操作者、审批和执行结果。
