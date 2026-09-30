# 天明 ERP 代码评审报告

- **评审对象**：`slj19890902/tianming-erp`（2026-09-30 公开版本，clone 自 GitHub）
- **评审人**：Marlowe（代码评审 / 质检）
- **评审日期**：2026-10-01
- **评审方式**：静态代码审计（全量）＋ 运行时验证（本地起服务、枚举路由、鉴权探针、跑测试、比对 models/migrations）
- **报告语言**：简体中文。分级：P0（必须立即修，有安全/数据风险）、P1（重要，影响功能正确性）、P2（建议优化）

---

## 一、结论先行（给 Kimiyf 的三句话）

1. **当前生产入口（`start_erp.bat → uvicorn app.main:app`，端口 8002）存在可被局域网内任何人利用的未鉴权数据读写接口**：旧单体 `main.py` 的 16 个路由（含客户资料完整 CRUD）没有任何登录检查，且仍挂载在生产应用上。运行时已实测验证：新接口无 cookie 返回 401，旧接口无 cookie 直接穿透到数据库层（P0-1、P0-4）。
2. **Vue 新前端（`tm_frontend/`）与当前后端处于"断裂"状态，实际不可用**：前端调用的 `/api/master/*`、`/api/wms/*`、`/api/engine/*`、`/api/receipts`、`/api/statements/*`、`/api/requisitions/*` 等前缀在生产后端 `app/` 中**根本不存在**（这些前缀只活在已废弃的 `phase1_postgres/` 里）；且前端全站不发送 session cookie，即使路径对了也会 401。生产实际跑的仍是 `static/` 下的旧 HTML 页面（P0-2、P0-3、P1-6）。
3. **代码质量底子不差，但"新后端"是包装器而非重写**：`app/` 的鉴权、审计、备份、测试覆盖都做得认真；但 `app/main.py` 本质是把旧单体 `main.py`（2131 行）的 FastAPI 对象拿过来做"路由手术"，订单创建等核心写操作仍委托给旧的 raw-sqlite 代码。models 与 alembic 迁移存在漂移，8 个测试失败。建议按文末"两周路线图"先止血、再收敛。

**不建议在 P0 修复前把服务暴露给不可信网络。**

---

## 二、评审方法与覆盖范围

| 维度 | 做法 | 结果 |
|---|---|---|
| 后端架构 | 通读 `app/main.py`、`main.py`（2131 行）、全部 `app/api/*.py`、`app/core/*` | 确认"包装器"架构（P1-1） |
| 鉴权审计 | AST 扫描全部路由的 `Depends`；运行时 TestClient 探针 | 新路由全有 RoleChecker；旧路由零鉴权（P0-1） |
| 前后端契约 | 枚举运行时路由表 × grep 前端全部 API 调用 | 6 组前缀对不上（P0-2） |
| 数据层 | alembic 链校验、空库 `upgrade head`、反射比对 models | 链完整但存在漂移（P1-3） |
| 前端 | 通读 `tm_frontend/src`（10 个视图、路由、API 层） | 无登录页、无鉴权发送、假数据（P1-7） |
| 测试 | 独立 venv 跑 `pytest tests/`（排除需 SQL Server 的 migration） | 238 通过、8 失败（P1-11） |
| 安全配置 | CORS、cookie、密码策略、密钥管理、git 泄漏检查 | 见 P0/P2 |

**运行时验证环境**：Linux + Python 3.12 独立 venv（fastapi/sqlalchemy/alembic/bcrypt/PyJWT/pytest/httpx/openpyxl/reportlab/pandas/qrcode/pypdf），`ERP_ENVIRONMENT=development`，临时 SQLite 库。**未核实**：Windows 工厂电脑上的真实运行表现、NAS 备份路径、PostgreSQL 相关代码（`phase1_postgres/`、`deployment/nas` 未深入）。

---
## 三、P0 发现（立即修复）

### P0-1 旧单体路由零鉴权且仍挂载在生产应用上 —— 局域网内可免登录读写客户数据

