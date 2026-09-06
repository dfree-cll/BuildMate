# PDF / DWG 通用墙体流水线（当前产品架构下的 BIM 独立入口）

本文件沿用当前 API 技术合同；用户入口和意图路由调整不改变 PDF/DWG → WallEvidence → WallModel → Revit 的确定性主链路。

## 目标链路

```text
PDF / DWG
→ 来源适配
→ 统一矢量实体
→ 可追溯 WallEvidence
→ 单位、原点、旋转与轴网归一
→ 墙厚、去重、断墙与 T/L/Z 拓扑
→ 确定性门禁 + 人工批准
→ Revit 轴网 → 墙体
→ 原始图纸 / Revit 实际视图独立叠图
```

实现位于 `backend/engines/wall_pipeline/`。它不调用 IFCOpenShell；IFC 继续用于交换和既有模型读取。OCR、YOLO、颜色或像素检测可以补充元数据和复核，但不得写入最终墙体坐标。

国标建模 profile 与模型生成规则见 [BIM_MODELING_STANDARD.md](BIM_MODELING_STANDARD.md)。默认采用 GB/T 51212-2016、GB/T 51269-2017、GB/T 51301-2018 和 GB/T 51235-2017；该 profile 是工程实现约束，不替代项目专业规范审查。

## 墙厚声明与交付显示（2026-09-05）

- 墙面配对前先读取现有图例/墙表解析结果。默认墙厚搜索上限仍为 `wall.max_thickness_m`（600 mm），但同一来源 frame 内无冲突、带构件编号的明确墙厚，可作为离散候选规格参与配对。
- 超过默认上限的两条边线必须与声明规格在 `thickness_tolerance_m`（默认 5 mm）内一致；声明 800 mm 不等于允许任意 600–800 mm 的线距。没有规格、规格冲突或仅另一个来源 frame 有规格时，不自动扩大接受范围。
- 所有坐标和实测墙厚仍来自真实矢量边线；`WallEvidence` 保留实测值，并通过 `WALL_DECLARED_THICKNESS_SEARCH` 记录编号、声明厚度和原文实体 ID。正式族类型尺寸仍由后续规格核定决定，不对测量值盲目取整。
- 交付编译器 `typed_wall_model_v40` 区分平面与三维：平面使用白色轮廓、隐藏表面/截面的前景和背景填充，便于黑底线框阅读；三维继续保留灰色材质/显示。显示处理仍在独立审计 PNG 导出后执行，不改变验收图。
- 墙体角色只接受可追溯来源：`S-WALL`/结构图层为 `shear_wall`，`A-WALL`/`A-PART` 为 `architectural_wall`，混合来源为 `unresolved` 并进入审核。几何形状、颜色和“看起来像”不能替代来源证据。当前 `S-G50-01` 单文件任务只有 `S-WALL` 来源，因此不能从该文件单独推出同一红圈内一段建筑墙；要得到混合结果，需同时上传对应建筑平面图或提供明确的建筑墙图层/编号。
- 对任务 `task_ed6aa543fc2f45abb5f5c1eebfe6c70d` 的只读几何重放确认：120 → 122 面墙，新增两段 `Q6-800mm`；225 根柱不变，几何 Gate 通过，墙—墙和墙—柱碰撞均为 0。该验证未修改原任务、未重新写入 RVT；旧交付不会自动更新。
- 历史验证覆盖墙体几何、Revit 合同和独立叠图审计；真实 Revit 2020 视图切换仍需在 Windows Revit 环境执行。

## 输入配置

模板为 `config/wall_pipeline.example.yaml`。路径相对配置文件解析；`source.files` 可以列出一份或多份同类来源，每份来源都带独立 `role`，CLI 配置还可用 `page_no` 只选 PDF 的指定页。项目图层规则、源文件、单位、坐标原点、轴网、楼层和 Revit 目标都只存在于配置中。核心代码不含图号、图框边界、固定轴距、个人目录或项目墙层名。几何计算的规范化坐标仍以米保存；Revit 交付属性、项目显示单位和操作员可见尺寸统一为毫米（mm），不会以英寸作为工程单位。

独立叠图比较几何边缘，不比较 Revit 自动生成的轴网端头文字。导出审计 PNG 时临时隐藏轴网气泡但保留轴线，导出后立即恢复气泡，因此交付 RVT 仍保留完整轴网名称。

