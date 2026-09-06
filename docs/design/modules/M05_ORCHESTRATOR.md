# M05 意图路由与工具治理设计

## 功能描述

为问答入口提供受控意图路由，并统一管理 Workflow 调度、MCP 工具 ACL、参数校验、限额、重试和失败归因。BIM 仍保留独立入口；用户也可以在问答中提出 BIM 请求，由路由层跳转到 BIM 页面并预填参数。

## 功能实现流程

1. 接收自然语言消息、项目上下文和角色上下文。
2. 提取注册意图和参数，输出置信度、缺失参数和候选路由。
3. 通过白名单映射选择 `rag.answer`、`bid.search`、`workflow.bid_review`、`workflow.contract_review`、`workflow.procurement`、`workflow.wall_pipeline` 或 `clarify`，不允许 LLM 创建新 Workflow。
4. 对路由和工具输入执行 Pydantic Schema、租户/项目和角色 ACL 校验。
5. 查询类请求可在会话内返回；审核、审批、BIM 写入和正式报告创建持久化任务。
6. 调用工具并记录 ToolCall；按结果决定下一固定步骤或人工节点。
7. 将最终结果转换为统一 AgentResult，并保留原始意图和路由记录。

## 业务规则

- LLM 只能在预定义 Workflow/工具集合内选择。
- 意图置信度不足、项目上下文缺失或参数不完整时只能澄清，不能产生副作用。
- BIM 是独立用户入口；意图路由只能跳转/预填，不得绕过 BIM 的静态门禁和两次审批。
- 合同审核、正式标书审查和采购审批必须进入任务与审核中心，不能以聊天文本代替正式结论。
- 工具调用最多 12 次；工具参数、路径、文件和网络能力显式限制。
- 超时、重试、熔断和死信必须可观测。
- 工具返回的事实字段不能由 LLM 改写。
- 未授权工具调用在进入外部服务前拒绝。

## 使用角色

- 业务用户间接使用。
- `admin` 管理 Workflow 和工具策略。
- Agent、Worker、MCP Gateway 使用受控服务接口。

## 界面设计要求

- 普通用户不显示工具名和内部堆栈，只显示执行阶段和结果。
- 管理员可查看工具调用摘要、耗时、错误和 Trace，不默认展示敏感输入。
- 失败提示区分参数错误、权限错误、超时和外部服务错误。

## API 与数据

- 内部 Port：WorkflowSelector、ToolExecutor、PolicyChecker、AgentResultMapper。
- 入口预览：`POST /api/v2/chat/intents/preview`，只返回意图、参数、置信度、缺失参数和白名单路由，不产生副作用。
- 表：`tool_calls`、`workflow_steps`、`llm_calls`。
- MCP 对外暴露版本化 ToolSpec，例如 `knowledge.search`、`bid.search`、`contract.review`、`revit.run_wall_model`。

## 异常与验收

- 非法 Workflow、工具、参数、角色和项目均拒绝。
- 未注册意图、低置信度路由和跨项目请求均进入可读澄清/拒绝状态。
- MCP 超时不会静默降级；降级必须在结果和事件中标记。
- 同一任务超过限额后失败并说明原因。
