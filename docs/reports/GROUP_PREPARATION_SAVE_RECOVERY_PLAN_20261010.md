# 整组子件保存恢复：独立只读方案复核

日期：2026-10-10。状态：方案复核完成，未实现、未提交或发布 group 修复。本轮只读既有源码和合成证据，未新增或重跑业务探针；未改根发布树、仓库测试、进程、浏览器、服务或正式数据。v603 当时正在根标准部署，不能据候选版本声称技术发布完成。

结论：同意下一卡限定 group_job 的 action=dispose，包含原有 semi 分存和 finished 成套两个去向；必须形成独立持久请求、原事务冻结证明和只读查询闭环。不能直接套用 single 的分批投入/continuation 合同。现有同键重放、事务、版本和权限保护有效，本轮不新增“原键重复扣库”缺陷，也不凑数量。

## 依据与源核查

依次读取 CODEX_START、NAS AI_START、REVIEW-TASK/TASK、ORDER_FLOW、总需求第 7/14/17 章及章程 3～9。依据 api/REPORT.md、source-fingerprints.json、api.xml、frontend-entry.json，并独立读取 GroupAction/post_group_action、mutate_group、dispose/assemble、源解析、Command 模型与 sprep0912 trigger 的必要片段。

只读 API 树 production-save-recovery-api-20261010 的 HEAD 为 c02693f8a678b1a72ed3de285b32633842b7c22c，核查干净。独立复核 6 个关键完整文件哈希与作者指纹一致；所选 group API 片段与 6640d536 业务合同等价。再比较 group save 分支（从原 plan 校验至模块尾部）与 UI b9ac296b 来源，字节等价，LF SHA256 为 cd4cd1d2ea55977302bb3e235ca0407a22d8adf0cb9c7a47d5d6dda1266539ac。不能因此声称整个已含 single 修复的 API/UI 文件等价。

复用 8 项真实 HTTP/数据库观察（api.xml：8 passed、20.359 秒）及 3 项实际安装页面方法观察，不扩跑。路径为 ../group-preparation-save-recovery-audit/api/。第一版 Node 观察把数组首成员误认成固定 15，作者已按 job ID 修正；是探针顺序错误，不是业务 Bug。

确认的剩余缺陷仍是此前保留的 group 同类问题：空/坏 2xx 成功关框；未知 body/key 仅在弹窗内存中，改数或关闭重开会换键/恢复理论值。没有原保存键的只读完整核对接口。共享 Axios 的迟到认证副作用属相关剩余边界，本轮没有新 401 探针，不宣称已复测。当前 history 是组/任务追溯，不是保存键完整结果证明。

## 必须保留的真实数量关系

主入口是 group_job“保存入库” → saveStockDialog('dispose') → group-actions。group_stock 的 store_outputs/assemble 只作为关联未知锁的入口，不在本卡重做这些算法。

- **semi：** 每成员消耗全部已计划投入。例投入 15、20 张，实际产出 14、19 时，两任务仍已全部完成，没有剩余投入、没有 single 的 continuation。页面应直接显示“本组计划投入 15/20 张；实际产出 14/19”，不能把较少产出称作“只加工一部分材料”。若要支持整组分批投入，须另立数量流程任务，不能藏进恢复补丁。
- **finished：** 先记录每个子件真实加工产出，再按冻结配比实际组套。15/20 子件可形成 5 套是两层事实，不能将 35 片和 5 套相加作为库存。库存已组装消耗的子件不能再算现货，但原加工产出事实不能被当前余量覆盖。
- 一款冻结子件可由多个 receipt/job 供料。proof 必须以 job_id/receipt/lot 对应，不能硬设“一产品一个 job”，更不能按数组第一个就是 15 或 20 对号。
- finished 的 sets 可少于本组最大可组套量，或实际产出高于理论而留下余片。assemble.inputs 只记录 take>0 的 job；不能要求每个成员都出现在组套消耗数组，也不能要求所有产出清零。按冻结产品配比核组成，不用当前 BOM，不改余片算法。

以上是当前代码和已有观察支持的兼容边界，不是本轮修改加工数量的授权。总需求已有单任务分批投入原则，现有历史 group 合同必须在页面说清，不能悄悄伪装成 single 新流程。

## 推荐的最小后端合同

沿用 StockPreparationCommand：operation_key 全局主键，actor_id、receipt_item_id、原 request_json/result_json。只为新鲜 dispose 父记录加入 group_completion_receipt；当前认证 actor 放响应包络，与冻结历史 actor 分列。首写与同原键精确重放整个 JSON 应相同，不再加入会随重放变化的 replayed 字段。

