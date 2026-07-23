# N081-B1：账外库存私有导入草稿与 dry-run 实施报告

日期：2026-07-23

开发起点：

`codex/n081-b0-inventory-semantics-20260723@91a888777cdf5aee97c6ed5a83dc756524e23e98`

开发分支：

`codex/n081-b1-onboarding-dryrun-20260723`

## 1. 本阶段边界

N081-B1 只完成以下闭环：

```text
私有 CSV/XLSX
→ 保留源文件 SHA-256、原始行和原文
→ 持久化草稿
→ 匹配与人工修正
→ dry-run
→ 显式确认并冻结不可变草稿
```

本阶段没有正式入账接口，不创建或修改：

- `inventory_lots`
- `inventory_movements`
- `inventory_pallets`
- `inventory_pallet_items`
- `inventory_reservations`

`/api/warehouse/inventory-onboarding` 下不存在 `/apply`。正式小范围试盘属于
独立的 N081-B2；工厂分区域正式入账属于 B3，不在家庭开发授权范围。

## 2. 迁移与状态模型

新增线性迁移：

```text
cj66v8x9z55
→ ck67v8x9z56
```

新增：

- `inventory_onboarding_batches`
- `inventory_onboarding_lines`

批次状态为 `draft → submitted`。草稿使用批次和明细双版本 CAS；提交要求：

- 最新批次版本；
- 最新 dry-run SHA-256 指纹；
- 零阻断错误；
- 单区域；
- 显式 `confirmed=true`；
- 唯一提交幂等键。

SQLite 触发器保护源文件元数据、原始行和 submitted 事实；已提交批次及明细
禁止更新、删除或新增明细。存在 B1 事实时 downgrade fail-closed。

## 3. 文件与权限门禁

文件先完整解析和校验，再进入 P0-B 私有存储：

- 支持 UTF-8 BOM、UTF-8、GB18030 CSV；
- 支持真实 `.xlsx`；
- 拒绝伪扩展名/伪 MIME、宏、公式、外链、嵌入对象、多非空工作表、路径穿越、
  控制字符、公式注入、超限文件和 ZIP bomb；
- 同一文件 SHA-256 重复导入返回原批次，不重复保存事实；
- 事务失败清理未关联私有文件；数据库已成功提交后不再误删正式引用的源文件。
- 提交冻结前重新解析私有引用并流式核对文件存在性、字节数和 SHA-256；源文件
  缺失、替换或损坏时 fail-closed，不能形成断链的 submitted 审计事实。

权限：

- 查看、模板和错误清单：`warehouse.stocktake.view`；
- 上传、修正、重新匹配、dry-run、提交冻结：
  `warehouse.stocktake.submit`；
- 所有 B1 端点拒绝客户范围受限账号。

## 4. 匹配、漂移和审计

成品必须精确匹配客户、产品、存货编码、库位和归属；半成品还必须精确核对
材质、供应商、层数、楞型、长宽、片料类型、组件、换算参数及压线事实。
上传同时给出客户/产品/材质 ID 与编码或名称时，两种身份必须逐项一致；冲突
保留原始行并标记阻断，不能用 ID 静默覆盖错误文本。
客户编码和客户名称均可在草稿解释中分别修正；重新匹配使用修正后的快照，
而 `original_values_json` 中的原始上传编码保持不可变，便于复核差异来源。

已有现场事实只做路由，不重复建账：

- 已有正式成品批次：`route_n035`；
- 已有正式半成品批次：`route_semi_adjust`；
- 已匹配但未形成正式批次的成品快照：`route_snapshot_conversion`；
- 半成品快照没有现成正式转换流程，B1 明确阻断，不得误走成品转换。

成品快照只有在客户、产品、存货编码、数量和单位均与盘点行完全一致时才允许
进入转换路由；数量或单位不一致时使用专用错误阻断，不能由 B2 猜测采用哪一方。

dry-run 证据同时锁定：

