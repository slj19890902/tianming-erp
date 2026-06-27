# 纸箱厂 ERP / 天明包装开票系统真实现状体检与产品路线图

审计时间：2026-06-27 14:49:32  
审计性质：只读  
代码修改：否  
数据库修改：否  
配置修改：否  
历史数据修改：否

## 0. 路径与审计边界

- Codex 当前工作区 `D:\360MoveData\Users\Administrator\Documents\新erp交接搭建` 是空目录。
- 同机可用且包含最新提交的实际项目目录为 `D:\纸箱厂erp软件搭建`。
- 本报告只基于 `D:\纸箱厂erp软件搭建` 的当前代码、数据库结构、测试和 Git 状态。
- 当前 Git 分支：`factory-current-baseline`。
- 当前工作树：干净。
- 当前提交：`c61dc61 v0.19.2-B Phase 3: 供应商报料单完整流程`。
- 本轮没有启动正式服务，没有执行写接口，没有运行迁移。
- 浏览器无法访问本机 8000 端口，且当前 8000 端口没有服务监听。因此 UI 结论基于当前生效模板、交互代码和测试，不声称完成了运行时截图验收。

## 第一部分：当前 ERP 项目真实现状摘要

## 1. 技术栈

### 1.1 当前正式后端

- Python 3.12
- FastAPI 0.115.6
- SQLAlchemy 2.0.50
- Alembic 1.18.4
- SQLite 正式库：`data/carton_erp.sqlite3`
- Pydantic 数据校验
- bcrypt 密码哈希
- JWT 会话 Cookie
- OpenPyXL / Pandas：Excel 导入导出
- ReportLab：PDF/打印相关
- PyPDF：客户 PDF 订单识别
- PyMuPDF + EasyOCR：可选 OCR，未安装时降级

### 1.2 当前正式前端

- 当前真正由 FastAPI 提供的是 `static/index.html`。
- Vue 3 全局构建、Axios、pinyin-pro 均从公网 CDN 加载。
- 主页面是单文件 Vue 模板，约 5,190 行、360 KB。
- `static/customers.html` 是另一套约 1,661 行的客户页。
- `static/incoming.html` 是独立手机来料页，使用 Tailwind CDN。
- `static/delivery-print.html`、`static/requisition-print.html` 为打印页。

### 1.3 并存但未接管正式入口的前端

`tm_frontend/` 是 Vue 3 + TypeScript + Vite + Element Plus + VXE Table 的独立前端工程，包含：

- DashboardView
- OrderView
- RequisitionView
- DeliveryView
- StatementView
- MasterDataView
- MobileReceiveView
- PrintDemo
- ScanDemo

当前 `app/main.py` 不提供其构建产物，正式入口仍是 `static/index.html`。因此目前存在“两套前端并存、只有静态单页正式使用”的架构分叉。

## 2. 目录真实情况

当前项目没有用户提示中预期的 `backend/`、`frontend/`、`templates/` 目录。

实际主要目录：

- `app/`：模块化后端、模型、服务
- `static/`：当前正式页面
- `tm_frontend/`：未接管正式入口的 Vue/Vite 前端
- `alembic/versions/`：数据库迁移
- `data/`：正式 SQLite 和 PDF 训练样本
- `tests/`：752 个测试用例
- `scripts/`：管理、迁移、导入、备份脚本
- `docs/`：迁移、上线、交接和业务文档
- `phase1_postgres/`：PostgreSQL 方向的早期基础代码

## 3. 当前页面清单

