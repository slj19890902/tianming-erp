# 整组加工保存恢复：CONTRACT v1 独立预审

日期：2026-10-10。结论：**合同预审通过，未发现需阻断实现的 P1/P2；不等于候选实现验收或 group 已发布。** 仅审 action=dispose 的 semi/finished 保存与原键恢复，以及同 group 后续入口的未知锁。API/UI 仍在各自新树开发，最终 SHA、实际事务/HTTP 和页面函数交叉验证待定稿后进行。

本轮只读取规则、任务卡、合同、既有审查证据及实际基线源码；没有运行中间候选、业务探针、大测试组、浏览器或进程操作，没有改仓库、正式库、迁移或提交/推送。只增加本独立报告和 NAS 同内容回执。

## 审查对象与证据边界

- 已依次读取 CODEX_START、NAS AI_START、任务卡、ORDER_FLOW、总需求相关 7/14/17 章和执行章程 3～9；未变入口沿用上一轮已读上下文。
- 直接对照 `group-preparation-save-recovery-fix/TASK.md`、`api/CONTRACT.md` v1、独立 `PLAN.md` 和 `group-preparation-save-recovery-audit/api/REPORT.md`。复用审查作者的 8 HTTP / 3 实际页面方法观察，未把复用证据称作本轮重跑。
- 任务卡固定正式源码 `5b167953446d07d9b77718e05061099803932cfb`、v0.22.603。正式发布与备份状态依任务卡/根回执，本轮未重新访问正式服务证明它们。现场验收仍 pending。
- 新 API 树 HEAD 核为上述正式基线，已出现作者新增的专用测试文件；这是允许的在途开发，不要求作者树此时干净。本轮不审该未定稿文件。
- 核对的 10 个实际模型/服务文件，在只读稳定树 `c02693f8a678b1a72ed3de285b32633842b7c22c` 与正式 `5b167953` 之间 Git diff 为空：product、product_bom、stock_replenishment、warehouse_inventory 模型，以及 stock_preparation_groups、stock_preparation_disposition、stock_preparation、stock_preparation_history、product_unit_labels、warehouse_inventory 服务。API group 段亦按上一轮精确片段与正式合同核对，不宣称整个 API 文件未变。
- 本次审阅 TASK SHA256：`E3B4E0386FFD39E0F7C7618F5F2095972960972F7881C3120D6D04E41CA1102D`；CONTRACT v1 SHA256：`9F4A793877EF9EC519861B1434171EB70353162092FE0ADFCD5F4704D0388C66`。后续合同修改需另确认，不把此报告套用于未知版本。

## 合同与合法业务的逐项核对

| 项目 | 独立结论与源码依据 |
| --- | --- |
| 多 job 同产品 | 正确。`groups.preview` 按子件遍历多来源 receipt，`dispose`/`members` 按 job ID 执行。新 proof.children 必须保持每 job 一条、允许 product_id 重复；recipe.children 每产品一条有 `product_bom_components(parent_product_id, component_product_id)` 唯一约束支持。不能把两层数组混成同一唯一性规则。 |
| recipe 版本 | 正整数约定有实际模型支持：`Product.version` 非空、默认 1，`ck_products_version` 要求 >=1；recipe 在安排时冻结 parent/child.version。恢复必须读冻结值，不能要求仍等于当前 Product.version。 |
| semi 少产 | 正确。`disposition.dispose` 逐 job 调原 `prep.mutate` 完成，不传 actual_input_quantity；实际输入是完整 job.input_quantity。14/19 产出与 15/20 投入并存，没有 continuation。不能套用 single 的部分投入合同或重复乘开料/模数。 |
| finished 少套/余片 | 正确。`assemble` 以剩余配比逐 job 取 `min(remaining, lot.quantity_available)`，take=0 跳过。proof.inputs 是非零子集，不要求每 job 出现，也不要求加工产出全部被组装。按 product 汇总各输入等于 per_set × sets，不能按数组位置对应。 |
| 首次产量与余量 | 正确。每子件首次 manual_in 与本次 assembly consume 分列；remaining_stock_quantity 是本请求完成时首次产出减本次组套消耗的冻结事实，不能读取后来 available 或称当前库存。新 helper 必须匹配精确 lot / movement / job / operation key，不能按同产品挑一条。 |
| 单位 | 合同的分层正确。半成品底层 manual_semi_finished_in 固定 ledger unit=sheets，产量是实际子件计数，profile 标 output_piece / quantity_unit=pieces；冻结产品显示单位另列。finished 的操作 sets_unit=套 与实际成套 manual_in 台账量/单位另列。不得把片/套/原纸张数相加，也不得靠名称统一数字。 |
| nullable | 合法。来源 `StockReplenishmentOrderItem.customer_id` 可空，已有真实 nullable HTTP；冻结 recipe.customer_id 来自非空 Product.customer_id，不能同样放空，也不能反向要求每个 receipt 客户必须等于 recipe 客户。WarehouseLocation.warehouse_floor 可空、location_name 非空列但空字符串合法。缺可靠物理显示标签可 null。位置 ID、实际台账单位/数量、原身份仍严格核对。 |
| 位置 | 正确。semi 取每 job 原请求的位置/layout；finished 的子件和成套取顶部位置/layout。finished 原 jobs 行里未实际使用的位置仍属于原请求签名，不能删除或假称它们是产出地址。空名称仅降级显示 ID，不补写主资料。 |
| 原请求 | 正确。沿用 GroupAction 默认字段，仅删 jobs[].lot_id=null，compact JSON 不改，jobs/sources 原数组顺序保留。proof.children 可按 ID 映射，不反向重排 request。expected_actor_id 严格正整数、排除旧 model_dump/签名，并核认证账号与持久原 actor。 |
| 父首次 INSERT | 正确。dispose 子件完成及可选 assemble 在原 atomic_bom 内完成后，父 Command 第一次 INSERT 前 builder 装配证明。旧键先精确 actor/request 重放，不运行 builder。不能对已 INSERT/flush 的父记录 UPDATE，即使仍在事务内；sprep0912 禁改触发器须在测试实际启用。 |
| 首次/重放 | 正确。新写整个响应在父 result_json 一次冻结；同 actor/key/body 整体 JSON 相等，不加入 replayed 等动态字段。旧无 proof 的父记录只能 legacy_trace，不能由当前库存/任务补证明或补写历史。 |
| 只读恢复 | 正确。原 key、actor、完整规范化 body、group、parent 以及持久 proof 精确对应才 completed。当前 parent/每原 receipt 身份权限和原冻结客户范围独立核，不能使用当前 recipe()/prep.source 的 active/BOM/posted/quantity 写资格去否认原保存。no_autoflush、零业务 flush/commit；标准拒绝审计保持。 |
| 未查到/拒绝 | 正确。not_recorded 只是时点未见，不代表原在途请求取消；不自动 POST/删键/换键。Rejected 仅同写锁 fresh 判定、提交前业务失败及完整回滚成功，旧 unknown 后再收到它也不能解锁。其他 action/actor/body 占原全局 key 属冲突，不应回 not_recorded。 |
| 前端未知锁 | 正确。首 POST 前可靠落盘并读回原 body/key；按 actor + 原 group_key 锁，包含同组 group_stock 的 store_outputs/assemble。不同组不锁，不能扩大成 parent/product 锁。恢复卡独立于列表 error/empty；成功、刷新失败、缓存清理失败分别表达。 |