DWG 使用 ODA File Converter。配置 `source.converter.executable`，或设置 `ODA_FILE_CONVERTER`；转换失败、代理对象/放置展开失败和 DXF 审计错误都会进入诊断或终止流程。PDF 的页面点本身不能推导模型比例；未提供 `coordinate.scale_to_m` 或 `coordinate.calibration` 时，系统只允许从同一来源的主平面标题文字（例如 `墙柱配筋平面图 1:150`）提取可追溯比例，并把候选值、原文和实体 ID 写入 `PDF_SCALE_INFERRED`。主平面比例并列不清时明确失败，由操作员填写比例覆盖值，不按几何结果反推比例。

PDF 适配器在来源边界将 PyMuPDF 的页面坐标统一为未旋转 media box、左下角为原点的 `pdf_media_box_y_up`（单位 pt）坐标系；每一页都有稳定的 `frame_id`（例如 `source_0001:page:0002`），并在实体 `style` 中保留页面尺寸、旋转角和 media box 元数据。CAD 文件的 frame 是其 `source_file_id`（例如 `source_0002`）。`role` 只说明来源用途，不代表两个 frame 已对齐；不同页面或补充图纸默认分别识别，禁止仅因局部坐标相似就跨 frame 配成一堵墙。只有在 `coordinate.frame_offsets_m` 为每个参与 frame 显式登记后，才允许跨页/跨文件配对。PDF 的 source 级 offset 可作为各页的平移默认值，但不会自动开启跨页配对；要跨页配对仍必须登记精确的页面 `frame_id`。网格轴可通过 `grid.axes[].frame_id` 选择精确页面，旧配置继续使用 `source_file_id`。

来源进入清单前会先校验 regular file、非空和 512 MiB 大小上限，随后才计算 SHA-256；哈希前后会比较文件元数据，检测到文件变更会以 `SOURCE_CHANGED_DURING_HASH` 明确终止。适配器开始解析前还会复核 manifest SHA-256，替换或删除的来源以 `*_SOURCE_CHANGED_AFTER_MANIFEST` 拒绝，不让解析器继续消费不一致输入。PDF 页数上限为 500，解析实体上限为 500,000；超限、页数损坏和解析器异常均返回带错误码的失败。

执行：

```powershell
.\.venv\Scripts\python scripts\run_wall_pipeline.py run config\wall_pipeline.example.yaml
```

## 当前工作流接入

上传一份或多份 PDF、DWG 或 DXF 后，只能提交 `wall_pipeline` 工作流。它是 BIM
Agent 唯一入口；IFC/图片不再进入 BIM 墙体主链路，仅在隐藏迁移适配器中保留历史
数据读取能力。任务会把每个 JSON/渲染文件登记为不可变 Artifact，
并在两个门禁处暂停：

```text
wall_pipeline_prepare
→ waiting_human（WallModel 审核）
→ wall_model_approval_and_dry_run
→ waiting_human（Revit 写入审批）
→ revit_write_approval
→ succeeded（默认只生成 write_approved 交接物）
```

如果 Windows Revit Bridge 与后端在同一可访问文件系统上，并且任务明确允许
外部执行，可在提交选项中设置 `execute_revit: true`（或
`revit.execute: true`）。第二次人工批准之后，Worker 会调用类型化的
`/tools/run-wall-model`，Bridge 在隔离 RVT 工作副本中编译 WallModel、执行
Transaction、读回墙体数量并导出实际平面视图。Worker 只有在实际 RVT、PNG
和 SHA-256 均返回后，才会运行独立叠图审计并将结果标记为交付完成；Bridge
不可用、读回不一致或叠图失败都会使任务失败，不会静默降级为成功。

```json
{
  "execute_revit": true,
  "revit": {
    "target_model_path": "C:/models/project.rvt",
    "floor_code": "L1"
  }
}
```

`execute_revit` 默认是 `false`。无论是否开启，写入都必须同时满足确定性门禁、
静态 Dry-run、持久化人工审批、Bridge 执行开关和短时 HMAC 审批令牌；Bridge
只写隔离工作副本，原始 RVT 不会被直接覆盖。

通过 `POST /api/v2/workflows` 提交时，`input_artifact_ids` 必须至少包含一份源图纸且 ID 不得重复；可提交多份 PDF，或提交一组 CAD（DWG/DXF 可混用），但禁止把 PDF 与 CAD 放进同一个任务。所有来源都先通过当前 tenant/project 的 Artifact 服务解析，再以净化后的扩展名文件名原子复制到任务私有目录；任务不会直接信任上传文件名、客户端路径或对象存储缓存路径。暂存前后会校验 regular file 和大小；若 Artifact 带 SHA-256 则必须匹配，缺失时仍以暂存文件哈希建立 manifest。随后 manifest、adapter 入口与解析结束再次验证来源身份。