| 页面 | 当前入口 | 当前状态 |
|---|---|---|
| 登录 | `static/index.html` | 已有账号、强制改密、错误提示 |
| 首页仪表盘 | 主单页 `dashboard` | 已有月收入、毛利、应收、今日待送/待收；指标不完整 |
| 客户管理 | 主单页 + `customers.html` | 已有分页、搜索、启停、开票资料；存在双页面实现 |
| 常用箱与材质 | 主单页 `products` | 已有客户主从、产品、材质、价格、图纸、报料尺寸 |
| 订单管理 | 主单页 `orders` | 已有客户单号分组、展开明细、状态、OCR、新建编辑 |
| 报料管理 | 主单页 `requisition` | 已有待报料、已报料、合并建议、供应商报料单 |
| 桌面来料入库 | 主单页 `incoming` | 已有列表、入库、撤回 |
| 手机来料入库 | `incoming.html` | 已有大按钮、待收/今日已收、详情、撤回 |
| 送货与回单 | 主单页 `deliveries` | 已有新增、确认发货、打印、回单；缺编辑/取消发货 |
| 对账开票收款 | 主单页 `finance` | 已有对账、导出、开票、收款核销 |
| 系统设置 | 主单页 `system` | 已有用户密码、备份、材料映射、PDF 训练 |
| 送货单打印 | `delivery-print.html` | 已有打印模板，但公司信息硬编码 |
| 报料单打印 | `requisition-print.html` | 已有打印模板 |

## 4. 当前模块化 API 清单

模块化 API 共 130 个路由。主要能力如下：

### 4.1 认证与权限（6）

- `POST /api/auth/login`
- `POST /api/auth/logout`
- `GET /api/auth/me`
- `PUT /api/auth/password`
- `GET /api/auth/users`
- `PUT /api/auth/users/{username}/reset-password`

### 4.2 客户（6）

- 客户分页、详情、新增、修改、启停、删除

### 4.3 产品与常用箱（14）

- 产品分页、详情、新增、修改、启停
- 图纸上传、版本删除
- 垃圾站、恢复、彻底清理
- 订单字段回写常用箱

### 4.4 材质与成本（16）

- 材质分页、详情、增删改
- 批量调价预览/应用/历史
- 纸板成本参考
- 楞型加价规则
- 材质比价

### 4.5 订单（16）

- 订单分页、详情、新增、修改、删除
- 明细修改、删除
- 主单/明细编号预览
- OCR 单文件/多文件预览
- 草稿重新匹配、成本预览
- 图纸上传
- 状态变更、流程撤回

### 4.6 报料（13）

- 待报料、已报料、批次创建、编辑、取消
- 历史报料搜索、合并建议、打印
- 供应商报料单新增、列表、详情、作废

### 4.7 来料（6）

- 待收料、今日已收、历史
- 手机入口二维码
- 入库、撤回

### 4.8 送货（8）

- 待送货明细、送货单列表/详情/打印
- 创建送货单
- 确认发货
- 标记打印
- 尾数强制结案

### 4.9 财务（10）

- 回单新增/编辑/详情
- 待对账
- 对账单新增/列表/导出
- 开票新增/列表
- 收款核销

### 4.10 首页、定价、系统、PDF 训练（35）

- 首页 KPI
- 纸箱报价计算
- 备份创建、恢复、清理、删除
- 材质映射
- PDF 训练批次、样本、模板、纠错、评分、OCR 状态

此外仍保留顶层 `main.py` 的兼容接口。`app/main.py` 通过“绞杀者模式”复用旧 `main.py` 的 FastAPI 实例，再替换部分路由。这是当前最重要的架构风险之一。

## 5. 数据库表清单与现状

正式库共 66 张非 SQLite 内置表，Alembic 当前版本为 `s57m1p9q6r39`，与代码迁移头一致。

### 5.1 当前主业务表

- 客户/产品/材质：`customers`、`products`、`materials`
- 订单：`sales_orders`、`sales_order_items`
- 报料：`material_requisitions`、`material_requisition_items`
- 供应商报料单：`supplier_requisition_orders`、`supplier_requisition_order_items`
- 送货：`sales_deliveries`、`sales_delivery_items`
- 回单：`finance_return_receipts`、`finance_return_receipt_items`
- 对账：`finance_statements`、`finance_statement_items`
- 开票/收款：`finance_invoices`、`finance_settlement_records`
- 用户/日志：`users`、`operation_logs`
- 图纸：`product_drawings`
- PDF 训练：`pdf_order_training_batches`、`pdf_order_training_samples`、`pdf_order_customer_templates`、`pdf_order_correction_logs`

