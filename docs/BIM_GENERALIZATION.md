# BIM 泛化设计

## 目标

在保留现有 PDF/DWG/DXF → WallEvidence → WallModel → Revit 确定性主链的前提下，
把 BIM 能力扩展为可注册的构件插件、来源适配器和目标编译器。新增构件不修改坐标、单位、证据、审批和审计内核。

## 分层

```text
SourceAdapter
  → CanonicalSourceEntities
  → Coordinate/Unit Kernel
  → DisciplinePlugin
  → ModelEnvelope / ElementEnvelope
  → Geometry + Evidence + Rule Gates
  → Human Approval
  → EngineAdapter
  → Readback + Independent Audit
```

- **BIM Kernel**：租户/项目上下文、来源哈希、坐标系、单位、楼层、轴网、稳定 ID、证据引用、质量状态和审批状态。
- **SourceAdapter**：处理 PDF、DWG/DXF、IFC、图片等来源差异，只输出统一来源实体；来源特有的坐标和解析规则留在适配器内。
- **DisciplinePlugin**：处理墙、柱、梁、洞口以及后续门窗、楼板、机电等专业算法。插件负责识别、构建、专业校验和模型投影。
- **EngineAdapter**：负责 Revit、IFC 或其他目标的预检、编译、Dry-run、事务写入、读回、渲染和回滚。

## 通用合同

通用模型使用 `ModelEnvelope` 和 `ElementEnvelope` 外壳。`kind` 可以扩展，但必须在服务端插件注册表中映射到严格的专业 Pydantic 合同；未知构件只能进入待识别或不可编译状态，不能静默丢弃。

```text
ModelEnvelope
  project / coordinate / units / levels / grids
  elements[] / provenance / quality / review

ElementEnvelope
  id / kind / placement / geometry / semantics
  relations / evidence / quantities / quality / review
```

几何使用判别联合（点、线、折线、多边形、拉伸体、实体、网格等），不得继续用无约束的 `dict[str, Any]` 表示核心几何。构件关系显式记录 `host`、`opening_of`、`connected_to`、`supports`、`intersects` 等语义。

证据使用通用 `EvidenceBundle`：通用部分保存 Artifact、来源 frame、定位信息、原文引用和哈希；墙双线配对、柱轮廓、洞口宿主等专业结果放在插件的严格 payload 中。证据不足、来源不一致或门禁失败时必须阻断后续编译。

## 插件生命周期

```text
recognize → build → validate → project/compile
```

插件通过显式注册表加载。注册表拒绝重复 kind、未知目标和不完整能力声明，不接受 API 调用者动态注入插件。工作流只按能力注册表调度，不使用构件类型条件分支。

## 迁移顺序

1. 为现有 `WallModel` 增加只读映射门面，将墙、柱、梁、洞口投影为通用元素；不改变几何算法、哈希和 Revit 交付。
2. 建立 `WallPlugin`、`ColumnPlugin`、`BeamPlugin`、`OpeningPlugin` 注册项，专业规则继续保留在各自模块。
3. 合并领域层和引擎层重复的 Model IR，实现一个统一的 `ModelIR` 外壳；旧调用通过短期适配器迁移。
4. 把墙体 Workflow 的阶段拆成通用模型流水线，保留 `wall_pipeline` 作为当前墙体 profile 的入口。
5. 将 Revit 墙体请求扩展为通用 Build Plan；原墙体接口保留为兼容入口，直到所有调用方完成迁移。
6. 用合同测试、性质测试、Golden Artifact 和 capability matrix 验证每个来源、插件和目标组合。

## 不抽象的部分

- 墙双边配对、T/L/X 拓扑和墙柱碰撞。
- 柱异形轮廓、梁标高推导和洞口单宿主约束。
- PDF media-box、ODA 转换和 DWG 图层语义。
- Revit 族加载、Transaction、视图、读回和回滚。
- 国标、项目容差、命名和审批规则。

这些逻辑可以共享基础工具，但不能为了形式统一而压成一个大而全的通用算法。

## 当前实施边界

第一阶段只覆盖 PDF、DWG、DXF 来源，墙、柱、梁、洞口构件，以及 Revit 2020 交付。IFC 来源、门窗、楼板、机电和其他目标编译器在通用合同稳定并完成独立验收后再接入。
