# 后端数据库路径统一实施报告

执行时间：2026-06-19 20:09:19（Asia/Shanghai）

## 执行结论

- 本轮执行历史迁移：否。
- 本轮执行主库 apply：否。
- 本轮删除数据库：否。
- 本轮写入历史订单：否。
- 正式数据库：`D:\纸箱厂erp软件搭建\data\carton_erp.sqlite3`
- 当前后端入口：`app.main:app`
- `tm_phase3_dev.sqlite3`：保留为废弃测试参考库，其中 6 张测试订单不予同步。

## 修改范围

- `app/core/config.py`
  - 从项目根目录主动加载 `.env`，不依赖启动工作目录。
  - `ERP_DATABASE_PATH` 优先；未配置时固定回退到项目根目录 `data/carton_erp.sqlite3`。
  - 相对路径统一按项目根目录解析为绝对路径。
- `app/main.py`
  - 启动日志显示当前数据库绝对路径。
  - `/api/health` 只读报告数据库路径、正式订单表名及订单/明细数量。
- `phase1_postgres/database.py`
  - 移除 `tm_phase3_dev.sqlite3` 默认值，兼容入口也统一读取正式配置。
- `start_erp.bat`
  - 后端入口由旧的 `phase1_postgres.main:app` 改为完整后端 `app.main:app`。
  - 未配置路径时回退到 `%ROOT%data\carton_erp.sqlite3`，启动日志记录实际路径。
- 测试：
  - 新增 `tests/test_database_path_unification.py`。
  - 更新 `tests/test_phase13_deployment.py` 的环境变量启动断言。

修改前文件备份：
`docs/migration_reports/config_backups/DATABASE_PATH_UNIFICATION_20260619_200355/`

## 修复前后路径来源

修复前运行进程：

- 命令：`uvicorn phase1_postgres.main:app --port 8002`
- 路径来源：`phase1_postgres/database.py` 相对默认值
- 实际数据库：`data/tm_phase3_dev.sqlite3`
- API 订单：`orders=6`

修复后运行进程：

- 命令：`uvicorn app.main:app --port 8000`
- 路径来源：项目根目录 `.env` 的 `ERP_DATABASE_PATH`
- 实际数据库：`D:\纸箱厂erp软件搭建\data\carton_erp.sqlite3`
- API 正式订单表：`sales_orders`
- 健康接口订单/明细：`4 / 7`

## 正式库只读核验

| 项目 | 结果 |
|---|---:|
| `PRAGMA integrity_check` | `ok` |
| `sales_orders` | 4 |
| `sales_order_items` | 7 |
| `legacy_ruida_orders` | 39,922 |
| `legacy_ruida_order_items` | 40,449 |
| `customers` | 132 |
| `products` | 3,316 |
| `users` | 2 |
| 活跃用户 | 2 |

主库执行前后均为：

- SHA-256：`6ED950245BC91E18A8D4C8F55CB55B6630396B16117DF4145922C725E28E7F77`
- 大小：`209182720` 字节
- 修改时间 UTC：`2026-06-19T01:07:42.3186118Z`

因此主库没有被修改。

## API、前端与 RBAC 验证

- `GET /api/health`：200，报告正式库绝对路径及 `sales_orders=4`、`sales_order_items=7`。
- `GET /`：200，完整前端由同一 8000 端口提供。
- 未登录访问 `/api/orders`：401，RBAC 生效。
- 未登录访问 `/api/auth/me`：401，认证门禁生效。
- 正式库现有用户：`admin/admin`、`workshop/workshop`，均为启用状态。
- 本轮未使用账号密码登录，未产生登录日志写入。

## 测试结果

- 数据库路径统一、配置、认证、正式订单、仪表盘、系统和部署相关测试：`49 passed`。
- Python 编译检查通过。
- 覆盖了环境变量优先、默认正式库、禁止默认测试库、绝对路径、健康接口和启动入口。

## 表模型结论

- 当前实际启动的完整后端 `app.main:app` 使用 `sales_orders / sales_order_items`，与历史迁移目标一致。
- 旧的 `phase1_postgres` 代码仍使用 `orders / order_items`，但已不再由正式启动脚本加载；其默认数据库路径也已统一，防止再次错连测试库。
- `carton_erp.sqlite3` 中仍保留旧 `orders` 表（1 条）且没有 `order_items`。该遗留表不作为本次历史迁移目标，不应由旧入口启动时自动补表。

## 下一步建议

先完成一次“正式迁移前只读 dry-run + 停机/备份复核”，确认运行进程仍为 `app.main:app`、健康接口仍报告正式库及 `sales_orders=4`，再单独申请首批 100 张订单主库迁移授权。
