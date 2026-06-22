# 现有代码分析与系统合并计划书

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在保留当前纸箱厂 ERP 历史数据、客户档案和完整业务链路的前提下，将蓝图中的 SQLAlchemy 核心模型、RBAC、智能算价、订单明细事务、WMS 扫码入库、行业送货单打印和 NAS 备份能力融合为一套可正式使用的局域网 ERP。

**Architecture:** 以当前项目为代码和历史数据基座，采用“模块化单体”重构，不另起一套互不相通的系统。FastAPI 只运行一个后端实例，SQLAlchemy 管理 SQLite 事务；浏览器和手机只通过 API 访问，不能直接打开数据库。核心六表负责客户、产品、材质、订单和用户主数据，现有送货、回单、对账、开票、附件、日志等表作为业务扩展表保留。

**Tech Stack:** Python 3.12、FastAPI、SQLAlchemy 2.x、Alembic、Pydantic、SQLite、原生 HTML/JavaScript、Vue 3 CDN、Tailwind CSS CDN、pytest、FastAPI TestClient、qrcode、openpyxl、ReportLab。

---

## 1. 业务闭环理解

本系统服务三级纸箱厂，核心不是复杂库存核算，而是订单状态和单据链可追溯：

```text
客户/产品档案
  -> 报价与算价
  -> 客户订单（主表 + 多明细）
  -> 报料/采购纸板
  -> 纸板到厂扫码入库
  -> 生产/成品待送
  -> 送货单打印与发货
  -> 客户回单确认
  -> 月结对账
  -> 开票
  -> 收款/结清
```

关键控制点：

1. 订单主表和全部明细必须同事务提交，任意明细失败则整体回滚。
2. 来料状态属于订单明细，不与复杂仓库库存强绑定。
3. 未到料不能进入可生产/可送货，未送货不能回单，未回单不能正式对账。
4. 送货单对外打印不显示单价、金额和成本。
5. workshop 不能读取销售价格、成本和财务接口。
6. 每次关键状态变更必须记录操作者、时间、前后状态和撤回原因。

## 2. 当前项目代码审计

### 2.1 当前结构

```text
D:\纸箱厂erp软件搭建
├─ main.py                         # 2,118 行 FastAPI + sqlite3 + 建表 + API
├─ static/
│  ├─ index.html                   # 3,696 行 Vue CDN 单页演示界面
│  └─ customers.html               # 1,500+ 行真实客户管理页面
├─ data/carton_erp.sqlite3         # 当前本地数据库，约 202 MB
├─ migration-reports/              # 瑞达旧 ERP 迁移结果和分析
├─ handoff/                        # 部署与后续任务记录
└─ requirements.txt
```

NAS 上另有：

```text
Z:\sata1-18015598002\BoxERP\erp.db
```

该库是另一套轻量模型，包含 128 个客户、46 个材质、1 个产品、0 个订单。它适合作为蓝图模型和部分基础数据来源，不适合作为覆盖当前数据库的目标文件。

### 2.2 已有能力，应保留

1. 客户管理已有真实分页、搜索、新增、编辑、启停和关联产品查询。
2. `product_archives` 已保存 3,317 条客户款号档案，是下单复用的核心资产。
3. 已迁入 39,766 张历史订单和 40,293 条历史订单明细。
4. 已有订单历史差异预警，可比较材质、楞型、长宽高和历史单价。
5. 已有送货、回单、对账、开票、操作日志和状态事件的数据库表结构。
6. 已有局域网启动地址和终端二维码输出能力。
7. 当前数据库与 BoxERP 数据库的 `PRAGMA integrity_check` 均为 `ok`。

### 2.3 当前主要缺口

#### 后端架构

