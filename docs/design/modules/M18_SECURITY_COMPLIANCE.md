# M18 安全与合规设计

## 功能描述

统一提供身份、权限、租户隔离、输入校验、文件安全、MCP ACL、Revit 脚本安全、审计和敏感信息保护。

## 功能实现流程

1. API 验证 JWT 并构造 RequestContext。
2. Repository Scope 和生产 RLS 双重过滤租户/项目。
3. Artifact、工具、下载和审批执行角色/资源/状态检查。
4. 外部输入通过 Pydantic、文件头和路径安全校验。
5. Bridge 通过 AST 白名单、只读副本、Transaction 和审批执行。
6. 操作写入 AuditEvent，敏感字段脱敏。

## 业务规则

- 默认拒绝；未声明权限不能访问。
- LLM 不得直接访问数据库、文件系统、进程、网络和 Revit。
- Bridge 禁止任意文件、进程、网络和动态执行。
- Revit 写入必须两次审批，失败 Rollback。
- Token、密码、密钥和敏感原文不得入日志。

## 使用角色

所有角色受权限保护；管理员维护策略；Bridge 是最小权限服务身份。

## 界面设计要求

- 无权限、过期、版本冲突和策略阻断使用明确文案。
- 危险动作展示影响范围、审批链和审计提示。
- 管理员可查看审计，但敏感值默认脱敏。

## API 与数据

- JWT/RBAC、Repository Scope、PostgreSQL RLS、MCP ToolSpec。
- `audit_events`、`approvals`、`tool_calls`。

## 异常与验收

- 跨租户访问、越权下载、非法工具、危险脚本和跳过审批全部拒绝。
- 安全测试、日志脱敏和审计完整性通过。