`options.source_roles` 按 `input_artifact_ids` 的顺序一一对应，长度必须完全相同；省略时第一份默认为 `plan`，其余默认为 `supplementary`。前端会把文件名含“详图/大样/构件表/柱表”的来源标记为 `column_detail`，供柱/梁规格解析识别来源优先级。角色会进入 `source_manifest.json` 和证据链，但不会隐式完成几何配准。`project_id`、`options.revit.target_model_path` 必填；楼层编码可以由明确的 `options.level.elevation_range` 自动推断时省略（例如 `-6.4~0` 推断为 `B1`），其他情况仍必须提供 `options.revit.floor_code`。
比例、单位、图层、楼层和轴网等参数放在 `options.coordinate`、`options.wall`、
`options.level`、`options.grid` 中。PDF 可省略 `scale_to_m`/`calibration` 以启用主平面标题栏比例识别；人工值一旦提供即作为显式覆盖。
人工决定仍使用 `POST /api/v2/tasks/{task_id}/resume`，每次决定都会带操作者并
持久化到任务事件和生成的 Artifact。任务恢复消息由 Outbox 以租约代数投递；旧 Worker
在租约过期后返回的成功或失败回执会被 CAS 拒绝，不会把新的交付尝试误标记为已完成。
审批动作由等待阶段推导：`wall_model_review` → `revit_write`。

多来源提交示例：

```json
{
  "project_id": "project-demo",
  "workflow": "wall_pipeline",
  "input_artifact_ids": ["art_plan_pdf", "art_structure_pdf"],
  "options": {
    "source_roles": ["plan", "structural_supplement"],
    "coordinate": {
      "scale_to_m": 0.000352778,
      "frame_offsets_m": {
        "source_0001:page:0001": [0.0, 0.0],
        "source_0002:page:0001": [0.0, 0.0]
      }
    },
    "level": {
      "id": "level-b1", "name": "B1", "elevation_range": "-6.4~0",
      "elevation_m": -6.4, "top_elevation_m": 0.0, "wall_height_m": 6.4
    },
    "modeling_standard": {
      "profile": "cn_gb_bim_delivery_v1",
      "references": ["GB/T 51212-2016", "GB/T 51269-2017", "GB/T 51301-2018", "GB/T 51235-2017"]
    },
    "revit": {"target_model_path": "C:/models/project.rvt", "floor_code": "B1"}
  }
}
```

## 六个固定合同

每次运行先在 `output_dir` 下的任务私有 staging 目录完整生成，再逐文件原子替换到发布目录；若替换过程中发生可处理的错误，会删除已发布的新文件并恢复旧 bundle（进程被强制终止时不宣称目录级原子性）：

1. `source_manifest.json`：来源解析后的绝对路径（当前工作流为任务内暂存路径，CLI 为配置解析路径）、角色、SHA-256 与配置身份。
2. `source_entities.json`：PDF/DXF 的统一线、多段线、MLINE、HATCH 边界和文字实体。
3. `wall_evidence.json`：双线/闭合条带墙体证据、来源实体、页码/句柄和变换链。
4. `wall_model.json`：米制墙体、柱/异形柱、连梁、墙厚/截面、轴网、T/L/Z 连接、洞口语义、国标 profile、分类/楼层/材料/数量/来源参数、门禁与人工审批。
5. `revit_result.json`：Dry-run/事务/回读/实际视图结果；初始状态不会伪装为成功。
6. `audit_report.json`：两份独立渲染的哈希、边缘覆盖、叠图和通过/失败原因。

合同代码事实源是 `backend/engines/wall_pipeline/contracts.py`。`created_at` 不参与工程内容哈希，所以相同文件、配置和算法能生成相同阶段身份。阶段谱系固定为 `SourceManifest → SourceEntities.manifest_sha256 → WallEvidence.source_entities_sha256 → WallModel.wall_evidence_sha256`；两个 HITL 门禁都会从 Artifact 存储重新读取并校验 tenant/project、合同哈希、EvidenceRef 和模型引用，不能只相信上一步进程内 payload。首次 WallModel 审批还必须绑定准备阶段的 `revit_result.json`：其状态必须仍为 `pending_approval`、没有审批记录，且 `wall_model_sha256` 必须等于当前模型哈希；模型、结果或来源文件任一被替换都会停止流程。网格 `frame_id` 只能来自适配器实际登记的 frame：PDF 仅接受源文件页表中真实存在的页面 frame（`source_NNNN:page:MMMM`，即使该页没有实体也可供网格使用）或 source-level 默认，CAD 仅接受 source-level frame，不能用任意 `frame_offsets_m` 键制造坐标系。