### 5.2 序列与规则表

- `order_daily_sequences`
- `order_item_number_sequences`
- `delivery_daily_sequences`
- `requisition_daily_sequences`
- `statement_monthly_sequences`
- `supplier_flute_price_rules`
- `material_price_history`
- `material_price_adjustment_batches`

### 5.3 历史迁移与兼容表

- 旧系统原始层三表
- `migration_ruida_sales_order_map`
- `migration_ruida_sales_item_map`
- `migration_entity_map`
- `historical_requisition_maps`
- `product_archives`
- `material_code_mapping_candidates`

这些属于内部追溯，后续不能因 UI 改造而删除或改名。

### 5.4 旧版/空表并存

库中仍有 `orders`、`deliveries`、`statements`、`invoices`、`material_reports`、`delivery_items`、`statement_items` 等旧版或空表，与正式 `sales_*`、`finance_*` 表并存。后续开发必须明确正式写入目标，禁止误接旧表。

### 5.5 当前核心数量

- `sales_orders`：15,594
- `sales_order_items`：15,660
- `customers`：132
- `products`：3,316
- `materials`：46
- `sales_deliveries`：4
- `sales_delivery_items`：7
- `finance_statements`：3
- `users`：4
- `integrity_check`：`ok`
- `foreign_key_check`：0

注意：这些是 2026-06-27 当前库的只读结果，与部分旧交接文档中的 9,548/9,614 数量不一致。后续执行必须以当前库再次核对为准。

## 6. 纸板长宽接入核查结论

结论：**当前已存在，并且已经接入多个业务层，但尚未覆盖所有展示和未来库存流程。**

### 6.1 已存在

产品资料：

- `products.default_cardboard_length`
- `products.default_cardboard_width`
- `products.report_length_mm`
- `products.report_width_mm`
- 压线类型和三段压线尺寸

订单快照：

- `sales_order_items.cardboard_len`
- `sales_order_items.cardboard_width`
- `snapshot_report_length_mm`
- `snapshot_report_width_mm`
- 压线快照字段

报料：

- `material_requisition_items.cardboard_len`
- `material_requisition_items.cardboard_width`
- 报料接口读取、修改、打印和合并

供应商报料单：

- `supplier_requisition_orders.report_length_mm`
- `supplier_requisition_orders.report_width_mm`
- 压线字段

前端：

- 常用箱编辑表单可维护报料长宽和压线
- 新建订单会固化产品报料字段快照
- 报料待办/已报料列表显示尺寸
- 桌面来料和手机来料显示纸板尺寸

### 6.2 未完整接入

- 常用箱默认列表没有直接显示纸板长宽和压线，只在编辑弹窗中可见。
- 手机待收料默认卡片显示字段仍偏多，且没有显示供应商。
- 来料详情没有完整展示压线尺寸。
- 送货、标签、生产单、库存栈板尚未使用这些字段。
- 没有 2500mm 超长警告。
- 没有单双拼自动建议算法，只有人工选择“大做小/双拼/多拼”。
- `default_cardboard_*`、`report_*`、订单 `cardboard_*` 三组字段语义接近，后续必须定义唯一来源和快照规则。

## 7. 送货单状态流真实现状

### 7.1 当前代码

`sales_deliveries.status` 只允许：

- `pending`
- `dispatched`

当前流程：

1. 新建送货单，保存为 `pending`
2. 点击确认发货，变为 `dispatched`
3. 确认发货时累计订单明细 `delivered_quantity`
4. 可标记已打印
5. 财务创建回单
6. 回单明细进入对账

### 7.2 当前缺口

