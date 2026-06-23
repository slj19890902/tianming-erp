# 天明ERP_架构与需求总纲

版本：v1.0  
日期：2026-06-16  
项目定位：天明包装现代化局域网 Web ERP  
适用对象：三级纸箱厂订单、报料、生产、送货、回单、对账、开票、收款流程

---

## 1. 总目标

天明 ERP 的目标不是复刻一套大型 SAP，也不是继续维护单机 exe，而是基于老系统真实数据和操作习惯，重建一套稳定、清晰、适合工厂长期使用的 B/S 架构 Web ERP。

核心目标：

1. 所有电脑和手机访问同一套后台和同一份数据库。
2. 数据库集中部署在局域网 NAS 的 PostgreSQL 服务中。
3. PC 端负责录单、报料、送货、对账等高密度办公操作。
4. 手机端负责扫码、收料、查单等现场轻量操作。
5. 业务流程保持老系统操作直觉，避免复杂隐藏菜单和花哨交互。
6. 纸箱行业公式、压线、开料、报价逻辑进入独立规则引擎。
7. 订单明细保存快照，确保历史订单不会因资料库修改被污染。

---

## 2. 当前资产边界

### 2.1 正式主项目目录

当前主项目目录：

```text
D:\纸箱厂erp软件搭建
```

后续新架构代码、文档、测试和迁移脚本均以此目录为主干。

### 2.2 NAS 老系统资产目录

老系统提取资料位于：

```text
Z:\sata1-18015598002\纸箱ERP新建
```

该目录包含：

- `boxdb20_full.bak`：老 SQL Server 数据库备份。
- `erp_audit`：老库结构、字段、视图、存储过程导出。
- `legacy-client-瑞达中奇`：老 WinForms 客户端包，仅作为复现和参考。
- `legacy_ui_assets`：老 UI 资源、菜单、模板、图片。
- `ui_migration`：老窗体清单、截图、UI 迁移资料。
- `new-erp-ui`：早期网页壳，仅作视觉参考。
- `tm_desktop_erp`：早期桌面复刻原型，仅作流程参考。

### 2.3 废弃参考区

以下目录统一标记为“废弃参考区”：

```text
Z:\sata1-18015598002\docker
Z:\sata1-18015598002\BoxERP
```

扫描结论：

- `docker/carton-erp`：早期 SQLite + Docker Web ERP 探索，包含迁移报告、截图、SQLite 备份和部分原型代码。
- `docker/erpnext`：ERPNext/MariaDB 试验，包含 CSV 清洗结果和迁移记录。
- `docker/瑞达参考*`：旧瑞达界面和迁移参考资料。
- `BoxERP`：早期 SQLite 数据库、产品图纸和备份文件。

使用原则：

1. 可以读取其中的截图、迁移报告、CSV、字段映射和业务分析。
2. 禁止直接复制旧 Docker 配置、SQLite 代码、ERPNext 结构进入新主干。
3. 禁止将早期 SQLite 数据库作为新系统主数据库。
4. 禁止复用其中的明文口令、临时端口和实验性部署方式。
5. 如需使用某个文件作为输入，必须在新项目中重写清洗逻辑并保留来源说明。

---

## 3. 技术栈锁定

### 3.1 后端

后端采用：

```text
Python 3.10+
FastAPI
SQLAlchemy 2.0
Alembic
Pydantic
PostgreSQL
```

硬性要求：

- 所有业务写入通过 FastAPI API 完成。
- ORM 使用 SQLAlchemy 2.0 typed declarative 模型。
- 所有核心关系必须启用 `ForeignKey`。
- 禁止延续老系统“无外键裸奔”的数据库风格。
- Alembic 迁移脚本必须可审查、可回滚。
- 生产环境关闭调试 traceback、Swagger 可按环境隐藏。

### 3.2 数据库

主数据库采用：

```text
PostgreSQL on NAS Docker
```

禁止使用 SQLite 作为生产主库。

原因：

- PC 端开单、财务查询、移动端扫码收料存在并发写入。
- PostgreSQL 能提供事务隔离、锁控制、外键、索引和长期扩展能力。
- NAS 集中部署便于多端同步和统一备份。