## 审核与 Revit

### 剪力墙洞口语义证据

当配置 `opening.label_layers`、`opening.boundary_layers` 和
`opening.label_pattern` 后，来源适配器会保留例如 `JD3` 的文字实体及同帧
闭合矢量边界。几何引擎把它们登记为 `wall_model.json.openings`，并关联最近
的墙宿主和原始 `source_refs`。文字只负责分类，洞口中心、宽度和深度全部来自
矢量边界；没有边界或没有墙宿主的标注会标记为 `review_required`，不会静默
生成墙体坐标。柱墙避让后会再次执行端点修复，再跑一次避让，确保连接修复
不会重新制造实体碰撞。
平行墙的端点修复只接受同一直线上的制图级横向误差；相邻但平行的墙面不会
互相吸附。真正的 L/T/X 交点保留为连接，重复平行实体则按证据强度确定性去重。

洞口分为“语义/平面已匹配”和“具备实际开洞条件”两种状态，不能混为一谈。
`opening_specs.py` 通过“洞口编号、洞口尺寸（宽×高）、洞底标高”三个表头，按同页同排/旋转列
关联尺寸和标高，再校验唯一宿主、平面宽度、墙段覆盖和竖向范围。平面边界的深度是墙厚，不能当成洞高。
`opening.elevation_reference` 必须选择 `project`（项目 ±0.000）或 `level`（相对本层底部）；
未选择或证据冲突时，`cut_status=review_required`，显示具体原因，不创建假洞。
图纸说明会自动确定洞底标高是相对于项目 ±0.000；若旋转 OCR 漏掉负号，且当前输入楼层明确是全地下层，
系统将正的无符号表值记录为 `elevation_source=drawing_inferred_basement` 并按深度转成负标高，同时保留说明证据。
存在显式正/负号或人工校正时始终以显式值优先。前端仍支持本次任务的 `编号=洞底标高` 校正，如 `JD3=-2.200`，单位 m。
校正值保存在 `opening.sill_elevation_overrides_m`，绑定任务配置和审批，并在洞口记录中标为 `elevation_source=input`；
不会因为是地下室就自动把全部正数变成负数，也不会自动继承到另一张图纸。

审核后的 `cut_status=ready` 洞口由 Revit 原生 `NewOpening(Wall, XYZ, XYZ)` 创建，在事务内
读回 `Host` 与 `BoundaryRect` 的位置、宽度、底/顶标高；失败回滚当前事务并明确失败。
Bridge 回执记录 `opening_semantics.cut_count` 和 `created_opening_ids`，没有物理洞口回执不能报告开洞成功。
例如洞底 −2.200m、表格高度 1500mm，则洞顶为 −0.700m，不额外叠加 B1 底标高。
宿主断墙、多个宿主、缺失尺寸/标高、宽度不符或切割重叠，仍需核定，不强行贯通。

### 灰色交付视图

构件使用灰色表面/截面和灰色轮廓线，仅覆盖本次生成的墙、柱和梁，不修改原材料或其他构件。
先输出独立 Revit 审核 PNG，再应用灰色显示，避免显示样式改变验收分数。
交付保留 `BM-ACTUAL-楼层` 平面视图，同时提供 `BM-3D-楼层` 灰色三维视图。
洞口高于平面切平面时，平面墙线可以仍然连续，需在三维视图检查实际切割；三维展示不替代独立叠图验收。

### 连梁与异形柱

结构图中的 `S-WALL-BEAM`/`S-BEAM` 线对先经过独立的双边配对，生成
`wall_model.json.beams`，不会再混入墙体候选。`LL01` 等文字只负责给已经存在的
矢量梁标记类型；未标注梁使用内容哈希作为稳定类型后缀，并明确标记规格待核定。
连梁在 Revit 2020 中使用 `OST_StructuralFraming` 的原生梁族类型；有图例/详图编号时，
族类型按图纸编号加宽×高命名（例如 `LL1-300x700mm`），写入 `BM_FamilyName`、
`BM_FamilyType`、`BM_TypeMark` 和数量参数后逐项回读。目标模板未加载梁族时，Bridge
会从已安装的 Revit 2020 标准库加载“混凝土 - 矩形梁”族；标准库不存在且模板中
也没有可写宽/深参数的结构框架族时明确失败，不把连梁伪装成墙或通用模型。

