# 瑞达历史数据迁移运行手册

## v331合并86隔离边界

已整合正式d844ce576751d732a5f561ab199bfbd0b58e5013及迁移rw10v8x9z71；唯一候选head sl24v8x9z86。在有准确备份的私有副本升级；保留供应商纸种颜色、原表原字段、用途与成本事实。没有新规则和非默认颜色事实的可丢弃副本可显式降84，保留84/71两个head，再升级86；已有颜色或规则版本时必须在修改迁移元数据前拒绝，不能删事实通过。26项隔离迁移/片料检查通过，旧v330源不冒充v331新鲜正式状态。私有主副本85→86已核验原268表原字段/索引/触发器保留、完整性ok及外键0；运行SHA与启动门禁另核对，工厂不得执行。

## v330合并85隔离边界

最新已整合正式基线673ddf5ddb221fd01c4c95c5d99eb69dfd70f9a4，正式revision rv10v8x9z70；候选合并head sk23v8x9z85。使用已核验副本演练升级85，撤合并时显式指定84，保留84/正式70双头，再升级85。不要相对降级或stamp。有规则版本时合并节点先拒绝降级，避免低版本随后拒绝前已修改迁移元数据；有片料用途/白面纸事实时不能降掉正式70。已完成带片料用途和白面纸事实的往返保留测试；新鲜v330-20260910-173331导出已取得并核验；另建v330私有副本70→85，原251表原字段/显式索引/触发器保留，完整性ok、外键0，18082启动门禁通过。以下为旧v327副本记录：私有主库已升级85，备份SHA256 80e9b37da7d5feb538cc820be0d49ea4e9c342e05a635dcc3a028ea8bf18098d，原266表原字段及显式索引/触发器保留、完整性ok、外键0；运行SHA另核对启动门禁。不能将旧源兼容测试称当前正式数据验收。

## 外购冻结来源版本84隔离演练

先确认独立副本和备份哈希，再运行83→84。该迁移重建外购BOM关联表以保留不同冻结来源的身份，并增加采购来源订单唯一索引和复合外键；所有旧关联、采购候选、订单、库存及成本行保持。核对完整性、外键、全部原表原列。无版本事实时可演练84→83→84；已有规则版本或同产品多个来源时，降级应在任何DDL前拒绝，文件哈希不变。不得删行解除门禁。私有主副本已升级84，升级前备份before-sj22v8x9z84.sqlite3，SHA256 fe0ead154aa4f82a8d4a89f27e49e6fdec6d77c856dd8248a0cab83850f913d4；原266表原字段及显式索引/触发器保留，完整性ok、外键0。运行状态另以启动门禁核验；工厂仍不得执行。

## 结构规则版本83隔离演练

在已核验v327只读源的另一个副本中升级83，记录备份哈希，核对唯一head、原表原列及触发器、完整性和外键。空版本表演练83→82→83；已有任一版本/产品/来源记录时降82必须立即拒绝，原文件哈希及事实均保持。不要删除版本事实解除门禁。三个新表和一个唯一索引不激活订单切换；须待执行器、历史来源读取和全闭环完成后再更新正式部署步骤及运行实例。私有主UAT数据库现已升级83，升级前备份及263表原字段/索引/触发器保留证据在migration-83-runtime-evidence.json；运行状态仍须核对实际启动门禁日志。

## 库存实物身份82隔离演练

从核验过的只读导出另建副本升级82，比较原业务表原列及触发器，检查唯一head、integrity和FK。旧身份列均为空时可演练82→80/67→79/67→82；有新身份事实时降81必须拒绝且字节哈希不变。不要用当前产品主档批量补写旧批次，也不要删除事实解除降级门禁。管理员现场核实只是待发布功能，家庭不得在正式库执行。

## 合并81边界

81合并80与正式rt10v8x9z67，无DDL。隔离撤销合并节点显式选80，保留80/正式67双头，再撤80到79时仍保留正式67；upgrade head恢复81。禁止相对-1和stamp。已有BOM配置时撤80必须拒绝，已有材料候选记录时不得降正式67。正式恢复以已核验整库备份为准。旧导出兼容演练不代表已验证最新正式数据。

## 三维BOM配置80家庭演练

在已核验的工厂导出隔离复制品执行upgrade80→downgrade79→upgrade80，保留79之前的两个迁移分支；完整性、外键、所有原业务表原列和原触发器均须一致。有新模式配置或schema3订单时禁止降级，恢复采用验证过的时点备份，不删除事实。当前唯一候选head80，工厂不得据此执行升级，须待完整候选验收及最新正式基线核对。

## 本体库存77候选演练

核验指定隔离复制品与其升级前备份，先升76，再升77→降76→升77；比较库存/流水/位置/订单原行、触发器定义，执行完整性及外键检查。已有bom_body_inventory_details或assembly_body库存时，降级必须拒绝；禁止删事实换取降级。库存表批量重建前保存并移除引用触发器，重建后原样恢复。正式发布仍需最新时点备份和完整下游适配验收，不执行本文退役历史抽取命令。

> **已退役（P0-30，2026-08-27）：** 本手册仅保留作历史审计，以下命令不得再执行。`gn49v8x9z38` 已删除旧系统抽取表和映射表，相关刷新/迁移脚本也已移除；严禁重建、刷新或重新导入。需要恢复时，只能使用经验证的升级前数据库备份。

## 启动要求

新线程首先执行只读检查：

```powershell
Set-Location "D:\纸箱厂erp软件搭建"
Get-Content -Raw -Encoding UTF8 .\AGENTS.md
Get-Content -Raw -Encoding UTF8 .\docs\CODEX_HANDOFF.md
```

用户交接中还记录了路径：

```text
C:/Users/Administrator/Documents/纸箱厂erp软件搭建/
```

必须先确认实际工作区，不能跨目录覆盖文件。

## 数据源优先级

1. 权威历史源：SQL Server 隔离库 `BoxDB20_REPRO`。
2. 映射参考：`data/carton_erp.sqlite3` 中的 `legacy_ruida_*`。
3. `Z:\sata1-18015598002\BoxERP\erp.db` 仅含基础资料，不是订单源。

## 第一个任务：只读差异统计

新线程应从此任务开始：

```text
对 BoxDB20_REPRO 的 Orders、OrderXLs、Customers
与 data/carton_erp.sqlite3 的 legacy_ruida_orders、
legacy_ruida_order_items、legacy_ruida_customers
执行只读主键和字段差异统计，生成报告，不写数据库。
```

报告目录：

```text
docs/migration_reports/
```

建议报告至少包含：

