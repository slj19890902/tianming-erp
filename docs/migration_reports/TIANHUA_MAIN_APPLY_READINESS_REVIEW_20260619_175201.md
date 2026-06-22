# 天华超净主库正式表迁移就绪性评审

- 评审时间：2026-06-19 17:52:01 +08:00
- 本轮是否修改主库：否
- 是否执行主库正式表迁移：否
- 是否运行 `--apply`：否
- 结论：**当前不具备执行主库正式表迁移的全部条件。**

数据、人工映射和副本验证已经通过，但执行脚本和运行环境仍有三项阻断：

1. 脚本明确禁止对规范主库路径执行 `--apply`。
2. 当前 `--limit` 先截取候选再排除已导入台账，不能连续推进第2批，尚不支持可靠分批。
3. 当前 ERP API 返回订单总数6，而 `data/carton_erp.sqlite3` 为4，后端实际数据库与拟迁移主库不一致。

## 当前主库状态

主库：`data/carton_erp.sqlite3`

| 项目 | 结果 |
|---|---:|
| SHA-256 | `6ED950245BC91E18A8D4C8F55CB55B6630396B16117DF4145922C725E28E7F77` |
| 文件大小 | 209,182,720 字节 |
| 修改时间 UTC | 2026-06-19 01:07:42 |
| `integrity_check` | `ok` |
| `sales_orders` | 4 |
| `sales_order_items` | 7 |
| `legacy_ruida_orders` | 39,922 |
| `legacy_ruida_order_items` | 40,449 |
| `products` | 3,316 |
| `customers` | 132 |
| 订单迁移台账表 | 不存在 |
| 明细迁移台账表 | 不存在 |

本轮正式 dry-run 报告：

`docs/migration_reports/FORMAL_SALES_SAMPLE_DRY-RUN_20260619_175030.md`

正式 dry-run 与全范围副本结果一致：

- 计划订单15,589，明细15,651。
- 金额31,761,489.94。
- Approved prefix明细14,175，精确匹配1,476。
- 多明细订单50。
- 跳过：未匹配8,527、rejected 70、费用项1。
- 进入迁移的 rejected、费用项、未匹配明细均为0。

## 副本验证摘要

报告：`docs/migration_reports/TIANHUA_PREFIX_FULL_SCOPE_SANDBOX_APPLY_20260619_174719.md`

- 副本完整迁移15,589张订单、15,651条明细。
- 正式表由4/7变为15,593/15,658。
- 逐单明细数、逐单金额、逐明细金额差异均为0。
- 无孤儿明细、无效产品、重复台账或跨客户迁移。
- 第32、157物理行对应款号及费用项迁移数均为0。
- 复跑dry-run为0/0。

## 迁移脚本能力评审

已支持：

- `--sqlite-path`
- `--product-review-csv`
- `--customer-name`
- `--sample-mode complete_orders`
- `--limit`
- `--all`
- `--apply`及确认短语
- 默认dry-run和SQLite只读连接
- 事务 `BEGIN IMMEDIATE`、失败rollback
- 订单/明细源ID迁移台账
- rejected映射整单排除
- 费用项整单排除
- `--all`强制绑定单一客户及审核CSV

尚不满足主库执行：

- `validate_apply_target` 永久拒绝规范主库路径，必须另行设计显式主库授权参数和独立确认短语。
- `--limit` 在排除已导入台账之前截断，不能作为连续批次游标。
- 主库尚无两张迁移台账；首次正式迁移将创建表，必须纳入明确授权和备份验收。
- 未提供不可变批次清单、批次ID和批次报告关联。
- 未锁定审核CSV、脚本和主库的预期SHA-256。

建议先改造并在新副本重复验证，不能直接解除主库保护。

## 运行环境与停机评审

当前发现：

- 正在运行 uvicorn：`phase1_postgres.main:app`，监听端口8002。
- `/api/health` 返回 `ok`。
- `/api/orders` 返回总数6。
- 拟迁移主库 `sales_orders=4`。

因此必须先确认并固定生产后端的 `TM_ERP_DATABASE_URL`，确保它明确指向：

`sqlite+pysqlite:///D:/纸箱厂erp软件搭建/data/carton_erp.sqlite3`

在该项确认前，即使迁移SQLite成功，前端也可能看不到新订单。

未来获授权后的停机步骤：

1. 公告维护窗口，禁止用户新增、编辑、送货或对账。
2. 记录匹配 `uvicorn phase1_postgres.main:app` 的进程树和8002监听PID。
3. 先正常结束启动窗口或父进程；无响应时才对已确认PID使用 `Stop-Process`。
4. 等待并确认：
   - 无匹配uvicorn进程。
   - 8002端口不再监听。
   - 无新的ERP日志写入。
5. 执行SQLite锁探针：连接主库，`BEGIN IMMEDIATE` 后立即 `ROLLBACK`；前后哈希必须一致，并确认无残留 `-journal`、`-wal`、`-shm`。
6. 任一检查失败立即停止，不备份、不迁移。

## 备份方案

停机和锁检查通过后创建：

`data/backups/carton_erp_before_tianhua_formal_sales_apply_YYYYMMDD_HHMMSS.sqlite3`

