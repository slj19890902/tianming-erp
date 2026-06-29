# Codex 项目交接

## 2026-06-28 v0.20.8 常用箱编辑页面优化

- 现场修正版把常用箱编辑弹窗压缩为五排：核心信息；尺寸/报料/压线；材质；工艺/印刷/图纸；单价/备注。
- 长宽高、报料长宽和压线尺寸在编辑与保存边界统一为整数毫米，单价继续保留原有小数精度。
- A1/0201 普通开槽箱恢复自动推荐：报料长 `2 × (L + W) + 30`，报料宽 `W + H + 5`，压线 `round(W/2) / H / round(W/2)`。
- 自动推荐要求箱型、长宽高和材质齐全；压线类型仍由人工选择。
- 已删除“A1 推荐计算”按钮，保留“重新推荐”作为用户主动覆盖手工值的入口。
- 页面通过前端状态区分自动推荐和人工修改；已有报料/压线值或人工修改后的值不会被自动覆盖。
- 箱型列表补充 A1/0201、A3 天地盖、平卡、刀卡、隔板、围套、半开槽箱、全搭盖箱、异形箱和其他；非 A1/0201 暂不编造公式。
- 生产工艺复用现有 `production_process` 字段，可多选粘贴、打钉、模切、双拼和其他；旧的非标准工艺文字会保留，不会因打开页面被覆盖。
- 印刷类型复用现有 `print_content` 字段；无印刷时隐藏图纸区域，有印刷时显示上传和版本历史。
- 图纸继续复用现有 `product_drawings` 版本表和上传接口，新版本不会覆盖旧版本。
- 本轮不改数据库结构、不执行历史迁移、不批量修改产品或历史订单；不改订单、送货、回单和对账流程。
- 现场修正版回归测试：`132 passed in 153.30s`；前端 JavaScript 语法检查通过。
- 定向验证记录见：
  `docs/go_live_checklists/COMMON_BOX_EDIT_OPTIMIZATION_V0208_20260628_221929.md`

## 2026-06-28 v0.20.7 首页待对账汇总与一键启动

- 首页的“待对账”提醒现在按客户 + 月份合并显示，不再重复刷同一个客户的多条记录。
- 新增一键启动脚本，双击后会自动检查 Python、升级数据库、启动 ERP、打开浏览器。
- 新增停止脚本、桌面快捷方式和开机自启脚本，方便工厂电脑直接使用。
- 这次改动不改数据库结构，不做历史迁移，不碰 `legacy_*`。

## 2026-06-28 v0.20.3 合并进基线

- 合并结果：`feature/v0203-company-info` 已合并到 `factory-current-baseline`
- 合并提交：`6913749`
- 关联修复提交：`4df6c50`
- 验证结果：
  - `tests/test_v0203_company_info.py`：16 passed
  - `tests/test_phase10_frontend.py`：7 passed
  - `tests/test_phase7_deliveries.py`：14 passed
- 主库未写入，正式数据库 SHA-256 未变化
- 已补充状态文档：
  - `docs/ERP_PROJECT_STATE.md`
  - `docs/BUSINESS_RULES.md`
  - `docs/UI_STYLE_GUIDE.md`

## 2026-06-28 订单历史清理（2026-03 以前）

- 已按授权删除 `sales_orders.order_date < '2026-03-01'` 的历史订单。
- 删除前主库：`sales_orders=15595`、`sales_order_items=15661`，目标订单 `15098`、目标明细 `15158`。
- 已连带删除对应的回单、对账、结清、送货、报料、迁移映射和操作日志。
- 删除后主库：`sales_orders=497`、`sales_order_items=503`。
- 完整性检查：`integrity_check=ok`，`foreign_key_check=0`。
- 备份：`data/backups/carton_erp_before_delete_orders_before_2026_03_20260628_131335.sqlite3`
- 当前主库 SHA-256：`9947423b4f820a1bf822a3cd4a7a9e78dbff13a8ab80e09b8e6b23d86416cc1c`
- 说明：这次是本地数据库数据清理，不是历史迁移，不改 `legacy_*`。

## 2026-06-22 全量测试收尾（P1，仅改测试，未动正式库/源码/RBAC）

- 目标：将全量 `python -X utf8 -m pytest -q` 从 `285 passed / 6 failed` 修到 `291 passed / 0 failed`。
- 本轮未修改正式库 `data/carton_erp.sqlite3`、未动 `legacy_ruida_*` / `migration_*` / `RUIDA-*`、未改 RBAC/权限/密码、未改业务源码。改动全部在 `tests/`。
- 失败 6 项分类与修法：
  - `tests/test_phase12_uat.py::test_material_normalizes_weight_and_rejects_unknown_flute`、`::test_product_drawing_upload_saves_compressed_files_not_base64`：过时。master 写接口（`app/api/materials.py`、`app/api/products.py` 的 `can_write`）现为 `RoleChecker(["admin"])`，测试仍以 `sales` 登录 → 403。修法：`_login(client,"sales")` → `_login(client,"admin")`（不放宽 RBAC）。
  - `tests/test_phase10_frontend.py::test_phase10_frontend_enforces_auth_and_workshop_finance_masking`：过时。`static/index.html` 的 workshop 菜单新增 `incoming`（来料）。修法：断言更新为 `workshop: ["dashboard", "orders", "incoming", "deliveries"]`。
  - `tests/test_database_path_unification.py::test_start_script_uses_complete_backend_entrypoint`、`tests/test_phase13_deployment.py::test_start_batch_uses_project_venv_one_worker_and_production_port`：过时。启动入口已由 `start_erp.bat` 委托 `scripts/admin/start_erp_background.ps1`，uvicorn/venv/port/worker 等参数现位于 ps1。修法：测试改为校验「bat 委托 ps1 + ps1 含 `app.main:app`/`0.0.0.0`/`8000`/`--workers 1`/`.venv\\Scripts\\python.exe`/`erp_server.log` + `.env` 含 `ERP_DATABASE_PATH`/`ERP_ENVIRONMENT`」。
  - `tests/test_database_path_unification.py::test_database_path_is_absolute_and_independent_of_working_directory`：环境相关（Windows）。子进程打印含中文路径，父进程按 cp936 解码 UTF-8 失败导致 `stdout=None`。修法：`subprocess.run` 子进程环境加 `PYTHONUTF8=1`/`PYTHONIOENCODING=utf-8` 并显式 `encoding="utf-8"`。
- 结果：`tests/test_database_path_unification.py tests/test_phase13_deployment.py tests/test_phase10_frontend.py tests/test_phase12_uat.py` → `26 passed`；全量 `python -X utf8 -m pytest -q` → `291 passed, 0 failed`。

## 2026-06-22 订单状态约束迁移 h48d9f6c1e32 应用（P0）

- 本轮未执行历史迁移，未修改 `legacy_ruida_*` / `migration_*` / `snapshot_*` / `RUIDA-*`，仅扩展订单状态 CHECK 约束。
- 主库 alembic：`g37c8e5b0d21` → `h48d9f6c1e32`；`sales_orders/sales_order_items` 保持 `9549/9615`，`integrity_check=ok`，`foreign_key_check=0`。
- `sales_orders.ck_sales_orders_status` 已由 6 状态扩展为 14 状态（含 `dead/closed/archived/completed/pending_confirmation/pending_reconciliation`），修复管理员"标记死单/已结档/已归档"会触发 CHECK 失败的问题。
- 迁移前备份：`data/backups/carton_erp_BEFORE_ORDER_STATUS_CLOSURE_20260622_144745.sqlite3`（SHA-256 `b1f597bf3172ade5cdee847fccf7f1ed9ead0642ed445a2430d3a3eb348a95e3`，与迁移前主库一致）。
- 功能验证：受控测试单 `TM20260621001`(id=15594) 标记 `dead` 成功且写入审计日志，随后恢复为 `cancelled`。
- 回归测试：`75 passed`（`tests/test_phase5_orders.py tests/test_phase14_frontend.py tests/test_phase8_finance.py tests/test_phase7_deliveries.py`）。
- 已重启 8000 服务，`/api/health` 返回 `9549/9615` 正常。
- 报告：`docs/go_live_checklists/ORDER_STATUS_CLOSURE_MIGRATION_APPLY_20260622_144745.md`

## 2026-06-22 老板端仓库来料、手机卡片与天华规格识别优化