- 仍直接使用 `sqlite3`，没有 SQLAlchemy Session、Repository 或 Service 边界。
- 数据库路径固定为 `data/carton_erp.sqlite3`，没有读取 NAS 配置。
- 建表 SQL、迁移、业务查询和 API 全部集中在 `main.py`。
- 没有 Alembic 正式迁移链，只有运行时 `ALTER TABLE ADD COLUMN`。
- CORS 为 `*` 且允许凭据，不符合局域网最小暴露原则。

#### 权限与安全

- 用户表只允许 `boss/workshop`，与蓝图的 `admin/finance/sales/workshop` 冲突。
- 虽有 roles、permissions、role_permissions 表，但 `role_permissions` 当前为空。
- 登录接口只返回用户信息，不签发会话或令牌。
- 所有业务 API 都可以绕过登录直接调用。
- 前端在 API 登录失败且密码为 `123456` 时会降级为“模拟登录”，必须删除。
- 密码使用单次 SHA-256，不带盐，应改为可靠密码哈希。
- 默认密码在每次启动时被重新写回 `123456`，存在严重风险。

#### 数据模型

- 当前 `orders` 一行就是一个产品，没有规范的订单主表 + `order_items`。
- 当前订单号为 `SO + 毫秒时间戳`，不符合 `PO-年月日-3位流水号`。
- Customer 只对名称做防重，客户编号没有唯一约束，也没有简称字段。
- Product 仍叫 `product_archives`，缺少明确箱型、普通/模切分类、印刷内容和展开尺寸。
- 没有独立 Material 主数据表；材质散落在产品和订单字符串字段中。
- 当前税率在订单创建时固定按 13%反算，没有使用客户默认税率。

#### 真实业务实现

- 首页、产品、报价、报料、库存、送货、回单、对账、开票和设置主要使用前端 seed 模拟数据。
- 后端目前只有健康检查、登录、客户、档案搜索和订单创建等 15 个 API。
- 报价和送货没有真实 API；打印只是调用整个页面的 `window.print()`。
- 设置页的权限、备份和恢复按钮都是模拟操作。
- 没有 `incoming.html`，没有移动端来料 API 和撤回防呆。
- 没有自动备份服务、备份清单、校验、保留策略和恢复演练接口。

#### 测试与可维护性

- 当前没有 `tests/`。
- `main.py` 虽能通过 `py_compile`，但业务规则没有自动化测试。
- `index.html` 超过 3,600 行，状态、API、模板和模拟数据混在一起。
- 主页面设置 `min-width: 1180px`，不能直接承担手机扫码入库页面。

## 3. 合并原则与目标数据模型

### 3.1 合并原则

1. 不覆盖当前 202 MB 数据库，不直接用 BoxERP 的 258 KB 数据库替换。
2. 先复制数据库到测试环境完成迁移，验证后才切换正式路径。
3. 当前历史迁移表保持只读，不强行塞入新业务表。
4. 当前 `product_archives` 原地迁移为 Product 能力，保留旧 ID 映射。
5. 当前一产品一订单迁移为“一张订单 + 一条订单明细”，未来支持多明细。
6. 保留回单、对账、开票、附件和状态日志等扩展表，不把系统削减成只有六张表。
7. BoxERP 的客户和材质仅做去重合并，产生迁移报告，不静默覆盖当前值。

### 3.2 目标核心表

#### users

- `id`
- `username`，唯一
- `password_hash`
- `role`：`admin/finance/sales/workshop`
- `real_name`
- `is_active`
- `must_change_password`
- `created_at/updated_at`

#### customers

- 保留当前整数 `id` 作为内部主键。
- `customer_number`：独立排序数字，唯一。
- `customer_code`：客户缩写/业务代码，唯一，不区分大小写。
- `name`：客户全称，规范化后唯一。
- `payment_term_days`
- `credit_limit`
- 联系人、电话、默认地址、税率、开票资料、状态。

#### materials

- `id`
- `code`：如 `K=A-BC`，唯一。
- `paper_composition`
- `layer_count`
- `flute_type`
- `basis_weight_description`
- 供应商报价、计价单位、报价日期、启用状态。

