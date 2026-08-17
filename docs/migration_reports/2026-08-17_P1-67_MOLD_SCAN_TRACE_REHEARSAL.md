# P1-67 模具扫码生产追溯迁移演练

## 范围

- 新迁移：`pp24v8x9z13_mold_scan_task_trace.py`
- 上一版本：`oo23v8x9z12`
- 新表：`mold_scan_events`
- 演练日期：2026-08-17
- 正式数据库：只读复制来源，未执行迁移、回写或结构变更。

## 权威隔离副本

- 升降级副本：`D:\tm-uat\p1_67_mold_scan_20260817\carton_erp_p1_67_rehearsal_v2.sqlite3`
- 最终只读复核副本：`D:\tm-uat\p1_67_mold_scan_20260817\carton_erp_p1_67_postcheck_v2.sqlite3`
- 升降级副本最终 SHA-256：`574A6A65C796FA50C4DFDDC42967D9F480130F772F17ECAB100D556CC1AFE6EE`

## 演练步骤与结果

1. 使用 SQLite 在线备份 API 从当前正式库创建一致性副本。
2. 副本起始 Alembic 版本为 `oo23v8x9z12`，`PRAGMA integrity_check=ok`，`PRAGMA foreign_key_check=0`。
3. 执行 `upgrade head` 到 `pp24v8x9z13`，成功。
4. 执行 `downgrade oo23v8x9z12`，空扫码历史表下成功。
5. 再次执行 `upgrade head` 到 `pp24v8x9z13`，成功且保持唯一 head。
6. 对最终复核副本再次检查：`integrity=ok`、`foreign_key_errors=0`。

## 数据保护

- 降级脚本在 `mold_scan_events` 已有任何记录时主动阻断，避免删除正式扫码生产历史。
- 本次没有向正式库写入扫码事件，也没有迁移正式库。
- 正式迁移仍须按发布流程重新备份、验证并取得老板明确授权。