### 3.3 前端

前端采用：

```text
Vue 3
TypeScript
Vite
Element Plus
vxe-table
VueUse
html5-qrcode
Print.js
Pinia
Vue Router
Axios 或 Fetch 封装
```

插件职责：

| 插件 | 用途 | 约束 |
|---|---|---|
| Vue 3 | 前端主框架 | 使用 Composition API，代码分模块 |
| Element Plus | 基础表单、弹窗、按钮、消息 | 全局大字号、高对比度、默认 large |
| vxe-table | 工业级表格 | 订单、库存、报料、对账等复杂列表必须使用 |
| VueUse | 浏览器能力、响应式工具 | 用于本地状态、窗口、扫码辅助 |
| html5-qrcode | 手机端扫码 | 用于收料、查单、流转单扫码 |
| Print.js | 打印 | 用于送货单、标签、报料单、对账单 |
| Pinia | 前端状态管理 | 登录态、菜单、标签页、字典缓存 |
| Vue Router | 路由 | 与多标签页状态联动 |

---

## 4. 前端 UI/UX 总原则

### 4.1 新瓶装旧酒

界面必须吸收老系统优点：

- 顶部一级菜单。
- 多标签页。
- 大工具栏。
- 固定查询区。
- 高密度表格。
- 固定表头。
- 底部分页和状态栏。
- 清晰选中行。
- Excel 式键盘操作。

禁止：

- 复杂隐藏式侧边菜单。
- 悬浮按钮作为主要操作入口。
- 大面积留白的互联网风格。
- 低对比度浅灰小字。
- 操作按钮藏在二级 hover 菜单中。

### 4.2 适老化标准

主要操作人员包含 60 岁以上员工，因此默认标准为：

- 默认字体不低于 14pt。
- 按钮、输入框、弹窗使用 Element Plus `large` 尺寸。
- 主按钮明确写动词，如“确认入库”“生成报料”“确认发货并打印”。
- 弹窗底部固定“确认 / 取消”。
- 删除、作废、清空必须二次确认。
- 错误提示使用中文业务语言，不使用技术错误码直出。
- 所有列表提供搜索和重置。

### 4.3 vxe-table 使用规范

以下页面必须使用 `vxe-table`：

- 客户订单管理。
- 订单明细编辑。
- 常用箱资料。
- 报料工作台。
- 纸板入仓。
- 纸板库存。
- 成品库存。
- 送货单明细。
- 回单确认。
- 月结对账明细。

必须启用：

- 虚拟滚动。
- 键盘方向键移动。
- Enter 确认或跳到下一行。
- 固定表头。
- 列宽拖拽。
- 行选中高亮。
- 批量勾选。
- Excel 导入导出入口。

---

## 5. 核心业务流程

系统主流程：

```text
报价
→ 下单
→ 报料
→ 纸板到料
→ 生产/待送货
→ 送货
→ 待回单
→ 回单确认
→ 月结对账
→ 开票
→ 收款
→ 完成
```

状态原则：

1. 未下单不能报料。
2. 未报料不能标记纸板已到，除非管理员走异常补录。
3. 未送货不能回单。
4. 未回单确认不能进入正式对账。
5. 未对账不能开票。
6. 未开票也允许记录预收款，但必须标记为预收或挂账。
7. 已完成订单只允许查看和冲正，不允许直接改历史数量。
8. 所有关键状态变更写入操作日志。

### 5.1 生产流转单轻量化与免打印适配

系统不能强迫车间每一单都打印纸质流转单。真实工厂里，员工和管理层会抗拒繁琐流程，因此必须同时支持“免打印极简绑定”和“诱导式智能派工单”两条路径。

#### 5.1.1 预生成二维码贴纸绑定

系统支持提前印制一批空白二维码不干胶贴纸。开单或排产时，页面提供“扫码绑定外部二维码”输入框，扫码枪或手机扫入二维码内容后，将实体贴纸与系统订单/订单明细强绑定。

要求：