#### products

- `id`
- `customer_id`
- `product_code`
- `customer_material_code`
- `product_name`
- `material_id`
- `length_mm/width_mm/height_mm`
- `box_category`：`normal/die_cut`
- `box_style`
- `print_content/printing_colors`
- `production_process`
- `die_cut_length_mm/die_cut_width_mm`
- 销售价、成本价、建议价、图纸路径、状态。

唯一约束：

```text
(customer_id, customer_material_code)
(customer_id, product_code)
```

#### orders

- `id`
- `order_number`：`PO-YYYYMMDD-001`
- `customer_id`
- `customer_po`
- `order_date`
- `delivery_date`
- `status`
- `payment_status`
- `total_amount`
- `remarks`
- `created_by/created_at/updated_at`

#### order_items

- `id`
- `order_id`
- `product_id`
- 数量、单价、小计。
- `material_status`：`pending/received`
- `material_received_at`
- `material_received_by`
- `material_reverted_at`
- 订单快照字段：客户款号、品名、规格、材质、箱型、工艺。

快照字段用于防止后续修改产品档案导致历史订单内容变化。

### 3.3 保留的扩展表

- `quotations/quotation_items`
- `deliveries/delivery_items`
- `return_confirmations`
- `statements/statement_items`
- `invoices`
- `attachments`
- `operation_logs`
- `order_status_events`
- `system_settings`
- `database_backups`
- `legacy_*`

## 4. SQLite 与 NAS 的最优安全方案

SQLite 不应被多台电脑通过 SMB/FUSE 直接同时打开。当前 `Z:` 是 `FUSE-rclone`，BoxERP 数据库还启用了 WAL；这类组合存在锁失效和损坏风险。

目标部署：

```text
办公室电脑/手机
       |
       | HTTP（局域网）
       v
唯一 FastAPI 服务
       |
       +-- 单进程写 SQLite
       |
       +-- SQLite Online Backup
               |
               v
        NAS 时间戳备份目录
```

配置：

```text
ERP_DATABASE_PATH       # 可配置正式数据库路径
ERP_BACKUP_DIR          # NAS 备份目录
ERP_ALLOWED_ORIGINS     # 允许访问的局域网来源
ERP_BIND_HOST
ERP_PORT
ERP_SESSION_SECRET_FILE
```

安全策略：

1. 支持将正式数据库映射到 NAS 路径，但只允许一个 FastAPI 实例持有数据库。
2. 如果检测到网络盘/FUSE，使用 `journal_mode=DELETE`、`busy_timeout` 和单写进程，不使用 WAL。
3. 推荐生产模式是服务端本地 SQLite + NAS 热备份，避免网络文件系统锁风险。
4. 备份使用 SQLite `Connection.backup()` 生成一致性快照，不能运行中直接复制 `.db`。
5. 文件名：`carton_erp_YYYYMMDD_HHMMSS.sqlite3`。
6. 每次备份后运行 `PRAGMA integrity_check` 并写入 SHA-256、大小、表记录数和创建人。
7. 保留策略：最近 24 个小时备份、30 个日备份、12 个周/月备份。
8. 恢复前自动备份当前库；恢复只允许 admin 操作并要求二次确认。

## 5. 文件重构目标