- 老板/admin 业务中心新增桌面版“仓库来料入库”；workshop 电脑端也可进入，页面使用表格布局，不复用手机卡片 UI。
- 桌面端和手机端均展示纸板报料尺寸、客户名称、存货编码、产品名称、材质、报料日期和入库数量。
- 入库数量默认取报料数量，可在确认入库前修改；确认后同步更新订单明细和有效报料明细，并写入既有入库审计日志。
- 手机卡片顺序已调整：第一行纸板报料尺寸；第二行客户名称/存货编码/产品名称；第三行材质和可编辑数量；第四行报料日期。
- 天华 PDF 规格型号解析改为仅提取尺寸表达式；例如 `28.5*19.5*5.5cm`，不会混入数量、单价、总价和日期。
- 相关回归：`83 passed`；前端 JavaScript 语法检查通过。
- 本轮未执行历史迁移，未修改 `legacy_*`，未做批量历史订单修改。

## 2026-06-22 ERP 自动启动与桌面入口

- `start_erp.bat` 闪退原因：项目 `.venv` 指向已失效的 Codex 临时 Python 路径。
- 已新增 `scripts/admin/start_erp_background.ps1`，自动跳过损坏的虚拟环境，使用本机可用 Python 后台启动，并检查 8000 健康接口。
- 已把 `Tianming ERP Auto Start.lnk` 写入当前 Windows 用户启动目录；用户登录 Windows 后 ERP 自动后台运行。
- 已在桌面创建“天明ERP系统”网页快捷方式，员工只需双击该图标。
- `start_erp.bat` 也已改为后台启动成功后自动打开网页；失败时窗口不再闪退，会保留中文错误提示。
- 当前已验证 `http://127.0.0.1:8000/` 和 `/api/health` 正常。

## 2026-06-22 产品 PDF 图纸与手机扫码查看

- 本轮未执行历史迁移，未修改历史订单，未修改 `legacy_*`，未写正式主库业务数据。
- 产品图纸上传现支持 JPG、PNG、WEBP 和 PDF，单文件最大 20MB。
- PDF 会校验 `%PDF-` 文件头并作为图纸版本保存；图片图纸的压缩和缩略图逻辑保持不变。
- 产品编辑页的图纸版本历史可直接点击打开 PDF。
- 手机扫码来料/订单尺寸页面接口会返回产品最新图纸；存在 PDF 时卡片显示“打开 PDF 图纸”按钮。
- 图纸上传权限保持现有管理员权限；管理员和车间可在手机页面查看。
- 无数据库结构变更。

## 2026-06-22 OCR 草稿匹配、多文件识别、成本参考与订单状态闭环

- 本轮未执行历史迁移，未修改历史订单，未修改 `legacy_*`，未写正式主库业务数据。
- 已完成 OCR 独立草稿层、手动换客户保留识别内容、客户三态匹配、产品/材质/规格候选匹配。
- 已完成多文件识别、文件 hash 去重、订单明细签名去重、逐份确认和批量保存已确认草稿。
- 已在新建订单和编辑订单明细显示客户单价、预估成本和预估毛利；数据不足显示待计算；对账主表未增加成本列。
- 已完成订单删除双确认、有关联订单删除门禁、死单/已结档备注和日志、默认未完成筛选与统计口径。
- 新增结构版本 `h48d9f6c1e32`，仅扩展订单状态检查约束；正式主库尚未执行，隔离副本升级演练通过。
- 测试：OCR/订单/前端 `49 passed`；送货/财务/报料跨模块 `38 passed`；合计 `87 passed`。
- 报告：`docs/go_live_checklists/OCR_ORDER_DRAFT_MATCHING_AND_ORDER_STATUS_OPTIMIZATION_20260622_105522.md`
- 下一步：人工验收真实 PDF；计划停机窗口内备份后执行 Alembic 结构升级并重启 8000 服务。

## 2026-06-21 工厂电脑独立 Codex 接手文档

- 新增独立交接文档：
  `docs/go_live_checklists/FACTORY_CODEX_HANDOFF_20260621_222400.md`
- 用途：
  - 给工厂电脑上的另一套 Codex 独立接手使用
  - 不依赖当前聊天记录
  - 明确正式项目目录、正式数据库路径、Z 盘旧目录不要作为运行目录
  - 明确下一步只做 OCR 订单识别草稿 / 新建订单 / 订单状态与成本显示流程优化
- 重点门禁：
  - 不执行历史迁移
  - 不批量修改历史订单
  - 不修改 `legacy_*`
  - 不把 `Z:\sata1-18015598002\纸箱厂erp软件搭建` 当正式运行目录
  - 不把 `Z:\sata1-18015598002\BoxERP\erp.db` 当正式订单库

## 2026-06-21 存量订单归档与纸板尺寸显示调整摘要

- 本轮未执行历史迁移，未修改 `legacy_*`。
- 已新增脚本：
  `scripts/admin/archive_existing_orders.py`
- 已执行一次主库存量订单归档：
  - 备份：
    `data/backups/carton_erp_before_archive_existing_orders_20260621_170308.sqlite3`
  - 截止 `order_id <= 15594`
  - 当前已有订单统一标记为已结款口径
  - 当前已有订单明细统一标记为 `已结算`，仅用于历史查看，不再参与待报料/待收料
- 主库核对：
  - `sales_orders = 15594`
  - `sales_order_items = 15660`
  - `pending_pool = 0`
  - `incoming_pool = 0`
  - `integrity_check = ok`
- 前端已调整：
  - 报料管理“纸板规格”显示为整数 `mm`
  - 报料弹窗列名改为 `纸板长(mm)` / `纸板宽(mm)`
  - 来料页面“纸板报料尺寸”显示为整数 `mm`
  - 收款核销前端不再要求输入收款账户
- 后端已支持收款核销缺省 `account`，但 8000 端口当前运行中的 Python 进程无法由本轮会话直接结束，若页面仍沿用旧逻辑，需要现场手动重启服务后生效。
- 回归测试：
  `.\.venv\Scripts\python.exe -m pytest tests\test_phase11_requisition.py tests\test_phase8_finance.py tests\test_phase14_frontend.py tests\test_phase15_requisition_finance_adjustments.py tests\scripts\test_archive_existing_orders.py -q`
  结果：`48 passed`

## 2026-06-21 历史报料归档与收款核销交互调整摘要

- 本轮未执行历史迁移，未修改历史订单，未修改 `legacy_*`，未写主库业务数据。
- 已调整报料接口：
  - 历史订单不再进入 `/api/requisition/pending` 待报料池。
  - 历史订单即使原始 `requisition_status` 仍为 `未报料`，也会在 `/api/requisition/items` 中按归档记录返回。
  - 归档历史报料项接口展示状态映射为 `settled`，用于只读查看，不回写数据库。
- 已调整财务收款核销：
  - 前端 `收款核销` 不再弹出“收款账户”输入框。
  - 后端 `/api/finance/statements/{id}/settle` 允许缺省 `account`，落库为空字符串，不要求改表。
- 相关回归测试：
  `.\.venv\Scripts\python.exe -m pytest tests\test_phase11_requisition.py tests\test_phase8_finance.py tests\test_phase14_frontend.py tests\test_phase15_requisition_finance_adjustments.py -q`
  结果：`44 passed`

## 2026-06-21 订单管理前端 UI 重构摘要

- 本轮未执行历史迁移，未修改历史订单，未修改 `legacy_*`。
- 已重构订单管理顶部区域：标题说明、主按钮区、筛选卡片区。
- 默认订单组列表已调整为“客户名称优先”，主系统单号默认隐藏。
- 展开区已改为浅色明细卡片，显示主系统单号与明细系统单号。
- 新建订单表单已补齐 `customer_po` 与下单日期。
- 订单组编辑表单已补齐 `customer_po`，通过 `/api/orders/{order_id}` 支持订单头编辑。
- 订单列表/详情新增 `display_material`，显示层会把 `045 A113B` 清洗为 `A113B`，不批量改原始库值。
- 订单列表接口新增日期范围与状态筛选参数：`date_from` / `date_to` / `status`。
- 只读主库校验：
  - `sales_orders = 15594`
  - `sales_order_items = 15660`
  - `legacy_ruida_orders = 39922`
  - `legacy_ruida_order_items = 40449`
  - `integrity_check = ok`
- 测试命令：
  `.\.venv\Scripts\python.exe -m pytest tests\test_phase5_orders.py tests\test_phase14_frontend.py tests\test_order_number_structure_rehearsal.py tests\scripts\test_apply_order_number_structure.py -q`
  结果：`31 passed`
- 报告：
  `docs/go_live_checklists/ORDER_MANAGEMENT_UI_REDESIGN_AND_FORM_FIX_20260621_145404.md`
- 补充说明：本轮尝试做内置浏览器自动验收时，运行时返回 `codex/sandbox-state-meta: missing field sandboxPolicy`，因此页面层面仍建议再做一次人工点击验收。

## 2026-06-21 订单编号结构主库实施摘要

- 本轮未执行历史迁移，未修改历史订单，未修改 `legacy_*`。
- 已创建主库备份：
  `data/backups/carton_erp_before_order_number_structure_apply_20260621_093327.sqlite3`