- 源和目标行数。
- 仅 SQL Server 存在的 ID。
- 仅 SQLite 存在的 ID。
- 重复 ID。
- 孤儿 `OrderXLs.Order_FK`。
- 客户 ID 无法映射数量。
- 日期解析失败数量。
- 数量、单价、金额异常数量。
- 关键字段发生变化的记录数。

该任务已完成，报告为：

```text
docs/migration_reports/DRY_RUN_DIFF_20260619_081040.md
```

## 第二个任务：legacy 原始层增量刷新

方案：`docs/migration_reports/LEGACY_REFRESH_PLAN_20260619_083121.md`

脚本：`scripts/migration/refresh_legacy_ruida_from_sqlserver.py`

幂等键：

- `Orders.ID` → `legacy_ruida_orders.legacy_order_id`
- `OrderXLs.ID` → `legacy_ruida_order_items.legacy_item_id`
- `Customers.ID` → `legacy_ruida_customers.legacy_customer_id`

执行纪律：

- 默认只运行 dry-run。
- 仅追加源端独有 ID；已有 legacy 行只报告，不自动更新。
- 客户无差异时默认跳过。
- apply 必须使用 `--apply --confirm-apply APPLY_LEGACY_RUIDA_REFRESH`。
- 写主沙盘还必须增加 `--allow-main-sandbox`。
- 首次 apply 只能对主沙盘复制出的隔离副本执行。

Dry-run：

```powershell
.\.venv\Scripts\python.exe .\scripts\migration\refresh_legacy_ruida_from_sqlserver.py
```

隔离副本 apply 示例（必须另获明确授权）：

```powershell
.\.venv\Scripts\python.exe .\scripts\migration\refresh_legacy_ruida_from_sqlserver.py `
  --target-sqlite .\data\work\carton_erp_legacy_refresh_test.sqlite3 `
  --apply `
  --confirm-apply APPLY_LEGACY_RUIDA_REFRESH
```

回滚时停止 ERP，保留失败目标库，核验自动备份 SHA-256，将备份恢复到原目标路径，再执行 `integrity_check` 和五张表计数。校验失败时停止，不进入正式业务表迁移。

### 隔离副本 Apply 验证结果

报告：`docs/migration_reports/LEGACY_REFRESH_APPLY_TEST_20260619_085901.md`

- 副本 legacy 订单：39,766 → 39,922。
- 副本 legacy 明细：40,293 → 40,449。
- 客户保持 132，正式订单主/明细表保持 4 / 7。
- Apply 后 dry-run 为 0 / 0 / 0。
- 幂等键无重复、关联完整、主库未变化。

该结果只证明脚本在隔离副本通过。主库 Apply 必须重新获得明确授权，并重新执行备份、哈希、完整性和 apply 前 dry-run 门禁。

### 主库 Apply 结果

报告：`docs/migration_reports/LEGACY_REFRESH_MAIN_APPLY_20260619_090705.md`

- 主库 legacy 订单：39,766 → 39,922。
- 主库 legacy 明细：40,293 → 40,449。
- 客户保持 132，正式订单主/明细表保持 4 / 7。
- Apply 后 dry-run 为 0 / 0 / 0。
- 完整性、幂等键和关联校验通过。

原始层增量刷新阶段已完成。未经新的明确授权，不得继续写入 `sales_orders` / `sales_order_items`。

## 第三个任务：正式表映射与100条副本试迁移

方案：`docs/migration_reports/FORMAL_SALES_MAPPING_PLAN_20260619_091639.md`

脚本：`scripts/migration/migrate_legacy_ruida_to_sales_orders.py`

副本：`data/sandboxes/carton_erp_formal_mapping_test_20260619_091639.sqlite3`

- 默认dry-run，必须显式`--sqlite-path`。
- `--apply`永久拒绝主库路径。
- 固定`--limit 100 --sample-mode complete_orders`。
- 订单号使用`RUIDA-<legacy_order_id>`，并使用独立迁移台账记录订单/明细源ID。
- 产品仅按同客户款号精确匹配产品编码或客户料号。
- 首批副本测试新增100个订单、100条明细，复跑为0/0。
- 未经新授权，不得对主库正式表运行。

### 产品匹配缺口与多明细专项

报告：`docs/migration_reports/PRODUCT_MATCH_GAP_AND_MULTI_ITEM_TEST_20260619_094449.md`

- 当前唯一匹配10,145，0匹配30,304，多匹配0。
- 主要缺口是款号包含“编码 / 描述”。
- 斜杠前编码产生27,431条唯一候选，未经人工评审不得应用。
- 多明细副本样本20张、56条明细，迁移和幂等复查通过。
- 下一步只生成候选映射清单，不修改产品主数据。

### 斜杠前编码候选清单

报告：`docs/migration_reports/PRODUCT_PREFIX_CANDIDATES_REPORT_20260619_150159.md`

只读命令：

```powershell
python .\scripts\migration\analyze_product_prefix_candidates.py `
  --sqlite-path .\data\carton_erp.sqlite3 `
  --top 200 `
  --output-dir .\docs\migration_reports
```

- 脚本使用 SQLite 只读连接并执行 `PRAGMA query_only=ON`。
- 运行前后核对主库 SHA-256；不提供 `--apply`。
- 候选按同一客户下斜杠前编码匹配 `product_code` 或 `customer_material_code`。
- 本次严格斜杠记录口径为 27,430 / 0 / 707；所有候选保持 `pending`。
- 疑似费用项不得批准为普通产品映射。
- 下一阶段只能处理人工复核文件与副本验证，未经授权不得写产品表或主库正式表。

### 人工复核与审批校验

Worksheet：`docs/migration_reports/PRODUCT_PREFIX_TIANHUA_REVIEW_WORKSHEET_20260619_150757.csv`

填写说明：`docs/migration_reports/PRODUCT_PREFIX_REVIEW_GUIDE_20260619_150757.md`

校验脚本：`scripts/migration/validate_product_prefix_review.py`

只读校验命令：

```powershell
python .\scripts\migration\validate_product_prefix_review.py `
  --sqlite-path .\data\carton_erp.sqlite3 `
  --review-csv .\docs\migration_reports\PRODUCT_PREFIX_TIANHUA_REVIEW_WORKSHEET_20260619_150757.csv `
  --output-dir .\docs\migration_reports