```text
app/
├─ main.py
├─ core/
│  ├─ config.py
│  ├─ database.py
│  ├─ security.py
│  └─ permissions.py
├─ models/
│  ├─ user.py
│  ├─ customer.py
│  ├─ material.py
│  ├─ product.py
│  ├─ order.py
│  ├─ quotation.py
│  ├─ delivery.py
│  └─ workflow.py
├─ schemas/
│  ├─ auth.py
│  ├─ customer.py
│  ├─ material.py
│  ├─ product.py
│  ├─ order.py
│  └─ delivery.py
├─ services/
│  ├─ pricing.py
│  ├─ order_service.py
│  ├─ incoming_service.py
│  ├─ delivery_service.py
│  └─ backup_service.py
├─ api/
│  ├─ auth.py
│  ├─ customers.py
│  ├─ materials.py
│  ├─ products.py
│  ├─ quotations.py
│  ├─ orders.py
│  ├─ incoming.py
│  ├─ deliveries.py
│  └─ settings.py
└─ migrations/

static/
├─ index.html
├─ incoming.html
├─ delivery-print.html
├─ quotation-print.html
├─ js/
│  ├─ api.js
│  ├─ auth.js
│  ├─ app.js
│  ├─ orders.js
│  ├─ incoming.js
│  └─ deliveries.js
└─ css/
   ├─ app.css
   └─ print.css

tests/
├─ conftest.py
├─ test_auth.py
├─ test_customers.py
├─ test_pricing.py
├─ test_orders.py
├─ test_incoming.py
├─ test_deliveries.py
├─ test_backup.py
└─ test_migrations.py
```

重构采用绞杀式迁移：旧 `main.py` 接口保持可运行，新模块逐个接管路由；每完成一个模块再删除对应旧代码，不一次性推倒重写。

## 6. 分阶段实施顺序

### Phase 0：冻结、备份和基线测试

**目的：** 在任何结构迁移前建立可恢复基线。

- [ ] 记录当前本地库、NAS 旧库和 BoxERP 库的 SHA-256、大小、表结构、记录数和 `integrity_check`。
- [ ] 用 SQLite Online Backup 生成三份只读基线副本。
- [ ] 建立 pytest 基础设施和临时数据库 fixture。
- [ ] 为当前客户列表、客户保存、订单创建和历史差异预警补回归测试。
- [ ] 记录当前 API 响应契约，确保模块拆分后不破坏客户页面。

**验收：**

- 所有备份可打开，完整性检查为 `ok`。
- 当前已实现接口都有最小回归测试。
- 测试绝不读写正式数据库。

### Phase 1：配置、SQLAlchemy 与 Alembic 基座

**目的：** 先解决数据访问和迁移可靠性。

- [ ] 新增 `app/core/config.py`，读取数据库、备份、绑定地址、端口和允许来源。
- [ ] 新增 SQLAlchemy Engine 和按请求创建的 Session。
- [ ] SQLite 连接统一设置 foreign keys、busy timeout 和网络盘日志模式。
- [ ] 建立 Alembic，先反映现有表，不重建或丢弃历史表。
- [ ] 将健康检查扩展为数据库可写性、备份目录可写性和迁移版本检查。
- [ ] 保留旧 sqlite3 读取函数作为临时兼容层，禁止新增业务继续使用它。

**验收：**

- 本地临时库、复制的当前库、NAS 映射测试库均可启动。
- Alembic upgrade/downgrade 在测试副本上可重复执行。
- 旧客户管理页面仍能使用。

### Phase 2：认证、RBAC 和审计

**目的：** 先建立所有后续模块共同依赖的安全边界。

- [ ] 迁移用户角色到 `admin/finance/sales/workshop`。
- [ ] 使用带盐密码哈希，首次迁移保留账号但要求改密。
- [ ] 实现 HttpOnly、SameSite 会话 Cookie，提供登录、退出、当前用户接口。
- [ ] 为每个 API 增加 `require_role` 或 `require_permission` 依赖。
- [ ] 前端菜单按权限过滤，按钮按权限禁用，但以后端校验为最终依据。
- [ ] 删除前端模拟登录和硬编码默认密码降级。
- [ ] 关键增删改、打印、导出、状态变更、撤回和恢复都写操作日志。

角色边界：