**证据**：
- `main.py:1954-2113` 共 19 个 `@app` 路由（`POST /api/login`、`GET /api/archives/search`、`/api/customer-management*` 5 个、`GET /api/customers/{id}/styles`、`GET /api/legacy/summary`、`GET /api/meta`、`/`、`/customers`、`/incoming.html`、`/delivery-print.html` 等），函数签名中**无任何 `Depends` / cookie 检查**。
- `app/main.py:create_app()` 的"路由手术"只删除了三个精确路径（`/api/orders`、`/api/health`、`/api/customers`），其余旧路由原样保留。
- **运行时实测**（TestClient 打真实 `app.main:app`，不带 cookie）：
  - `GET /api/orders` → **401**（新路由鉴权正常）
  - `POST /api/customer-management` → **500**（穿透鉴权、死在数据库层——在有表的生产库上就是 201）
  - `GET /api/archives/search`、`GET /api/legacy/summary` → **500**（同理，无鉴权门）
  - 运行时完整路由表共 ~90 条，上述旧路由全部在列。

**影响**：同一局域网任何人可免登录增删改客户资料、查档案。客户数据是工厂核心资产，此为最高风险项。

**给 Claude/Codex 的修复任务**：
1. 在 `app/main.py` 的 `create_app()` 中，**删除所有来自 `legacy.app` 的旧 API 路由**（至少 `/api/login`、`/api/archives/search`、`/api/customer-management*`、`/api/customers/{id}/styles`、`/api/legacy/summary`、`/api/meta`），而不是只删三个。静态页面（`/`、`/customers`、`*.html`）如仍需使用，必须加鉴权中间件或迁移到新前端。
2. 更彻底的方案：`main.py` 的业务函数逐个迁移到 `app/services/` + `app/api/` 后，让 `app/main.py` 不再 `import main as legacy`。
3. 验收标准：无 cookie 时 `GET /api/customer-management`、`/api/archives/search`、`/api/legacy/summary` 全部返回 401；回归测试 `pytest tests/` 通过。

### P0-2 前后端 API 契约断裂 —— Vue 前端调用的接口在生产后端不存在

**证据**（前端 `tm_frontend/src/api/masterData.ts` 实际调用 vs 运行时路由表）：
| 前端调用的前缀 | 生产后端 `app/` 是否存在 |
|---|---|
| `GET /api/master/customers` 等（客户/物料/产品） | ✅ 存在（`app/api/customers.py` 等） |
| `/api/wms/*`（移动收料、扫码） | ❌ 不存在 |
| `/api/engine/calculate-carton`（算料） | ❌ 不存在（只有 `/api/pricing/calculate`） |
| `/api/receipts`、`/api/receipts/*/settle` | ❌ 不存在（只有 `/api/finance/return_receipts`） |
| `/api/statements/*` | ❌ 不存在（只有 `/api/finance/statements`） |
| `/api/requisitions/*`（复数） | ❌ 不存在（只有 `/api/requisition/*` 单数） |
| `/api/deliveries/pending-items`（连字符） | ❌ 不存在（只有 `/api/deliveries/pending_items` 下划线） |

这些前缀只存在于已废弃的 `phase1_postgres/*.py`（如 `api_master.py:16` 的 `prefix="/api/master"`）。**结论：Vue 前端是按 Phase 1（Postgres 版）API 写的，Phase 2+ 重写后端时改了前缀，前端没跟上。**

**影响**：`MasterDataView`、`RequisitionView`、`StatementView`、`DeliveryView`、`MobileReceiveView` 等核心业务视图调接口会 404，基本不可用。

**给 Claude/Codex 的修复任务**：
1. 二选一并**只选其一**执行到底：(a) 按当前后端路由表批量修正前端 `masterData.ts` 的 URL；(b) 在后端加兼容别名路由。推荐 (a)，因为后端前缀更贴近 REST 惯例。
2. 逐个视图做冒烟测试：每个列表页能加载、每个写操作能提交。
3. 验收标准：前端 10 个视图无 404 调用（用浏览器开发者工具 Network 面板验证）。

### P0-3 前端全站不发送 session cookie —— 新后端鉴权对前端形同虚设

**证据**：
- `grep -rn "credentials" tm_frontend/src` → **0 结果**。`masterData.ts` 的 `request()` 用原生 `fetch`，未设置 `credentials: 'include'`。
- 后端登录（`app/api/auth.py`）靠 HttpOnly cookie（`erp_session`）维持会话；前端不带 cookie → 所有受保护接口 401。
- 前端无登录页、无路由守卫（`router.ts` 无 `beforeEach`），`App.vue` 硬编码 `currentUser = ref('管理员')`。

