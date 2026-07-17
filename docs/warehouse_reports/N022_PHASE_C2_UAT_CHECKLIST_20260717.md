# N022 Phase C.2 模具码 + 位置码双码移动确认 UAT 清单

状态：代码、自动化测试和隔离迁移副本演练已完成；现场人工 UAT 待执行。

## 1. 安全前提

- [ ] 本次只使用已确认隔离的 UAT 数据库副本，绝不连接或写入正式数据库。
- [ ] UAT 副本升级前已创建可恢复备份，并核对文件大小、SHA-256 和 `integrity_check`。
- [ ] UAT 服务的 `ERP_DATABASE_PATH` 明确指向该副本，端口不与正式服务冲突。
- [ ] 准备两个测试账号：一个具有 `warehouse.view`，另一个具有 `warehouse.view + warehouse.execute`。
- [ ] 已记录移动前基线：模具、成品/半成品库存、库存流水、订单/明细、报料/明细和 `warehouse_locations` 的行数，以及库存和订单关键数量。

`3F-M` 是独立模具位置体系。禁止创建、修改或映射任何三楼成品 `warehouse_locations` 作为模具位置。

## 2. 迁移与结构检查

- [x] 新 revision 为 `bb55v8x9z45`，`down_revision=ba54v8x9z44`，迁移链保持单一 head。
- [x] `mold_tools` 新增非空 `location_version`（默认 1）、`last_location_confirmed_at`、`last_location_confirmed_by`。
- [x] 新增 `mold_location_movements`，包含模具、起点、终点、操作人、时间、唯一幂等键、期望/结果版本、来源和备注。
- [x] 移动流水的 UPDATE/DELETE 由数据库触发器拒绝；唯一键、检查约束和外键均已验证。
- [x] 隔离副本已完成 `ba54 -> bb55 -> ba54 -> bb55` 往返演练。
- [x] upgrade、downgrade、re-upgrade 三阶段均为 `integrity_check=ok`、`foreign_key_check=0`。
- [x] 两条非空模具基线在往返后保留，升级后 `location_version=1`。
- [x] 一旦产生移动流水或位置确认版本，downgrade 会 fail-closed，要求停止服务并恢复升级前完整备份。

演练副本：`C:\tmp\n022_c2_rehearsal_20260717_125745\carton_erp_uat_copy.sqlite3`

升级前备份：`C:\tmp\n022_c2_rehearsal_20260717_125745\carton_erp_uat_copy_BEFORE_BB55.sqlite3`

备份 SHA-256：`97F792619E4E3D4B76885F59A843DDAB1C4A3677DBC05BDA164EF2E3EE149B5D`（与升级前副本一致）。

## 3. 原查询和二维码标签回归

- [ ] 打开 `/mobile/mold-lookup?mold=<测试模具码>`，原模具查询正常。
- [ ] 模具名称、模具编号、当前 `rack_location`、关联客户/产品和位置路线提示正常。
- [ ] “查看/打印模具标签”可打开，二维码和原查询链接可用。
- [ ] 移动成功后重新查询并打开标签，两处均显示新位置。

## 4. 双码预览与确认

建议准备一套平放测试模具和一套竖放测试模具；每个目标位置必须由现场确认真实存在且为空。

- [ ] 手动输入或粘贴模具码与位置码，点击“预览移动”。
- [ ] 预览明确显示模具码/名称、当前位置、规范化目标位置和 `expected_version`。
- [ ] 平放位置码（例如 `3F-M-R02-L2-D03-P08`）可预览并确认。
- [ ] 竖放位置码（例如 `3F-M-R01-L1-V-P12`）可预览并确认。
- [ ] URL 参数 `?mold=<模具码>&location=<位置码>` 可自动填充并预览。
- [ ] 具有 `warehouse.execute` 的账号可确认移动。
- [ ] 只有 `warehouse.view` 的账号可查询和预览，但确认按钮不可用，直接调用确认接口返回 403。
- [ ] 成功确认后位置版本加 1，记录最后确认时间/人员，新增一条移动流水和一条 `OperationLog`。

## 5. CAS、幂等和拒绝场景

- [ ] 旧自由文本（例如“二楼模具架 B-12”）作为目标时返回 422。
- [ ] 非法 `3F-M`（错误层号、缺段、0 号位置或非三楼）返回 422。
- [ ] 目标被另一启用模具占用时，预览显示冲突，确认返回 409。
- [ ] 使用过期 `expected_version` 确认时返回 409，并要求重新预览。
- [ ] 仅含空白的幂等键在去除首尾空白后不足 8 个字符，接口返回 422；service 直接调用也执行相同校验。
- [ ] 相同幂等键重放完全相同的请求时返回同一移动流水，不重复增加流水或 `OperationLog`。
- [ ] 相同幂等键改用其他模具、目标、版本、来源或备注时返回 409。
- [ ] 当前与目标位置相同时返回“无需移动”，不新增移动流水，也不增加位置版本。该请求没有状态变化，不生成业务事实，因而不占用幂等键；同一 key 可用于后续首个真实移动。
- [ ] 管理员不能通过旧模具档案编辑接口绕过流水直接修改位置，必须使用双码确认。

## 6. 业务隔离复核

- [ ] 移动前后成品/半成品库存批次数和各数量合计不变。
- [ ] `inventory_movements` 与 `inventory_location_movements` 行数不变。
- [ ] 订单、订单明细及订单数量/金额不变。
- [ ] 报料单、报料明细及报料数量不变。
- [ ] `warehouse_locations` 行数和三楼位置内容不变。
- [ ] 仅目标 `mold_tools` 记录、`mold_location_movements` 和对应 `operation_logs` 发生预期变化。

## 7. 自动验证记录

- [x] 独立审计修正后的 N022 C.2 专项、迁移和原模具工作流定向运行：`19 passed`。
- [x] 扩展 N022、权限、仓库、订单和报料相邻回归：`156 passed, 2 failed`。
- [x] 两项失败均来自本轮未修改的旧前端断言：一项仍要求旧菜单连续字符串，另一项禁止基线中已经存在的 `/static/assets/time-utils.js`；未扩大范围修改这些文件或测试。
- [x] 移动页内联 JavaScript 的 `node --check` 在扩展回归中通过。

## 8. 人工验收结论

- [ ] 平放移动通过。
- [ ] 竖放移动通过。
- [ ] 权限、占用、CAS、幂等和同位置场景通过。
- [ ] 原查询、二维码标签和模具工作流回归通过。
- [ ] 库存、订单、报料和 `warehouse_locations` 隔离复核通过。
- [ ] 现场负责人确认可进入后续集成。

人工确认不包含正式库迁移、commit 或 push。若 UAT 已产生移动事实，不执行破坏性 downgrade；需要回退时停止 UAT 服务并恢复 `bb55` 升级前完整副本备份。
