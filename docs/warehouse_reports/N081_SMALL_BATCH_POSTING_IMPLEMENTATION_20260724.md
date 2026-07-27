# N081 小批确认入账收缩版实施报告

## 1. 结论

旧 B2 试盘界面已经完成技术验收，但不作为正式候选继续交付。当前实现从
B1 提交 `a1ec412891756a609de5bb19a1a3cc3ee841d1d4` 独立开发，将操作流程
收缩为：

1. B1 上传、匹配、修正和 dry-run。
2. 提交批次，形成不可再编辑的页面预览。
3. 有盘点复核权限的人员点击一次“正式入账（N 行）”。
4. 系统自动把该批次全部 `create_new + ready` 行写入现有正式库存体系。

页面不提供逐行勾选、固定栈板数量、原因输入、确认词、二次确认或专用回滚。
旧 B2 提交 `2466e3360c08468adf5b716034467e58f236d1c0` 仅保留为技术证据，
没有合并、推送或部署。

## 2. 根因与设计取舍

实际操作 ERP 的人员只有两人。旧 B2 为隔离试盘设计的逐行选择、口令、原因、
二次确认和回滚入口会增加现场操作成本，却没有增加库存事实本身的可信度。

本轮将“批次提交后的完整页面”作为操作员预览，把复杂门禁留在后台：

- 权限使用现有 `warehouse.stocktake.review`。
- 每次入账重新验证私有源文件大小和 SHA-256、批次版本及 dry-run 指纹。
- 只接收同一楼层、同一区域、最多 100 行的完整栈板组。
- 重新检查客户、产品、材质、库位、栈板和库位占用事实。
- 一个来源行只能生成一个正式库存批次。
- 整批使用一个数据库事务；任一行失败则全部回滚。
- 服务端生成幂等键；重复请求返回原入账结果，不重复创建库存。
- 保留库存流水、库位、栈板关联和操作日志。

入账后的日常错误修正继续使用现有库存调整流程，不建立 B2 专用回滚状态机。

## 3. 修改范围

- `app/api/inventory_onboarding.py`
  - 批次列表和详情返回入账摘要。
  - 新增无请求体的
    `POST /api/warehouse/inventory-onboarding/batches/{batch_id}/post`。
- `app/services/inventory_onboarding.py`
  - 提供已提交批次、源文件和冻结事实的统一复核入口。
- `app/services/inventory_onboarding_posting.py`
  - 自动选择全部合格行，执行锁定后复核、现有入库服务调用、栈板绑定、
    幂等和审计。
- `app/models/inventory_onboarding_posting.py`
  - 保存一次不可变的小批入账回执，不建立逐行审批状态。
- `app/services/warehouse_inventory.py`
  - 在不改变既有调用默认行为的前提下，支持盘点来源的通用成品和半成品入库。
- `app/services/floor3_locations.py`
  - 修正通用成品绑定三楼栈板时被产品客户重新污染的问题。
- `app/models/warehouse_inventory.py`
  - 声明盘点来源行唯一索引。
- `static/warehouse.html`
  - B1 提交取消复选框和 JavaScript 确认弹窗。
  - 已提交批次只显示一条摘要和一个正式入账按钮。
- `alembic/versions/cm69v8x9z58_n081_small_batch_posting.py`
  - 线性接续 `ck67v8x9z56`。
  - 新增不可变入账回执、来源唯一索引和 SQLite 事实触发器。
- `tests/test_n081_small_batch_posting_*.py`
  - 覆盖服务、API、前端和迁移。

## 4. 迁移与隔离数据库证据

- 唯一 Alembic head：`cm69v8x9z58`。
- 只读源副本：
  `D:\tm-uat\n081_small_batch_posting_20260724\migration_rehearsal_v2\source_ck67_backup.sqlite3`
- 源副本 SHA-256：
  `E6588578879DD8A0192BDFCF6733F36AD53815C8A907044722C9205536CCF59F`