**给 Claude/Codex 的修复任务**：
1. `masterData.ts` 的 `request()` 加 `credentials: 'include'`、加超时（AbortController，默认 30s）。
2. 新增登录页 + 路由守卫：未登录访问业务路由跳转 `/login`；401 响应统一跳转登录。
3. 验收标准：登录后刷新页面仍保持会话；退出后访问业务页被拦截。

### P0-4 旧 `POST /api/login` 是"假登录"且种子账号弱口令

**证据**：
- `main.py:1977-1991`：只校验口令、写日志，**不设置任何 session/token**，返回用户信息 JSON。前端若调它，"登录"只是自欺欺人。
- 旧单体密码为无盐 SHA256（`main.py:743-744`）；种子账号 `boss/123456`、`workshop/123456`（`main.py:749-750`）；README 公开这些账号。
- 新鉴权用 bcrypt（rounds=12，`app/core/security.py`）是对的，但旧登录路径绕过了它。

**给 Claude/Codex 的修复任务**：随 P0-1 删除旧登录路由；README 删除默认账号密码，改为"首次启动用 `init_db.py` 初始化，密码见 `ERP_INITIAL_PASSWORD` 环境变量"。

### P0-5 明文密码提交进 git 历史

**证据**：`scripts/admin/final_password_handoff.py:30-31,229-231` 含明文 `"123456"` / `"12345678"` 及旧密码映射。虽未发现 `.env` / `*.key` 泄漏（`.gitignore` 正确，`git ls-files` 已确认），但仓库是公开的。

**给 Claude/Codex 的修复任务**：
1. 立即修改所有在该脚本中出现过的账号密码（生产库）。
2. 用 `git filter-repo` 或 BFG 从历史中清除该文件，或至少确认当前 HEAD 无明文密码（当前 HEAD 有，但历史也有）。
3. 以后密码交接走 `python -c "from getpass import getpass"` 交互式输入，不落盘。

---
## 四、P1 发现（重要，排期修复）

### P1-1 "新后端"是旧单体的包装器，不是独立应用

**证据**：`app/main.py:11` 执行 `import main as legacy`，`:88` 令 `application = legacy.app`；`create_app()` 对旧路由做"手术"（按精确路径删除、按"路径不存在才挂载"条件式添加）；`:209` 猴子补丁 `legacy.db_path = lambda: current.database_path`；剥离重加 CORSMiddleware；`application.middleware_stack = None`。`main.py`（2131 行）并未废弃，是实际运行核心。

**风险**：路由手术是精确字符串匹配，旧单体加一条路由就可能悄悄绕过鉴权（P0-1 的根因）；两个 DB 路径（`legacy.db_path` vs `settings.database_path`）靠猴子补丁同步，极易漂移。

**修复任务**：制定 `main.py` 退役计划：业务函数按"客户→订单→报料→送货→财务"顺序迁入 `app/services/`，每迁完一个就删掉旧路由并补测试；目标：`app/main.py` 不再 import 旧 `main`。

### P1-2 订单创建仍走旧 raw-sqlite 路径

**证据**：`app/api/orders.py:126-136` 的 `_legacy_create()` 在函数内 `import main as legacy`，调用 `legacy.OrderCreateRequest` / `legacy.create_order_record()`。新订单接口只是把校验过的 Pydantic 模型**翻译回旧模型再调旧函数**，写库走 `sqlite3` 直连而非 SQLAlchemy。

**风险**：事务、审计日志、字段级权限（如 workshop 隐藏金额是在新 `_order_response` 做的）与旧写路径混用，一致性靠人工保证。

**修复任务**：用 SQLAlchemy 重写 `create_order_record` 等价逻辑（或确认旧函数行为后整体迁移），`_legacy_create` 改为直接调 service 层；补订单创建的集成测试（当前测试只覆盖隔离 router）。

### P1-3 models 与 alembic 迁移漂移 —— 迁移建库会得到残缺表结构