- 主库结构迁移已完成：
  - `sales_order_items.item_order_number`
  - `sales_order_items.item_sequence`
  - `order_item_number_sequences`
  - `ix_sales_orders_customer_po`
  - `ix_sales_orders_customer_po_group`
  - `ux_sales_order_items_item_order_number`
  - `ix_sales_order_items_snapshot_product_code`
  - `ix_sales_order_items_snapshot_product_name`
- 后端已启用新编号：
  - 主单：`TMYYYYMMDDNNN`
  - 明细：`TMYYYYMMDDNNN-001`
- 订单管理默认已切换到 `customer_id + customer_po` 分组；系统单号默认隐藏，展开后显示主 / 明细系统单号。
- 已创建 1 张受控测试订单并按业务规则作废：
  - 客户单号：`TEST-ORDER-NUMBER-VERIFY-20260621`
  - 主系统单号：`TM20260621001`
  - 明细系统单号：`TM20260621001-001/002/003`
- 不复用验证通过：
  - 下一张主单预览：`TM20260621002`
  - 下一条明细序号预览：`4`
- 主库当前数量：
  - `sales_orders = 15594`
  - `sales_order_items = 15661`
  - 历史订单 = `15589`
  - 台账订单 / 明细 = `15589 / 15651`
- `integrity_check = ok`
- 应用实际连接 `foreign_keys = 1`
- 验证报告：
  `docs/go_live_checklists/ORDER_NUMBER_STRUCTURE_APPLY_AND_VERIFY_20260621_093846.md`
- 下一步建议：重新执行综合人工试用验收，重点复核新建订单预览、分组展开、搜索与测试订单作废显示。

## 2026-06-20 阻塞项修复摘要

- 本轮未执行历史迁移，未修改历史订单，未修改 `legacy_*`。
- 修复了综合人工试用报告中的 10 个阻塞项：
  1. 历史订单列表改为 TM 显示编号
  2. TM 搜索恢复可用
  3. `/api/customers` 未登录改为 401，普通链路不再暴露旧追溯字段
  4. 客户默认分页统一 25
  5. 常用箱页面恢复客户主从视图
  6. 产品资料筛选补齐
  7. 新建订单未选客户前禁用产品明细输入
  8. 保存失败改为中文友好提示
  9. 订单管理补齐客户单号 / 客户名称 / 日期分组
  10. 历史订单稳定显示“历史归档”
- 相关测试：
  `.\.venv\Scripts\python.exe -m pytest tests/test_phase2_auth.py tests/test_phase3_api.py tests/test_phase5_orders.py tests/test_phase8_finance.py tests/test_phase14_frontend.py -q`
  结果：`54 passed`
- 主库只读核对保持：
  `sales_orders=15593`、`sales_order_items=15658`、历史订单 `15589`、`integrity_check=ok`
- 修复报告：
  `docs/go_live_checklists/MANUAL_TRIAL_BLOCKERS_FIX_20260620_205211.md`
- 建议下一步重新执行综合人工试用验收；本轮未创建测试订单。

更新时间：2026-06-19

## 1. 项目路径

用户指定交接路径：

```text
C:/Users/Administrator/Documents/纸箱厂erp软件搭建/
```

本线程实际工作区：

```text
D:/纸箱厂erp软件搭建/
```

新线程应先确认实际工作区，禁止因路径差异复制、覆盖数据库。

## 2. 当前重要结论

### 2.0 2026-06-19 dry-run 差异核对已完成

只读报告：

```text
docs/migration_reports/DRY_RUN_DIFF_20260619_081040.md
```

本轮未修改任何数据库，未刷新 `legacy_ruida_*`，未写入正式业务表。

| 对比 | SQL Server | SQLite legacy | SQL Server 独有 | SQLite 独有 |
|---|---:|---:|---:|---:|
| 订单 | 39,922 | 39,766 | 156 | 0 |
| 订单明细 | 40,449 | 40,293 | 156 | 0 |
| 客户 | 132 | 132 | 0 | 0 |

缺失订单 ID 为 `42688–42843`，缺失明细 ID 为 `43370–43527`，集中于 2026-06-01 至 2026-06-16。三组源键均无重复，订单/客户关联无孤儿。正式表仍为 `sales_orders=4`、`sales_order_items=7`。

### 2.0.1 legacy 原始层刷新方案已设计，尚未执行

方案：`docs/migration_reports/LEGACY_REFRESH_PLAN_20260619_083121.md`

脚本：`scripts/migration/refresh_legacy_ruida_from_sqlserver.py`

- 默认 dry-run，写入必须显式 `--apply` 和确认短语。
- 写当前主沙盘还需要 `--allow-main-sandbox`。
- 幂等键为 `legacy_order_id`、`legacy_item_id`、`legacy_customer_id`。
- 只追加缺失源 ID，不自动更新已有 legacy 行。
- 客户 132 对 132，默认不刷新。
- dry-run 实测预计新增：订单 156、明细 156、客户 0。
- 最新脚本 dry-run 报告：`docs/migration_reports/LEGACY_REFRESH_DRY_RUN_20260619_083632.md`。
- 本轮未修改数据库，未运行 `--apply`。

### 2.0.2 隔离副本 Apply 验证已通过

报告：`docs/migration_reports/LEGACY_REFRESH_APPLY_TEST_20260619_085901.md`

副本：`data/sandboxes/carton_erp_legacy_refresh_apply_test_20260619_085901.sqlite3`

- 仅副本执行一次 apply，主库未修改。
- legacy 订单：39,766 → 39,922；明细：40,293 → 40,449。
- 客户保持 132；`sales_orders` / `sales_order_items` 保持 4 / 7。
- Apply 后 dry-run 为 0 / 0 / 0。
- 幂等键无重复，孤儿明细为 0，关键字段对照差异为 0。
- 主库 SHA-256、大小、修改时间和表数量均未变化。

### 2.0.3 主库 legacy 原始层增量刷新已完成

报告：`docs/migration_reports/LEGACY_REFRESH_MAIN_APPLY_20260619_090705.md`

备份：`data/backups/carton_erp_before_legacy_refresh_apply_20260619_090705.sqlite3`

- 仅主库三张 `legacy_ruida_*` 表追加数据。
- legacy 订单：39,766 → 39,922。
- legacy 明细：40,293 → 40,449。
- legacy 客户保持 132。
- `sales_orders` / `sales_order_items` 保持 4 / 7。
- Apply 后 dry-run 为 0 / 0 / 0。
- `integrity_check = ok`，幂等键无重复，孤儿明细为 0。
- 本轮未执行任何正式业务表迁移。

### 2.0.4 正式表映射与100条副本试迁移已完成

方案：`docs/migration_reports/FORMAL_SALES_MAPPING_PLAN_20260619_091639.md`

副本：`data/sandboxes/carton_erp_formal_mapping_test_20260619_091639.sqlite3`

- 主库未修改，正式表主库仍为4/7。
- 副本正式表由4/7变为104/107，新增100个订单和100条明细。
- 样本金额165,528.97，完整性、外键和金额规则校验通过。
- Apply后同样本dry-run为0/0，幂等验证通过。
- 产品按“客户+款号精确匹配产品编码/客户料号”；未匹配整单跳过。
- 全量前仍需解决约30,304条未匹配产品明细。

### 2.0.5 产品匹配缺口与多明细专项测试已完成

报告：`docs/migration_reports/PRODUCT_MATCH_GAP_AND_MULTI_ITEM_TEST_20260619_094449.md`

副本：`data/sandboxes/carton_erp_multi_item_formal_test_20260619_094449.sqlite3`

- 产品唯一匹配10,145，0匹配30,304，多匹配0。
- 未匹配金额48,944,776.46，涉及30,074个订单。
- 主要原因是`style_no`包含“编码 / 描述”；提取斜杠前编码有27,431条唯一候选，但尚未应用。
- 找到90张完整多明细候选，选20张共56条明细做副本测试。
- 副本正式表4/7 → 24/63，逐单明细和金额差异均为0，复跑为0/0。
- 主库未修改，产品和客户资料未修改。

### 2.1 `erp.db` 不是历史订单源

`Z:\sata1-18015598002\BoxERP\erp.db` 只读探查结果：

| 项目 | 结果 |
|---|---:|
| 文件大小 | 258,048 字节 |
| `PRAGMA integrity_check` | `ok` |
| `customers` | 128 |
| `materials` | 46 |
| `products` | 1 |
| `orders` | 0 |
| `order_items` | 0 |
| `delivery_records` | 0 |
| `users` | 4 |

结论：不得把 `erp.db` 当作 4 万历史订单来源。

### 2.2 瑞达 SQL Server 数据规模

4 万历史订单位于瑞达 SQL Server 备份体系。审计文件和已还原数据库的记录数为：

