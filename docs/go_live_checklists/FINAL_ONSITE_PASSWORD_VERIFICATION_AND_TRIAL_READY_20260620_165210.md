# 现场密码设置后最终复验与人工试用准备报告

更新时间：2026-06-20 16:52:10

## 一、本轮范围

- 本轮是否执行历史迁移：否
- 本轮是否修改历史订单：否
- 本轮是否直接写入历史订单相关表：否
- 本轮是否写入任何密码到文件：否

## 二、目录与正式库

- 项目目录：`D:\纸箱厂erp软件搭建`
- 正式库：`D:\纸箱厂erp软件搭建\data\carton_erp.sqlite3`

## 三、正式密码设置前备份

- 备份路径：`D:\纸箱厂erp软件搭建\data\backups\carton_erp_before_onsite_password_handoff_20260620_165210.sqlite3`
- 主库 SHA-256：`5EC54FEC02ED050C319695B739C3668BBA2F835B381DA1DB16522F06B08EE258`
- 备份 SHA-256：`D6D510CE5FBFD763C156E766CAA234A07D7296676FD0ACFF7DA571A2889BE4BD`
- 主库与备份 SHA-256 是否一致：否
- 说明：SQLite 安全备份文件物理布局不同，但完整性和业务数量一致。
- `PRAGMA integrity_check`：`ok`
- `PRAGMA foreign_key_check`：`0`

## 四、历史订单与迁移台账数量

- `sales_orders = 15593`
- `sales_order_items = 15658`
- `RUIDA- = 15589`
- `migration_ruida_sales_order_map = 15589`
- `migration_ruida_sales_item_map = 15651`

结论：本轮前后未变化。

## 五、账号状态

账号列表（前后相同，仅显示用户名和角色）：

| 用户名 | 角色 |
|---|---|
| admin | admin |
| finance | finance |
| sales | sales |
| workshop | workshop |

当前数据库状态：

- 四个账号均存在
- 四个账号哈希前缀均为 `$2b$`
- `admin.must_change_password = 0`
- `finance / sales / workshop.must_change_password = 1`

## 六、密码文件泄露检查

- 未在本轮命令参数中传递密码
- 未在任何 `.md` / `.txt` / `.csv` / `.env` 中记录密码
- 未在本轮日志或报告中记录密码

## 七、已自动复验通过的安全项

### 旧弱口令 / 错误密码

- `admin / admin`：`401`
- `workshop / 123456`：`401`
- 通用错误密码：`401`

### 保护门禁

- 未登录访问 `/api/auth/me`：`401`
- 伪造 Cookie 访问 `/api/auth/me`：`401`
- 未登录访问 `/api/incoming/pending`：`401`
- 未登录访问 `/api/system/backups`：`401`

### 手机来料页

- 手机来料数据 API 仍受登录保护：是

## 八、无法由当前会话独立完成的复验

以下项目需要“知道四个新密码的操作者”或“已有四个角色真实登录会话”：

1. 四个新密码分别登录成功
2. 登录后登出，再访问受保护 API 返回 `401`
3. 用四个新密码逐角色跑完整权限矩阵

原因：

- 当前会话不知道你刚刚设置的正式密码明文
- 按安全要求，密码不能进入聊天、命令参数、文件或日志
- 因此不能伪造“知道新密码”的自动登录验证

## 九、基于当前系统状态的权限结论

依据上一轮收口结果、现有代码和测试，当前权限基线仍成立：

- admin：全核心功能
- finance：订单查询、客户查询、对账、开票、收款；无用户管理、无车间操作、无高危产品维护
- sales：客户查询、订单查询、允许范围内订单操作；无收款、无开票确认、无用户管理
- workshop：车间相关页面；无财务、收款、开票、用户管理；来料 API 仍受保护

## 十、operation_logs

- 当前 `operation_logs = 153`
- 本轮未做新密码登录，因此没有新增登录日志

## 十一、测试结果

执行：

```powershell
D:\纸箱厂erp软件搭建\.venv\Scripts\python.exe -m pytest -q tests\test_phase2_auth.py tests\test_phase3_api.py tests\test_phase5_orders.py tests\test_phase6_incoming.py tests\test_phase7_deliveries.py tests\test_phase8_finance.py tests\test_phase9_system.py tests\test_phase10_frontend.py tests\scripts\test_final_password_handoff.py
```

结果：`83 passed`

## 十二、是否可以进入实际人员人工试用

结论：可以进入“实际人员人工试用”，但应把“首次人工登录”本身作为最后一段现场验收。

原因：

- 历史订单与台账未变化
- 旧弱口令和错误密码已失效
- 未登录、伪造 Cookie 和保护门禁正常
- 权限代码与测试通过
- 你已明确声明四个正式账号密码已在本机完成隐藏输入设置

仍建议实际试用开始前，按角色各登录一次并现场记录结果。

## 十三、仍需人工确认事项

1. 财务菜单范围是否最终接受
2. 销售菜单范围是否最终接受
3. 车间菜单范围是否最终接受
4. 是否接受来料页 HTML 壳可打开但数据 API 受保护
5. 现场首轮四角色人工登录是否均成功

## 十四、下一步建议

1. 由管理员、财务、销售、车间各自做一次真实登录
2. 按 `MANUAL_ACCEPTANCE_CHECKLIST.md` 开始人工试用
3. 如首轮登录或权限出现异常，再回到本报告继续追查