**证据**（空库 `alembic upgrade head` 后反射比对 `Base.metadata`）：
- `order_item_number_sequences` 表：模型有（`app/models/order.py:219`），迁移**没建**。
- `sales_order_items` 表：模型有 `item_order_number`、`item_sequence` 两列，迁移建出的表**没有**。
- 运行时确实用到：`app/services/order_numbering.py:59` 裸 `INSERT INTO order_item_number_sequences`；`app/api/orders.py:180` 读 `item_order_number`。

**风险**：任何用 `alembic upgrade head` 建的库（新开发机、CI、灾后重建）调订单相关接口会 `OperationalError`。生产暂时没事是因为 `init_db.py:75` 用的是 `Base.metadata.create_all`——**两种建库路径并存且结果不一致**。

**修复任务**：新增一个 alembic migration 补建 `order_item_number_sequences` 表和两列；并立规矩：以 alembic 为唯一建库路径，`init_db.py` 改为调 `alembic upgrade head` + 种子数据。

### P1-4 权限矩阵与测试规范冲突 —— sales 建不了物料、传不了图纸

**证据**：
- `app/api/materials.py:22`、`app/api/products.py:42`：`can_write = RoleChecker(["admin"])`。
- 但测试明确要求 sales 可写：`tests/test_phase12_uat.py:272`（sales 建物料期望 201）、`tests/test_phase14_product_drawings.py:107`（sales 传图纸期望 201），而 workshop/finance 期望 403。
- 实测：`pytest tests/` → **238 passed, 8 failed**，其中 3 个是此冲突（另 2 个图纸版本测试、3 个订单编号排练测试见 P1-11）。

**修复任务**：先由 Kimiyf **定夺**：(a) 测试为准 → `can_write` 改为 `["admin", "sales"]`（物料、产品、图纸）；(b) 代码为准 → 更新测试并书面确认"只有 admin 能维护基础资料"。定夺前不要两边各改一点。

### P1-5 源码乱码 bug：规格显示 `"脳"` 应为 `"×"`

**证据**：`app/api/orders.py:114`：`return "脳".join(...) + "mm"` —— U+8113"脑"字取代了 U+00D7 乘号。全仓唯一一处此类乱码。

**影响**：所有订单明细的 `snapshot_spec` 会显示成 `450脳340脳300mm`，用户可见。

**修复任务**：把 `"脳"` 改为 `"×"`；已产生的历史数据如需清洗，写一次性脚本（先备份）。

### P1-6 Vue 前端没有部署路径 —— 生产实际跑的是旧 HTML

**证据**：全仓库无任何 `vite build` / `npm run build` / `dist` 部署脚本；`scripts/deploy_production.py` 只管数据库 promotion；`static/` 下是旧版 HTML（`index.html`、`customers.html` 等），由旧路由 serve。`tm_frontend/dist` 构建产物与 `static/` 共存，部署关系未定义。

**修复任务**：确定前端部署方案（二选一）：(a) `vite build` 产物部署到 `static/` 并由 FastAPI 托管（需处理 SPA fallback 路由）；(b) 前后端分离部署。写进 README，并加一条构建脚本。

### P1-7 生产路由里有 Demo 页、假数据、无登录

**证据**：`router.ts` 有 `/print-demo`（智能派工单）、`/scan-demo`（扫码状态面板）两个 demo 路由且出现在菜单；`DashboardView.vue` 的 alerts/flowNodes/tableData 全硬编码；`App.vue` 硬编码"管理员"。

**修复任务**：demo 页移出生产菜单（或加 `import.meta.env.DEV` 守卫）；Dashboard 接 `/api/dashboard/kpi` 真数据；登录页见 P0-3。

### P1-8 SQLite `journal_mode = DELETE`（非 WAL），并发写易阻塞

**证据**：`app/core/database.py` 显式 `PRAGMA journal_mode = DELETE`，`busy_timeout` 仅 5s，`check_same_thread=False`，单 worker。局域网多用户同时下单/收料时，写锁等待易超时。

**修复任务**：切到 WAL（`PRAGMA journal_mode=WAL`），`busy_timeout` 提到 15-30s；备份脚本确认 WAL 下 `sqlite3 backup` API 仍可用（当前 `backup_to_nas` 用 backup API，WAL 下是安全的）。