- 客户、产品、材质版本；
- 库位状态；
- 原本不存在的栈板仍不存在；
- 原本空闲的库位仍空闲；
- 已有栈板版本和全部明细；
- 已有正式批次状态、库位、单位、余额和成品/半成品详情。

明细全部后续入账和审计字段进入指纹。修正、重新匹配、dry-run 和提交均写
`operation_logs`；排除行必须填写备注原因。

## 5. 页面

仓库页面新增高密度“库存建账草稿”页签，支持：

- CSV/XLSX 模板下载和私有上传；
- 批次、明细、错误和警告查看；
- 盘点日期、盘点人、客户编码/名称和成品身份字段修正；
- 半成品材质、供应商、层数、楞型、长宽、片料/组件、换算、压线和开料修正；
- 重新匹配、dry-run、错误 CSV、显式确认后冻结。

成品行不显示无关半成品字段；编辑区保持四列高密度布局，无嵌套或横向滚动。
页面明确说明 B1 提交只冻结草稿，不生成正式库存。

## 6. 隔离工厂副本演练

只读前置副本：

`D:\tm-uat\n081_b0_inventory_semantics_20260723\carton_erp_ch64_to_cj66_rehearsal.sqlite3`

- revision：`cj66v8x9z55`
- SHA-256：
  `A60437AA761300CB9DF006E27B66B5790C26039AFC7F2BA49EF0435DDF61B6D4`

迁移再次复制件：

`D:\tm-uat\n081_b1_onboarding_dryrun_20260723\carton_erp_cj66_to_ck67_rehearsal_v2.sqlite3`

- `cj66 → ck67` 成功；
- SHA-256：
  `E6588578879DD8A0192BDFCF6733F36AD53815C8A907044722C9205536CCF59F`
- `integrity_check=ok`；
- `foreign_key_check=0`；
- 新表均为 0 行；
- `customer_code_snapshot` 修正快照字段存在；
- 正式客户、产品、材质、订单、库存、栈板和预占计数与迁移前一致；
- 7 个 B1 保护触发器存在。

流程再次复制件：

`D:\tm-uat\n081_b1_onboarding_dryrun_20260723\carton_erp_ck67_b1_flow_v2.sqlite3`

- 使用工厂副本中的真实客户、产品和材质；
- 仅在流程副本内新增成品、半成品各一个 `UAT_ONLY` 测试库位；
- 同一私有 CSV 的一行成品和一行半成品完成草稿 → 修正 → dry-run →
  submitted；
- 成品行最初因错误客户编码被 `CUSTOMER_IDENTITY_CONFLICT` 阻断；草稿把编码
  修正为 `TH` 后重新匹配成功，原始上传值仍保持 `WRONG-CODE`；
- `error_count=0`，私有源文件存在，复核字节数与 SHA-256 均与批次一致；
- 正式库存五张核心表前后计数完全一致：
  `110 / 157 / 115 / 132 / 17`；
- submitted 明细的非法 SQL 改写被触发器拒绝；
- `integrity_check=ok`、外键异常 0；
- 流程副本最终 SHA-256：
  `F6566114A1762F168363FE656BBAB7AE3A21C3314C4574E0939CC112429E73B0`。

## 7. 自动验证

- B1 模型、迁移、上传安全、服务、API、前端：`70 passed`；
- B0、P0 数量迁移、N035、库存基础、库存洞察和三楼 API 相邻回归：
  `130 passed`；
- 旧迁移测试已改为验证“唯一 head + 本 revision 位于线性祖先链”，后续新增
  线性迁移不再制造旧阶段 revision 不是当前 head 的伪失败；
- Python 编译、内联 JavaScript 语法、Alembic 唯一 head 和
  `git diff --check` 作为提交前门禁继续执行。

## 8. 下一阶段

N081-B2 必须另建独立迁移、应用台账、权限和运行环境门禁，只在隔离副本完成
3～5 个成品栈板和 2～3 个半成品栈板的小范围试盘、幂等和受控回滚。
B0/B1/B2 全部自动验证完成后，才进行统一人工 UAT。
