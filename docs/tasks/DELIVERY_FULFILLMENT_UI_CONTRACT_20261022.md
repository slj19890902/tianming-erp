# 保票实际交付事实接口与UI接入（候选未发布）

目标：送货142原单、回单129、对账57、发票55保持原记录，追加50@9/22与400@9/22+400@10/12。任何新界面不能显示“重新送货”“新签收”或把这三段送去生成第二张对账。

## 预览及提交

独立API路由由 `app/api/delivery_fulfillment_facts.py` 提供，已在候选主应用注册：`POST /api/deliveries/{delivery_id}/fulfillment-facts/preview` 预览，`POST /api/deliveries/{delivery_id}/fulfillment-facts` 提交。具体Pydantic以该文件为准。首先预览返回 `reviewed_hash` 与 `expected_revision`；用户操作提交时带相同完整payload、两个预览字段和稳定 `operation_key`。网络重试必须重用同key/payload；409必须刷新重预览，不能自动覆盖。

业务输入包含客户、原送货id、全单逐明细分段的实际日期与正整数数量、用户确认摘要及原文件名/sha256/sheet/行号。不能改订单/产品、销售价、原开票金额、采购成本或货位。预览必须展示原单据日期、原明细总数、每段日期数量、原回单/对账/发票身份与金额，以及“原开票/应收/库存成本不变”的实际冻结前值比较。

候选经典页面送货“更多”菜单已有“保留票据更正交付”入口；V2送货页展示实际分段并跳转同一受控入口，避免两套提交逻辑。文件hash由本地文件计算，不上传原件；手填hash明确标注。预览后冻结输入，修改即取消审批，必须重新预览。提交前需勾选核准；操作key先写当前用户/送货的sessionStorage，再发请求，网络结果未知时关闭重开仍恢复原key/payload，不能自动创建第二次提交。

## 查询显示

- 原有 `delivery_date`、`display_quantity`、`total_quantity`、回单与金额字段保留原单据口径，避免改写历史PDF和已确认账单。
- 新 `actual_fulfillment` 含 `original_document_date`、revision、`segments`、`total_physical_quantity`、`matched_physical_quantity`。日/月筛选后的主显示使用 matched_physical_quantity，并明确原单总量；金额若仍展示5030，必须标“原已开票整单金额”，不能当作当期新应收或把每月相加。
- 每段含唯一 `segment_identity`、原 `delivery_item_id`、实际日期、数量、revision_id及 `cost_allocation_status`。以segment_identity作为分段行key，不能只按原item id覆盖。
- 原回单129的800签收量和签字不复制成每段的签收证据。`signed_receipt_proven_by_segments=false`，新增日期事实不证明补签回单。
- 日期与产品/订单筛选绑定同一有效分段：10/12+30515不命中；10/12+60089命中400，不能显示整单850为当期数量。
- 订单完成时间按有效实际分段最后日期；原dispatch审计时间保留。追溯的`actual_fulfillment_segments`单独呈现，不与原dispatch事件数量相加。

## 成本与财务

原回单行仍是唯一对账来源；API不生成第二份回单、对账、应收、发票或库存动作。对账57与票55仍在原冻结日期和金额。

60089的6只旧库存+794只新料，没有证据指明各400使用何种来源。两个月都显示 `fulfillment_split_cost_allocation_pending`：原来源成本保持，但尚未分配的部分不计入当期已知成本、不能补参考单价或均摊；月结与毛利保持缺口状态。业务方核准来源份额后需要独立后续受控事实，本候选不实现。30515单段整行来源成本可原量投影到实际交付日期，无金额重算或写库。

## 集成阻断点

迁移fa1022df已承接核验的正式v714唯一head ez1022hc，并完成合成SQLite升降升/非空拒绝降级/旧事实保持测试。旧reader门禁覆盖真实签名能力及Manager兼容性/已有进程/备份入口；主应用路由、UI源码和V2产物已接入。但仍缺正规认证取得新鲜去凭据副本后的迁移验收，以及实际安装独立supervisor的签名Mac正常/失败恢复；未正式发布或执行业务。不能只 cherry-pick 查询修改去连未迁移库，也不能混入v715。合成和UI脚本测试不代表正式迁移、实际安装运行时或人工业务验收通过。

无追加事实的旧单保持普通日期/账期更正合同。90078的192/171依据最新回读，只需普通送货实际日期10/21→10/12；171显式与有效账期均为2026-10、版本1，签收日期10/21保留，不应因送货建议月11再写账期。正式操作前仍需新鲜版本和对账锁复核。