连梁标高是独立的来源证据，不再默认取楼层标高。解析器支持同一行的“梁顶/梁底
相对标高 +0.150/-0.350”、结构表格中分列的 `LL/KL` 编号与标高值（包括 OCR 把 `0.150`
拆成 `0. 150` 的情况），以及 CAD/PDF
常见的 `h+4.200`、`h-0.200` 缩写。表格关联要求同页同 frame、同一行轴向对齐并处于
受限的表格距离内；管线的 `DN...h+...` 注记会被排除。只识别到顶或底一侧时，另一侧
由已解析的连梁高度确定性计算，并保留相同证据引用。`BeamRecord` 会写入
`top_elevation_m`、`base_elevation_m`、`elevation_status`、`elevation_source`、
`elevation_text` 和 `elevation_refs`；无证据为 `unresolved`，互相矛盾或与梁高不一致为
`conflict`，两者都会进入审核诊断，不会被伪装成已解析的 0 m；未解析时以目标 Revit
没有图纸证据时，若说明明确写出“未注明连梁梁顶标高同该层顶板标高”，则使用用户输入楼层区间的顶标高作为
`level_default`，并按梁截面深度推导物理底标高；没有该说明时仍保持 `unresolved`，使用楼层底标高作为待审核的几何基准。
Revit Bridge 2020 使用
`base_z` 创建结构梁后，按梁实体包络的底/顶标高校正族的插入基准，实例参数 `BM_BeamBaseElevationMm`、`BM_BeamTopElevationMm`、
`BM_BeamElevationStatus`、`BM_BeamElevationSource` 和 `BM_BeamElevationText` 同步写入，
事务完成后再次读取梁实体包络；缺失或不一致会使交付失败并回滚。

柱轮廓先由闭合矢量/HATCH 边界确定，再由 `GBZ/YBZ` 文字绑定图纸编号；OCR 不能
移动轮廓。PDF OCR 丢失尺寸分隔符时，仅对明确的柱编号邻近项恢复 `9001000`、
`1600x1100` 等尺寸并保留原始文本引用，几何与图例冲突时进入审核而不四舍五入。
PDF 将柱轮廓拆成多个独立 drawing path 时，适配器只在同一源帧/图层内按端点连接
重建闭合边界；三边及以上的有效多边形都保留为异形柱，避免把三角形或 L/T 轮廓
静默丢弃。明确标记为非矩形或 `profile_directshape` 的轮廓在 Revit 2020 中使用精确
DirectShape 拉伸；只有识别结果明确为矩形时才优先绑定可编辑结构柱族。
异形柱保持精确轮廓；有图例/详图编号和截面规格时，族类型使用编号加规格；尺寸未核定时使用
`编号-规格待核定-稳定构件ID`，不同未知截面不共用可变类型。即使异形轮廓的
外包框恰好接近四边矩形，也不能据此外观改成矩形族；只有 `profile_kind=rectangular`
且目标模板存在可写宽深参数的结构柱族时，才使用原生族实例。其他情况使用结构柱
类别 DirectShape，并在 `BM_Representation` 标记实际表达，避免把异形几何伪装成
矩形族实例。
内容寻址的已审核轮廓只能提供形状模板；每个落位还必须在当前上传图纸中找到同编号、
邻近且未被其他实例占用的文字证据。文件哈希、轴网配准和当前编号三项缺一不可，避免
把训练图中的相邻 GBZ/YBZ 轮廓批量注入当前模型或造成重叠柱。
异形柱编号旁的两个尺寸可能描述局部肢长，而不是完整轮廓的外包宽度和深度；因此
异形柱保留这组文字规格及来源，但不再用它和外包框做矩形截面冲突判断。矩形柱仍按
图纸宽×深与矢量测量值做严格容差校核。

### 构件信息与算量

### PDF 解析速度与重复任务缓存

PDF/DWG 主链路的确定性几何解析不会因为提速而跳过墙体、柱或轴网。对于没有
可搜索文字的 PDF，只有 OCR 文字证据需要栅格识别：默认仍为 150 DPI，但使用
3,000 px 重叠分块，减少 A0 图纸的重复推理次数。具体耗时随图纸复杂度和机器配置变化，
不在合同中承诺固定秒数；关键墙厚、构件编号和 `JD` 标注仍保留在证据合同中。

