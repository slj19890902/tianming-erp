# 订单与报料分阶段抵扣 v0.22.465 正式发布回执

状态：技术发布完成，待管理员人工验收。

## 正式基线

- 版本：`v0.22.465`
- 代码提交：`d01b1de14e95c6716db824e636f6df3ecb0b778d`
- 签名包：`3d08c0b060dd7db27732f8a76617226551e2f780b973524d1ddaad345a3b278d`
- 回滚包：`756af3b82d9c317f9ff091db331de2176716b7c0b68bc328e85c97b1062a4b55`
- 数据库 revision：`dt0920`
- 数据库迁移：无

## 实现结果

- 新建订单与 PDF 识别订单只推荐可直接抵扣的成品、半成品；需要重新分切的片料不在订单阶段采用。
- 需要分切的原材料和片料进入合并报料，由用户确认“每张分切几片”的用料方案后再预占。
- 后端在订单保存时拒绝伪造的需分切库存选择，保留客户、权限、适用性、数量、版本、事务和幂等门禁。
- 报料预占冻结分切方案，全额库存覆盖时不生成零张采购。

## 验证结果

- `pytest -q tests/test_package_a_inventory.py tests/test_p1_18_pdf_inventory_contract.py tests/test_pdf_batch_inventory_refresh_frontend.py`：12 passed。
- `pytest -q tests/test_requisition_deduction_ui.py tests/test_bidirectional_sheet_cut.py`：10 passed。
- `pytest -q tests/test_sheet_cut_production.py tests/test_p1_80_purchase_purpose_allocation.py`：51 passed。
- 新增订单候选、伪造提交、报料预占及前端契约定向用例通过；`node tests/requisition_deduction_ui.cjs` 通过。
- 签名包 29,702 个文件逐一核对哈希；三个正式健康入口均返回 200，正式静态资源哈希与签名包一致，未登录报料接口返回 401。
- 正式数据库 `integrity_check=ok`、外键检查为空，发布前后 revision 均为 `dt0920`。

现有完整测试集中有两组与本次改动无关的旧断言失败：liner direct coverage 的既有行为，以及 PDF 前端源码测试中的旧静态断言/未定义 `TMOrderReference`。隔离库没有待处理合并报料及可用于构造流程的有效片料库位，因此未把完整浏览器业务流程写成已通过；按正式环境规则未自动点击正式页面。

## 备份与保护

- 正式 NAS 备份：`Z:\sata1-18015598002\BoxERP\backups\20260920-154412-5b7319a3.tmbackup`
- 备份 SHA-256：`191fa7920ece0945a49d7ccfd232a99b6688d448a7d2f2f2061e16ed1592b2a0`
- 备份已完成解密回读和哈希校验，停服备份期间正式数据库哈希未变化。
- 发布没有执行业务写入，没有自动点击正式页面；原有未提交文档和发布期间出现的并行计划文件均保留。

## 管理员人工验收（不超过 3 步）

1. 打开正式订单或 PDF 识别订单，确认只能直接选择无需重新分切的成品/半成品；需要分切的片料不会在订单阶段出现。
2. 打开 `http://192.168.3.80:8000/?page=requisition`，进入一张有片料候选的合并报料，确认页面显示“每张 N 片”，点击“确认用料并预占”后再生成供应商采购报料单。
3. 用另一操作占用候选库存或制造数量不足，再确认提交，验证后端拦截且原报料草稿、已选方案仍保留。

完整机器证据位于 `Z:\sata1-18015598002\BoxERP\desktop-assistant\release-v465-20260920`。
