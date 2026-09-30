# Codex 同步任务包：v2 前端上线 + P0 安全止血

> 给 Codex（或 Claude Code）直接执行的任务清单。按 Part 顺序执行，全部完成后由 Marlowe 审核。
> 背景：`tianming-erp` 仓库，生产后端 `start_erp.bat → uvicorn app.main:app`（端口 8002）。
> 完整评审报告见 `review/REVIEW_REPORT.md`，v2 构建说明见 `review/V2_BUILD_NOTES.md`。

---

## Part A — P0 安全止血（后端先行，最高优先级）

### A1. 删除旧单体未鉴权路由
- 文件：`app/main.py` 的 `create_app()`
- 现状：`create_app()` 只删了 3 个旧路由（`/api/orders`、`/api/health`、`/api/customers`），其余来自 `main.py`（旧单体）的路由原样挂载，且**零鉴权**（已实测穿透）。
- 动作：删除所有来自 `legacy.app` 的旧 **API** 路由，至少包括：`POST /api/login`、`/api/archives/search`、`/api/customer-management*`（5 个）、`/api/customers/{id}/styles`、`/api/legacy/summary`、`/api/meta`。
- 静态页面（`/`、`/customers`、`*.html`）如继续使用，必须加鉴权中间件，否则一并下线。
- 验收：无 cookie 时 `GET /api/customer-management`、`/api/archives/search`、`/api/legacy/summary` 全部返回 **401**；`pytest tests/` 回归通过。

### A2. 换密码 + 清理 git 历史中的明文密码
- `scripts/admin/final_password_handoff.py:30-31,229-231` 含明文 `123456`/`12345678`，仓库是**公开**的。
- 动作：① 立即修改所有在该脚本中出现过的账号密码（生产库）；② 用 `git filter-repo` 或 BFG 把该文件从 git 历史中清除；③ 以后密码交接走交互式输入（`getpass`），不落盘。
- 验收：`git log -p --all -S '123456' -- scripts/admin/final_password_handoff.py` 无结果；当前 HEAD 无明文密码。

### A3. README 去默认账号 + 旧登录路由下线
- 随 A1 删除旧 `POST /api/login`（假登录，不写 session）。
- README 删除 `boss/123456` 默认账号表，改为"首次启动用 `init_db.py` 初始化，初始密码见 `ERP_INITIAL_PASSWORD` 环境变量"。
- 新鉴权（bcrypt + cookie session）保持不动。

### A4. 修复 models 与迁移漂移（P1-3，不修则建库残缺）
- `alembic upgrade head` 后 `order_item_number_sequences` 表和 2 列缺失，`app/services/order_numbering.py:59` 会炸。
- 动作：补一个 alembic 修订版，把缺失的表/列建上；空库 `upgrade head` 后反射比对 `Base.metadata`，零差异。
- 验收：全新空库 `alembic upgrade head` 成功，表结构与 models 一致。

---

## Part B — v2 前端接入

v2 代码包：`frontend-v2.tar.gz`（解压即用，不含 node_modules）。

### B1. 本地构建
```powershell
# 解压到 D:\纸箱厂erp软件搭建\ 同级，例如 D:\frontend-v2\
tar -xzf frontend-v2.tar.gz
cd frontend-v2
npm install
npm run build   # 必须 0 错误
```
- 后端地址：默认取 `http(s)://<当前主机>:8002`；如需指定，新建 `.env` 写 `VITE_API_BASE=http://192.168.x.x:8002` 后重新 build。

### B2. 部署（二选一，推荐方案一）
- **方案一**：`npm run build` 的 `dist/` 内容覆盖后端 `static/` 目录，后端继续 `start_erp.bat` 启动，前后端同源（无跨域问题）。
- **方案二**：`dist/` 放独立目录（如 `deployment/frontend-v2/`），后端 `app/main.py` 的 StaticFiles 指向它。
- 注意：旧 `static/` 下的 HTML 页面在 A1 完成后已无鉴权问题（路由已删）；如仍需保留旧页面做过渡，放到 `/legacy/` 路径并加鉴权。

### B3. 登录联调
1. 启动后端，浏览器打开前端，自动跳 `/login`。
2. 用新鉴权的账号登录（A2 换过密码的账号），登录后刷新页面，会话保持。
3. 点右上角退出，再访问任意业务页，应被拦回登录页。
4. 打开开发者工具 Network：所有 `/api/*` 请求都带 cookie；未登录调业务接口返回 401 并跳转登录。

---

## Part C — 冒烟测试清单（10 个页面逐个过）

| 页面 | 验证点 |
|---|---|
| 登录页 | 空密码/错密码提示；成功进仪表盘 |
| 首页工作台 | 数字卡片、待办列表正常加载 |
| 基础资料 | 客户/物料/产品的增删改查；产品停用走 `PUT /{id}/status` |
| 订单生产 | 新建订单（含历史产品匹配、纸箱算料 `POST /api/pricing/calculate`）；临时新箱下单（后端缺口，见 Part D） |
| 报料工作台 | 待报料列表 → 建报料单（`order_item_id`、`cardboard_len/width` 必填>0）；取消报料仅 admin |
| 超级K列报料台 | Excel 导入解析正常 |
| 出货回签 | 待出货列表（`pending_items` 下划线）→ 建送货单（只有 `vehicle_number`，明细 `delivered_quantity`）→ 回单录入/更新 |
| 月结对账 | 生成对账单 → 收款登记 `PUT /statements/{id}/settle` |
| 智能派工单 | 打印预览正常（演示页，已降为二级入口） |
| 扫码状态面板 | 扫码枪输入响应正常（演示页，已降为二级入口） |
| 移动收料（手机） | 手机浏览器打开 `/mobile-receive`，扫码收料流程可用 |

---

## Part D — 后端缺口清单（v2 已规避未编造，按需排期）

以下功能 v2 前端**没有**对应后端接口（已在 `V2_BUILD_NOTES.md` 记录），如需上线请先补后端：
1. 楞型 CRUD
2. 算料纸板展开尺寸（`/api/pricing/calculate` 只返回单价）
3. 临时新箱直连下单
4. 工序流转 / 图纸 UI 接口
5. WMS 库存查询
6. 发票管理 UI 接口
7. 回单列表 / 老板核对接口（v2 改为以送货单 `return_receipt_id` 为入口）
8. 系统备份页面接口

---

## 验收总表（全部打勾才算完）

- [ ] A1：旧未鉴权路由无 cookie 全部 401
- [ ] A2：密码已换，git 历史无明文密码
- [ ] A3：README 无默认账号
- [ ] A4：空库迁移零漂移
- [ ] B1：`npm run build` 0 错误
- [ ] B3：登录/会话/退出/401 跳转全部正常
- [ ] C：10 页面冒烟全过，无 404 调用（Network 面板验证）
- [ ] `pytest tests/` 回归通过（排除需 SQL Server 的 migration 测试）
