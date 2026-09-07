# Frontend 约束

- 新功能调用当前 API 合同；兼容页面仅保留存量入口，禁止新增功能。
- Pinia 按 `auth/project/artifact/task/review/modeling/chat/knowledge` 领域拆分。
- UI 必须展示任务真实状态、失败原因、证据来源、审批状态与 Revit 差异，不把“已提交”显示为“已完成”。
- 跨页共享状态放 Store；请求必须携带用户当前项目上下文。
- 完成修改后运行 `npm run build`。