同批次归档：

- `PRODUCT_PREFIX_TIANHUA_REVIEW_WORKSHEET_APPROVED_EXCEPT_32_157_FIXED_YYYYMMDD_HHMMSS.csv`
- `migrate_legacy_ruida_to_sales_orders_YYYYMMDD_HHMMSS.py`
- 执行前dry-run报告和批次清单。

必须记录并核对：

- 主库、主库备份、审核CSV、脚本的SHA-256。
- 文件大小和修改时间。
- 主库及备份的 `integrity_check=ok`。
- 主库六张业务/源表数量。
- 审核CSV固定哈希：
  `6A4C966AD497273533D04E69FF82DB2F421AA5392970B5A0AE61B9318EBAE8A7`
- 当前脚本固定哈希：
  `0202C104F27B53F4A1A364A6FFEEEFF1194B14FCFB4A5BA9CA4B3F4A55007739`

## 分批方案

建议5批，而不是一次性写入：

| 批次 | 订单数 |
|---|---:|
| 第1批 | 100 |
| 第2批 | 1,000 |
| 第3批 | 5,000 |
| 第4批 | 5,000 |
| 第5批 | 余下约4,489，以正式dry-run为准 |

虽然副本全范围事务执行很快，但生产风险主要来自数据可见性、后端连接和业务验收，不是写入耗时。

正式改造应先由全范围dry-run生成不可变订单ID清单，再拆成5个批次CSV。迁移脚本必须增加：

- `--batch-order-csv`
- `--batch-id`
- 显式主库授权参数
- 主库预期SHA-256
- 审核CSV预期SHA-256

每个批次只读取自己的订单ID，不使用当前 `--limit` 作为连续分页机制。

## 每批执行流程

1. 保持ERP后端停止。
2. 对本批次清单运行dry-run。
3. 校验订单/明细数、金额、prefix/精确数量、客户ID、rejected/费用/未匹配进入数。
4. dry-run全部通过后，才使用同一批次清单apply。
5. Apply使用独立事务；异常自动rollback当前批次。
6. Apply后检查：
   - `integrity_check=ok`
   - 正式订单和明细增量与计划一致
   - 无孤儿明细、无效产品ID
   - 无重复订单/明细台账
   - 逐单明细数、逐单金额、逐明细金额差异为0
   - rejected两个款号和费用项迁移数为0
7. 对同一批次清单复跑dry-run，必须为0/0。
8. 任一项失败，停止后续批次并执行全库回滚。

## 回滚方案

当前没有可靠的“按已提交批次反向删除”工具，因此正式策略采用全库恢复：

1. 停止后端并保持8002关闭。
2. 将失败现场主库复制到：
   `data/failed_evidence/carton_erp_failed_tianhua_formal_sales_apply_YYYYMMDD_HHMMSS.sqlite3`
3. 记录失败现场SHA-256、大小、修改时间、完整性和失败报告，不删除现场。
4. 核验迁移前备份SHA-256和 `integrity_check`。
5. 从已验证备份恢复 `data/carton_erp.sqlite3`。
6. 再次核对主库哈希、六张表数量和 `integrity_check` 均回到迁移前基线。
7. 确认后端数据库URL指向恢复后的主库，再启动服务。
8. 执行健康检查和前端抽查。

事务内失败只rollback当前未提交批次；批次提交后的验收失败则恢复迁移前整库备份，撤销全部批次。

## 最终验收

正式执行前仍以新的主库dry-run为唯一基线。预计值为：

- 新增订单15,589。
- 新增明细15,651。
- 总金额31,761,489.94。
- Prefix明细14,175。
- 精确匹配明细1,476。
- 多明细订单50。
- 跳过8,598：未匹配8,527、rejected 70、费用项1。

全部迁移后必须验证：

- 累计增量与正式dry-run一致。
- 两个rejected款号、费用项、其他客户迁移数均为0。
- 全范围复跑dry-run为0/0。
- `/api/health` 返回 `{"status":"ok"}`。
- `/api/orders?customer_id=5` 能查询迁移订单，总数与台账一致。
- 前端订单页能按天华客户查询并打开订单详情。
- 送货、回单和月结对账页面能正常加载；本次历史订单不会自动生成送货、回单或对账数据。
- 后端日志无SQLite锁、外键、序列化或500错误。

## 必须明确授权后才能执行的命令类型

以下操作本轮均未执行，未来必须由用户再次明确授权：

1. 停止或启动ERP后端进程。
2. 创建主库及审核材料备份。
3. 修改脚本以支持主库授权、批次清单和预期哈希。
4. 在副本复验改造后的分批模式。
5. 对主库运行任何带 `--apply` 的命令。
6. 创建主库迁移台账表。
7. 发生失败时覆盖主库执行恢复。

## 下一步建议

下一最小任务不是主库apply，而是：

1. 只改造脚本的“可复现分批清单、主库双重授权、预期哈希”能力。
2. 确认生产后端实际数据库URL，并使API订单总数与拟迁移主库一致。
3. 在新副本完整复验5批连续迁移和全库回滚演练。

三项完成并再次评审通过后，才具备请求主库正式迁移授权的条件。
