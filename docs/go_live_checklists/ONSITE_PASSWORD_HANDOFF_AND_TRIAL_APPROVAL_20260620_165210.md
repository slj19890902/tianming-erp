# 现场正式密码交接与人工试用审批报告

更新时间：2026-06-20 16:52:10

## 一、本轮结论

- 本轮是否执行历史迁移：否
- 本轮是否修改历史订单：否
- 本轮是否修改账号密码：否
- 原因：当前会话无法代替现场管理员完成“本机隐藏输入正式密码”，且不得把密码放入命令参数、文件或日志。

因此，本轮严格停在“备份 + 环境核对 + 测试 + 现场手工执行指令准备”，未擅自改密。

## 二、目录与数据库

- 项目目录：`D:\纸箱厂erp软件搭建`
- 主库：`D:\纸箱厂erp软件搭建\data\carton_erp.sqlite3`

## 三、正式密码设置前备份

- 备份路径：`D:\纸箱厂erp软件搭建\data\backups\carton_erp_before_onsite_password_handoff_20260620_165210.sqlite3`
- 主库 SHA-256：`5EC54FEC02ED050C319695B739C3668BBA2F835B381DA1DB16522F06B08EE258`
- 备份 SHA-256：`D6D510CE5FBFD763C156E766CAA234A07D7296676FD0ACFF7DA571A2889BE4BD`
- 主库与备份 SHA-256 是否一致：否
- 说明：SQLite 安全备份的文件物理布局可能不同，但大小、完整性与业务数量一致。
- `PRAGMA integrity_check`：`ok`
- `PRAGMA foreign_key_check`：`0`

## 四、历史订单与台账基线

- `sales_orders = 15593`
- `sales_order_items = 15658`
- `RUIDA- = 15589`
- `migration_ruida_sales_order_map = 15589`
- `migration_ruida_sales_item_map = 15651`

本轮前后未发生变化。

## 五、当前账号列表

| 用户名 | 角色 |
|---|---|
| admin | admin |
| finance | finance |
| sales | sales |
| workshop | workshop |

说明：本轮未改密，账号列表不变。

## 六、密码安全约束执行情况

- 未在聊天中输出密码
- 未在命令行参数中传密码
- 未在 `.md` / `.txt` / `.csv` / `.env` 中记录密码
- 未在报告或日志中记录密码

## 七、当前无法由 Codex 代替完成的步骤

需要由现场管理员本人在本机终端隐藏输入四个正式交接密码。

当前阻塞不是技术问题，而是权限与安全边界：

1. 正式交接密码必须由现场管理员掌握
2. 当前会话不能安全读取或保存该密码
3. 继续由 Codex 代输会违反“不得写入参数 / 文件 / 日志”的限制

## 八、现场管理员手动执行命令

请在本机 PowerShell 手动执行：

```powershell
cd D:\纸箱厂erp软件搭建
.\.venv\Scripts\python.exe .\scripts\admin\final_password_handoff.py `
  --sqlite-path .\data\carton_erp.sqlite3 `
  --api-base-url http://127.0.0.1:8000 `
  --output-json .\docs\go_live_checklists\FINAL_PASSWORD_HANDOFF_RESULT_LOCAL.json
```

该脚本行为：

1. 交互式隐藏输入 `admin / finance / sales / workshop` 四个账号密码
2. 不把密码写入参数或输出文件
3. 改密后自动验证：
   - 新密码可登录
   - 错误密码不可登录
   - 旧弱口令不可登录
   - 登出后 `/api/auth/me` 返回 `401`
   - 未登录 / 伪造 Cookie / 越权访问门禁正常
   - 手机来料数据 API 仍受保护

## 九、四角色权限当前基线

当前仍沿用上一轮收口结论：

- admin：全核心功能
- finance：首页、订单查询、客户查询、对账、开票、收款；不可访问系统管理、车间、产品高危维护
- sales：首页、客户查询、订单查询、报料、送货；不可访问收款、开票确认、用户管理
- workshop：车间相关页面、允许的送货/来料范围；不可访问财务、收款、开票、用户管理

手机来料结论：

- 页面壳可打开
- 数据 API 仍受登录保护

## 十、测试结果

执行测试：

```powershell
D:\纸箱厂erp软件搭建\.venv\Scripts\python.exe -m pytest -q tests\test_phase2_auth.py tests\test_phase3_api.py tests\test_phase5_orders.py tests\test_phase6_incoming.py tests\test_phase7_deliveries.py tests\test_phase8_finance.py tests\test_phase9_system.py tests\test_phase10_frontend.py tests\scripts\test_final_password_handoff.py
```

结果：`83 passed`

## 十一、是否可以进入实际人员人工试用

当前结论：还不可以直接开始。

只差最后一步：

- 现场管理员在本机完成一次隐藏输入正式改密，并保存本地 JSON 结果用于复核。

完成后即可生成最终批准报告，并进入实际人员人工试用。

## 十二、仍需人工确认事项

1. 现场管理员是否已完成四账号正式密码输入
2. 财务菜单范围是否最终确认
3. 销售菜单范围是否最终确认
4. 车间菜单范围是否最终确认
5. 是否接受来料页 HTML 壳可打开但数据 API 受保护

## 十三、下一步建议

1. 由现场管理员手动执行上面的交互式命令
2. 把生成的 `FINAL_PASSWORD_HANDOFF_RESULT_LOCAL.json` 保留在本机，不要提交到仓库
3. 完成后再由我读取结果并生成“可进入人工试用”的最终批准报告
