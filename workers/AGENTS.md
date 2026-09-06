# Workers 约束

- Worker 消费 `TaskEnvelope`，按至少一次投递设计并保证幂等。
- 每一步先持久化再调用外部依赖；失败必须分型、记录且进入有限重试/死信。
- Revit Bridge 只监听 loopback，默认 validate-only；禁止任意文件、进程和网络调用。
- Revit 写入必须使用工作副本、Transaction、Dry-run、人工批准、读回验证和差异快照。
- 不得在 Worker 内维护不可恢复的业务事实源。