### P1-9 `/api/health` 未鉴权且泄露数据库绝对路径

**证据**：`app/main.py:52-61` 的 `database_health()` 返回 `"database": str(database_path)`（如 `Z:\sata1-18015598002\...`），该路由无鉴权。

**修复任务**：health 接口只返回 `ok/version`，数据库路径只打日志不外泄；或给 health 加鉴权。

### P1-10 README 过时且公开默认账号

**证据**：README 写 `python main.py` 启动（实际是 `start_erp.bat` → `uvicorn app.main:app`）；公开 `boss/123456` 等默认账号。

**修复任务**：重写 README：正确启动方式、环境变量说明、删默认密码、加前端构建部署说明（随 P1-6）。

### P1-11 测试 8 失败（238 通过）

**证据**：独立 venv 全量 `pytest tests/ -q --ignore=tests/migration`（260s）：
- 3 个权限冲突（P1-4，sales 写物料/图纸）
- 3 个 `test_order_number_structure_rehearsal.py`（排练库表结构断言失败——与 P1-3 的漂移相关，待核实）
- 2 个 `test_database_path_unification.py`（`no such table: sales_orders`，测试环境表缺失，疑似测试隔离问题，待核实）

**修复任务**：P1-3、P1-4 修完后重跑；`tests/migration`（需 SQL Server）单独在能连的环境跑。另：测试只覆盖隔离 router，**没有针对真实 `create_app()` 的集成测试**——P0-1 这类"路由手术"问题零覆盖，建议补一个"生产路由表鉴权快照测试"（断言每条路由的鉴权状态）。

---
## 五、P2 发现（建议优化）

- **P2-1 CORS 范围过宽**：`allow_credentials=True` + `app/core/config.py` 的 `PRIVATE_LAN_ORIGIN_REGEX` 匹配任意局域网 IP。局域网场景下可以理解，但建议收敛为明确的白名单（`ERP_ALLOWED_ORIGINS` 已支持，生产环境填上具体机器 IP）。
- **P2-2 `session_cookie_secure=False` 默认**：纯 HTTP 局域网可用，但如果将来上 HTTPS/反向代理，记得打开。`samesite="lax"` 当前合理。
- **P2-3 `must_change_password` 从不强制**：`app/api/auth.py:178,215`、`init_db.py` 只设置该标志，登录流程从不检查。建议登录时若为 True 则 403 并提示改密（前端配合 P0-3 的登录页）。
- **P2-4 默认初始密码**：`init_db.py:76` 的 `ERP_INITIAL_PASSWORD` 未设置时为 `ChangeMe123!`，四个账号共用。建议首次启动强制交互式设置各账号密码。
- **P2-5 登录接口无限流**：`/api/auth/login` 无失败次数限制，局域网内可暴力猜解。建议加简单限流（如 5 分钟内失败 10 次锁定 IP 15 分钟）。
- **P2-6 f-string 拼接 SQL**：`main.py:642,649`（`PRAGMA table_info({table}）` / `ALTER TABLE ...`）。当前调用方全是内部硬编码常量，**无注入风险**，但建议加表名白名单校验，防以后有人传参进来。
- **P2-7 硬编码路径含疑似设备序列号**：`app/core/config.py` 的 `DEFAULT_BACKUP_DIR = Z:\sata1-18015598002\BoxERP\backups`。建议全量走环境变量/配置文件。
- **P2-8 `get_lan_ip()` 依赖外网**：`main.py:1906-1912` 用 `connect(("8.8.8.8", 80))` 探测本机 IP，无外网时回退 127.0.0.1。内网环境建议改用网卡枚举。
- **P2-9 旧 `init_database()` 在空库上直接失败**：`run_database_migrations` 期望 `legacy_ruida_order_items` 表已存在（`main.py:722`），全新部署会 `OperationalError`。随 P1-1 退役旧 main 时一并处理。
- **P2-10 `DEFAULT_ALLOWED_ORIGINS` 写 8000 端口**：实际服务跑 8002，靠 `allowed_origin_regex` 兜底。配置与实际不一致，建议改成 8002 或删掉硬编码。
- **P2-11 单 worker + 同步 sqlite3**：`start_erp.bat` 用 `--workers 1`。配合 P1-8 的 WAL 改造后，如仍有并发诉求再考虑多 worker（注意 SQLite 写串行化，多 worker 收益有限）。