```

- 初始 200 行全部为 `pending`，允许决定留空，但不能进入迁移。
- 任何已填写决定的行必须有审核人和审核时间。
- 批准项必须引用现有、同客户产品；费用项默认禁止批准。
- 校验报告出现任何审批错误时，停止后续副本试迁移。
- 校验通过也不自动应用映射；后续迁移脚本必须显式读取已审核 CSV，并且只能对隔离副本运行。

#### 天华已审核文件校验结果

报告：`docs/migration_reports/PRODUCT_PREFIX_REVIEW_VALIDATION_20260619_172131.md`

- 审核文件状态为 approved 198、rejected 0、pending 0、needs_check 2。
- 第 32、157 物理行未批准，决定均为 `create_product_later`。
- 现行校验规则要求 `create_product_later` 使用 `review_status=rejected`，因此报告含 2 个审批错误并返回退出码 2。
- 其余检查通过：必要字段完整、决定合法、批准产品存在且与候选一致、无费用项误批准、无重复键、无空白批准。
- 主库未修改，SHA-256 未变化，`integrity_check=ok`。
- 停止条件已触发：审批错误未归零前，禁止设计或执行隔离副本试迁移。

#### 天华审核状态修正复验

修正文件：`docs/migration_reports/PRODUCT_PREFIX_TIANHUA_REVIEW_WORKSHEET_APPROVED_EXCEPT_32_157_FIXED.csv`

报告：`docs/migration_reports/PRODUCT_PREFIX_REVIEW_VALIDATION_FIXED_20260619_172509.md`

- 原审核表保留不变；新文件仅修改第 32、157 物理行。
- 两行改为 `review_status=rejected`、`review_decision=create_product_later`，产品 ID 为空。
- 复验结果为 approved 198、rejected 2、pending 0、needs_check 0，审批错误 0。
- 无费用项误批准，主库哈希未变化，`integrity_check=ok`。
- 校验门禁已通过，但本轮未应用映射、未导入订单。后续若获授权，只能先设计并执行隔离副本小批量试迁移。

#### 天华 approved prefix 隔离副本试迁移结果

报告：`docs/migration_reports/TIANHUA_PREFIX_REVIEW_SANDBOX_APPLY_20260619_173202.md`

- 脚本现支持 `--product-review-csv` 和 `--customer-name`。
- 产品匹配顺序为精确匹配优先、approved 人工映射补充；rejected 键整单否决，费用项禁止迁移。
- 副本 dry-run/apply 均为100张订单、100条明细，其中80条使用 approved prefix、20条使用精确匹配。
- 副本正式表由4/7变为104/107，金额689,972.39。
- 两个 rejected 款号未迁移；逐单明细、金额、关联、产品外键和台账校验通过。
- Apply 后复跑为0/0，主库未变化。
- 此结果不授权主库正式表迁移；后续仍须单独评审并获得明确授权。

#### 天华 approved prefix 全范围隔离副本试迁移

报告：`docs/migration_reports/TIANHUA_PREFIX_FULL_SCOPE_SANDBOX_APPLY_20260619_174719.md`

- 脚本新增显式 `--all`，该模式强制要求 `--customer-name` 和 `--product-review-csv`，避免扩展到其他客户。
- 天华全范围完整订单为15,589张、15,651条明细，prefix 14,175条、精确1,476条。
- 副本正式表由4/7变为15,593/15,658，金额31,761,489.94。
- 多明细订单50张；rejected款号、费用项、未匹配明细和其他客户均未进入迁移。
- 关联、产品外键、台账、逐单明细与金额校验通过，复跑为0/0。
- 主库未变化；本结果不授权主库正式表迁移。

### 天华主库正式表迁移就绪性评审

报告：`docs/migration_reports/TIANHUA_MAIN_APPLY_READINESS_REVIEW_20260619_175201.md`

- 主库正式dry-run为15,589张订单、15,651条明细，结果与全范围副本一致。
- 当前不允许主库apply：脚本仍禁止主库路径，limit不能连续分批，且运行中API订单数6与拟迁移SQLite订单数4不一致。
- 建议先生成不可变批次清单，按100、1,000、5,000、5,000、余量分5批。
- 正式执行必须停uvicorn、确认8002关闭、完成锁探针、备份主库/审核CSV/脚本并校验SHA-256。
- 任一批验收失败立即停止；提交后失败采用迁移前整库备份恢复，并先保留失败现场库。
- 在脚本改造、后端数据库URL确认、5批副本连续复验和回滚演练完成前，不得申请或执行主库正式迁移。

### 主库保护改造与5批副本演练

报告：`docs/migration_reports/TIANHUA_MAIN_APPLY_GUARD_AND_5BATCH_REHEARSAL_20260619_180153.md`

- 运行后端真实使用 `data/tm_phase3_dev.sqlite3`，不是 `data/carton_erp.sqlite3`；前者为`orders/order_items`模型，后者为`sales_orders/sales_order_items`。
- `.env`中的`ERP_DATABASE_PATH`未被`phase1_postgres/database.py`读取，故后端使用相对默认路径。
- 脚本现支持不可变manifest、batch-id、主库双重授权和预期数据库哈希。
- Manifest：`docs/migration_reports/TIANHUA_MAIN_BATCH_MANIFEST_20260619_175953.csv`，SHA-256为`91D4340513DAFC2C039261167CCD9CB85FFB4F9EDB3BA5F7C02AAFDD87041FF3`。
- 5批副本演练按100/1,000/5,000/5,000/4,489完成，合计15,589/15,651，每批和全范围复跑均归零。
- 主库未变化。后端数据库与迁移目标的路径和表模型统一前，仍禁止主库apply。

### 后端数据库路径统一结果

报告：`docs/migration_reports/DATABASE_PATH_UNIFICATION_APPLY_20260619_200919.md`

- 正式启动命令使用 `uvicorn app.main:app --host %ERP_BIND_HOST% --port %ERP_PORT% --workers 1`。
- 后端路径优先读取项目根目录 `.env` 中的 `ERP_DATABASE_PATH`；未配置时固定使用项目根目录 `data/carton_erp.sqlite3`。
- 启动后必须先检查 `GET /api/health`，确认：
  - `database` 为正式库绝对路径；
  - `orders_table=sales_orders`；
  - 订单/明细数量与正式库一致。
- `data/tm_phase3_dev.sqlite3` 仅作为废弃测试参考库保留，6 张测试订单不做同步。
- 禁止再用 `phase1_postgres.main:app` 作为正式启动入口；该旧模型使用 `orders/order_items`。
- 本次切换未修改主库。任何历史迁移仍须另行停机、备份、dry-run 和授权。

### 天华正式迁移前最终 Dry-run 结果

报告：`docs/migration_reports/TIANHUA_MAIN_APPLY_FINAL_DRY_RUN_20260619_202709.md`

- 五批正式库 dry-run 为 100 / 1,000 / 5,000 / 5,000 / 4,489，合计 15,589 张订单、15,651 条明细。
- 金额合计 31,761,489.94；Prefix 14,175、精确 1,476。
- 所有 dry-run 均为只读，`inserted=0/0`；rejected、费用项和未匹配明细进入计划均为 0。
- 正式字节级备份：`data/backups/carton_erp_before_tianhua_sales_apply_20260619_202543.sqlite3`，SHA-256 与主库一致。
- 两张正式表为 `INTEGER PRIMARY KEY`，非 AUTOINCREMENT，无对应 sqlite_sequence；迁移后不得手工修改 sequence。
- 正式 apply 时必须在同一连接确认 `PRAGMA foreign_keys=1`，每批后执行 `PRAGMA foreign_key_check` 并要求为空。

#### 正式执行停机门禁

1. 仅在用户明确授权具体 batch 后执行。
2. 停止 `uvicorn app.main:app`，确认 8000 端口不再监听。
3. 检查无其他 Python/SQLite 进程占用主库。
4. 停机后重新计算主库 SHA-256，并创建新的迁移时点备份。
5. 备份 SHA-256、大小及 `integrity_check` 必须通过。
6. 先运行同 batch dry-run；结果必须与本报告一致。
7. apply 命令必须显式包含 batch manifest、batch ID、主库授权参数、确认短语及实时 expected hash。
8. 任一验收失败立即停止，保留失败现场库后恢复整库备份。

当前允许申请 `batch_1`（100 张）主库 apply 授权，但本结果本身不构成授权。

### 天华主库 Batch 1 正式迁移结果

报告：`docs/migration_reports/TIANHUA_MAIN_BATCH_1_APPLY_20260619_203511.md`

- 仅执行 `batch_1`，新增 100 张订单、100 条明细，金额 689,972.39。
- 正式表由 4/7 变为 104/107。
- 时点备份：
  `data/backups/carton_erp_before_tianhua_batch_1_apply_20260619_203207.sqlite3`
- Apply 前主库和备份 SHA-256 一致。
- Apply 后完整性、外键、孤儿、产品 ID、台账、逐单明细和金额校验均通过。
- Rejected 款号、费用项和 Batch 2 迁移数均为 0。
- Batch 1 dry-run 复跑为 0/0。
- 后端已恢复，健康接口指向正式库并报告 104/107。

后续门禁：

1. 先只读验收 Batch 1 的订单列表、详情和业务页面。
2. 未经新的明确授权，不执行 `batch_2`。
3. 如需回滚，先停机并保留当前现场库，再用 Batch 1 时点备份整库恢复。

### 天华 Batch 1 只读验收结果

报告：`docs/migration_reports/TIANHUA_BATCH_1_READONLY_ACCEPTANCE_20260619_205822.md`

- 订单 GET API 正常，分页总数104，三页累计可见100张 `RUIDA-` 历史订单。
- Batch 1 的100张订单和100条明细台账、关联、金额、数量、产品外键及排除项校验通过。
- 5张样例订单的客户、日期、金额、产品快照、规格和材质返回正常。
- 前端登录页加载正常；本轮禁止登录 POST，因此认证后的订单列表未做真实点击验收。
- 当前订单 API 不支持 `keyword` 搜索，订单详情采用列表内展开，无独立详情 GET。
- 在认证后页面验收完成前，不建议申请 Batch 2。

### Batch 1 搜索、详情与登录后页面验收结果

报告：`docs/migration_reports/TIANHUA_BATCH_1_SEARCH_DETAIL_ACCEPTANCE_20260619_211909.md`

- 订单列表 GET 支持 `keyword`、`order_number`、`customer_name` 和分页。
- 独立详情 GET 为 `/api/orders/{order_id}`，不存在订单返回404。
- 前端订单页支持订单号搜索、客户组合筛选、分页和只读详情弹窗。
- 主库搜索 `RUIDA-` 返回100张；5张详情数据与明细金额正确。
- 浏览器登录验收在主库副本的8004临时服务完成，主库仅执行GET。
- 主库保持104/107，SHA-256不变，`integrity_check=ok`。
- 回归测试34项通过。
- 当前可申请 Batch 2，但正式执行仍需新的明确授权、停机、时点备份、
  Batch 2 dry-run、expected hash 和逐批验收。

### 天华主库 Batch 2 正式迁移结果

报告：`docs/migration_reports/TIANHUA_MAIN_BATCH_2_APPLY_20260620_074806.md`

- 仅执行 `batch_2`，新增1,000张订单、1,006条明细，金额1,230,739.83。
- 正式表由104/107变为1,104/1,113。
- 时点备份：
  `data/backups/carton_erp_before_tianhua_batch_2_apply_20260620_074551.sqlite3`
- Apply 后完整性、外键、孤儿、产品ID、台账、逐单明细和金额校验通过。
- Batch 1 保持100张；Rejected、费用项和 Batch 3～5迁移数均为0。
- Batch 2 dry-run 复跑为0/0。
- 后端已恢复，健康接口指向正式库并报告1,104/1,113。

后续门禁：

1. 先只读验收 Batch 2 的搜索、分页、详情和页面性能。
2. 未经新的明确授权，不执行 `batch_3`。
3. 如需回滚，停机并保留现场库后，使用 Batch 2 时点备份整库恢复。

### 天华 Batch 2 只读验收结果

报告：`docs/migration_reports/TIANHUA_BATCH_2_READONLY_ACCEPTANCE_20260620_082805.md`

- 正式库和健康接口均为1,104张订单、1,113条明细。
- `RUIDA-`搜索为1,100张；天华客户筛选为1,103张，组合筛选为1,100张。
- 第1、11、22页均正常，代表性列表查询最大31.44ms，未超时。
- 抽查Batch 1三张和Batch 2七张详情，数据与主库一致。
- 前端10张详情、搜索、筛选、分页和对账页面正常，控制台无应用错误。
- 主库SHA-256、大小、修改时间和操作日志计数未变化，完整性为ok。
- 发布后旧浏览器会话如仍显示旧订单筛选栏，应强制刷新静态页面。

Batch 3门禁：

1. 本次只读验收允许申请Batch 3，但不构成授权。
2. 必须获得仅针对`batch_3`的新授权。
3. 执行前仍须停机、锁检查、新时点备份、实时SHA-256和Batch 3 dry-run。
4. 禁止因Batch 2验收通过而自动执行Batch 3。

### 天华主库 Batch 3 正式迁移结果

报告：`docs/migration_reports/TIANHUA_MAIN_BATCH_3_APPLY_20260620_085943.md`

- 仅执行`batch_3`，新增5,000张订单、5,054条明细，金额8,590,604.37。
- 正式表由1,104/1,113变为6,104/6,167。
- 时点备份：
  `data/backups/carton_erp_before_tianhua_batch_3_apply_20260620_085704.sqlite3`
- Apply前主库和备份SHA-256一致，完整性均为ok。
- Apply后完整性、外键、孤儿、产品ID、台账、明细数和金额校验通过。
- Rejected款号、费用项及Batch 4/5迁移数均为0。
- Batch 3复跑dry-run为0/0。
- 后端已恢复，健康接口为6,104/6,167，`RUIDA-`订单为6,100。

后续门禁：

1. 先只读验收Batch 3的API、搜索、分页、详情和页面性能。
2. 未经新的明确授权，不执行`batch_4`。
3. 如需回滚，停机并保留现场库后，使用Batch 3时点备份整库恢复。

### 天华 Batch 3 只读验收结果

报告：`docs/migration_reports/TIANHUA_BATCH_3_READONLY_ACCEPTANCE_20260620_093126.md`

- 正式库和健康接口均为6,104张订单、6,167条明细。
- `RUIDA-`及天华迁移订单为6,100张，Batch 4/5仍为0。
- 第1、10、50、80、122页、订单号搜索、客户和组合筛选均正常。
- 抽查Batch 1三张、Batch 2四张、Batch 3八张详情，数据与主库一致。
- 列表API中位9.87ms、最大32.09ms；详情中位4.40ms、最大28.44ms。
- 前端15张详情及对账开票收款页面正常，控制台无应用错误。
- `sales_orders.order_number`、`sales_orders.customer_id`和
  `sales_order_items.order_id`均已有索引，不应重复创建。
- `RUIDA-`前缀LIKE当前为全表扫描，但现有规模性能可接受；Batch 4后继续监控。
- 主库哈希、大小、修改时间和操作日志计数未变化，完整性为ok。

Batch 4门禁：

1. 本次验收允许申请Batch 4，但不构成授权。
2. 必须获得仅针对`batch_4`的新授权。
3. 执行前仍须停机、锁检查、新时点备份、实时SHA-256和Batch 4 dry-run。
4. 禁止因Batch 3验收通过而自动执行Batch 4。

## 备份规范

任何写入前：

```powershell
Copy-Item -LiteralPath ".\data\carton_erp.sqlite3" `
  -Destination ".\data\backups\carton_erp_before_legacy_refresh_<timestamp>.sqlite3"
```

