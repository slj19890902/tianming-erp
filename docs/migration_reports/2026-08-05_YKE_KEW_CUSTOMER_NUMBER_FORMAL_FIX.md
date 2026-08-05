# YKE / KEW 客户编号正式修复报告

日期：2026-08-05

## 结论

正式导入新增的 YKE、KEW 客户主档缺少 `customer_number`，导致客户列表接口在把数据库记录转换为 `CustomerResponse` 时触发 Pydantic 校验错误并返回 HTTP 500。本次按老板明确批准执行受控修复：YKE 固定为 135，KEW 固定为 136，并为导入脚本增加冻结编号门禁。

## 正式数据修复

- YKE / 研光：ID 137，`customer_number: NULL -> 135`，版本 `1 -> 2`。
- KEW / 光洋：ID 138，`customer_number: NULL -> 136`，版本 `1 -> 2`。
- 主数据对象版本：`564 -> 566`，新增 2 条。
- 操作日志：`3730 -> 3732`，新增 2 条。
- 审计来源：`controlled_repair.yl_yke_kew_customer_numbers_20260805`。
- 未修改客户名称、客户代码、常用箱、材质、订单、库存或报价数据。

## 备份与完整性

- 修复前备份：`D:\纸箱厂erp软件搭建\data\backups\carton_erp_before_YKE_KEW_customer_number_fix_20260805_180006.sqlite3`。
- 修复前正式库与备份 SHA-256：`EEF10A328461E4753ADE3CCF6CB7D9D55F09676CAB64A7CEAEB26D5C73DA7984`。
- 修复后正式库 SHA-256：`EF1BED45AAC488A8187D6E5AD415C4F3655EE186C52B6F85EBF9AD408821A74E`。
- Alembic revision：`dk93v8x9z82`。
- `PRAGMA integrity_check=ok`，外键异常 0，客户编号为空记录 0。

## 代码保护

- 导入脚本冻结客户编号：YKE=135、KEW=136。
- 清单编号为空时使用冻结编号；清单或现有主档编号冲突时立即中止，不继续写入。
- 新增 3 个安全测试，覆盖空编号补齐、准确编号接受、冲突编号拒绝。
- 相关提交：`9142d186ecdb61ba7b2bd107b985f633080b5f5f`。

## 验证

- 相关回归：48 passed。
- Python 编译和 `git diff --check` 通过。
- 正式库 134 条客户记录全部可通过 `CustomerResponse` 序列化。
- 客户列表查询：active total=96，首屏 25 条，可正常生成响应。
- 修复后导入脚本对正式库返回 `already_applied`，客户新增 0、常用箱新增 0；YKE 100、KEW 31，启用 117、停用 14。
- 本机与 `192.168.3.80` 的 `/api/health` 均返回 HTTP 200。

## 交付边界

- 已本地提交并快进合入 `factory-current-baseline`。
- 未 push，未修改 `main`。
- 正式运行版本仍为 `v0.22.51`；本次提交仅修改离线受控导入脚本和测试，不需要再次重启运行服务。
- 客户页面最终人工验收仍待老板刷新页面确认。