| 表 | 记录数 |
|---|---:|
| `Orders` | 约 39,918 至 39,922 |
| `OrderXLs` | 约 40,445 至 40,449 |
| `CaiGouDanDetails` | 约 31,961 |
| `OrderXLs_common` | 约 3,407 |
| `Customers` | 约 132 |

### 2.3 当前主沙盘状态

`data/carton_erp.sqlite3` 已有瑞达原始抽取层：

| 表 | 记录数 |
|---|---:|
| `legacy_ruida_orders` | 约 39,766 |
| `legacy_ruida_order_items` | 约 40,293 |
| `legacy_ruida_customers` | 约 132 |

正式业务表当前数据很少：

| 表 | 记录数 |
|---|---:|
| `sales_orders` | 约 4 |
| `sales_order_items` | 约 7 |

结论：历史数据已经部分抽取，但尚未完成：

```text
legacy_ruida_* 原始层
→ sales_orders / sales_order_items 正式 ERP 业务表
```

## 3. SQL Server / BAK 状态

1. 初始环境没有 LocalDB 和 `sqlcmd`。
2. `winget` 可用，已找到官方包 `Microsoft.SQLServer.2022.Express`。
3. 安装介质已下载并解压到 `C:\SQL2022\Express_ENU`。
4. winget 引导器规则检查通过，但曾以 `0x84C4000E` 退出。
5. 后续直接调用 `SETUP.EXE` 创建独立实例 `BOXERP`。
6. `MSSQL$BOXERP` 服务后来显示 `Running`。
7. BAK 已复制到 SQL Server 可访问目录。
8. 本地副本与 Z 盘源文件 SHA-256 一致：

```text
39570E96824B2E5E054FACAF5BBF75612C5DB531982BE11F06EE94783459521D
```

9. `RESTORE VERIFYONLY = OK`。
10. BAK 信息：
    - 数据库名：`BoxDB20`
    - 备份时间：2026-06-16 14:50:06 至 14:50:26
    - 来源版本：SQL Server 2005
    - 数据库版本：611
    - 数据文件约 558 MB
    - 日志约 285 MB
11. 隔离还原目标：`BoxDB20_REPRO`。
12. 本线程曾观察到 `BoxDB20_REPRO` 为 `ONLINE`，但新线程仍须只读复核。
13. 禁止覆盖任何生产库或原始备份。

## 4. 下一步推荐路线

1. 新线程先读取本文件和项目根目录 `AGENTS.md`。
2. 不依赖旧聊天记录。
3. 以 `BoxDB20_REPRO` 为权威源，以现有 `legacy_ruida_*` 作为字段映射参考。
4. dry-run 差异统计和原始层增量刷新方案评审已完成。
5. 主库 legacy 原始层增量刷新已完成并归零。
6. 正式表映射和首批100条副本试迁移已完成。
7. 产品缺口分析和多明细专项样本已完成。
8. 斜杠前编码候选映射清单已生成，下一步只做人工复核结果文件设计与校验。
9. 未经新授权，不修改产品，不写入主库正式表。
10. 人工映射机制确认后，才评审扩大副本迁移范围。

## 5. 风险

- 不能把 `erp.db` 当作历史订单源。
- 不能直接覆盖 `data/carton_erp.sqlite3`。
- 不能直接把 4 万订单灌入正式业务表。
- SQL Server 2005 数据需处理字段类型、编码、日期和金额精度。
- 现有 `legacy_ruida_*` 比最新 BAK 少约 156 条订单，必须差异校验。
- 正式表已有少量数据，导入前必须备份并确认去重策略。
- 长日志写入 `docs/migration_reports/`，不要放入聊天上下文。

## 6. 斜杠前编码候选清单（2026-06-19）

- 只读脚本：`scripts/migration/analyze_product_prefix_candidates.py`
- 报告：`docs/migration_reports/PRODUCT_PREFIX_CANDIDATES_REPORT_20260619_150159.md`
- 全量候选：`docs/migration_reports/PRODUCT_PREFIX_CANDIDATES_20260619_150159.csv`
- 天华 Top 200：`docs/migration_reports/PRODUCT_PREFIX_TIANHUA_TOP_REVIEW_20260619_150159.csv`
- 严格斜杠记录口径：`prefix_unique=27,430`、`prefix_multi_match=0`、`prefix_no_match=707`。
- 疑似费用项 85 条、金额 173,704.40，禁止自动映射为普通产品。
- 模拟批准非费用 `prefix_unique` 后，完整可迁移订单预计由 9,823 增至 37,027，可迁移明细预计为 37,543。
- 主库 SHA-256 前后均为 `6ED950245BC91E18A8D4C8F55CB55B6630396B16117DF4145922C725E28E7F77`，未写数据库。

## 7. 人工复核与审批校验（2026-06-19）

- Worksheet：`docs/migration_reports/PRODUCT_PREFIX_TIANHUA_REVIEW_WORKSHEET_20260619_150757.csv`
- 填写说明：`docs/migration_reports/PRODUCT_PREFIX_REVIEW_GUIDE_20260619_150757.md`
- 校验脚本：`scripts/migration/validate_product_prefix_review.py`
- 流程报告：`docs/migration_reports/PRODUCT_PREFIX_REVIEW_WORKFLOW_20260619_150757.md`
- 首次校验报告：`docs/migration_reports/PRODUCT_PREFIX_REVIEW_VALIDATION_20260619_150845.md`
- 当前 200 行全部为 `pending`，已批准映射为 0，校验错误为 0。
- 脚本只读访问 SQLite，不提供 `--apply`，运行前后校验主库 SHA-256。
- 下一步由人工填写审批字段；完成小批审批并校验通过后，才设计隔离副本试迁移。

## 8. 天华已审核 worksheet 校验（2026-06-19）

- 审核文件：`docs/migration_reports/PRODUCT_PREFIX_TIANHUA_REVIEW_WORKSHEET_APPROVED_EXCEPT_32_157.csv`
- 校验报告：`docs/migration_reports/PRODUCT_PREFIX_REVIEW_VALIDATION_20260619_172131.md`
- 状态统计：approved 198、rejected 0、pending 0、needs_check 2。
- 字段、决定值、产品 ID、候选 ID、重复键、空白批准和费用项检查均无异常。
- CSV 第 32、157 物理行保持未批准，但使用 `needs_check + create_product_later`；现行规则要求 `create_product_later + rejected`，因此审批错误为 2，校验不通过。
- 主库 SHA-256 未变化，`integrity_check=ok`，正式表和产品表未写入。
- 当前门禁：禁止进入副本试迁移。下一步由用户决定修改 worksheet 状态或另行授权调整校验规则。

## 9. 天华审核状态修正与复验（2026-06-19）

- 修正文件：`docs/migration_reports/PRODUCT_PREFIX_TIANHUA_REVIEW_WORKSHEET_APPROVED_EXCEPT_32_157_FIXED.csv`
- 校验报告：`docs/migration_reports/PRODUCT_PREFIX_REVIEW_VALIDATION_FIXED_20260619_172509.md`
- 仅第 32、157 物理行由 `needs_check` 改为 `rejected`，决定保持 `create_product_later`，产品 ID 为空；其余 198 行未变化。
- 状态统计：approved 198、rejected 2、pending 0、needs_check 0。
- 审批错误 0，无费用项误批准，校验通过。
- 主库 SHA-256 未变化，`integrity_check=ok`，正式表和产品表未写入。
- 下一步只可设计“已批准映射驱动的隔离副本小批量试迁移”，仍不得写主库正式表。

## 10. 天华 approved prefix 副本试迁移（2026-06-19）

- 副本：`data/sandboxes/carton_erp_tianhua_prefix_review_apply_test_20260619_173046.sqlite3`
- 报告：`docs/migration_reports/TIANHUA_PREFIX_REVIEW_SANDBOX_APPLY_20260619_173202.md`
- Dry-run：100张订单、100条明细，prefix 80、精确20，金额689,972.39。
- 副本 Apply：正式表 4/7 → 104/107；产品表保持3,316。
- 两个 rejected 款号迁移数均为0；关联、金额、台账和完整性校验无差异。
- 复跑 dry-run 为0/0，幂等通过。
- 主库 SHA-256、大小、修改时间及4/7/3,316数量均未变化。
- 本结果仅证明隔离副本小批量验证通过，不授权主库正式表迁移。

## 11. 天华 approved prefix 全范围副本试迁移（2026-06-19）