OCR 候选结果按源文件 SHA-256、页码和 OCR 参数写入
`data/runtime/wall_pipeline/.ocr-cache`。缓存只保存可重新校验的文字框和置信度，
不保存或替代原始图纸，也不参与墙体坐标决定；缓存损坏、参数变化或源文件变化时
会自动重新识别。可通过环境变量 `WALL_PIPELINE_OCR_CACHE_DIR` 指定独立缓存卷。
同一张图纸从前端重复测试时，首次运行仍需完整解析，后续任务可按输入哈希、解析配置和
缓存版本复用 `SourceEntities`（缓存目录为 `data/runtime/wall_pipeline/.source-cache`，
可由 `WALL_PIPELINE_SOURCE_CACHE_DIR` 覆盖）。缓存损坏或算法版本变化会自动回到完整解析；
几何、审核门禁和 Revit 独立叠图流程保持不变。

### Revit 交付超时

`wall_pipeline_prepare` 与 `revit_write_approval` 使用独立的步骤超时。Revit 交付步骤
包含 Bridge 事务、读回校验、RVT/视图制品持久化以及独立叠图审计，不能使用普通 Agent
的 120 秒默认值。默认值为 `WALL_PIPELINE_REVIT_WRITE_TIMEOUT_SECONDS=900`，可按机器和
模型规模在环境变量中调整（180–3600 秒）；超时仍表示该步骤未完成，系统不会因为已有
部分文件就伪造成功状态。

### 图纸规格解析规则

墙厚、柱截面和连梁截面不再从测量值四舍五入生成。例如几何测量为 `264.7 mm` 时，系统
不会自动命名为 `265 mm`。来源适配器会保留图例、构件表、墙/柱表、大样和同帧
标注文字；PDF 没有可搜索文字时，适配器仅用项目已有 RapidOCR 生成带坐标的
语义文本证据（不参与墙柱坐标计算）。规格解析器按“大样/详图 > 构件表/明细表 > 图例/说明 > 普通标注”的
优先级解析 `267`、`600x300` 等尺寸，并把原文和 `source_refs` 写入
`construction.specification_*`。连梁按 `LL/KL` 编号解析 `b×h`（宽×高），随后用几何测量值做容差校验：一致才标记为
`resolved_from_*`；没有可追溯规格标记为 `unresolved`；冲突标记为 `conflict`，
进入人工审核。只有“墙厚/柱截面”等明确规格，或墙柱编号与截面位于同一表格行/同一对齐列时才可作为规格；PDF 页面旋转导致的相邻表格行也会按受限的同轴规则关联，并限制在同一标高列的横向容差内，避免把说明文字、管线 `h+` 标注或邻近表格数字误绑定；配筋间距、标高、洞口宽高和普通尺寸会被排除。带引线的规格可在同一图框内通过“真实图纸规格与几何测量在容差内相等”完成关联，但几何不会产生新规格。文字只决定类型和规格，不提供或移动构件坐标。

Revit 属性 `BM_ThicknessMm`、`BM_WidthMm`、`BM_DepthMm` 只有在规格证据解析成功
时才使用图纸数值（图纸写整数则显示整数）；未解析时保留几何实测值并同时显示
`BM_SpecificationStatus=unresolved`，禁止把实测值伪装成图纸规格。项目显示单位
仍统一为毫米，但编译器不再强行设置全局 1 mm 精度，以免掩盖图纸规格冲突。
当墙、柱或连梁成功关联图例编号和整数毫米规格时，`BM_FamilyType`、`BM_TypeName`
统一使用“编号-规格mm”（例如 `Q1-500mm`、`KZ1-600x500mm`、`LL1-300x700mm`），`BM_TypeMark` 保留编号；Revit 原生 Family.Name（如 Basic Wall）
作为族容器保留，不把族容器误改成构件编号。没有可追溯编号的构件保留稳定回退名并显示风险。
没有来源规格时，类型名不再拼接实测小数，也不四舍五入虚构整数规格；显示“规格待核定”。
矩形柱在规格与矢量截面通过已有容差校验后，用图纸的确切截面尺寸同步轮廓、类型和工程量，保留中心/朝向。
异形柱的局部肢长不用于缩放整个轮廓；测量坐标/轮廓允许小数，不能为名称整齐而破坏原始几何。

