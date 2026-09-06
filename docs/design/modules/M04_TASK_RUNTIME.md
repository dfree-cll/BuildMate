# M04 任务与工作流运行时设计

## 功能描述

将用户请求转换为持久化任务，负责步骤执行、状态机、事件、重试、取消、暂停、恢复和 SSE/轮询查询。

## 功能实现流程

1. API 校验 Workflow、Artifact、项目上下文和幂等键。
2. 事务内创建 WorkflowRun、首个 WorkflowStep、TaskEvent 和 OutboxEvent。
3. 本地 Runner 或 RabbitMQ 消费任务并领取 lease。
4. 每个步骤先写 `running`，再调用 Agent/工具，结束后 CAS 写入结构化输出或错误。
5. 遇到人工节点写入 `waiting_human` 和 `next_action`。
6. 审批通过后创建 `resumed` 事件继续；拒绝则取消或转补件。
7. 终态写入结果、交付物、完成时间和最终事件。

## 业务规则

- 状态只允许按状态机转换，客户端不能直接改状态。
- 单任务最多 20 步、12 次工具调用。
- 至少一次投递；消费者必须按租户 + 幂等键安全重复消费。
- 重试只针对 `retryable=true` 错误，指数退避并记录次数。
- 旧 lease 回执不能覆盖新领取任务。
- 任务终态不可回到处理中；修订必须创建新任务并关联旧任务。

## 使用角色

- `project`：创建和查看自己的项目任务。
- `reviewer/admin`：处理等待人工任务。
- `admin`：查看、取消、重试和审计全部租户任务。
- Worker/Agent：执行被授权步骤。

## 界面设计要求

- 任务卡显示状态、阶段、步骤、最近事件、错误、下一动作和产物。
- 用步骤条和时间线表达进度，不使用无限旋转等待。
- 刷新页面根据 task ID 恢复；SSE 断开自动轮询。
- 失败显示是否可重试和推荐修复动作。

## API 与数据

- `POST /api/v2/workflows`、`GET /tasks/{id}`、`GET /tasks/{id}/events`、`POST /tasks/{id}/cancel`、`POST /tasks/{id}/resume`。
- 表：`workflow_runs`、`workflow_steps`、`task_events`、`outbox_events`。

## 异常与验收

- 重复请求只返回同一任务。
- 服务重启、消息重复、Worker 超时、死信和人工暂停均可回放。
- 事件序号连续，SSE 使用 `Last-Event-ID` 后不重复丢失关键事件。