- 副本：`data/sandboxes/carton_erp_tianhua_prefix_full_scope_test_20260619_174557.sqlite3`
- 报告：`docs/migration_reports/TIANHUA_PREFIX_FULL_SCOPE_SANDBOX_APPLY_20260619_174719.md`
- Dry-run/Apply：15,589张订单、15,651条明细，金额31,761,489.94。
- Prefix映射14,175条、精确匹配1,476条；多明细订单50张。
- 跳过8,598张：未匹配8,527、rejected 70、费用项1。
- Rejected款号、费用项和未匹配明细进入迁移数均为0。
- 逐单明细、金额、关联、产品外键和台账校验通过；复跑0/0。
- 主库哈希、元数据及4/7/3,316数量未变化。
- 本结果仍不授权主库正式表迁移。

## 12. 天华主库正式迁移就绪性评审（2026-06-19）

- 报告：`docs/migration_reports/TIANHUA_MAIN_APPLY_READINESS_REVIEW_20260619_175201.md`
- 本轮仅运行主库dry-run，未运行apply；基线仍为15,589/15,651，金额31,761,489.94。
- 当前结论：尚不具备主库迁移条件。
- 阻断1：脚本明确禁止规范主库路径apply。
- 阻断2：当前limit不能连续推进分批，需不可变批次清单。
- 阻断3：运行中API订单总数为6，而拟迁移主库正式订单为4，后端实际数据库未锁定。
- 建议5批：100、1,000、5,000、5,000、余量。
- 备份、停机、整库回滚、逐批验收方案已完成；必须完成脚本改造、数据库URL确认和副本分批/回滚演练后再申请授权。

## 13. 主库保护改造与5批副本演练（2026-06-19）

- 报告：`docs/migration_reports/TIANHUA_MAIN_APPLY_GUARD_AND_5BATCH_REHEARSAL_20260619_180153.md`
- 后端真实数据库确认：`data/tm_phase3_dev.sqlite3`，使用`orders/order_items`；不是拟迁移的`data/carton_erp.sqlite3`。
- API订单6来自`tm_phase3_dev.sqlite3.orders`；目标库正式订单4来自`carton_erp.sqlite3.sales_orders`。
- 原因：数据库模块不读取`.env`中的`ERP_DATABASE_PATH`，回退到相对默认路径。
- 迁移脚本已加入主库双重授权、预期哈希、不可变manifest和batch-id门禁。
- Manifest：`TIANHUA_MAIN_BATCH_MANIFEST_20260619_175953.csv`，5批固定100/1,000/5,000/5,000/4,489。
- 新副本5批连续演练通过，合计15,589/15,651，全部批次及全范围复跑归零。
- 主库未变化。剩余阻塞是后端数据库/表模型与迁移目标不统一，禁止主库apply。

## 14. 后端数据库路径统一（2026-06-19）

- 报告：`docs/migration_reports/DATABASE_PATH_UNIFICATION_APPLY_20260619_200919.md`
- 正式启动入口已由 `phase1_postgres.main:app` 改为 `app.main:app`。
- 后端现读取 `D:\纸箱厂erp软件搭建\data\carton_erp.sqlite3`，健康接口报告 `sales_orders/sales_order_items=4/7`。
- 前端首页由同一 8000 端口提供；RBAC 未登录请求保持 401。
- `tm_phase3_dev.sqlite3` 保留为废弃测试库，6 张测试订单不迁移。
- 主库哈希、大小、修改时间未变化，`integrity_check=ok`；相关测试 49 项通过。
- 活跃后端已使用 `sales_orders/sales_order_items`；旧 `phase1_postgres` 模型不再作为正式入口。
- 下一步仍须先做正式迁移前只读 dry-run、停机和备份复核，不得直接主库 apply。

## 15. 天华主库正式迁移前最终 Dry-run（2026-06-19）

- 报告：`docs/migration_reports/TIANHUA_MAIN_APPLY_FINAL_DRY_RUN_20260619_202709.md`
- 本轮未执行主库 apply，未写入正式订单。
- 正式字节级备份：`data/backups/carton_erp_before_tianhua_sales_apply_20260619_202543.sqlite3`
- 主库与备份 SHA-256 均为 `6ED950245BC91E18A8D4C8F55CB55B6630396B16117DF4145922C725E28E7F77`，完整性均为 `ok`。
- 五批主库 dry-run 合计 15,589 张订单、15,651 条明细、金额 31,761,489.94；Prefix 14,175、精确 1,476。
- 结果与 Manifest 和五批副本演练完全一致；rejected 款号、费用项和未匹配明细进入计划均为 0。
- 正式表使用 `INTEGER PRIMARY KEY`、非 AUTOINCREMENT，无对应 sqlite_sequence，风险低。
- 表外键已定义，现有 `foreign_key_check=0`；脚本 apply 会在事务前开启外键，正式执行仍须现场断言 `foreign_keys=1`。
- 当前可申请仅 `batch_1`（100 张）的主库 apply 授权；执行前必须停机、确认无占用并重做时点备份。

## 16. 天华主库 Batch 1 正式迁移（2026-06-19）

- 报告：`docs/migration_reports/TIANHUA_MAIN_BATCH_1_APPLY_20260619_203511.md`
- 仅执行 `batch_1`；未执行 `batch_2` 或其他批次。
- 时点备份：`data/backups/carton_erp_before_tianhua_batch_1_apply_20260619_203207.sqlite3`
- Apply 前主库/备份 SHA-256 均为 `6ED950245BC91E18A8D4C8F55CB55B6630396B16117DF4145922C725E28E7F77`。
- Dry-run 与 Apply 均为 100 张订单、100 条明细，金额 689,972.39；Prefix 80、精确 20。
- 正式表由 4/7 变为 104/107；产品、客户、legacy 和用户数量未变化。
- `integrity_check=ok`、`foreign_keys=1`、`foreign_key_check=0`；无孤儿、无效产品、重复台账、明细数或金额差异。
- Rejected 款号及费用项迁移数为 0；Batch 2 迁移数为 0。
- Batch 1 复跑 dry-run 为 0/0。
- 后端已恢复并连接正式库，健康接口报告 104/107。
- 下一步只读验收 Batch 1 页面显示，不得自动执行 Batch 2。

## 17. 天华 Batch 1 只读 API / 页面验收（2026-06-19）

- 报告：`docs/migration_reports/TIANHUA_BATCH_1_READONLY_ACCEPTANCE_20260619_205822.md`
- 本轮未执行 Batch 2、未执行 apply、未修改数据库，仅发送 GET。
- API 分页 3 页成功返回正式订单总数 104，累计读取到 100 张 `RUIDA-` 历史订单。
- Batch 1 数据库交叉校验：100 张订单、100 条台账明细；无孤儿、无效产品、空金额、非法数量、重复单号、rejected 或费用项。
- 已抽查 5 张订单的嵌套明细，客户、日期、金额、产品名称、规格和材质合理。
- 前端登录页加载正常且无控制台错误；因本轮禁止登录 POST，未进入认证后的订单页面。
- 当前 API 不支持订单号 keyword 搜索，`keyword=RUIDA-` 被忽略；订单详情为列表内展开，没有独立详情 GET。
- 当前不建议申请 Batch 2；先单独授权只读业务页面登录验收，或补齐搜索/详情能力后再评审。

## 18. Batch 1 搜索、详情与登录后页面验收（2026-06-19）

- 报告：`docs/migration_reports/TIANHUA_BATCH_1_SEARCH_DETAIL_ACCEPTANCE_20260619_211909.md`
- 本轮未执行 Batch 2、未迁移订单、未修改主库数据。
- `GET /api/orders` 已支持 `keyword`、`order_number`、`customer_name` 和分页。
- 新增 `GET /api/orders/{order_id}`，返回订单、客户和嵌套明细，不存在返回404。
- 主库搜索 `RUIDA-` 返回100，天华客户名称返回103（原有3+Batch 1的100）。
- 前端订单页新增订单号搜索和独立只读详情弹窗。
- 登录后页面验收使用主库副本和8004临时服务；正式主库只执行GET。
- 浏览器验收搜索100条、客户筛选、2页分页和5张详情均通过，无应用控制台错误。
- 回归测试34项通过；主库保持104/107、哈希不变、完整性为ok。
- 当前可申请 Batch 2 授权，但仍须单独授权并重新执行全部迁移门禁。

## 19. 天华主库 Batch 2 正式迁移（2026-06-20）

- 报告：`docs/migration_reports/TIANHUA_MAIN_BATCH_2_APPLY_20260620_074806.md`
- 仅执行 `batch_2`；未执行 Batch 3、4、5。
- 时点备份：`data/backups/carton_erp_before_tianhua_batch_2_apply_20260620_074551.sqlite3`
- Apply 前主库/备份 SHA-256 均为 `2EBFB2F9A0BE2B7F50A08E114B44532C25B7B497505C3CDAB73ACFE66A67EFD3`。
- Dry-run 与 Apply 均为 1,000 张订单、1,006 条明细，金额 1,230,739.83；Prefix 822、精确 184。
- 正式表由 104/107 变为 1,104/1,113；Batch 1 的100张保持不变。
- `integrity_check=ok`、`foreign_keys=1`、`foreign_key_check=0`；无孤儿、无效产品、重复台账或逐单金额差异。
- Rejected 款号及费用项迁移数为0；Batch 3～5迁移数为0。
- Batch 2 复跑 dry-run 为0/0。
- 后端已恢复并连接正式库，健康接口报告1,104/1,113。
- 下一步只读验收 Batch 2，不得自动执行 Batch 3。