## 需要落实的追溯界限

现有 `app/api/stock_preparation.py:29` history 入口及 `app/services/stock_preparation_history.py:38` 的 completed_groups 仍调用 `prep.source`；后者含当前来源写资格。来源后来改变时，原 key 的新只读 resolver 应仍可核实持久保存，但当前 history GET 可能失败。这是旧追溯入口的限制，不是本轮新发现的重复扣库、权限绕过，也不扩大本卡改 history。

因此 `trace_url` 仅表示安全的当前组追溯入口，不能作为完整证明的前置条件或承诺随时可打开。API resolver 不得先调用 history/completed_groups/list_rows 获得原证明。UI 必须保留 frozen confirmed 结果；打开追溯失败时单独提示，允许只读重试，不降成待加工、不清原请求、不自动再提交。此项已发 API/UI 和根。

`remaining_stock_quantity` 也应在成功卡表述为“本次完成后余片”，不是实时剩余库存。后续取用/组装后打开旧成功卡，数字仍是原保存事实；需要当前余量时另走现库存查询，不覆盖证明。

旧记录缺原 body 时，不能凭当前默认字段构造 resolver 请求，更不能升级为 complete。如只能提供已有合法组追溯入口，须明确“原保存内容待核对”；当前 endpoint 要完整原 body 的范围保持。

## 候选定稿后最小独立核查

待精确 API/UI SHA 再检查实际代码和最短证据，不提前运行当前半成品，不复跑作者全部组：

1. 从最终真实 HTTP 包选择多 receipt 同子件 + finished 少套余片，确认按 job/lot/movement 映射、非零输入子集及首次台账量、nullable。原数组顺序完整保存；首回与同键重放整个 JSON 相等。
2. 实际 append-only trigger 下父首次 INSERT；builder/后置审计失败全回滚；来源状态/BOM/位置或现余量改变后只读取原证明，不调用写资格、无 DML/commit；旧 command 仍 legacy_trace。优先复用作者原文和定向必要反例，不扩算法。
3. 实际页面方法消费最终 HTTP：真实提交丢 ack → 刷新列表已变 group_stock → 同 group 后续入口仍锁 → 原键只读确认、零新业务 POST；异组可用，空/错列表仍显示恢复卡，nullable 显示与历史入口失败保持成功结果。
4. 项目已有真实 Vue 编译/组件注册/非 DOM 事件最短验证；不得宣称真实浏览器 DOM/Cookie/storage 或管理员现场验收。沿任务卡保留 single 邻近保护，不再次启动被拒的 Chrome 或停止旧服务。

当前结论只允许在既定卡与互斥文件范围继续实现。未发现新的合同阻断；最终候选技术验收、根集成发布、安全备份及人工验收均不能由本预审代替。