随后验证：

- 源文件和备份文件均存在。
- 文件大小一致。
- SHA-256 一致。
- SQLite `PRAGMA integrity_check = ok`。

## 写入纪律

- 默认 dry-run。
- 原始层和正式层分两次迁移。
- 禁止同一轮同时刷新 `legacy_ruida_*` 和写正式表。
- 正式表先迁移 100 条。
- 所有脚本必须幂等。
- 冲突写报告，不得自动猜测后静默落库。
- 禁止覆盖原始 BAK、`BoxDB20` 或主沙盘。

## 停止条件

出现以下任一情况立即停止：

- 目标不是数据库副本。
- 未创建或未验证备份。
- 数量与预期差异无法解释。
- 主键重复或孤儿明细异常扩大。
- 中文乱码。
- 日期、数量或金额解析错误。
- 正式表已存在订单发生冲突。
### 天华主库 Batch 4 正式迁移结果

报告：`docs/migration_reports/TIANHUA_MAIN_BATCH_4_APPLY_20260620_130527.md`

- 仅执行`batch_4`，新增5,000张订单、5,002条明细，金额16,331,793.23。
- 正式表由6,104/6,167变为11,104/11,169。
- 时点备份：
  `data/backups/carton_erp_before_tianhua_batch_4_apply_20260620_130056.sqlite3`