## 20. 天华 Batch 2 只读验收（2026-06-20）

- 报告：`docs/migration_reports/TIANHUA_BATCH_2_READONLY_ACCEPTANCE_20260620_082805.md`
- 本轮未执行 Batch 3、未修改数据库，仅发送 GET。
- API 正式订单/明细为1,104/1,113，`RUIDA-`历史订单为1,100。
- 天华客户筛选为1,103（原有3+迁移1,100），与`RUIDA-`组合筛选为1,100。
- 第1、11、22页分页正常；15次列表查询中位10.98ms、最大31.44ms。
- 抽查Batch 1三张、Batch 2七张详情，明细、金额、日期和产品快照均与数据库一致。
- 前端搜索、客户筛选、组合筛选、分页、10张详情及对账页面均正常，控制台无应用错误。
- 主库哈希、大小、修改时间、当前1,104/1,113数量及操作日志计数前后不变，完整性为ok。
- 当前可申请Batch 3授权，但未经单独授权不得执行。

## 21. 天华主库 Batch 3 正式迁移（2026-06-20）

- 报告：`docs/migration_reports/TIANHUA_MAIN_BATCH_3_APPLY_20260620_085943.md`
- 仅执行`batch_3`，未执行Batch 4、5或全量迁移。
- 时点备份：`data/backups/carton_erp_before_tianhua_batch_3_apply_20260620_085704.sqlite3`
- Dry-run与Apply均为5,000张订单、5,054条明细，金额8,590,604.37。
- Prefix 4,638、精确416，多明细订单43张。
- 正式表由1,104/1,113变为6,104/6,167。
- `integrity_check=ok`、`foreign_keys=1`、`foreign_key_check=0`。
- 明细数、金额、关联、台账、排除项和sqlite sequence检查均无异常。
- Batch 3复跑为0/0；Batch 4/5迁移数仍为0。
- 后端已恢复并读取正式库，健康接口为6,104/6,167，`RUIDA-`为6,100。
- 下一步只读验收Batch 3，不得自动执行Batch 4。

## 22. 天华 Batch 3 只读验收（2026-06-20）

- 报告：`docs/migration_reports/TIANHUA_BATCH_3_READONLY_ACCEPTANCE_20260620_093126.md`
- 本轮未执行Batch 4/5、未修改数据库，仅执行GET和只读查询。
- API正式订单/明细为6,104/6,167，`RUIDA-`和天华迁移订单均为6,100。
- 指定第1、10、50、80、122页、客户筛选和组合筛选全部正常。
- 抽查Batch 1三张、Batch 2四张、Batch 3八张详情，数据与主库一致。
- 列表API 39次中位9.87ms、最大32.09ms；详情60次中位4.40ms、最大28.44ms。
- 前端15张详情、分页和对账开票收款页面正常，控制台无应用错误。
- `order_number`、`customer_id`、`order_id`关键索引均已存在，不建议重复加索引。
- 主库哈希、大小、修改时间、正式表和操作日志计数前后不变，完整性为ok。
- 当前可申请Batch 4授权，但未经单独授权不得执行。
## 23. 天华主库 Batch 4 正式迁移（2026-06-20）

- 报告：`docs/migration_reports/TIANHUA_MAIN_BATCH_4_APPLY_20260620_130527.md`
- 仅执行`batch_4`，未执行`batch_5`或全量迁移。
- 时点备份：`data/backups/carton_erp_before_tianhua_batch_4_apply_20260620_130056.sqlite3`
- Apply前主库/备份SHA-256一致：
  `1BE0238B6C5B9AD8BAA48488AB2DD7B3283B1D56D40E5460CF18BD4C408EC1FF`
- Dry-run与Apply均为5,000张订单、5,002条明细，金额16,331,793.23。
- Prefix 4,533、精确469，多明细订单2张。
- 正式表由6,104/6,167变为11,104/11,169；`RUIDA-`订单变为11,100。
- `integrity_check=ok`、`foreign_keys=1`、`foreign_key_check=0`。
- 无孤儿、无效产品、重复台账、重复单号、非法数量或逐单金额差异。
- Rejected款号、费用项和Batch 5迁移数均为0。
- Batch 4复跑dry-run为0/0。
- 后端已恢复并读取正式库，健康接口为11,104/11,169。
- 下一步只做Batch 4只读验收，不得自动执行Batch 5。
## 24. 天华 Batch 4 只读验收（2026-06-20）
- 报告：`docs/migration_reports/TIANHUA_BATCH_4_READONLY_ACCEPTANCE_20260620_133200.md`
- 本轮未执行 Batch 5，未修改数据库，仅执行 GET/API/页面只读验收。
- 健康检查继续指向 `D:\纸箱厂erp软件搭建\data\carton_erp.sqlite3`，返回 `sales_orders/sales_order_items = 11104/11169`。
- `RUIDA-` 历史订单数 = 11100；天华客户 + `RUIDA-` 组合筛选数 = 11100。
- 数据库交叉校验通过：Batch 1/2/3/4/5 订单数 = `100/1000/5000/5000/0`，明细数 = `100/1006/5054/5002/0`。
- `integrity_check=ok`、`foreign_key_check=0`、孤儿明细 0、无效产品 0、重复单号 0、费用项命中 0。
- API 深分页校验通过：第 1 / 10 / 50 / 100 / 150 / 200 / 最后一页均正常；列表接口中位 20.34 ms，详情接口中位 8.02 ms。
- 前端订单页、搜索 `RUIDA-`、客户筛选、组合筛选、详情抽屉和第 2 页翻页正常，浏览器 console 未见 error/warn。
- 当前允许申请 Batch 5 授权，但仍需新的单独授权、停机、锁检查、时点备份和 Batch 5 dry-run。
## 25. 天华主库 Batch 5 正式迁移与最终总对账（2026-06-20）
- 报告：`docs/migration_reports/TIANHUA_MAIN_BATCH_5_APPLY_AND_FINAL_RECON_20260620_135253.md`
- 仅执行 `batch_5`，未重新执行 batch_1/2/3/4，未发生全量滑批。
- 时点备份：`data/backups/carton_erp_before_tianhua_batch_5_apply_20260620_134925.sqlite3`
- Apply 前主库/备份 SHA-256 一致：`9C7E6BF7CAE37E02DD573F328250A53D075FD8E578C28BC6AB204EB029C13BD1`
- Batch 5 dry-run 与 apply 均为 `4489` 张订单、`4489` 条明细，金额 `4,918,380.12`。
- Prefix 明细 `4102`，精确匹配 `387`；rejected 实际选入 `0`，费用项选入 `0`，未匹配选入 `0`。
- 正式表由 `11104/11169` 变为 `15593/15658`；`RUIDA-` 订单变为 `15589`。
- 订单/明细台账分别为 `15589 / 15651`；五批订单数 `100/1000/5000/5000/4489`，五批明细数 `100/1006/5054/5002/4489`。
- `integrity_check=ok`、`foreign_keys=1`、`foreign_key_check=0`；无孤儿、无无效产品、无重复单号、无非正数量。
- Batch 5 逐单明细数差异 `0`、逐单金额差异 `0`；batch_5 legacy / sales 金额合计均为 `4,918,380.12`。
- 第 32、157 行 rejected raw 款号进入正式明细均为 `0`；费用项进入正式明细为 `0`。
- Batch 5 dry-run 复跑 `0/0`，全量剩余 dry-run `0/0`。
- 后端已恢复并读取 `D:\纸箱厂erp软件搭建\data\carton_erp.sqlite3`，健康检查为 `15593/15658`，`GET /api/orders?keyword=RUIDA-` 为 `15589`。
- 当前建议进入“最终只读验收”，不要宣布项目结束；之后还需做最终备份归档。

## 26. 最终只读验收与最终备份归档（2026-06-20）

- 最终只读验收报告：`docs/migration_reports/TIANHUA_FINAL_READONLY_ACCEPTANCE_20260620_142359.md`
- 最终备份归档报告：`docs/migration_reports/TIANHUA_FINAL_BACKUP_ARCHIVE_20260620_145234.md`
- 正式库：`data/carton_erp.sqlite3`
- 最终归档备份：`data/backups/carton_erp_FINAL_AFTER_TIANHUA_MIGRATION_20260620_145234.sqlite3`
- 当前正式表：`sales_orders=15593`、`sales_order_items=15658`
- `RUIDA-` 订单：`15589`
- 迁移台账：订单 `15589` / 明细 `15651`
- `integrity_check=ok`、`foreign_key_check=0`
- 第 32、157 行 rejected raw 款号未进入正式明细，费用项未进入正式明细
- 本轮未执行任何追加迁移写入