- 二维码贴纸可以先批量生成，不要求每张都现场打印。
- 订单明细保存 `external_qr_code` 或等价绑定字段。
- 一个外部二维码同一时间只能绑定一个有效订单明细。
- 重新绑定、解绑、作废必须写操作日志。
- 手机扫码后直接打开该订单明细状态面板。

这样即使车间暂时不打印系统流转单，也能通过贴纸实现最小成本溯源。

#### 5.1.2 诱导式智能派工单

订单详情页必须提供“智能派工单”一键打印。该功能不是为了增加流程负担，而是用“机器替人计算”的利益点吸引员工主动使用。

派工单内容必须突出：

- 客户名称。
- 任务编号。
- 款号/存货编码。
- 产品规格。
- 材质/楞型。
- 订单数量。
- 超大字体显示纸长、纸宽。
- 超大字体显示压线。
- 开料指导。
- 内置系统二维码。

打印方式：

- 前端使用 Print.js 调用浏览器打印。
- 支持 A4、半张纸、票据打印机等模板。
- 派工单二维码由系统生成，扫码进入订单明细状态面板。

#### 5.1.3 无感扫码状态面板

手机端必须提供极简扫码页面。基于 VueUse 和 html5-qrcode 调用摄像头，扫码后只显示现场需要内容。

扫码成功后的页面结构：

```text
订单信息大字号摘要
客户 / 款号 / 规格 / 材质 / 数量
纸长 / 纸宽 / 压线

[确认收料/确认完成] 绿色超大按钮
```

要求：

- 按钮高度接近半屏，适合戴手套点击。
- 不显示金额、利润、成本。
- 不要求员工填写复杂表单。
- 异常只提供“备注异常”入口，不阻塞正常确认。
- PC 端看板实时收到状态更新。

---

## 6. 数据库核心设计

### 6.1 基础资料层

核心表：

- `users`：用户。
- `customers`：客户。
- `suppliers`：供应商。
- `materials`：材质。
- `flute_types`：楞型。

设计要求：

- 客户、供应商、产品、订单均保留 `legacy_source` 和 `legacy_id`。
- 客户删除采用停用优先；存在未完成订单时禁止硬删除。
- 产品删除采用停用优先；历史订单引用后禁止硬删除。
- 所有编码字段必须唯一或在客户范围内唯一。

### 6.2 行业规则层

核心表：

- `box_type_rules`：箱型规则。
- `score_line_rules`：压线公式。
- `cutting_width_rules`：开料档位。
- `customer_material_prices`：客户材质平方价。
- `supplier_material_prices`：供方平方价。

规则原则：

- 箱型公式不写死在订单 API 中。
- 压线公式按箱型 + 楞型匹配。
- 开料档位按纸宽区间匹配。
- 规则修改不影响历史订单，历史订单只读快照字段。

### 6.3 核心业务层

核心表：

- `products`：产品/常用箱。
- `orders`：订单主表。
- `order_items`：订单明细快照。
- `requisitions`：报料批次。
- `requisition_items`：报料明细。
- `material_receipts`：纸板入仓记录。
- `deliveries`：送货主表。
- `delivery_items`：送货明细。
- `return_receipts`：回单主表。
- `return_receipt_items`：回单明细。
- `statements`：对账单主表。
- `statement_items`：对账明细。
- `invoices`：发票记录。
- `payments`：收款记录。
- `operation_logs`：审计日志。

### 6.4 外键策略

必须执行：

- `customers -> products`：`RESTRICT`
- `customers -> orders`：`RESTRICT`
- `products -> order_items`：`RESTRICT`
- `orders -> order_items`：`CASCADE`
- `deliveries -> delivery_items`：`CASCADE`
- `return_receipts -> return_receipt_items`：`CASCADE`
- `statements -> statement_items`：`CASCADE`
- 字典类数据被引用后不允许硬删，只能停用。

---

## 7. 纸箱行业计算引擎

### 7.1 引擎边界

计算引擎独立于订单 API，负责：

