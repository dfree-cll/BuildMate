# M17 基础设施与可观测性设计

## 功能描述

提供本地/生产运行适配、队列、缓存、对象存储、向量库、监控、日志、Trace、健康检查和备份恢复。

## 功能实现流程

1. 启动时加载环境配置并检查依赖健康。
2. 本地使用 SQLite、文件、本地向量和 DB Runner。
3. 生产使用 PostgreSQL、RabbitMQ、Redis、MinIO/S3、Milvus 和观测栈；向量库不承担权限事实。
4. Worker 从 Outbox/RabbitMQ 领取任务并发布事件。
5. API、Agent、RAG、几何、Bridge 统一写指标和 Trace。
6. 失败任务进入重试/死信；运维根据 Trace 和错误码处理。

## 业务规则

- 基础设施替换不能改变领域合同。
- RabbitMQ 至少一次投递，不能假设 exactly-once。
- Redis 只能做缓存/限流，不能做任务事实源。
- 健康检查区分 liveness 和 readiness。
- 生产密钥不进仓库和日志。

## 使用角色

管理员查看健康摘要；平台/运维人员维护生产；业务用户只看到与任务有关的错误。

## 界面设计要求

- 管理台显示 API、DB、队列、Worker、向量库、对象存储和 Bridge 状态。
- 指标用趋势图和阈值告警，异常可点击到 Trace/任务。
- 普通页面显示可行动错误，不展示基础设施内部拓扑细节。

## API 与数据

- `/health`、`/ready`、指标端点和 OpenTelemetry。
- RabbitMQ exchange `buildmate.tasks`，DLQ；Prometheus/Grafana/Loki/OTLP。

## 异常与验收

- 依赖不可用时 readiness 失败，相关任务明确标记可重试。
- 服务重启、队列重复、死信和恢复演练通过。
