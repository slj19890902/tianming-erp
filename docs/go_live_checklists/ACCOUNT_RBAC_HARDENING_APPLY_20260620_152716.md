# 账号、密码与 RBAC 权限收口执行报告

更新时间：2026-06-20 15:27:16

## 一、执行范围

- 本轮是否执行历史迁移：否
- 本轮是否修改历史订单数据：否
- 本轮是否删除数据库：否
- 本轮是否修改账号与权限：是
- 本轮是否写入明文正式密码到代码 / 文档 / 报告：否

## 二、主库与备份

- 主库：`D:\纸箱厂erp软件搭建\data\carton_erp.sqlite3`
- 收口前显式备份：`D:\纸箱厂erp软件搭建\data\backups\carton_erp_before_account_rbac_hardening_20260620_150305.sqlite3`
- 主库完整性：`ok`
- 备份完整性：`ok`
- 说明：备份采用 SQLite 安全备份方式，文件级 SHA-256 可不同，但来源库与备份库都可正常打开并通过完整性校验。

## 三、账号收口结果

当前正式账号：

| 用户名 | 角色 | 启用 | 密码哈希 | must_change_password |
|---|---|---:|---|---:|
| admin | admin | 1 | modern-hash | 1 |
| finance | finance | 1 | modern-hash | 1 |
| sales | sales | 1 | modern-hash | 1 |
| workshop | workshop | 1 | modern-hash | 1 |

结论：

1. `finance`、`sales` 已补建。
2. `workshop` 旧式弱口令哈希风险已清除。
3. `admin` 已完成密码重置并改为现代哈希。
4. 四个账号均要求首次交接后改密。
5. 本轮未在任何代码、文档、报告中保存明文密码。

## 四、RBAC 与菜单收口

### 后端权限

- `customers`：销售可读，写入仅管理员。
- `products` / `materials`：财务无读取权限；写入仅管理员。
- `deliveries`：财务只读，不可执行发货相关操作。
- `requisition`：财务不可读。
- `incoming`：仅管理员 / 车间可读。

### 前端菜单

- admin：仪表盘、客户、产品、订单、报料、送货、财务、系统
- finance：仪表盘、客户、订单、财务
- sales：仪表盘、客户、订单、报料、送货
- workshop：仪表盘、订单、送货

## 五、四角色登录验收

结果：

- admin 登录 / 读取 `/api/auth/me` / 退出：通过
- finance 登录 / 读取 `/api/auth/me` / 退出：通过
- sales 登录 / 读取 `/api/auth/me` / 退出：通过
- workshop 登录 / 读取 `/api/auth/me` / 退出：通过

附加门禁：

- 错误密码：`401`
- 未登录访问：`401`
- 伪造 Cookie：`401`
- 越权访问：`403`

角色矩阵摘要：

| 角色 | 系统备份 | 订单 | 财务 | 来料 | 产品 |
|---|---:|---:|---:|---:|---:|
| admin | 200 | 200 | 200 | 200 | 200 |
| finance | 403 | 200 | 200 | 403 | 403 |
| sales | 403 | 200 | 403 | 403 | 200 |
| workshop | 403 | 200 | 403 | 200 | 403 |

## 六、手机来料页结论

- `/incoming.html` 页面壳可直接打开。
- `/api/incoming/pending` 未登录返回 `401`。
- 当前结论：页面壳公开，但数据接口仍受登录保护。

## 七、测试结果

执行命令：

```powershell
D:\纸箱厂erp软件搭建\.venv\Scripts\python.exe -m pytest -q tests/test_phase2_auth.py tests/test_phase3_api.py tests/test_phase5_orders.py tests/test_phase6_incoming.py tests/test_phase7_deliveries.py tests/test_phase8_finance.py tests/test_phase9_system.py tests/test_phase10_frontend.py
```

结果：`80 passed in 94.37s`

## 八、修改文件

代码：

- `app/api/customers.py`
- `app/api/products.py`
- `app/api/materials.py`
- `app/api/deliveries.py`
- `app/api/requisition.py`
- `app/api/incoming.py`
- `static/index.html`
- `scripts/admin/manage_users.py`

测试：

- `tests/test_phase3_api.py`
- `tests/test_phase6_incoming.py`
- `tests/test_phase7_deliveries.py`
- `tests/test_phase10_frontend.py`

## 九、剩余人工确认项

1. 需要由现场管理员使用本地脚本为 `admin / finance / sales / workshop` 设置“实际人员知道”的交接密码。
2. 需要现场确认财务菜单范围是否还要继续收紧。
3. 需要现场确认车间账号是否允许继续保留订单列表只读访问。
4. 需要确认手机来料页是否接受“页面壳公开、数据接口受保护”的现状。

## 十、当前结论

- 技术收口已完成。
- 不建议直接把当前一次性临时密码交给实际人员试用，因为明文未保存在文档中，也不应保存在文档中。
- 下一步应由管理员本地安全重置四个账号的正式交接密码，然后再进入实际人员人工试用。