- 成箱长宽高转纸板长宽。
- 推荐开料宽度。
- 计算压线。
- 计算面积。
- 计算客户销售单价。
- 计算供方采购成本。
- 计算毛利参考。

订单 API 只调用引擎并保存结果快照。

### 7.2 基础面积公式

老系统核心面积逻辑：

```text
单张面积 m² = 纸长_mm × 纸宽_mm / 1_000_000
总面积 m² = 单张面积 × 数量
```

拼片/多片时：

```text
计价面积 = 单张面积 × 数量 × piece_multiplier / pieces_per_sheet
```

### 7.3 普通箱默认公式

普通箱可使用默认公式作为兜底：

```text
纸长 = (长 + 宽 + 8) × 2
纸宽 = 宽 + 高 + 4
面积 = 纸长 × 纸宽 / 1_000_000
```

但正式计算优先读取 `box_type_rules`，因为不同箱型存在不同开料公式。

### 7.4 压线公式

压线规则：

```text
按 box_type_rule_id + flute_type_id 匹配 score_line_rules
内径订单使用 inner_formula
外径订单使用 outer_formula
```

输出示例：

```text
33.8*10.9*33.8
```

订单明细必须保存：

- `snapshot_score_line`
- `snapshot_cardboard_length_mm`
- `snapshot_cardboard_width_mm`
- `snapshot_material`
- `snapshot_flute_type`
- `snapshot_product_name`
- `snapshot_spec`

### 7.5 价格计算

销售价：

```text
销售单价 = 面积 × 客户平方价 + 加工费 + 附加费
销售金额 = 销售单价 × 数量
```

采购成本：

```text
采购单价 = 面积 × 供方平方价
采购金额 = 采购单价 × 报料数量
```

重量参考：

```text
重量kg = 面积 × 克重gsm / 1000
```

金额计算必须使用 Decimal，不得使用 float 直接落库。

---

## 8. 后端 API 总体规划

API 前缀：

```text
/api
```

统一要求：

- 所有写接口鉴权。
- 所有关键操作写 `operation_logs`。
- 列表接口统一分页。
- 导入接口必须先预览，再确认落库。
- 导出接口生成文件并记录操作日志。
- 移动端接口只暴露必要字段。

### 8.1 基础 API

- `POST /api/auth/login`
- `POST /api/auth/logout`
- `GET /api/auth/me`
- `GET /api/menus`
- `GET /api/system/health`
- `GET /api/system/backups`
- `POST /api/system/backups`

### 8.2 基础资料 API

- `GET/POST/PUT /api/customers`
- `POST /api/customers/{id}/disable`
- `POST /api/customers/{id}/enable`
- `GET/POST/PUT /api/suppliers`
- `GET/POST/PUT /api/materials`
- `GET/POST/PUT /api/flute-types`
- `GET/POST/PUT /api/products`
- `POST /api/products/{id}/disable`
- `POST /api/products/{id}/enable`

### 8.3 规则引擎 API

- `POST /api/engine/calculate-carton`
- `POST /api/engine/calculate-price`
- `GET/POST/PUT /api/rules/box-types`
- `GET/POST/PUT /api/rules/score-lines`
- `GET/POST/PUT /api/rules/cutting-widths`

### 8.4 订单与报料 API

- `GET/POST/PUT /api/orders`
- `GET/PUT/DELETE /api/orders/{id}/items/{item_id}`
- `POST /api/orders/{id}/cancel`
- `GET /api/requisitions/pending`
- `POST /api/requisitions`
- `POST /api/requisitions/{id}/cancel`
- `GET /api/requisitions/{id}/print`

### 8.5 WMS 与移动端 API

- `GET /api/wms/pending`
- `GET /api/wms/received`
- `PUT /api/wms/receive/{item_id}`
- `PUT /api/wms/revert/{item_id}`
- `GET /api/mobile/orders/{code}`

### 8.6 送货、回单、财务 API

- `GET /api/deliveries/pending-items`
- `POST /api/deliveries`
- `PUT /api/deliveries/{id}/dispatch`
- `GET /api/deliveries/{id}/print`
- `POST /api/receipts`
- `PUT /api/receipts/{id}`
- `POST /api/statements`
- `GET /api/statements/{id}/export.xlsx`
- `POST /api/invoices`
- `POST /api/payments`