- Apply前主库和备份SHA-256一致；Apply后主库SHA-256为
  `9C7E6BF7CAE37E02DD573F328250A53D075FD8E578C28BC6AB204EB029C13BD1`。
- Apply后完整性、外键、孤儿、产品ID、台账、明细数和金额校验通过。
- Rejected款号、费用项及Batch 5迁移数均为0。
- Batch 4复跑dry-run为0/0。
- 后端已恢复，健康接口指向正式库并报告11,104/11,169，`RUIDA-`订单为11,100。

后续门禁：

1. 先只读验收Batch 4的API、搜索、分页、详情和页面性能。
2. 未经新的明确授权，不执行`batch_5`。
3. 如需回滚，停机并保留现场库后，使用Batch 4时点备份整库恢复。
### 天华 Batch 4 只读验收结果

报告：`docs/migration_reports/TIANHUA_BATCH_4_READONLY_ACCEPTANCE_20260620_133200.md`

- 本轮未执行 Batch 5，未修改数据库，仅执行 GET / 页面只读验收。
- 健康检查继续指向 `D:\纸箱厂erp软件搭建\data\carton_erp.sqlite3`，返回 `sales_orders/sales_order_items = 11104/11169`。
- `RUIDA-` 历史订单数 = 11100；天华客户 + `RUIDA-` 组合筛选数 = 11100。
- 数据库交叉校验通过：Batch 1/2/3/4/5 订单数 = `100/1000/5000/5000/0`，明细数 = `100/1006/5054/5002/0`。
- `integrity_check=ok`、`foreign_key_check=0`、孤儿明细 0、无效产品 0、重复单号 0、费用项命中 0。
- API 深分页第 1 / 10 / 50 / 100 / 150 / 200 / 最后一页均正常；列表接口中位 20.34 ms，详情接口中位 8.02 ms。
- 前端订单页、搜索 `RUIDA-`、客户筛选、组合筛选、详情抽屉和第 2 页翻页正常，浏览器 console 未见 error/warn。

Batch 5 门禁：

1. 本次只读验收允许申请 Batch 5，但不构成授权。
2. 必须获得仅针对 `batch_5` 的新授权。
3. 执行前仍须停机、锁检查、时点备份、Batch 5 dry-run、expected SHA-256 校验。
4. 禁止因 Batch 4 验收通过而自动执行 Batch 5。

## 日常使用前账号 / 权限收口

报告：`docs/go_live_checklists/ACCOUNT_RBAC_HARDENING_APPLY_20260620_152716.md`

执行纪律：

1. 不再执行任何历史迁移 apply。
2. 不修改历史订单、legacy、客户、产品或迁移台账。
3. 仅允许修改账号、密码哈希、角色菜单和 RBAC 代码。
4. 严禁把明文正式密码写入代码、文档、报告或仓库文件。
5. 所有账号写入前先备份主库。
6. 权限代码修改前后均跑测试。

本次收口结果：

- 已创建显式备份：
  `data/backups/carton_erp_before_account_rbac_hardening_20260620_150305.sqlite3`