### 父首次插入和锁

dispose 在原 atomic_bom 写锁内先查父键，旧原键核 actor+完整签名后直接返回，绝不运行 builder。新写逐成员完成，并在 finished 时完成同事务 assemble 后，父 Command 首次 INSERT 前调用可选 builder，完整校验/序列化后一次插入。sprep0912 对 Command 禁止 UPDATE/DELETE，即使仍在事务中也不能 API 返回后补 UPDATE。builder 出错、后续 flush 或审计失败必须连子件/组套一起回滚。

最小装配点是 stock_preparation_disposition.dispose 最后父 INSERT 前；mutate_group 只向 dispose 转发默认 None 的可选参数。其它动作维持原函数签名调用和结果。不要在锁外查不到键后，就认定后续结果是 fresh。不要补写旧 Command、修改迁移或绕过 trigger。

### 原请求和签名

使用原 GroupAction.model_dump 默认字段，并保持 API 当前规范化：只移除每个 job 的 lot_id=null，然后 compact encode(sort_keys=True,ensure_ascii=False,separators=(',',':'))。jobs 数组顺序是原签名的一部分，不排序、不按数据库成员顺序重建。sources 等旧默认字段仍保留；不能因为 dispose 当前不使用这些字段而从旧签名删掉。

新增 expected_actor_id 仅严格正整数、optional，并从业务 dump/旧签名排除；首写和查询都与认证账号核对，不拿它替代历史 actor。只读原 body 也执行相同旧规范化，不改其它原字段的类型接受规则。证明成员可按 job_id 映射验证集合，原 request 的数组字节关系仍独立保留。

### 新冻结证明

建议先由 API 固定 JSON 合同再交 UI，最低分层如下：

| 层 | 必须冻结/核实的事实 |
| --- | --- |
| 请求 | schema、operation_key、历史 actor、完整规范化原请求、原 group_key/plan key、disposition |
| 组身份 | 原 parent_id、冻结 recipe 客户/名称/单位/配比及必要身份；不能用当前 BOM 覆盖 |
| 每成员加工 | 原 job_id/version、receipt_id、source lot、原来源客户（可空）、该成员全部计划投入、实际产出、完成时 job 事实、consume reservation/movement |
| 每成员输出 | 首次产出 lot、manual_in movement、实际一次 ledger 数量/单位、冻结物理显示单位、产品身份、原位置 ID/名称/楼层/布局版本 |
| semi | 明确本请求没有组套事实；不得因为当前以后已组套就补 assembly |
| finished | 本次 assembly key/证据键、sets、冻结 recipe、实际非零子件消耗 movement/lot/quantity、成套 manual_in 输出 lot/数量/单位/原位置；与本次成员输出关联 |

finished proof 要在新事务内取子件的首次 manual_in 和组套 consume，不能拿子件当前 available（可能已为 0）证明原 actual_output。一项 job 的加工消耗与后面的组套消耗属于不同流水/单位，不混计。半成品底层 sheets 与 profile.quantity_unit=pieces、冻结产品标签、成套的“套”应分别保留；不要再乘几模/一开数或每箱套数。无需回财务成本。

StockReplenishmentOrderItem.customer_id 可空，已有真实 semi dispose 成功且 profile.customer_ids=[null]。因此每 receipt 客户 nullable 与 frozen recipe 客户应分列；不能新加“每个来源客户都必须正整数/必须与当前 recipe 客户相等”的保存门槛。parent Product/customer 仍按原真实非空身份校验。货位名称空、楼层可空、无法可靠取得显示单位时 null 的合同也不应误拒原本合法加工；原位置 ID 和台账单位等业务核心事实仍严格核对，不补造显示信息。

## 只读恢复与旧结果

建议独立 group-completion-result 端点，请求原 key、完整 original_request、expected_actor_id；最终路径/字段另卡定稿。仅 dispose；同全局 key 若属于其它 action、actor 或 body，应冲突并保留，不当 not_recorded。