### 8.7 实时同步

PC 看板和手机扫码状态同步使用：

```text
WebSocket /ws
或
Server-Sent Events /api/events/stream
```

最小事件类型：

- `order.created`
- `requisition.created`
- `material.received`
- `delivery.dispatched`
- `receipt.confirmed`
- `statement.created`

---

## 9. 前端页面结构

### 9.1 顶层框架

前端采用传统 ERP 框架：

```text
顶部菜单
多标签页
工具栏
查询筛选区
主表格
底部分页/状态栏
```

一级菜单：

- 首页工作台
- 基础资料
- 订单生产
- 采购报料
- 仓库送货
- 财务月结
- 系统设置

### 9.2 首页工作台

首页采用三栏：

```text
左侧：交期/异常提醒
中间：业务流程图
右侧：常用报表快捷入口
底部：系统时间、当前用户、版本、备份状态
```

左侧提醒：

- 今日交货。
- 明日交货。
- 后日交货。
- 逾期未交。
- 采购过期未到。
- 送货未回签。
- 应收逾期。

中间流程：

```text
客户订单 → 报料 → 纸板入仓 → 生产 → 送货 → 回单 → 对账 → 开票 → 收款
```

每个节点显示待处理数量。

### 9.3 订单管理

订单管理必须支持双视图：

1. 客户视图：先选客户，再进入该客户订单、常用箱、历史价格。
2. 订单视图：直接按交期、状态、任务编号处理。

默认进入客户视图。

顶部固定搜索：

- 客户编码。
- 客户名称。
- 任务编号。
- 客户单号。
- 款号。
- 模具号。
- 图纸号。
- 皮板号。

### 9.4 报料管理

报料工作台功能：

- 待报料列表。
- 勾选多条订单明细。
- 生成合并报料。
- 自动带出历史纸板长宽、压线、材质。
- 手动修改报料数量。
- 打印或截图式供应商报料单。
- 报料后 WMS 待收料列表可见。

### 9.5 手机 WMS

手机端只做轻量现场动作：

- 扫码查单。
- 查看待收料。
- 确认入库。
- 撤回入库。
- 查看订单和客户摘要。

页面要求：

- 大按钮。
- 大字号。
- 单列卡片。
- 不显示金额和利润。
- 只显示现场需要字段。

---

## 10. ETL 与历史数据迁移原则

### 10.1 数据源

旧库：

```text
SQL Server / BoxDB20 或 BoxDB20_REPRO
```

新库：

```text
PostgreSQL / tm_erp_db
```

连接字符串通过环境变量配置：

```text
SOURCE_DB_URL
TARGET_DB_URL
```

禁止把真实密码写死进代码。

### 10.2 迁移顺序

第一批：

1. `Customers`
2. `Supplys`
3. `KengLeis`
4. `ZhiBanZiLiaos`
5. `XiangLeis`
6. `YaZhiGongShis`
7. `KaiLiaoWs`
8. `KeHuBaoJiaZBs`
9. `Gongfangbaojias`
10. `OrderXLs_common`

第二批：

1. `Orders`
2. `OrderXLs`
3. `CaiGouDans`
4. `CaiGouDanDetails`
5. `ZhiBanRuCangDans`
6. `ZhiBanRuCangDanDetails`

第三批：

1. 送货。
2. 回单。
3. 月结。
4. 收款。
5. 操作日志。

### 10.3 清洗规则

ETL 必须：

- 分批读取。
- dry-run 默认开启。
- NULL、空字符串、乱码字段做清洗。
- 字符串 trim。
- 重复编码自动加后缀。
- 孤儿记录归入“未知客户/历史归档”。
- 所有源表 ID 写入 `legacy_source` / `legacy_id`。
- 每轮生成迁移报告。
- 失败行写入日志，不中断整批迁移。

---

## 11. 部署架构

### 11.1 推荐生产拓扑