- 已补齐正式账号：`admin / finance / sales / workshop`
- `workshop` 弱口令旧哈希已移除；`admin` 已重置为现代哈希
- 四个账号均设置 `must_change_password=1`
- 财务不可访问产品 / 材质 / 来料接口，不可执行送货操作
- 销售不可写客户，保留订单 / 报料 / 送货操作
- 车间仅保留订单 / 送货只读与来料访问
- 四角色登录、退出、401 / 403 / 伪造 Cookie 门禁验证通过
- 手机来料页面壳可打开，但数据接口仍受登录保护
- 回归测试 `80 passed`

上线前仍需人工完成：

1. 用本地安全方式为四个账号重新设置“现场可交接”的正式密码。
2. 由财务和车间实际使用人确认最终菜单范围。
3. 确认是否接受手机来料页 HTML 壳公开但数据接口受保护的现状。

## 正式交接密码设置运行说明

报告：`docs/go_live_checklists/FINAL_PASSWORD_HANDOFF_AND_TRIAL_READY_20260620_160320.md`

本轮新增：

- 交互脚本：`scripts/admin/final_password_handoff.py`
- 单测：`tests/scripts/test_final_password_handoff.py`

执行纪律：

1. 不执行任何历史迁移 apply。
2. 不修改历史订单、legacy、客户、产品或迁移台账。
3. 正式密码只能由现场管理员在本机交互输入。
4. 密码不得出现在命令行参数、文档、报告、`.env`、CSV、日志中。
5. 改密前必须重新备份主库。

本轮状态：

- 已创建改密前备份：
  `data/backups/carton_erp_before_final_password_handoff_20260620_154200.sqlite3`
- 已确认主库和备份完整性正常，历史订单数量未变化。
- 已完成脚本和测试准备，但尚未完成最终正式密码写入。

现场执行方式：

```powershell
cd D:\纸箱厂erp软件搭建
.\.venv\Scripts\python.exe .\scripts\admin\final_password_handoff.py `
  --sqlite-path .\data\carton_erp.sqlite3 `
  --api-base-url http://127.0.0.1:8000 `
  --output-json .\docs\go_live_checklists\FINAL_PASSWORD_HANDOFF_RESULT_LOCAL.json
```

执行完成后再做：

1. 四角色正式密码登录验收
2. 旧密码 / 错误密码失效验证
3. 实际人员人工试用

### 当前门禁状态（2026-06-20 16:52）

报告：`docs/go_live_checklists/ONSITE_PASSWORD_HANDOFF_AND_TRIAL_APPROVAL_20260620_165210.md`

- 已完成新的改密前备份与测试。
- 未执行正式改密，因为正式密码必须由现场管理员在本机隐藏输入。
- 在现场管理员手动执行 `final_password_handoff.py` 前，不得宣告进入人工试用。

### 最终复验结论（2026-06-20）

报告：`docs/go_live_checklists/FINAL_ONSITE_PASSWORD_VERIFICATION_AND_TRIAL_READY_20260620_165210.md`

- 已确认历史订单、迁移台账、完整性和外键未变化。
- 已确认旧弱口令和错误密码失效。
- 已确认未登录、伪造 Cookie 和来料 API 保护正常。
- 已确认四账号仍为现代哈希格式。
- 因当前会话不知道新密码明文，无法独立完成“四个新密码登录成功”的自动复验。
- 处理方式：把四角色首次人工真实登录纳入试用第一步。

## 正式使用层历史订单显示优化运行说明（2026-06-20）

报告：`docs/migration_reports/ERP_USABILITY_AND_HISTORY_DISPLAY_OPTIMIZATION_20260620_181607.md`

### 目标

仅隐藏日常使用层的旧系统名称，不改动主库真实历史订单号，不改动原始抽取层。

### 已落地规则

1. 普通业务 API 统一返回历史订单展示编号 `TMYYYYMMDD-####`
2. 前端页面只显示 `display_order_number`
3. 用户可见备注、提示文案和 go-live 文档不再出现旧系统名称
4. 旧名称仅允许保留在内部技术对象名、迁移审计文件和历史迁移资料中

### 相关代码

- 共享服务：`app/services/history_orders.py`
- 已改造接口：
  - `app/api/orders.py`
  - `app/api/incoming.py`
  - `app/api/requisition.py`
  - `app/api/finance.py`
  - `app/api/deliveries.py`
- 前端页面：`static/index.html`

### 副本真实改号演练

执行脚本：

```powershell
.\.venv\Scripts\python.exe .\scripts\migration\rehearse_history_display_and_status.py `
  --sqlite-path .\data\carton_erp.sqlite3