| 模块 | admin | finance | sales | workshop |
| --- | --- | --- | --- | --- |
| 客户/产品 | 全部 | 查看 | 维护 | 只读必要字段 |
| 报价/订单 | 全部 | 查看金额 | 维护 | 不看价格 |
| 来料/WMS | 全部 | 查看 | 查看 | 入库/撤回 |
| 送货 | 全部 | 查看 | 创建 | 查看待送 |
| 对账/开票/收款 | 全部 | 维护 | 只读状态 | 无权限 |
| 用户/备份 | 全部 | 无 | 无 | 无 |

**验收：**

- 未登录访问业务 API 返回 401。
- workshop 请求价格或财务接口返回 403。
- 前端隐藏菜单不能替代后端权限校验。

### Phase 3：核心主数据合并

**目的：** 合并当前数据库与 BoxERP 的客户、材质、产品模型。

- [ ] 为 customers 增加客户序号、缩写、账期天数和信用额度。
- [ ] 建立规范化名称字段，对编号、缩写、名称分别建立唯一索引。
- [ ] 新建 materials 表，导入 BoxERP 的 46 条材质。
- [ ] 将 `product_archives` 数据迁移或映射到 products，不删除原表。
- [ ] 增加普通箱/模切箱、展开尺寸、印刷内容和生产工艺字段。
- [ ] 生成客户合并报告：当前 132 条与 BoxERP 128 条按名称、电话、地址匹配。
- [ ] 冲突数据进入人工确认清单，禁止自动覆盖客户编号、账期和地址。
- [ ] 建立 `migration_entity_map`，记录旧表 ID、BoxERP 键和新表 ID。

**验收：**

- 当前 3,317 条产品档案都有迁移结果或明确异常原因。
- 客户、材质和产品的唯一约束可阻止重复录入。
- 历史订单仍能通过映射找到原客户和产品。

### Phase 4：智能算价与报价

**目的：** 先固化纯函数和行业规则，再接页面。

普通箱面积：

```text
area_m2 = ((length_mm + width_mm + 80) * (width_mm + height_mm + 40) * 2) / 1_000_000
```

模切箱面积：

```text
area_m2 = (die_cut_length_mm * die_cut_width_mm) / 1_000_000
```

- [ ] 单独实现 `pricing.py`，面积、纸板成本、加工成本、建议售价均使用 Decimal。
- [ ] 普通箱缺少长宽高、模切箱缺少展开尺寸时拒绝算价。
- [ ] 实现材质平方价、损耗率、印刷/钉箱/糊箱/模切附加费。
- [ ] 报价保存时写入计算快照，不能因材料价格变化改写历史报价。
- [ ] 新增报价打印视图，内部版显示成本和毛利，对外纯净版隐藏成本、底价和内部备注。
- [ ] sales 可以生成对外报价，只有 admin/finance 可查看成本。

**验收：**

- 公式边界、单位转换、Decimal 舍入和缺失字段均有单元测试。
- 对外报价 HTML 和打印 PDF 中搜索不到成本字段。

### Phase 5：订单主表 + 明细事务

**目的：** 解决幽灵订单并支持一个客户订单多产品。

- [ ] 新建 order_items 并迁移当前每条 orders 为一主一明细。
- [ ] 将订单创建改为 `OrderCreate` 包含 `items[]`。
- [ ] 在一个 SQLAlchemy Session 中创建订单、明细、产品快照和状态事件。
- [ ] 使用日期流水表或事务内查询生成 `PO-YYYYMMDD-001`。
- [ ] 同一天并发建单时依赖唯一索引重试，不能生成重复单号。
- [ ] 任一产品不存在、数量小于等于零或价格非法时整体 rollback。
- [ ] 保留当前历史差异预警，并把确认人改为当前登录用户。
- [ ] 总金额从明细汇总计算，禁止客户端直接指定最终金额。

**验收：**

- 故意让第二条明细失败后，orders 和 order_items 都不新增记录。
- 多明细金额、状态和历史快照正确。
- 旧单号仍可查询，新单使用 PO 规则。

### Phase 6：WMS 固定二维码来料入库