1. 维持 admin/boss 和现客户门禁。当前 parent 和每个原 receipt 的客户范围、冻结 recipe 客户和每条原来源客户范围分别核。当前身份可读取，不等于允许重新执行。
2. 使用独立只读身份/范围解析，不调用当前 recipe() 的 active/BOM 最新资格或 prep.source 的 posted/has_raw_plan 等写资格去否认原结果。源当前数量/版本/状态变化不改变保存事实；关键归属缺失/冲突应保留并明确待核对，不能伪装无记录。
3. 原键、actor、group、parent、完整 body 和新持久 proof 逐层对应才 completed。proof 的原成员来自当时持久记录，不能用后来当前 group member 列表补齐原组或重算原产出。当前对象可用于权限/追溯，不能冒原完成证据。
4. 旧 Command 无新 proof 只 legacy_trace：原 result 可证明保存 action/group/disposition/job_ids，以及确实已写入的可选 assembly 结果。它仍缺完整原加工来源/输出/位置单位合同，不能凭当前 job/lot/history 自动升为 complete。旧合法结果可显示安全组 history 入口，缺 body 则不据当前默认数量重建。
5. no-store、no_autoflush、业务零 flush/commit，安全拒绝审计保留。未见父记录仅本次 not_recorded；已有在途实证表明此时原 POST 可能尚未提交。不隐式执行、不删键、不换键。明确“按原内容继续”才发送原 body/原 key，后续 409/500 仍保留。

首发明确 Rejected 如需支持，只能是同锁内无父键、进入提交前业务失败、完整回滚成功；已有 unknown 即使之后收到此头也不解锁。没有永久结束 marker，不接管他人账号，不新增 migration。

## 前端持久记录与员工入口

首个 POST 前，按 actor + 独立 operation_key 存原 endpoint、完整不可变 body、稳定 group_key、原 parent/子件数量位置摘要，写后读回失败则零 POST。锁身份为同 actor 的原 group_key，不能仅锁旧弹窗或一款 product，也不能按 parent 全局锁所有组。每条保存记录独立 key，不用 group 单格覆盖记录。

unknown 后刷新/关闭重开仍显示原成员和原数量/位置；原数组顺序不变，关联使用 job ID，不拿当前理论值重建。当前 pending 列表可能已经没有 group_job，因此恢复卡必须独立于列表 empty/error；组输出变成 group_stock 后，其同组的 store_outputs/assemble 入口也先核原 unknown。此处仅补关联入口锁，不扩大这些动作的业务算法。其他 group 可正常操作；localStorage 不是跨设备全局原子锁。

semi 成功卡逐款显示投入张数、实际子件产量和各自原货位。finished 成功卡分“子件加工”和“成套入库”两部分，组套套数单独显示，余片存在时不说“全部已组装”。未知卡的主要操作为“查原结果”“查看加工记录”；not_recorded 后才出现明确“按原内容继续”，不得自动 POST。

沿 single 已审模式使用局部有限超时通道，覆盖本 group 保存/查询/首次选位与地图相关 GET 的生命周期，不改全局 Axios。旧账号晚 401 不得先经共享拦截器注销新账号；真实当前 401 仍执行原登录规则。错/空/截断回执、未知后的任何错误均保存原依据。在途关闭/忙态一致；当前窗口被替换后迟到结果不关另一弹窗。

confirmed 后成功与列表刷新分开；刷新失败保留逐款结果和只读刷新，缓存清理失败保留 confirmed 防重，不重新加工。超产仍只确认一次，正常保存不增加无必要确认。记录只能说明软件已保存事实，不能用软件响应证明现场又加工了一遍。

## 下一卡最小验收及剩余风险

复用当前 8 HTTP/3实际方法证据，实施后只扩必要新合同边界：semi 全量/少产、finished 新证明；已组套后的子件当前余额不冒原产量；允许余片/多 job 同产品映射；触发器下父首次 INSERT；原 jobs 顺序与 lot_id=null 清理签名；原键完全重放；原 source/BOM/货位后变仍只读恢复；nullable 来源、单位；旧 result 仅 trace；commit 前全回滚与 commit 后丢 ack；真实当前未见后原提交成功；未知持久、同组后续入口锁/异组可用、迟到认证、成功后列表/缓存失败。最终真实 API 原文由实际 UI 方法消费，浏览器与管理员验收另列，不能冒称。

本次不发现新的方案阻断，但上述多来源 job、finished 余片、不可改写父记录和旧数组签名是实施合同必须包含的条件。当前 8 场景没有单独覆盖“多 receipt 同子件”或“finished 少套留余片”，此两项是源码推导的合法兼容边界，待实现卡定向覆盖，未冒称已经实测。未执行新的迟到 401、DOM 或正式页面验收。

建议 allowlist 仅 API group 入口/新 recovery helper、dispose 父插入前 callback、dispatcher 转发、真实 group UI 分支/独立恢复组件及专用测试；根另定实施卡才可开发。根 v603 发布及 single 修复不与本方案混称为 group 已修复。NAS 独立回执与本 PLAN 同内容校验。