前端建模入口要求用户填写墙柱材料；材料、楼层和 Revit 目标与图层、比例一样，
属于项目输入配置，不写死在识别算法里。图纸没有给出强度等级时，演示默认值会明确
标记为“钢筋混凝土（强度等级未注明）”，不会伪造 C30/C40 等等级。WallModel 为每个
墙柱保存类型、类型标记、IFC 分类、材料状态、来源引用、确定性长度/面积/体积和算量
依据；Revit 编译器把这些参数直接写入每个构件的实例属性：`BM_` 共享参数（构件编号、
族名、族类型、实际表达方式、墙体结构角色、类型、材质、标高、墙厚/柱截面、图纸规格状态/来源/原文、来源计数、
置信度和毛量）以及原生 `Mark`、`Comments`。矩形柱优先使用目标模型中匹配的真实结构柱族；
异形轮廓或目标模型没有可匹配族时，才使用明确标记的精确轮廓 DirectShape，并在事务内逐项
回读校验。
不会创建明细表，也不会把构件参数只放在外部报表中。洞口没有竖向尺寸证据、尚未执行
实体开洞时，体积明确标为 `GROSS_NO_OPENINGS`，不得当作净量。
已实际开洞时，`BM_GrossVolumeM3` 仍然是毛量；净量需读取 Revit 原生切割后的体积，不以旧毛量代替。

前端要求用户填写本层标高范围，例如 `-6.4~0`（单位 m），表示本层实体底标高为
`-6.4m`、顶标高为 `0m`，本层墙柱高度确定为 `6.4m`。Revit 项目中的原生 Level 仅作为
宿主与楼层语义，不会覆盖这个物理标高；当两者不一致时，墙体使用带符号的 Base Offset，
柱体使用族偏移或精确 DirectShape，连梁使用实体包络校正。后端将范围归一化为
`level.elevation_m`（底标高）、`level.top_elevation_m`（顶标高）和 `wall_height_m`，
墙柱的底/顶和数量计算均基于这一组值。对于 `-6.4~0` 这种唯一可判定的范围，系统
自动推断楼层编码为 `B1`；其他负标高范围仍要求用户填写 `B2`、`B3` 等项目编码，
不根据高度猜测地下层数。用户显式填写的 `revit.floor_code` 优先于自动推断值。
如果用户不提交范围而只提交楼层编码，系统仍兼容旧合同：WallModel 会增加
`LEVEL_ELEVATION_UNRESOLVED` 风险提示，Revit 通过楼层编码查找目标 RVT 的原生 Level，
不会把默认 `0m` 当作图纸事实。

### 续建记忆

前端保存最近一次成功的 `wall_pipeline` 任务编号，操作者可点击“使用最近一次成功交付”。
后端接收 `options.continue_from_task_id` 后，只从同租户/同项目、已成功且有持久化
`revit_output.rvt` 的任务复制隔离基模型，再执行新图纸的全链路；客户端不能用任意本地
路径伪造续建来源。续建关系会写入新任务的 `structured_output.continued_from_task_id`，
旧交付文件保持不变，可随时回滚或重新下载。

属性字段统一使用 `BM_` 前缀，例如 `BM_ElementId`、`BM_FamilyName`、`BM_FamilyType`、
`BM_Representation`、`BM_TypeMark`、`BM_Material`、
`BM_ThicknessMm`、`BM_HeightMm`、`BM_WidthMm`、`BM_DepthMm`、`BM_GrossVolumeM3` 和
`BM_SourceRefCount`。连梁另外写入 `BM_BeamBaseElevationMm`、
`BM_BeamTopElevationMm`、`BM_BeamElevationStatus`、`BM_BeamElevationSource`、
`BM_BeamElevationText` 和 `BM_BeamElevationRefCount`。这些是构件实例参数，选中
构件即可查看；不是 Revit 明细表。

墙体结构角色由来源证据确定并写入 `construction.structural_role` 与
`BM_WallRole`：`S-WALL`/结构图纸证据为 `shear_wall`，明确建筑图层为
`architectural_wall`，结构与建筑来源冲突或无法证明时为 `unresolved`。只有
`shear_wall` 会调用 Revit 的结构墙开关；未知角色不会被默认当成剪力墙，也不会把
几何厚度当作语义依据。

墙体连接采用“外侧面接触、不中断闭合”的规则。L/T/X 节点只把终止墙端点移到
相交墙的外侧面，位移为相交墙半厚度；不再把两面墙的半厚度相加后同时修剪。后者
会在核心筒 T 节点留下约等于两墙宽度的可见断缝，导致原图闭合而模型开口。重新运行
流水线后，交叉墙实体在边界处接触，非平行节点仍由 Revit Join 处理；平行重复墙和
墙柱碰撞继续由独立去重/归属门禁处理。Revit 编译器在审计平面和交付三维视图
中显式解除墙、结构柱、结构梁和轴网的类别隐藏，避免模板的可见性设置让模型看起来
为空；结构墙同时写入 Revit 原生结构标志和 `BM_WallRole` 实例参数。