- 没有送货单编辑接口。
- 没有待发货送货单删除接口。
- 没有取消发货接口。
- 没有确认发货后的数量回滚逻辑。
- 没有取消发货后的订单状态重算入口。
- 没有统一的“已签收/已完成/已对账”送货状态；这些状态分散在回单和对账表。
- 前端待发货行只有“确认发货并打印”，没有“编辑/删除”。
- 已发货行只有“确认回单/补打”，没有“取消发货”。

### 7.3 库存恢复逻辑

当前没有正式库存台账、库存批次、库存占用或库存流水，因此不存在可复用的“库存恢复”实现。

取消发货至少需要：

- 将每条 `sales_delivery_items.delivered_quantity` 从订单明细已送数量中扣回
- 重算订单状态
- 清除发货人、发货时间、打印状态
- 写操作日志
- 已有回单时禁止普通取消
- 已进入对账时禁止取消，必须管理员按“对账反审核 → 回单撤销 → 取消发货”逆序处理

推荐不要在多个表重复存“已签收/已对账”。送货单保存操作状态，回单和对账状态由关联表推导，前端组合成统一状态时间轴。

## 8. 当前已有功能

- 四角色登录和 RBAC：管理员、财务、业务、车间
- 多明细订单、主系统单号和明细系统单号
- 客户单号分组
- 客户、产品、材质主数据
- 产品图纸版本
- PDF/OCR 订单草稿、批量识别、重新匹配、去重
- 纸板成本参考和材质比价
- 报料、合并报料、供应商报料单
- 来料入库、撤回
- 送货、打印、回单
- 月结对账、开票、收款
- 首页基础 KPI
- 备份、恢复、保留策略
- 操作日志
- 手机来料入口和二维码
- 历史订单日常显示保护

## 9. 缺失或仅有占位的功能

### 9.1 P0 缺失

- 送货单保存后编辑
- 待发货删除
- 取消发货和数量回滚
- 已回单/已对账修改保护的完整逆向流程
- 断网可用的本地前端依赖

### 9.2 P1 缺失

- 全局标准/大字模式切换
- 统一可访问性字号、按钮、表格行高
- 公司信息维护 API/UI
- 对账导出客户缩写 + 月份命名
- 对账导出材质、开票状态、对账状态
- 2500mm 超长提醒
- 手机来料“简洁列表 → 展开详情”的字段收敛

### 9.3 P2 缺失

- 生产单
- 标签打印
- 独立仓库模块
- 片料/成品库存
- 库位、栈板、库龄、损耗
- 库存流水和占用
- 库存推荐和人工抵扣
- 通用二维码扫码管理
- 仓库/送货/只读专属角色

## 10. 当前 UI 问题

### 10.1 年龄友好度不足

- 主页面基础字号 13px。
- 小按钮高度 26px，普通按钮 32px。
- 表格和备注中大量 11px–12px 灰字。
- 没有大字模式。
- 主页面强制最小宽度 1180px。
- 状态信息密度高，弹窗内仍有密集大表格。

这些与“60 岁用户、老花眼、车间光线不稳定”的目标明显冲突。

### 10.2 视觉与交互不统一

- 主单页采用自定义 Apple 风格。
- 手机来料采用 Tailwind 风格。
- 客户页又是一套独立样式。
- `tm_frontend` 使用 Element Plus/VXE，但没有正式接管。
- 同一业务在多个页面或旧模板中重复存在。

### 10.3 离线风险

- Vue、Axios、pinyin-pro、Tailwind 都依赖公网 CDN。
- 工厂网络中断或 CDN 被拦截时，页面可能白屏或样式失效。
- 正式工厂系统应改为本地静态资源或构建产物。

### 10.4 手机可访问性

- 手机来料按钮和字号基本达标。
- 但 `maximum-scale=1.0, user-scalable=no` 禁止用户放大，不适合老花眼用户。
- 默认卡片展示客户、编码、产品、材质等较多信息，不符合“默认只看尺寸/数量/供应商/到货”的目标。