```text
NAS Docker:
  PostgreSQL
  定时备份目录
  附件/图纸目录

局域网服务器或 NAS Docker:
  FastAPI 后端
  Vue 静态前端

办公室 PC:
  浏览器访问

手机:
  Wi-Fi 内网扫码访问
```

### 11.2 安全约束

- 不将业务系统暴露公网。
- 仅允许局域网 IP 访问。
- 管理员接口必须鉴权。
- 备份恢复接口仅 admin 可用。
- 生产环境禁用默认调试页面。
- 密码使用哈希存储。
- 操作日志不可被普通用户删除。

### 11.3 备份策略

- PostgreSQL 每日自动 dump。
- 关键迁移前强制备份。
- 附件目录和数据库备份分开保存。
- 恢复前必须自动生成 pre-restore 备份。

---

## 12. 测试与验收标准

### 12.1 后端测试

必须覆盖：

- 模型外键约束。
- ETL 清洗。
- 订单事务。
- 报料状态流。
- WMS 并发入库。
- 送货超量 warning。
- 回单差异。
- 对账金额基于实收数量。
- 权限 401/403。

### 12.2 前端测试

必须覆盖：

- 登录失效跳转。
- 菜单权限隐藏。
- vxe-table 键盘导航。
- 订单新增。
- 报料生成。
- 手机扫码入库。
- 发货并打印。
- 回单可编辑。
- 对账导出 Excel。

### 12.3 UAT 验收

一条完整闭环：

```text
新建客户
→ 新建常用箱
→ 新建订单
→ 自动算纸长纸宽和压线
→ 生成报料
→ 手机确认入库
→ 生成送货单
→ 确认发货并打印
→ 回单确认
→ 生成月结对账
→ 开票
→ 收款
```

验收成功标准：

- PC 和手机看到同一状态。
- 所有金额计算可追溯。
- 历史订单快照不被资料修改影响。
- 老员工可以按老系统习惯完成核心操作。

---

## 13. 开发纪律

1. 任何数据库结构改动必须走 Alembic。
2. 任何迁移脚本默认 dry-run。
3. 任何生产数据覆盖前必须备份。
4. 任何导入都必须有报告。
5. 不直接修改老 exe、老 DLL、老生产库。
6. 不从废弃参考区直接复制代码进主干。
7. 每个阶段先补测试，再实现。
8. 前端页面不能空壳，必须接真实 API 或明确 mock 来源。
9. 打印和导出是核心功能，不是后补装饰。
10. 业务状态必须可解释、可追溯、可回滚或冲正。

---

## 14. 近期实施路线

### Phase 1：PostgreSQL 基座

- 完成 SQLAlchemy 2.0 模型。
- 完成 Alembic PostgreSQL 初始化。
- 完成 Customers / OrderXLs_common ETL dry-run。
- 完成规则表 ETL 骨架。

### Phase 2：Vue 3 前端底座

- Vite + Vue 3 + TypeScript。
- Element Plus 全局 large。
- vxe-table 全局注册。
- Pinia 登录态。
- 多标签页框架。
- 首页三栏工作台壳。

### Phase 3：真实主数据

- 客户资料。
- 常用箱资料。
- 材质/楞型/箱型/压线公式。
- 搜索与导入导出。

### Phase 4：订单和计算引擎

- 新建订单。
- 订单明细 vxe-table。
- 自动算价。
- 快照落库。

### Phase 5：报料和 WMS

- 报料工作台。
- 报料单打印。
- 手机扫码收料。
- PC 实时刷新。

### Phase 6：送货、回单、对账

- 送货单。
- 发货并打印。
- 回单确认。
- 月结对账。
- Excel 导出。

---

## 15. 最终原则

这套系统的核心不是“功能越多越好”，而是让纸箱厂每天最重要的状态看得清、流得动、查得到、打印准。

最终判断标准：

```text
订单有没有报料？
纸板有没有到？
生产有没有完成？
货有没有送？
客户有没有签收？
账有没有对？
票有没有开？
钱有没有收？
```

所有架构、页面、表结构、插件和接口都必须服务于这八个问题。