- 迁移副本：
  `D:\tm-uat\n081_small_batch_posting_20260724\migration_rehearsal_v2\working_ck67_to_cm69.sqlite3`
- 迁移副本 SHA-256：
  `30E9C2BD51CD43318CBB079CEE725F8AF8EC4A90AD889F801EA921A29354F455`
- 演练结果：`ck67v8x9z56 → cm69v8x9z58`，
  `integrity_check=ok`，`foreign_key_check=0`，入账回执数 0。

人工 UAT 使用迁移副本的再次复制件：

- 数据库：
  `D:\tm-uat\n081_small_batch_posting_20260724\manual_uat_v1\n081_small_batch_manual_uat.sqlite3`
- revision：`cm69v8x9z58`
- `integrity_check=ok`
- `foreign_key_check=0`
- 当前正式入账回执数：1
- 页面：`http://127.0.0.1:18089/warehouse.html`
- 账号：`n081_post_uat`
- 密码：`123456`

UAT 批次 `IOB-20260724-68A8CEA5B5` 有 3 行，均位于三楼 E1：

- 客户专用成品 21 个，库位 `E1-POST-F1`
- 通用成品 22 个，库位 `E1-POST-F2`
- 通用半成品 30 张，库位 `E1-POST-S1`

用户点击“正式入账（3 行）”后，后端返回 200 并一次生成：

- 1 份不可变入账回执
- 3 个正式库存批次
- 3 个栈板和 3 条栈板明细
- 3 条入库流水

首次成功响应中的 `posted_at` 直接返回了 UTC-naive 时间，前端严格的 RFC3339
格式化器因此显示红字
`datetime must be RFC3339 with Z or an offset`。该错误只发生在成功后的展示阶段，
没有回滚或重复写库存。现已让 `posting_payload()` 复用项目统一的
`utc_naive_to_api()`，并在 API 测试中强制断言 `posted_at` 带 `Z`。

修复后重启同一隔离 UAT 服务，页面已正确显示“已正式入账”、3 行、3 个栈板、
3 个正式库存批次和北京时间；按钮隐藏，红字为空，Console 无 error/warn。
数据库仍为 `integrity_check=ok`、外键异常 0。

## 5. 自动验证

- B0、B1、收缩版 B2、空间门禁、库存基础和三楼 API 最终有效回归：
  `169 passed`。
- 成功响应时间格式修复后的 B2 专项：`14 passed`。
- 前端测试包含内联 JavaScript `node --check`。
- Python `compileall`：通过。
- `git diff --check`：通过。
- Alembic：`cm69v8x9z58 (head)`，只有一个 head。

扩大回归共运行 222 项，其中 `217 passed`；另外 5 个失败均来自
`tests/test_floor3_locations_frontend.py` 的旧源码字符串精确断言。对照
`a1ec412` 基线后确认这些字符串在未修改基线中同样不存在，本轮
`warehouse.html` 的差异只涉及库存建账区域，因此这 5 项不是本轮回归。
未删除或篡改这些旧测试。

## 6. 人工 UAT

1. 登录 18089，进入“仓库库存管理 → 库存建账草稿”。
2. 核对批次显示 3 行、成品 2、半成品 1、3 个栈板、区域 E1。
3. 已完成：点击一次“正式入账（3 行）”，没有任何输入或二次确认。
4. 已完成：页面显示 3 行、3 个栈板、3 个正式库存批次和入账时间，按钮隐藏。
5. 在成品仓、半成品仓和库存流水中核对上述三条测试库存。
6. 再次打开该批次，确认只能看到已入账结果，不能重复创建库存。

用户已确认修复后的成功页人工 UAT 通过。当前候选允许形成独立提交并只推送
`codex/n081-small-batch-posting-20260724`；禁止修改 `origin/main`，
禁止家庭侧部署到工厂正式目录或迁移工厂正式数据库。