## 27. 日常使用前收口检查（2026-06-20）

- 文档目录：`docs/go_live_checklists/`
- 已生成：
  - `PASSWORD_AND_ACCOUNT_SECURITY.md`
  - `RBAC_PERMISSION_CHECK.md`
  - `BACKUP_AND_RESTORE_GUIDE.md`
  - `MANUAL_ACCEPTANCE_CHECKLIST.md`
  - `DAILY_OPERATION_GUIDE.md`
  - `GO_LIVE_READINESS_SUMMARY.md`
- 只读检查结果：
  - 正式库当前仅有 `admin`、`workshop` 两个账号
  - 未发现 `finance`、`sales` 正式账号
  - `workshop` 命中旧式 `123456` SHA256 痕迹，正式使用前必须重置
  - 未登录返回 `401`，伪造 Cookie 返回 `401`，越权访问返回 `403`
  - 手机来料页 HTML 可直接打开，但数据接口仍受登录保护
- 上线前仍需人工确认：
  - 财务 / 销售正式账号创建
  - 财务菜单是否过宽
  - 车间菜单是否过宽
  - 实际使用人员按清单完成一轮人工试用

## 28. 账号、密码与 RBAC 权限收口执行（2026-06-20）

- 报告：`docs/go_live_checklists/ACCOUNT_RBAC_HARDENING_APPLY_20260620_152716.md`
- 本轮未执行任何历史迁移 apply，未修改历史订单、legacy、客户、产品或迁移台账。
- 显式备份：`data/backups/carton_erp_before_account_rbac_hardening_20260620_150305.sqlite3`
- 正式账号现为：`admin / finance / sales / workshop`
- 四个账号均为现代密码哈希，`must_change_password=1`
- `workshop` 旧式弱口令哈希风险已移除；`admin` 已完成密码重置
- 后端 RBAC 已收紧：
  - 财务不可读产品/材质/来料，不可操作送货
  - 销售可读客户，但不可写客户；可操作订单/送货/报料
  - 车间仅保留订单/送货只读与来料访问
- 前端菜单已按 admin / finance / sales / workshop 分离
- 四角色登录验收通过；错误密码、未登录、伪造 Cookie 和越权门禁分别返回 `401 / 401 / 401 / 403`
- 手机来料页面 HTML 壳仍可直接打开，但数据接口未登录返回 `401`
- 回归测试：`80 passed`
- 剩余人工确认项：
  1. 现场管理员需重新设置四个账号的正式交接密码
  2. 财务菜单范围是否还需继续收紧
  3. 车间账号菜单范围是否还需继续收紧
  4. 手机来料页面壳公开是否接受
- 在“现场可交接密码”未设置前，不建议直接进入实际人员试用

## 29. 正式交接密码与人工试用准备（2026-06-20）

- 报告：`docs/go_live_checklists/FINAL_PASSWORD_HANDOFF_AND_TRIAL_READY_20260620_160320.md`
- 本轮未执行历史迁移，未修改历史订单、legacy、客户、产品或迁移台账。
- 已新增本地交互脚本：`scripts/admin/final_password_handoff.py`
  - `getpass` 隐藏输入四个账号正式交接密码
  - 不从命令行参数接收密码
  - 不把密码写入文档、报告、`.env` 或日志
  - 改密后会自动做登录 / 错误密码 / 登出 / 权限复核
- 新单测：`tests/scripts/test_final_password_handoff.py`
- 回归测试已通过：`83 passed`
- 已创建本轮正式改密前备份：
  `data/backups/carton_erp_before_final_password_handoff_20260620_154200.sqlite3`
- 主库 / 备份均为 `integrity_check=ok`、`foreign_key_check=0`，正式订单与历史订单数量未变化：
  - `sales_orders=15593`
  - `sales_order_items=15658`
  - `RUIDA-=15589`
- 本轮阻塞：正式交接密码必须由现场管理员在本机交互窗口输入，当前会话无法代替完成隐藏输入，因此未完成最终正式改密与登录验收。
- 下一步：由现场管理员本机执行交互脚本，完成后再做一次四角色试用验收。

## 30. 现场正式密码交接执行门禁（2026-06-20）

- 报告：`docs/go_live_checklists/ONSITE_PASSWORD_HANDOFF_AND_TRIAL_APPROVAL_20260620_165210.md`
- 本轮未执行历史迁移，未修改历史订单、legacy、客户、产品或迁移台账。
- 已创建新的改密前备份：
  `data/backups/carton_erp_before_onsite_password_handoff_20260620_165210.sqlite3`
- 主库与备份均为 `integrity_check=ok`、`foreign_key_check=0`
- 当前正式数量未变：
  - `sales_orders=15593`
  - `sales_order_items=15658`
  - `RUIDA-=15589`
  - 订单台账 `15589`
  - 明细台账 `15651`
- 回归测试通过：`83 passed`
- 按安全规则，本轮没有改密，因为正式密码必须由现场管理员本人在本机隐藏输入，不能由 Codex 代输，也不能写入参数、文件或日志。
- 已提供现场管理员手动执行命令：
  `.\.venv\Scripts\python.exe .\scripts\admin\final_password_handoff.py --sqlite-path .\data\carton_erp.sqlite3 --api-base-url http://127.0.0.1:8000 --output-json .\docs\go_live_checklists\FINAL_PASSWORD_HANDOFF_RESULT_LOCAL.json`
- 下一步：由现场管理员本机完成交互式改密，再读取本地 JSON 结果生成最终“可进入人工试用”批准报告。

## 31. 现场密码设置后最终复验（2026-06-20）

- 报告：`docs/go_live_checklists/FINAL_ONSITE_PASSWORD_VERIFICATION_AND_TRIAL_READY_20260620_165210.md`
- 本轮未执行历史迁移，未修改历史订单、legacy、客户、产品或迁移台账。
- 正式库继续为：`data/carton_erp.sqlite3`
- 数量保持不变：
  - `sales_orders=15593`
  - `sales_order_items=15658`
  - `RUIDA-=15589`
  - 台账订单/明细 `15589 / 15651`
- 新备份：`data/backups/carton_erp_before_onsite_password_handoff_20260620_165210.sqlite3`
- 主库 / 备份完整性均正常，`foreign_key_check=0`
- 已自动验证：
  - `admin/admin` 失效（401）
  - `workshop/123456` 失效（401）
  - 错误密码失效（401）
  - 未登录 / 伪造 Cookie / 未登录来料 API 门禁正常
- 四账号密码哈希均为 bcrypt (`$2b$`)
- 本轮无法在“不知道新密码明文”的前提下独立完成四个新密码登录复验；需把首次人工登录作为现场验收的一部分。
- 回归测试：`83 passed`
- 当前可进入“实际人员人工试用”，但建议以四角色首次真实登录作为首个试用动作。

## 32. 正式使用层历史订单显示优化与旧系统名称隐藏（2026-06-20）

- 实施报告：`docs/migration_reports/ERP_USABILITY_AND_HISTORY_DISPLAY_OPTIMIZATION_20260620_181607.md`
- 副本 TM 改号演练报告：`docs/migration_reports/HISTORY_ORDER_TM_RENUMBER_REHEARSAL_20260620_181354.md`
- 副本映射清单：`docs/migration_reports/HISTORY_ORDER_TM_RENUMBER_MAPPING_20260620_181354.csv`
- 本轮未执行任何历史迁移 apply，未修改主库历史订单号，未修改 `legacy_ruida_*`。
- 普通业务 API 与前端页面已统一隐藏旧系统名称；历史订单对外统一显示 `TMYYYYMMDD-####`。
- 已改造接口：`orders`、`incoming`、`requisition`、`finance`、`deliveries`。
- 已新增共享服务：`app/services/history_orders.py`。
- 已清理日常用户文档与页面中的旧系统名称；内部技术对象名和历史迁移审计材料暂保留。
- 已完成副本真实改号演练：15,589 条历史订单全部转为 TM 编号，重复 0，`integrity_check=ok`，主库未变化。
- 定向测试通过：`54 passed`。
- 后续若需主库真实批量改号，必须单独授权；当前仅完成显示层隐藏和副本演练。

## 33. 备份保留策略与常规备份清理（2026-06-20）