---

## 六、做得好的地方（保持）

1. **新鉴权体系是认真的**：bcrypt(12)、HttpOnly+SameSite cookie、30 天 remember-me、密码强度策略（≥10 位+字母数字）、登录/改密/重置全写审计日志（`OperationLog`）。
2. **权限审计全覆盖**：AST 扫描确认 `app/api/*` 全部端点都有 RoleChecker，无一漏网；订单接口对 workshop 角色做字段级脱敏（隐藏金额）。
3. **备份/恢复设计可靠**：`backup_to_nas`/`restore_from_backup` 有 sha256 校验、integrity_check、恢复前自动备份当前库、路径穿越防护（`_safe_backup_path`）。
4. **密钥与 git 卫生**：session secret 自动生成到 `data/session_secret.key`；`.gitignore` 正确排除 `.env`/`*.key`/`data/`/`logs/`，`git ls-files` 确认无泄漏（近期还有专门清理 `.bak` 的安全提交）。
5. **alembic 单链完整**：13 个版本零分叉、零孤儿，`upgrade head` 可跑通。
6. **测试体量大且多为真测试**：~35 个文件、238 通过，覆盖鉴权、订单、报料、财务、图纸、备份等核心链路（失败的 8 个见 P1-4/P1-11）。
7. **交接文档齐全**：`docs/CODEX_HANDOFF.md`、migration runbook、go-live checklist，对"不碰历史数据"的门禁意识很好。

---

## 七、两周优化路线图（给 Claude/Codex 的排期建议）

**Week 1 —— 安全止血（目标：局域网内不再有未鉴权写入口）**
- Day 1-2：P0-1（删除/鉴权旧路由）＋ P0-4（删旧登录）＋ P0-5（换密码、清 git 历史）。验收：无 cookie 全 401。
- Day 3-4：P0-3（前端 credentials + 登录页 + 路由守卫）＋ P0-2（前端 URL 对齐后端）。验收：浏览器走一遍"登录→下单→报料→送货→对账"无 404/401。
- Day 5：补"生产路由鉴权快照测试"（P1-11），把 P0-1 的回归锁死在 CI 里；全量 pytest 回到全绿。

**Week 2 —— 架构收敛（目标：新后端名副其实，前端可部署）**
- Day 6-7：P1-3（补 migration，统一建库路径为 alembic）＋ P1-8（WAL）。
- Day 8-9：P1-4（Kimiyf 定夺权限矩阵后执行）＋ P1-5（乱码修复）＋ P1-9（health 脱敏）＋ P1-10（README 重写）。
- Day 10：P1-6（前端构建部署方案落地）＋ P1-7（demo 页下线、Dashboard 接真数据）。
- 持续：P1-1/P1-2 的 `main.py` 退役按模块拆（不要求两周内做完，但要有计划和第一刀：建议先迁客户管理）。

**需要 Kimiyf 拍板的**：P1-4 权限矩阵（sales 能否维护物料/产品/图纸）、P1-6 前端部署方案二选一、P0-2 前后端对齐方向二选一。

---

## 八、未核实事项

1. `phase1_postgres/`（独立 FastAPI 应用，`main.py:146`）当前是否还在某处运行、是否仍被使用 —— 未核实。
2. 工厂电脑 Windows 环境的真实运行表现（NAS 路径、中文路径、端口占用）—— 未核实。
3. `tests/migration/`（需 SQL Server）在有源库环境下的通过情况 —— 未核实。
4. 3 个 `test_order_number_structure_rehearsal` 失败是否由 P1-3 漂移直接导致 —— 未核实（高度疑似）。
5. 2 个 `test_database_path_unification` 失败是产品 bug 还是测试隔离问题 —— 未核实。
6. `static/` 旧 HTML 页面当前是否仍在被一线使用、能否下线 —— 未核实。
7. P1-4 中"测试为准还是代码为准" —— 需业务定夺，未核实。

---

*报告生成：2026-10-01，Marlowe。原始证据（运行时路由表、鉴权探针输出、pytest 日志）见本次评审工作区。*