**目的：** 建立适合车间手机操作的轻量到料流程。

- [ ] 新增 `/incoming` 页面与 `/api/incoming/pending`、`receive`、`revert`、`detail`。
- [ ] 页面只显示待收料订单明细：客户、订单号、款号、尺寸、材质、数量和交期。
- [ ] 固定二维码只编码局域网 `/incoming` 地址，不为每个纸板生成二维码。
- [ ] 一键入库更新 order_item 的材料状态、时间和操作者，并写状态事件。
- [ ] 撤回要求选择原因；已进入送货或已关闭订单不能直接撤回。
- [ ] 入库接口使用条件更新：只有 `pending` 才能变为 `received`，防止重复点击。
- [ ] 手机页面采用响应式布局，不复用桌面端 `min-width: 1180px`。
- [ ] 订单详情可反查该次入库对应客户和订单。

**验收：**

- 同一明细连续扫码/点击两次，只产生一次有效入库。
- workshop 可操作，sales 只读，未登录不可访问。
- 撤回后状态、日志和时间戳一致。

### Phase 7：送货事务与苏州天明标准打印

**目的：** 用真实后端送货单替换前端模拟流程。

- [ ] 建立送货创建、列表、详情、发货、撤回 API。
- [ ] 支持同一客户多订单明细合并送货和部分送货。
- [ ] 创建送货单时校验客户一致、可送数量和材料/生产状态。
- [ ] 发货和订单已送数量更新放在同一事务中。
- [ ] 新增独立 `delivery-print.html?id=...`，不打印主系统壳。
- [ ] 使用 `@page` 和 `@media print` 固定纸张、页边距、表格和分页。
- [ ] 抬头采用苏州天明包装配置。
- [ ] 明细仅显示客户单号、款号、品名/规格、数量、备注。
- [ ] 隐藏单价、金额、成本和内部状态。
- [ ] 页脚固定显示：

```text
白联：存档    红联：客户    黄联：回单
送货人签字：________  客户签收：________  签收日期：________  公司盖章：
```

- [ ] 打印和发货分开：允许预览，只有确认发货才推进业务状态。

**验收：**

- 打印 DOM 不包含价格字段。
- 一张送货单可包含多订单明细，部分送货后剩余数量准确。
- 撤回发货能恢复数量并记录原因。

### Phase 8：回单、对账、开票闭环

**目的：** 将当前已有表结构变成真实 API 流程。

- [ ] 回单按送货明细记录实际签收数量和差异。
- [ ] 只有确认回单的明细进入对账池。
- [ ] 对账金额按实际签收数量计算。
- [ ] 对账按客户、月份、税务模式和开票主体分组。
- [ ] 开票后才能登记收款；已开票或已收款撤销需要 admin 审核。
- [ ] 首页指标全部从数据库聚合，不再读取 seed。
- [ ] workshop 响应模型排除金额字段，避免只在前端隐藏。

**验收：**

- 未回单记录无法生成正式对账。
- 少签数量正确反映到对账金额。
- 状态链不能非法跳转。

### Phase 9：备份、恢复与局域网部署

**目的：** 满足 NAS 热备份和安全隔离要求。

- [ ] 实现手动备份 API 和后台定时备份任务。
- [ ] 备份写入 NAS 时间戳目录并保存 manifest。
- [ ] 增加备份列表、完整性状态、下载和恢复入口。
- [ ] 恢复操作必须先创建恢复前备份。
- [ ] 生成固定 WMS 二维码和桌面访问二维码。
- [ ] CORS 只允许配置的局域网来源。
- [ ] Docker/Windows 服务只暴露局域网端口，不配置公网端口映射。
- [ ] 写入 NAS 部署、固定 IP、备份保留和恢复演练文档。

**验收：**

- 运行中备份后，备份库 `integrity_check=ok`。
- 从备份恢复到测试目录后，关键表记录数和抽样业务链一致。
- 公网地址不能访问 ERP。