- 报告：`docs/go_live_checklists/BACKUP_RETENTION_CLEANUP_20260620_194430.md`
- 新增共享模块：`app/core/backup_retention.py`
- 新增管理脚本：`scripts/admin/manage_backups.py`
- `backup_to_nas()` 已接入“最近 5 个常规备份”自动保留策略
- `final_password_handoff.py` 的本地备份也已接入同一策略
- 保护备份关键词：
  - `FINAL`
  - `ARCHIVE`
  - `MIGRATION`
  - `tianhua_batch`
  - `tianhua_sales_apply`
  - `legacy_refresh`
- 本轮未执行历史迁移，未修改历史订单，未修改主库
- 清理前：`.sqlite3` 备份 44 个（常规 33 / 保护 11）
- Dry-run 计划删除常规备份 28 个，预计释放约 5718.50 MB
- Apply 已执行：实际删除 28 个常规备份，保护备份全部保留
- 清理后：`.sqlite3` 备份 16 个（常规 5 / 保护 11）
- 主库仍存在且 `integrity_check=ok`
- 相关测试通过：`15 passed`

## 34. 收款核销兼容修复与 PDF 订单识别草稿（2026-06-21）

- 本轮未执行历史迁移，未修改历史订单，未修改 `legacy_*`，未写主库数据。
- 收款核销兼容修复：
  - 前端 `收款核销` 请求已改为显式提交 `account: ""`。
  - 这样旧版后端若仍要求 `body.account` 字段，也不会再因缺字段报右下角红字。
  - 后端源码仍保持“收款账户可缺省”逻辑。
- 已新增 PDF 订单识别草稿能力：
  - 新服务：`app/services/order_pdf_import.py`
  - 新接口：`POST /api/orders/pdf-preview`
  - 现阶段流程为：上传 PDF → 识别草稿 → 人工确认 → 带入新建订单表单
  - 不会直接写正式订单。
- 前端订单页已新增：
  - `识别PDF订单` 按钮
  - `识别采购订单 PDF` 弹窗
  - `识别预览草稿` / `带入新建订单` 流程
- 实测 3 份样本 PDF 可识别出：
  - 客户名
  - 客户单号（采购单号）
  - 下单日期
  - 交货日期
  - 明细行
- 当前真实样本匹配情况：
  - 客户已可自动归一化匹配（如“有限公司”对“股份有限公司”）
  - 产品主数据尚未按样本存货编码建档，因此当前为“识别成功、产品待人工确认”状态
- 依赖补充：`requirements.txt` 已加入 `pypdf==6.0.0`
- 回归测试：
  `.\.venv\Scripts\python.exe -m pytest tests\test_phase8_finance.py tests\test_phase5_orders.py tests\test_phase14_frontend.py tests\test_phase16_pdf_order_import.py -q`
  结果：`55 passed`
- 运行态说明：
  - 当前 8000 端口旧 Python 进程 PID `25776` 无法由本会话结束（Access denied）
  - 新代码已可在 `127.0.0.1:8002` 正常启动并返回健康检查
  - 若要让 8000 正式页面立即获得 PDF 识别后端能力，需现场手动重启 8000 服务

## 35. 订单未送货统计、流程撤回、历史清理与手机入口（2026-06-22）

- 报告：`docs/go_live_checklists/ORDER_WORKFLOW_ROLLBACK_CLEANUP_AND_MOBILE_ENTRY_20260622_133828.md`
- 订单菜单角标已改为独立使用 `unfinished_total`，只统计真正未送货的日常订单，不再显示全部历史订单数。
- 订单管理默认进入“日常订单”，已送货完成的日常订单仍可查看；“未送货”和“历史订单”可单独筛选。
- 当前真实未送货角标应为 `1`，对应 `TM20260622001`（剩余 100）；不是人为清零。
- 送货单打印“款号”仅显示产品款号，已清除拼接在款号后的总数量、单价、总价、订单日期等污染内容。
- 新建 PDF 订单在报料、入库、送货、收款后仍保留在“日常订单”列表，不再因默认未完成筛选而消失。
- 已新增管理员“撤回到未报料”操作：
  - 必须填写撤回原因。
  - 会清理该订单对应的报料、入库、送货、回单、对账、开票、收款核销链并恢复未报料、未送货、未收款状态。
  - 若送货单或对账单混有其他订单，会拒绝自动撤回，避免误删其他订单业务数据。
  - 操作写入审计日志。
- 已新增手机扫码入口：电脑端业务中心可显示局域网访问地址和二维码，手机登录后可进入车间来料入库。
- “已报料/已入库”默认列表隐藏历史迁移数据；需要审计时可显式使用 `include_history=true` 查询。
- 用户明确授权的数据清理已执行：
  - 删除 2020 年以前正式订单 `6048` 张及明细 `6048` 条。
  - 清理后正式库订单 `9548` 张、明细 `9614` 条。
  - 2020 年以前订单剩余 `0`。
  - 当前“已报料/供应商已排单”明细剩余 `0`。
  - 未修改任何 `legacy_*` 表。
- 清理前保护备份：
  `data/backups/carton_erp_ARCHIVE_before_cleanup_pre2020_20260622_132429.sqlite3`
- 数据库复核：`integrity_check=ok`，`foreign_key_check=0`。
- 新增受控清理脚本：`scripts/admin/cleanup_pre2020_orders.py`，默认 dry-run，正式执行需同时提供 `--apply --confirm DELETE_PRE2020_ORDERS`。
- 定向回归测试：`108 passed`。
- ERP 已重新启动，`http://127.0.0.1:8000/api/health` 返回正常。

## 36. 正式 UI 审查与 Figma 重设计准备（2026-06-27）

- 本轮目标：审查 `http://127.0.0.1:8000/` 当前正式前端，为标准模式、大字模式和手机收料端 Figma 重设计建立证据。
- 审查记录：`docs/ui_audit/2026-06-27-figma-redesign/audit-notes.md`
- 已保存并人工核对运行态截图：
  - `docs/ui_audit/2026-06-27-figma-redesign/01-login.png`
  - `docs/ui_audit/2026-06-27-figma-redesign/02-mobile-incoming-logged-out.png`
- 已确认正式首页存在登录门禁；旧默认密码已经失效，本轮未猜测、读取或代填现场密码。
- 手机来料页在 390×844 视口下出现标题、提示和按钮逐字纵排的严重响应式问题。
- 手机来料页 viewport 当前禁止用户缩放，与老花眼和长辈友好要求冲突。
- 本轮未写入数据库，未执行订单、报料、来料、送货、回单、对账、开票、收款或系统设置操作。
- 下一步：现场用户在已打开的浏览器中手动登录后，继续逐页截图审查；完成证据板后再创建 Figma 设计系统和关键页面方案。

## 37. 正式 UI 完整审查与 Figma 部分交付（2026-06-28）

- 用户已在浏览器中完成管理员登录。
- 已基于真实运行态检查 18 个界面 / 状态：
  - 登录、首页、订单、报料、桌面来料、手机来料
  - 送货回单、财务、客户、产品、材质、系统设置
  - 新建订单、OCR 导入、新增送货单、生成对账单、新增客户
- 截图与审查记录：
  `docs/ui_audit/2026-06-27-figma-redesign/`
- 完整设计说明：
  `docs/product_planning/FIGMA_UI_REDESIGN_20260628.md`
- Figma 文件：
  `https://www.figma.com/design/iyA1wrl2IK7G0zI2OGgnKy`
- Figma 已完成：
  - “00 现状审查”页面
  - 18 张截图上传和逐页问题说明
  - “01 设计系统”页面骨架
  - 标题、设计目标和第一组颜色语义
- 关键发现：
  - 手机来料页 390px 下严重错位，登录态和真实数据下均复现
  - 手机页禁止缩放，与老花眼目标冲突
  - 桌面来料、送货、新建订单仍依赖横向滚动
  - 缺少标准 / 大字模式
  - 状态色不统一，危险操作过度暴露
- Figma Starter 方案已触发 MCP 调用上限，服务端拒绝继续写入；现有节点已保留。
- 本轮未写入数据库，未执行任何业务状态操作。
- 下一步：Figma 调用额度恢复或方案升级后，继续完成标准 / 大字组件对照和首页、订单、送货、财务、手机收料高保真画板。

## 38. Figma 额度阻塞确认（2026-06-28）
- 当前 Figma 文件 `iyA1wrl2IK7G0zI2OGgnKy` 仍可保留，但 MCP 写入 / 读取调用已经触发 Starter 方案上限。
- 明确报错：`You've reached the Figma MCP tool call limit on the Starter plan.`
- 受影响的后续动作：
  - `get_metadata`
  - `use_figma`
  - 页面补全
  - 组件对照
  - 截图验证
- 现在可继续做的事情：
  - 维护本地设计说明 `docs/product_planning/FIGMA_UI_REDESIGN_20260628.md`
  - 维护审查记录 `docs/ui_audit/2026-06-27-figma-redesign/`
  - 等额度恢复后，回到同一个 Figma 文件继续补完未完成页面