Revit 2020 的共享参数文件由 Bridge 在每次交付工作目录中生成，使用 UTF-16 LE
和 Revit 2020 兼容的七列 `PARAM` 记录（不带花括号 GUID）；写入前会绑定到墙和
结构柱和结构框架类别，写入后逐个实例回读。这样参数不会依赖 Revit 当前语言或模板中是否
预先存在同名项目参数。

流水线默认只生成 `review_status=pending`。人工审核后显式批准：

```powershell
.\.venv\Scripts\python scripts\run_wall_pipeline.py approve `
  data\runtime\wall_pipeline\wall_model.json `
  --actor reviewer-id --reason "几何、墙厚和拓扑已复核"
```

几何批准只进入 `ready_for_dry_run`，不会被记作 Dry-run 已通过。继续执行静态 Dry-run，再对 Revit 写入做第二次人工批准：

```powershell
.\.venv\Scripts\python scripts\run_wall_pipeline.py dry-run `
  data\runtime\wall_pipeline\wall_model.json

.\.venv\Scripts\python scripts\run_wall_pipeline.py approve-revit `
  data\runtime\wall_pipeline\wall_model.json `
  --actor revit-approver-id --reason "Dry-run 与目标模型已复核"
```

CLI 的 `approve`、`dry-run`、`approve-revit` 和 `audit` 与 API 工作流使用同一
本地谱系门禁：必须能读取同目录（或工作流传入的私有副本）
`source_manifest.json`、`source_entities.json`、`wall_evidence.json`，并重新校验
合同哈希、来源文件 SHA-256、证据定位、坐标 frame、轴网和拓扑引用。缺失或被替换
的上游文件会直接停止，不会只凭 `WallModel` 自身的哈希继续交付；重复审批和绕过
`pending_approval`/`ready_for_dry_run` 状态也会被拒绝。

pyRevit 优先读取 `wall_model.json`，并在任何 Revit Transaction 之前再次校验：合同版本、确定性门禁、几何批准、Dry-run 结果身份、Revit 写入批准、证据、单位、项目、楼层、目标 RVT 和坐标原点。随后先创建轴网，再创建墙体；失败沿现有事务路径回滚。

## 独立验收

源侧验收必须来自原始来源：PDF 始终由 PyMuPDF 重新打开原文件页面渲染；DXF/DWG 使用来源适配器重新解析出的源实体（只含来源文件事实，不读取 `WallModel`）。源侧在进入渲染前会重新校验 manifest 中的文件哈希。它从不读取 `wall_model.json`，因此不会用模型结果反绘原图。Revit 侧必须提供事务完成后的实际视图文件及其 SHA-256。两份文件路径或内容哈希相同会直接阻断审计，避免同源自证。

交付门禁固定为独立比较的 **95%**：`edge_iou`、`source_edge_coverage`、`revit_edge_precision` 三项均不得低于 `0.95`，并以三者最小值作为 `similarity_score`。调用方传入更低阈值也不会降低这个硬门；任一项不足时任务失败，不发布 Revit 交付物。

```powershell
.\.venv\Scripts\python scripts\run_wall_pipeline.py audit `
  data\runtime\wall_pipeline\wall_model.json `
  data\runtime\wall_pipeline\revit_result.json
```

## 失败策略

- 单位、PDF 比例、文件、ODA 或坐标无法确定：停止，不生成猜测墙体。
- 来源 Artifact 重复、PDF/CAD 混合、暂存哈希不符或解析期间来源变化：停止，不发布混合谱系产物。
- 网格 frame 未在来源实体中登记、PDF 页 frame 不存在或 CAD 使用 page frame：停止，不应用偏移或进入 Revit。
- 多页/多文件来源缺少精确 frame 配准：保持 frame 内识别并阻断跨 frame 配对，不以 `role` 或重合坐标猜测对齐关系。
- 没有双线/闭合条带证据：门禁失败；单线只可由配置显式开启，并保留低置信度限制。
- 证据或来源定位缺失：不得批准、不得进入 Revit。
- 未批准：Revit 拒绝。
- 没有实际 Revit 回读或独立视图：审计为 `blocked`，不得宣称验收通过。
