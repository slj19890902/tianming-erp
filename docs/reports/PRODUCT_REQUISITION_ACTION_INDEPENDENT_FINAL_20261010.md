# 产品报料动作可靠性：候选独立终审

2026-10-10。结论：**pass，候选范围内未发现发布阻断**。这是技术候选结论，不代表已发布；根刚发现另一任务已发布 v608 / `f751729378a667c15b5d2a3293299e53458f7981`，仍须保留新正式内容并完成整合后的源码匹配、发布安全门禁。管理员页面/手机人工验收 **pending**。

精确候选：API `8c79504e0df337fa6ece5487927bd926ee00414e`，UI `90b0d4c47faf824cc9db04f728f51038130a3f15`。两个 managed tree 均核对 HEAD、clean 和最终 diff；API 4 文件、UI 5 文件的 LF SHA256 与证据索引在同目录 `verification.json`。本审者未改任何仓库源码、版本、历史事实或正式数据，未启动浏览器/服务、处理 PID、push 或发布。

## 独立结果：5 / 5

API 两条真实 TestClient HTTP 使用安全 conftest、tmp 合成数据库，6.130 秒，`independent-api.xml`：

1. 捕获真实 ORM 首次 INSERT，完整 actor+normalized body hash 已随 Order 写入。真实创建审计已 INSERT 后故障返回 500，Order、Batch、采购和审计全部回滚。相同原 key 随后成功一次，只有一条创建事件，未写库存/流水/预占。没有事后给历史订单补 hash。
2. 新外购保存后将产品、策略和供应商停用，原 body 精确重放仍返回原完整 201；只改嵌套 item.remark、数量不改时返回 409。原单打印、已报料全量及分页仍可读。上述 GET 捕获 SQL 零 DML；重放、拒绝和读取前后全部业务计数相同。

UI 三条实际 HTML 方法/共享组件 + linkedom/VM，263.216 毫秒，`independent-ui.xml`：

1. 实际 go 调用 loadPage 并卸载原组件后，正常路径只填原产品一条草稿；产品不可用路径仍在根显示一次可操作失败，不因组件消失吞错。无业务 POST。
2. 实际动作超时后换账号和表单，旧 local 401 晚回不退出新账号、不改其草稿；新账号随后可正常发起新动作。
3. 执行实际 index 同步 Axios 拦截器：同 actor 但 authGeneration 已更新时，旧 401/403 不产生退出、禁用提示或修复弹窗；当前 401/403 仍走原副作用与拒绝流程。

独立 UI 复用作者 harness 的源码提取与 VM 基础，未执行作者测试主体；组合断言由 review 单独编写。它不是完整浏览器/可见视觉/真实 Cookie 验收。API 原文和结构化响应保存在 `independent-atomic-http.json`、`independent-readers-http.json`，每个响应文本另带 SHA256；索引复核原文 SHA 与 JSON 等价。

## 合同及作者证据交叉复核

最终 service 必填完整 hash，仅真实 new Order 首次 INSERT 赋值；API 普通已有键与 IntegrityError 路径、service 内部 replay 均核当前 admin/cost/requisition 权限及客户，并比较原 actor/body hash。旧 null 不补写，409 明确原采购单号、报料→外购包材→采购历史核对路径和勿换 key 重建。新外购创建审计与单据同事务，重放不新增创建事件。审批仍使用真实 reviewer 执行、已 applied 重审只返回原申请；作者最终审批节点通过。

普通 fresh inactive gate 只在 `_build_replenishment_item` 解析到真实参考产品后执行，已有成功原请求重放先行；无参考产品的通用纸板继续合法，删除产品仍为原 404。作者最终节点包含 inactive/deleted fresh、旧成功重放及通用纸板对照。

新增 hash 的读取兼容风险已在预审提出并由 owner 修复。最终 predicate 是 **原 hash NULL，或真实外购 batch 存在且整个来源单没有任意非 NULL quantity_contract_json 成员**。打印与 reported 两处共用该规则；既有已采购 physical-warning 仍保持原范围，不因本卡扩大显示。通用收料仅提前原外购路线拒绝，未放宽纸板采购门禁。外购专用取消将来源作废，因此不会重回 pending；unified_procurement 未改。

作者最终 API `final-targeted.xml` 20 项（新增 7、原外购 11、审批 1、真实读/收/取消金样本 1）通过；physical 相邻 1 项通过。UI 新动作 18、原产品/首页/手机 56 与图纸脚本通过。本审者精读其中权限/旧 null/IntegrityError、严格来源分类、审批及金样本，没有重复宽跑。合成短 JWT 密钥警告来自现有 fixture；未用于正式系统。

作者保留的旧相邻静态测试失败没有被删除：4 个保存源码截取/旧字符串节点以及 1 个旧外购文案节点，均有实际 formal607 原文运行的同失败对照，不算本轮新增业务缺陷或“全部测试通过”。最初测试审计计数假设、历史 NULL 夹具漏字段和金样本漏货位等失败也留在作者报告；最终结论仅引用明确列出的有效节点和本审者 5 条。

## 保留问题与边界

- 空/坏保存 ack 误报成功，以及未知后重新打开丢原 key/body 的已复现问题没有在本卡修复，属于下一独立持久恢复闭环；没有新增只读 resolver。此次动作导航保护不能冒称保存恢复完成。
- generic 补库 void 对外购采购生命周期的相邻源码疑点已告根，未实证、未计本卡新增 Bug，也未扩大修改。正常外购专用取消的作者金样本通过。
- 共享拦截器只保护错误副作用；不能称所有页面 loader 的数据赋值均已完成会话隔离。正常页面列表加载仍保留。
- 新正式合并若改变本索引的任何受检文件，根须核交集并更新合并证据；不能仅因版本号或候选测试通过就跳过同源码、备份、恢复、健康等门禁。

本报告与 verification 独立归档 NAS；现场验收 pending，本任务截至报告没有发布动作。