## 11. 高风险改动点

1. `app/main.py` 复用旧 `main.py` 实例并动态替换路由，新增路由时容易出现旧接口覆盖新接口。
2. 正式前端是 5,190 行单文件，任何大面积 UI 改造都容易产生跨模块回归。
3. `tm_frontend` 和 `static` 双轨，必须先决定迁移策略，不能两套同时扩展。
4. 数据库有正式表和旧表并存，新增功能必须明确写 `sales_*`/`finance_*`。
5. 历史迁移表和原始层必须只读保护。
6. 送货取消涉及订单已送数量、回单、对账和操作日志，不能只改状态字符串。
7. 库存抵扣当前只是人工填写数量，没有库存来源、批次、占用和流水，不能冒充真实库存。
8. 公司名称目前部分硬编码在打印页，直接加设置页但不改打印数据源会造成不一致。
9. 当前版本文件仍写 `v0.19.1`，Git 最新提交和迁移已到 `v0.19.2-B`，版本元数据存在漂移。
10. 正式 `.venv` 未安装 pytest；系统 Python 可收集 752 个测试。全量测试 3 分钟未结束，定向 57 个报料/送货/前端测试通过。

## 12. 产品方案选择

### 方案 A：继续扩展单文件静态前端

优点：最快、对现有部署影响最小。  
缺点：5,000 多行单文件继续膨胀，状态和样式越来越难维护。

### 方案 B：一次性切换 `tm_frontend`

优点：长期结构清晰，组件化、表格能力强。  
缺点：当前功能很多，重写风险极高，不符合“不要一次性大改”。

### 方案 C：推荐——后端先保持，前端分页面渐进迁移

1. 保持现有 FastAPI 和 SQLite。
2. 先修送货状态流和业务数据一致性。
3. 建立统一 UI token、大字模式和组件规范。
4. 以页面为单位从 `static/index.html` 拆出，优先订单、送货、手机来料。
5. 每迁移一页，旧页保持可回退，验收后再切入口。

这是当前风险最低、最适合工厂持续使用的路线。

## 第二部分：Claude Design 提示词

完整可复制提示词见：

`docs/product_planning/CLAUDE_DESIGN_UI_PROMPT_20260627_144932.md`

## 第三部分：Claude Code 分阶段执行计划

完整执行计划见：

`docs/product_planning/CLAUDE_CODE_PHASED_EXECUTION_PLAN_20260627_144932.md`

## 第四部分：第一阶段建议从哪里开始

## 13. 第一实施阶段：送货单状态流闭环

建议先从“送货管理”开始，而不是先全局换皮。

理由：

1. 这是用户已经明确反馈的真实业务阻塞。
2. 当前 API 明确缺少编辑、删除、取消发货和回滚。
3. 它关系订单待送数量、回单和对账，数据风险高于视觉问题。
4. 边界相对清楚，可形成独立可验收闭环。
5. 先稳定状态机，后续 UI 才有可靠状态可展示。

实施范围：

- 待发货送货单编辑
- 待发货删除
- 确认发货
- 取消发货
- 数量和订单状态回滚
- 回单/对账门禁
- 操作日志
- 前端按钮与状态时间轴

主要风险：

- 重复扣减或重复回滚已送数量
- 混合订单送货单误影响其他订单
- 已回单/已对账数据被错误撤销
- 并发确认发货

验收：

1. 待发货可编辑、可删除。
2. 已发货默认只读。
3. 无回单时可取消发货，订单已送数量精确恢复。
4. 有回单时普通用户不能取消。
5. 已对账时必须阻断。
6. 取消后订单重新进入待送范围。
7. 所有关键操作有日志。
8. 定向测试、全量测试、数据库完整性均通过。
9. 主库实施前先在副本演练。

随后第二阶段再建立全局大字模式与统一 UI token，并优先应用到订单、送货、手机来料三个高频页面。