### Phase 10：前端收口与移除模拟数据

**目的：** 最终形成单一真实系统。

- [ ] 将主页面拆分为 API、认证、模块脚本和打印样式。
- [ ] 所有 KPI、列表、状态变更、导出和打印接真实 API。
- [ ] 删除 seed 中的模拟客户、产品、订单、送货和财务记录。
- [ ] 将已成熟的 `customers.html` 交互并回统一布局。
- [ ] 保留表格高密度、筛选区、状态标签、确认弹窗和抽屉详情。
- [ ] 桌面端最低宽度策略仅用于办公页面，WMS 页面独立响应式。

**验收：**

- 刷新浏览器后数据仍存在。
- 两个浏览器看到相同业务状态。
- 页面不存在“模拟”“后续接入”按钮或提示。

## 7. 数据迁移顺序

```text
1. users / roles
2. customers
3. materials
4. products（来自 product_archives + BoxERP products）
5. orders
6. order_items
7. deliveries / delivery_items
8. return_confirmations
9. statements / statement_items
10. invoices / logs / attachments
```

迁移脚本必须：

1. 支持 `--dry-run`。
2. 默认输出到新数据库，不原地修改正式库。
3. 每张表输出源数量、成功数量、跳过数量、冲突数量。
4. 对金额和数量做汇总校验。
5. 为每条冲突保留源值、新值和处理决定。
6. 可重复运行且不会重复导入。

## 8. 风险清单

| 风险 | 影响 | 控制措施 |
| --- | --- | --- |
| SQLite 位于 FUSE/SMB 且 WAL 开启 | 锁失效或数据库损坏 | 单后端实例；网络盘禁用 WAL；在线备份 |
| 当前表与 BoxERP 主键类型不同 | 客户/产品关联错位 | 使用映射表，不复用外部主键 |
| 当前订单一行一产品 | 合并后重复订单号 | 迁移为一主一明细，后续新单支持多明细 |
| 前端大量 seed 数据 | 用户误以为已落库 | 分模块替换，真实 API 完成后删除对应 seed |
| 权限只做前端隐藏 | 敏感数据泄露 | 后端依赖和响应模型双重隔离 |
| 打印整个主页面 | 格式不稳定、泄露金额 | 独立打印路由和专用 DTO |
| 默认密码启动时重置 | 账号被接管 | 一次性初始化、强制改密、禁止覆盖 |
| 直接复制运行中的数据库 | 备份不一致 | SQLite Online Backup + integrity check |

## 9. 完成定义

系统合并完成必须同时满足：

1. 使用一个 FastAPI 服务和一个正式业务数据库。
2. 当前客户、产品档案和历史订单数据无丢失。
3. BoxERP 的有效材质与客户信息完成可审计合并。
4. 四角色权限在菜单、按钮、API 和响应字段四层一致。
5. 多明细订单创建具备事务回滚测试。
6. 手机扫码可完成待收料、入库和防呆撤回。
7. 苏州天明送货单打印不包含任何金额。
8. 回单确认后才能对账，按实签数量计费。
9. NAS 备份具有时间戳、校验值、保留策略和恢复演练。
10. 核心自动化测试全部通过，正式页面不再依赖模拟数据。

## 10. 推荐执行切片

该蓝图包含多个独立但有依赖的子系统，不应一次性大改。推荐按以下四个可独立验收的实施计划推进：

1. **基础架构计划：** SQLAlchemy、Alembic、配置、认证、RBAC、备份。
2. **核心业务计划：** Customer、Material、Product、智能算价、报价、Order/OrderItem 事务。
3. **车间送货计划：** WMS 扫码入库、送货事务、苏州天明打印。
4. **财务闭环计划：** 回单、对账、开票、收款、首页真实指标。

第一阶段完成前不开发 WMS 或打印，因为后两者都依赖稳定的用户权限、订单明细和数据库事务。