```

输出：

- 副本库：`data/sandboxes/carton_erp_history_display_status_rehearsal_20260620_181354.sqlite3`
- 映射清单：`docs/migration_reports/HISTORY_ORDER_TM_RENUMBER_MAPPING_20260620_181354.csv`
- 报告：`docs/migration_reports/HISTORY_ORDER_TM_RENUMBER_REHEARSAL_20260620_181354.md`

结果：

- 历史订单 15,589 全部映射为 TM 编号
- 剩余旧前缀订单 0
- 重复单号 0
- `integrity_check=ok`
- `foreign_key_check=0`
- 主库未修改

### 运行边界

1. 本轮没有主库真实批量改号。
2. 如后续需要主库真实改号，必须单独授权。
3. 新增页面、接口、用户文档时，继续执行“旧系统名称不得出现在日常使用层”的规则。

### 天华主库 Batch 5 正式迁移结果

报告：`docs/migration_reports/TIANHUA_MAIN_BATCH_5_APPLY_AND_FINAL_RECON_20260620_135253.md`

- 仅执行 `batch_5`，新增 `4489` 张订单、`4489` 条明细，金额 `4,918,380.12`。
- 未重新执行 batch_1/2/3/4，未发生全量滑批。
- 时点备份：
  `data/backups/carton_erp_before_tianhua_batch_5_apply_20260620_134925.sqlite3`
- Apply 前主库和备份 SHA-256 一致：
  `9C7E6BF7CAE37E02DD573F328250A53D075FD8E578C28BC6AB204EB029C13BD1`
- Apply 后主库 SHA-256：
  `162450A396E1CC4A439618084501E915D181201BA8C3849D0836C0AF50ECDCAE`
- 正式表由 `11104/11169` 变为 `15593/15658`；`RUIDA-` 订单变为 `15589`。
- 订单/明细台账分别为 `15589 / 15651`；五批订单数 `100/1000/5000/5000/4489`，五批明细数 `100/1006/5054/5002/4489`。
- `integrity_check=ok`、`foreign_keys=1`、`foreign_key_check=0`。
- 无孤儿、无无效产品、无重复台账、无重复单号、无非正数量。
- Batch 5 逐单明细数差异 `0`，逐单金额差异 `0`，batch_5 legacy / sales 金额合计均为 `4,918,380.12`。
- 第 32、157 行 rejected raw 款号进入正式明细均为 `0`；费用项进入正式明细为 `0`。
- Batch 5 dry-run 复跑为 `0/0`，全量剩余 dry-run 为 `0/0`。
- 后端已恢复并读取正式库，健康检查为 `15593/15658`，`GET /api/orders?keyword=RUIDA-` 为 `15589`。

后续门禁：

1. 下一步只做“最终只读验收”，不得追加任何迁移写入。
2. 最终只读验收通过后，再做最终备份归档。
3. 本轮完成后不要宣布项目结束。

## N031 auth_version 迁移与受控回滚（2026-07-17）

迁移 `ba54v8x9z44` 在 `az53v8x9z43` 后为 `users` 增加不可为空的
`auth_version`，默认值为 `1`。升级会为已有用户回填 `1`，用于使登录
会话能够按用户撤销。

### 升级

1. 停止应用或确认没有并发数据库写入，创建并验证可恢复备份。
2. 对目标副本或已获批准的正式数据库执行：

   ```powershell
   .\.venv\Scripts\python.exe -m alembic upgrade ba54v8x9z44
   ```

3. 确认版本、字段及 SQLite 完整性：

   ```powershell
   .\.venv\Scripts\python.exe -m alembic current
   sqlite3 <database> "PRAGMA integrity_check; PRAGMA foreign_key_check;"
   ```

### 默认回滚策略：拒绝有用户数据的降级

空 `users` 表可以直接降级；有任一用户时，`downgrade az53v8x9z43` 默认
失败。这是 fail-closed 保护：删除 `auth_version` 会永久丢失按用户撤销
会话的状态，旧应用可能重新接受原本应失效的已签名会话。

### 正式紧急回滚：仅限停机并完成会话密钥轮换后

只有在以下条件全部满足时，才允许有用户数据的降级：

1. 应用及所有 worker 已完全停机，且已确认没有旧版本实例继续处理请求。
2. 已完成并验证数据库备份；回滚范围、负责人和恢复步骤已记录。
3. 已在部署密钥系统中轮换会话签名密钥，并确保旧密钥不再可用。不要把
   密钥写入命令行、环境确认值、文档或日志。
4. 仅为这一次 Alembic 命令设置下列精确确认值，然后立即清除它：

   ```powershell
   $env:N031_AUTH_VERSION_DOWNGRADE_CONFIRM = "DOWNTIME_COMPLETE_AND_SESSION_SECRET_ROTATED"
   .\.venv\Scripts\python.exe -m alembic downgrade az53v8x9z43
   Remove-Item Env:N031_AUTH_VERSION_DOWNGRADE_CONFIRM
   ```

该确认值只表示操作者已完成停机和密钥轮换；迁移不会读取、校验、输出或
记录任何会话密钥。它无法替代停机、密钥轮换和备份验证。若任一前提无法
确认，应保持在 `ba54v8x9z44`，不要降级。

降级后应在隔离环境执行登录和会话失效验收。若重新升级，`auth_version`
会以 `1` 回填，因此此前更高的撤销版本不可恢复；必须按一次新的会话安全
切换处理，并要求用户重新登录。

## N034 Phase A 复合产品 BOM（2026-07-18）

### 迁移范围

- worktree：`D:\tm-worktrees\erp-composite-bom-n034`；branch：`feature/composite-bom-n034`。
- Alembic：`bc56v8x9z46 -> bd57v8x9z47`。
- 新表：`product_bom_components`（产品 BOM 模板）、`sales_order_item_bom_components`（订单项历史快照）、`requisition_item_bom_sources`（报料来源）。
- `products` 新增 `is_composite` 与 `is_internal_component` 两个布尔标记。

### 历史与删除约束

- 订单项 BOM 快照字段一经生成即不可更新；模板删除只允许将快照的 `product_bom_component_id` 置为 `NULL`（`SET NULL`），不能改变已保存的历史快照字段。
- 报料来源引用订单项快照；不得通过删除模板或修改模板回写历史订单或报料事实。

### downgrade 门禁

降级前必须满足全部条件：`product_bom_components`、`sales_order_item_bom_components`、`requisition_item_bom_sources` 三表均为空，且 `products` 中不存在 `is_composite = true` 或 `is_internal_component = true` 的产品。任一条件不满足即 fail-closed 拒绝降级，并要求恢复 `bd57v8x9z47` 升级前的完整备份。

### Phase A 验证记录

- 隔离副本：`D:\tm-uat\composite_bom_n034_20260718_130112\carton_erp_uat.sqlite3`。
- 已完成 `head -> base -> head` 往返演练；最终 `integrity_check=ok`、`foreign_key_check=0`。
- 自动测试：`144 passed`；UAT：`18068`。
- 正式库未写入；本轮未 commit、未 push。

## 新旧BOM执行边界78（候选，2026-09-10）

`sc15v8x9z77 -> sd16v8x9z78`只增两张空关联表及原快照id/订单唯一索引。隔离副本先校验时点备份及FK，升级77→78、降77、再升78；逐行核对原订单、快照、库存、预占、流水、货位、完工、送货及触发器不变。有转换事实拒绝降级，正式回滚使用已验证备份。不手工插入边界记录：专用转换入口及历史校验、执行读取适配尚未完成，原已送快照保护不解除。任务卡MULTILEVEL-BOM-20260909。

## 生产资料追加版本76（候选，2026-09-10）

只在已验证隔离复制品先执行`upgrade sb14v8x9z76 -> downgrade sa13v8x9z75 -> upgrade sb14v8x9z76`，核对唯一head、完整性、外键和原订单/库存/位置计数。新表非空时降级必须拒绝，不得删除修订事实换取降级。正式升级仍须最新正式基线、迁移时点完整备份、恢复演练、发布窗口和健康验收；本条不表示已发布。此修订不改变原快照、工厂地图或草稿，也不恢复旧抽取入口。
# BOM 与工厂 v319 合并节点操作边界（2026-09-10 候选）

目标唯一 head 为 `se17v8x9z79`，两个前驱 `sd16v8x9z78` / `ru10v8x9z69` 均保留。正式 69 分支有已批准成本补充事实，禁止删除、重写或降掉该分支。隔离验收中 upgrade head 后，如只撤销合并节点，应显式 downgrade `sd16v8x9z78`，核对版本表保留两个前驱后再 upgrade head；**不能使用相对 `-1`，合并点会出现 Ambiguous walk**。此操作不等于业务/正式库回滚；生产回滚依照已核验整库备份方案。禁止对正式库套用测试副本或测试货位。

## 来源交接87隔离操作（2026-09-11）

仅在经过哈希验证、可丢弃的工厂导出复制品执行86→87→86→87；核对唯一head、完整性、外键、原表原列逐行事实及显式索引/触发器。新表为空可降86；已有交接事实必须在DDL前拒绝，验证失败前后字节哈希一致。不得删记录、stamp或重建已退役抽取层。正式回滚仍使用验证过的完整备份，不能把多步跨版本降级等同原子恢复；87以后不得直接套用旧测试的起始head。当前只升级定向测试复制品，私有运行主副本及正式库未升级。

## 外购实收执行来源88隔离操作（2026-09-11）

先验证复制备份，再在可丢弃测试库演练87→88→87→88，已有87来源交接事实必须保留。88表非空降级在DDL前拒绝，核对字节哈希。不得把旧实收批量回填为当前执行来源。后续正式操作仍使用工厂新鲜只读源复验及正式备份方案；当前主UAT未升88，旧运行进程不能称已验证新代码。

## v350与家庭BOM合并89（2026-09-11候选）
正式e2caa6906367e5182c6585720f8198dc19896e84保留rx10v8x9z72库存成本、ry10v8x9z73邮件暂存、rz10v8x9z74邮件订单关联。未发布家庭graph_delivery_cost的重号改为rx11v8x9z72，后继ry11保持BOM血缘；已处于86/88的候选不重跑祖先DDL。恰停于旧候选重号rx10的副本不可直接使用新代码升级，必须先核实schema并用原候选代码推进到无歧义后继；禁止stamp。正式编号和正式数据库不改写。
新增so27v8x9z89合并sn26v8x9z88与rz10v8x9z74，无DDL和业务回填。有BOM版本/交接、库存成本规则/操作或邮件事实，在改变合并元数据前拒绝降级，恢复使用已验证完整备份。
6项定向迁移验证通过11.10秒，含v330私有复制品升级、显式撤合并保留两分支后再次升级、原表原字段/索引/触发器、完整性与外键、成本和邮件事实降级拒绝文件哈希不变。尚未取得v350新鲜只读数据，不表示最新正式事实已验收；私有运行实例尚未升级89，工厂禁止据此发布。

## v352邮件PDF草稿与BOM合并90（2026-09-11家庭候选）
正式94fb12df036ed1ef820480f337461246a5d664fd新增sb11v8x9z76邮件PDF工作草稿。sp28v8x9z90合并so27v8x9z89和sb11v8x9z76，无DDL/回填，不改写89等既有revision。已有草稿、邮件、成本或BOM交接事实时，在合并元数据改变前拒绝降级；恢复采用已验证完整备份，不删事实或stamp。
隔离兼容10项通过25.67秒，另真实草稿记录阻止降级且文件哈希不变1项通过2.66秒；保留原表原列/索引/触发器及完整性/FK。源仍为v330，仅证明旧源升级兼容，不冒充最新正式资料。18083仍运行89/ccf03c4b，不把当前代码90称已在Chrome验收。正式环境未执行迁移或业务操作。

## MAIL014 邮箱处理记录（2026-09-12）

sprep0912 → mq0912仅新增email_pdf_dispositions空表，按附件SHA记录人工处理/删除/重复，不修改订单、库存和金额。原关联订单删除及已成功PDF导入审计仍作为排除依据。隔离正式副本升降升，284张原表事实逐表哈希一致，integrity ok/FK 0；有处理记录拒绝有损降级。正式部署须新时点备份、唯一head、隔离演练及只读健康/静态核对，回滚使用验证过的发布备份，不删除处理记录绕过门禁。


## 2026-09-22 报料全额抵扣后保留材料备库 dv0922

du0920 → dv0922 仅将用途来源快照的组权威订单张数约束从正数扩展为非负数；既有用途守恒与产出约束使零需求时订单用途只能为0。无历史数据回填。SQLite重建该表时原样保存并恢复引用触发器，检查外键；已有零需求备库事实时拒绝降级，禁止删事实回滚。当前新鲜正式副本完成升降升，298张原业务表和192个触发器保持，integrity ok/FK 0。正式迁移必须由Manager先停服、创建并验证新时点备份，再对签名包演练与比较原事实后执行；运行结果另见本轮独立发布回执。

## 2026-09-22 合作公司跨账单开票 dw0922

dv0922 → dw0922 只新增不可变 finance_invoice_task_statements 关联表，无历史回填。隔离新鲜正式副本升降升：298张原业务表逐行哈希和192个原触发器保持，integrity ok/FK0；非空降级在DDL前拒绝且文件哈希不变，关联UPDATE/DELETE拒绝。正式发布仍需Manager停服、新时点NAS备份校验、签名包隔离演练、原事实比对和只读健康检查。无关联事实时才允许回退旧程序；已有合并开票事实必须保留新库向前修复，禁止运行不理解份额的旧财务代码或恢复旧库覆盖新事实。完整证据与发布状态见 docs/release_reports/PARTNER_INVOICE_MERGE_20260922.md。


## 2026-09-22 财务待收款修复随最新正式基线发布 dx0922

财务查询本身无迁移。发布前 origin/factory-current-baseline 已整合图纸 V2 的 dw0922→dx0922，故保留该上游迁移，版本顺延 v0.22.487；不回退正式分支。最新正式库只读副本升降升通过：299 张原业务表逐行哈希、675 个原索引/触发器保持，完整性 ok、外键 0。新增图纸五表为空，不补建图纸或财务事实；正式更新仍由 Manager 先停服、核验完整 NAS 时点备份，再以签名包隔离演练并比较原事实后执行。新表出现业务事实后禁止有损降级，不能删行或覆盖旧库；采用保留事实的向前修复。具体正式结果以 FINANCE_UNPAID_HISTORY_20260922 独立回执为准。


## 2026-09-26 管理员回退与开票来源归档 ea0926

dz0922 → ea0926 为开票任务明细增加归档对账调整外键及原明细编号；有效来源与归档来源必须二选一。只有已作废任务、真实原明细和完整取消对账快照匹配时允许归档，归档快照禁止改写。升级不回填、删除或修改原业务事实；SQLite重建保留原触发器、索引及外键。已出现归档事实时降级在DDL前拒绝，禁止清空事实绕过；有新事实后采用向前修复，不用旧库覆盖。正式副本升降升已核对304张原业务表与683个原索引/触发器、integrity ok/FK0；正式发布仍须Manager停服时点NAS备份、签名包隔离演练与事实比对。具体发布状态见 WAREHOUSE_QUANTITY_REVERSAL_20260926 独立回执。
