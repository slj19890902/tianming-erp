# ERPNext 纸箱厂 ERP 实施梳理

本文档基于当前目录里的极简 ERP 原型重新整理，目标是把三级纸箱厂管理系统改为以 ERPNext 为底座、纸箱行业逻辑通过自定义应用扩展的方案。

## 1. 当前目录检查结论

当前项目不是 ERPNext 项目，而是一个单机轻量原型：

- 后端：[main.py](D:\纸箱厂erp软件搭建\main.py)：FastAPI + SQLite，启动时自动建表。
- 前端：[static/index.html](D:\纸箱厂erp软件搭建\static\index.html)：Vue CDN + Tailwind CDN，包含老板端和车间端入口。
- 数据库：[data/carton_erp.sqlite3](D:\纸箱厂erp软件搭建\data\carton_erp.sqlite3)：已有少量测试数据。
- 依赖：[requirements.txt](D:\纸箱厂erp软件搭建\requirements.txt)：FastAPI、Uvicorn、二维码、Excel、PDF 生成相关依赖。

我没有在当前目录发现单独的“另一台电脑对话记录”文件；能还原的上下文主要来自 README、源码和 SQLite 表结构。

## 2. 原型里已经确定的业务范围

原型已经设计了这些核心对象：

- 用户：老板端、车间端。
- 客户：客户名称、联系人、电话、地址、开票/对账备注。
- 供应商：纸板厂或外协供应商。
- 产品档案：客户 + 存货编码优先唯一，款号/客户单号作为辅助检索，记录尺寸、材质、楞型、层数、颜色数、工艺说明、历史售价、成本价、图纸、刀模。
- 客户资料维护：放在财务模块下面，统一维护客户送货地址、联系方式、联系人、开票/对账设置、客户常用纸箱规格。
- 报价单：客户需求、数量、含税价、未税价、成本价、报价金额、PDF。报价不是日常必用流程，菜单应放到基础资料或辅助功能里，客户需要报价时才使用。
- 订单：客户单号、款号、规格、材质、数量、售价、成本、待料/料到/送货/签收等状态。
- 报材料单：按供应商汇总需要采购或外协的纸板/材料。
- 成品库存：记录客户常用规格纸箱的现货库存。这类库存不是客户已下订单的纸箱，但客户临时下单时可以马上送货；同时要识别一年或几年不用的库存积压。
- 纸板材料库存：记录错报纸板、毛片备用料和可用于急单“大开小”的材料库存。
- 送货单：按客户汇总订单或成品库存，支持手动选择客户和实际要送的货，支持 241mm 打印宽度和回单确认。
- 回单修正：回单数量与送货单数量不一致时，可以修正送货单数量；送货单款号开错时，可以在回单确认时变更款号。
- 应收应付：客户应收、客户回款、供应商应付、供应商付款。
- 附件：图纸、刀模、报价 PDF、报材料 PDF、送货 PDF、回单照片。
- 订单预警：新订单与历史产品档案不一致时，需要老板确认。

这些业务不应该丢掉，而是要映射到 ERPNext 的标准模块与自定义纸箱模块里。

## 3. ERPNext 适配原则

ERPNext 负责通用 ERP 能力：

- 客户、供应商、联系人、地址。
- 销售报价、销售订单、销售出库/送货、销售发票、收款。
- 采购订单、采购收货、采购发票、付款。
- 物料、BOM、工单、工序、工作站、库存。
- 权限、审批、附件、打印格式、报表、审计日志。

纸箱厂自定义应用负责行业能力：

- 纸箱款号档案。
- 纸箱尺寸、材质、楞型、层数、颜色数。
- 纸箱报价公式。
- 纸板用料/面积/损耗计算。
- 客户历史款号自动带出。
- 历史档案差异预警。
- 车间端只看生产必要字段，不看金额、单价、利润。
- 241mm 送货单、报材料单、客户对账单打印格式。

建议创建一个 ERPNext 自定义 App，例如：`carton_factory`。

## 4. 标准 ERPNext 模块映射

| 当前原型对象 | ERPNext 标准对象 | 处理方式 |
| --- | --- | --- |
| customers | Customer / Contact / Address | 直接使用标准对象，增加对账备注字段 |
| suppliers | Supplier / Contact / Address | 直接使用标准对象，增加付款备注字段 |
| product_archives | Item + 自定义 Carton Style | Item 作为销售成品，Carton Style 保存行业档案 |
| quotations | Quotation | 标准报价单，增加纸箱字段和报价计算按钮 |
| orders | Sales Order + Work Order | 客户订单进 Sales Order，生产进 Work Order |
| material_reports | Purchase Order 或自定义 Material Report | 采购可用 PO，纸板报料单建议自定义 |
| deliveries | Delivery Note | 使用标准送货单，做 241mm 打印格式 |
| finished_stock | Bin / Stock Entry + 自定义 Carton Finished Stock | 客户常用纸箱现货库存，需显示库龄、积压和可送货数量 |
| board_stock | Stock Entry + 自定义 Carton Board Stock | 错报纸板、毛片备用料库存，支持按目标纸箱判断大开小、双拼、四拼 |
| accounts_receivable | Sales Invoice / Payment Entry | 使用标准应收和收款 |
| accounts_payable | Purchase Invoice / Payment Entry | 使用标准应付和付款 |
| attachments | File | 使用 ERPNext/Frappe 标准附件 |
| order_warnings | 自定义 Carton Order Warning | 保存差异确认记录 |

## 5. 建议新增的纸箱行业 DocType

### 5.1 Carton Style（纸箱款号档案）

用途：替代当前 `product_archives`，作为客户款号的主档案。

关键字段：

- customer：客户。
- customer_item_code：客户存货编码，客户内优先唯一。实际查历史价格、尺寸、图稿时优先使用这个字段。
- style_no：款号，客户内辅助检索。
- customer_po：最近客户单号，可选。
- item：关联 ERPNext Item。
- product_name：品名规格。
- carton_name：纸箱名称，用于订单和送货单展示。
- unit：单位，默认“只”。
- length_mm、width_mm、height_mm：内径或外径尺寸。
- dimension_type：内径/外径。
- material：材质，例如 A5A。
- flute_type：楞型，例如 A/B/C/E/AB/BC。
- layer_count：层数。
- color_count：印刷颜色数。
- process_note：工艺说明。
- drawing：图纸附件。
- die_cut_file：刀模附件。
- last_sale_rate_tax_included：最近含税售价。
- last_sale_rate_no_tax：最近未税售价。
- last_cost_rate：最近成本价。
- default_supplier：默认纸板供应商。
- warning_required_fields：参与差异预警的字段。

唯一性建议：

- 首选唯一键：`customer + customer_item_code`。
- 辅助唯一键：`customer + style_no`，仅在客户没有稳定存货编码时使用。
- 客户订单导入时先按客户识别模板，再用存货编码匹配 Carton Style；找不到时再用款号、品名规格、尺寸做模糊匹配。

### 5.1A Customer Profile Center（客户资料维护）

用途：在财务模块下面提供一个专门的客户资料维护界面，作为客户业务资料的统一入口。

放在财务下面的原因：

- 客户资料不仅用于接单，也直接影响对账、开票、收款和送货。
- 客户价格含税/未税、默认开票主体、发票信息、对账周期应由财务能维护。
- 跟单和老板新建订单时只引用这里维护好的资料，避免重复录入。

界面功能：

- 客户基础信息：客户名称、简称、客户编码、是否启用。
- 联系人：联系人姓名、岗位、手机、电话、微信、邮箱、是否默认联系人。
- 送货地址：多个送货地址、默认地址、收货人、收货电话、送货备注。
- 开票信息：发票抬头、税号、开户行、账号、注册地址、注册电话。
- 对账设置：含税/未税、默认税率、月结周期、对账联系人、默认开票主体。
- 客户常用纸箱：内嵌 Carton Style 列表，维护存货编码、款号、纸箱名称、规格、材质、单价、图纸、刀模。
- 附件：合同、营业执照、客户订单样例、开票资料、图纸包。

建议在 ERPNext 中实现为：

- 使用标准 Customer、Contact、Address、File。
- 给 Customer 增加纸箱厂专用字段。
- 新增一个客户资料维护 Page，把 Customer、Contact、Address、Carton Style 聚合在一个界面里。

权限：

- 财务：可维护客户资料、开票资料、对账设置。
- 老板：全部可见可改。
- 跟单：可维护联系人、送货地址、常用纸箱，但不能改税务和默认开票主体。
- 车间：不可见客户财务资料。

### 5.2 Carton Quote Calculator（纸箱报价计算）

用途：根据尺寸、材质、数量、损耗、税率、利润率生成报价。

菜单位置：

- 报价管理从日常业务菜单移到基础资料/辅助资料区域。
- 客户明确要求报价、需要留存报价历史或报价转订单时才使用。
- 日常接单优先走“客户 + 款号/存货编码”直接生成订单。

关键字段：

- customer。
- style_no。
- length_mm、width_mm、height_mm。
- material、flute_type、layer_count。
- order_qty。
- paper_area_formula：面积公式。
- paper_area_sqm：单只面积。
- waste_rate：损耗率。
- material_cost_rate：纸板单价。
- printing_fee、die_cut_fee、stitching_fee、gluing_fee、other_fee。
- tax_rate。
- profit_rate。
- suggested_rate_tax_included。
- suggested_rate_no_tax。
- quote：关联 Quotation。

### 5.3 Carton Finished Stock（成品库存）

用途：管理仓库里客户常用规格纸箱的现货库存。这些库存不是已经下单的订单数量，而是为客户临时要货准备的常备纸箱。

关键字段：

- customer：所属客户。
- carton_style：客户纸箱款号档案。
- item：成品 Item。
- stock_qty：当前库存数量。
- safety_qty：安全库存。
- warehouse_area：仓库区域，例如 A 区、B 区、呆滞区。
- last_in_date：最近入库日期。
- last_out_date：最近出库日期。
- stock_age_months：库龄。
- stock_status：正常、低于安全库存、积压、待处理、已用完。
- remark：积压原因、客户是否继续保留、处理意见。

业务要求：

- 送货管理新增送货单时，可以先选择客户，再从“已到料订单”和“成品库存现货”中选择本次要送的货。
- 从成品库存生成送货单时，要扣减库存，并在回单确认后进入对账。
- 库龄超过设定阈值，例如 12 个月，应标记为积压，后续做呆滞库存报表。

### 5.4 Carton Board Stock（纸板材料库存）

用途：管理错报纸板、毛片备用料、净片余料等纸板库存，用来判断是否能应对客户急单。

关键字段：

- board_no：材料批号。
- material：材质。
- flute_type：楞型。
- board_length_cm：纸板长度。
- board_width_cm：纸板宽度。
- qty：库存张数。
- source：错报材料、毛片余料、净片库存、退回材料。
- stock_date：入库日期。
- stock_status：可用、急单备用、积压、不可用。
- remark：材料来源、可用范围、处理建议。

匹配规则：

- 纸板宽度只能大不能小：库存纸板宽度必须大于或等于目标纸箱报料宽度。
- 纸板长度可以通过拼接处理：先判断单片可用，再判断双拼、四拼。
- 双拼示意：库存长度不足单片时，允许按两片加舌头模拟判断。
- 四拼示意：急单小批量可以进一步按四片方式判断。
- 系统只能给出“可用/不可用/需人工确认”的前端判断，最终还要结合压线、毛边、刀模和机器门幅确认。

## 5A. 纸箱行业规则

### 5A.0 新建订单款号搜索控件

客户的常用纸箱可能多达几千种，新建订单里的“款号”不能做普通下拉框，必须做可搜索选择控件。

交互要求：

- 先选择客户，再启用款号搜索。
- 搜索范围只限当前客户的 Carton Style。
- 支持输入存货编码、款号、纸箱名称、规格、材质进行模糊搜索。
- 搜索结果至少显示：存货编码/款号、纸箱名称、规格、材质、最近含税价/未税价、最近下单日期。
- 选择后自动填充：纸箱名称、规格、长宽高、材质、楞型、单位、单价、报料尺寸、图纸、刀模。
- 如果没有找到，允许新建客户常用纸箱档案，但要经过老板或有权限人员确认。
- 如果本次录入内容与历史档案不一致，触发差异预警。

技术实现建议：

- 后端接口按客户分页搜索，不一次加载几千条。
- 搜索字段建立索引：customer、customer_item_code、style_no、carton_name、product_name。
- 前端使用异步搜索，输入 2 个字符后开始查询。
- 搜索结果按“完全匹配、前缀匹配、模糊匹配、最近使用”排序。

### 5A.1 纸板报料尺寸公式

普通纸箱从客户订单里的纸箱规格计算供应商报材料尺寸：

- 纸板长：`(纸箱长 + 纸箱宽) * 2 + 3cm 舌头`
- 纸板宽：`纸箱宽 + 纸箱高 + 0.5cm`

这里的纸箱长、宽、高需要区分内径/外径。客户订单如果写的是外径，系统应允许设置外径转内径或直接按外径计算；如果写的是内径，则按内径公式计算。不同客户或不同箱型可能需要配置修正值。

### 5A.2 压线、内径外径和模切差异

订单可能是普通开槽箱、压线箱、模切箱、内盒、衬板等。不同类型的计算方式不一样：

- 普通纸箱：按默认长宽高公式计算纸板长宽。
- 压线纸箱：需要保存压线尺寸，车间按压线尺寸处理。
- 模切纸箱：不能简单套普通箱公式，优先使用历史档案里的刀模尺寸或人工确认尺寸。
- 内盒/衬板：很多不需要后续加工，可能直接报净料给供应商。

建议在 Carton Style 增加字段：

- carton_type：普通箱、压线箱、模切箱、内盒、衬板、其他。
- dimension_type：内径、外径、纸板尺寸、压线尺寸。
- crease_line：压线尺寸。
- material_length_cm：报料纸板长。
- material_width_cm：报料纸板宽。
- material_calc_method：自动公式、历史档案、人工指定、刀模指定。
- calculation_note：特殊说明。

### 5A.3 毛片和净片

毛片：

- 对应纸板的整数宽度。
- 供应商送来的宽度带毛边。
- 到厂后需要在分纸机器上先处理毛边，再按纸箱压线尺寸加工。
- 数量少、模切纸箱、内盒、衬板等场景经常报毛片。

净片：

- 纸板宽度精确到 0.5cm。
- 一般没有毛边。
- 有些衬板、垫板、简单片材可直接交给客户，不需要再加工。

建议在 Carton Material Report Item 增加：

- sheet_type：毛片、净片。
- raw_width_cm：供应商来料宽度。
- net_width_cm：实际使用宽度。
- trim_required：是否需要分纸/去毛边。
- direct_delivery：是否可直接交客户。

### 5A.4 双拼箱规则

印刷机器最长门幅约 250cm。普通纸箱公式算出的纸板长如果超过机器门幅，需要考虑双拼箱。

双拼箱计算规则：

- 单片尺寸：`纸箱长 + 纸箱宽 + 3cm`
- 每只纸箱需要：`2片`
- 系统应在计算报料时提示“超过门幅，建议双拼”，并允许老板确认。

建议配置：

- max_print_width_cm：默认 250cm，可按机器配置调整。
- auto_split_when_exceeded：超过门幅是否自动建议双拼。
- split_piece_count：默认 2。

### 5A.5 含税/未税客户策略

默认销售单价按含税 13% 处理。但新建客户时必须选择该客户对账价格类型：

- 含税价格：默认，单价视为含税价，系统按 13% 拆分未税金额和税额。
- 未税价格：该客户默认按未税价对账，开票时再按税率计算含税金额。

建议在 Customer 增加字段：

- default_price_tax_mode：含税、未税。
- default_tax_rate：默认 13%。
- invoice_company_preference：常用开票公司抬头。
- settlement_note：对账/开票备注。

业务单据必须严格区分：

- 已送货：Delivery Note 已提交。
- 已对账：客户已确认对账单。
- 已开票：Sales Invoice 已开具或已登记发票。
- 已收款：Payment Entry 已核销。

这四个状态不能混在一个字段里，否则后续财务和税务会对不上。

### 5A.6 多公司抬头和税务策略

`天明包装公司`、`天明包装厂`、`盛优扬` 等主体可能涉及不同开票抬头、会计做账和税负安排。ERPNext 里建议先按“多公司/多主体”设计，而不是简单备注。

建议：

- 如果这些主体都有独立纳税识别号、银行账户、发票抬头，应在 ERPNext 建多个 Company。
- 客户档案中保存默认开票主体，但每张销售订单/发票允许选择实际开票主体。
- 销售订单、送货单、发票、收款必须能按 Company 分开统计。
- 后续财务报表按 Company 输出，方便会计做账和交税。

需要注意：系统可以支持选择开票主体和统计税负，但具体税务筹划必须由会计确认，系统只做准确记录和权限控制。


### 5.5 Carton Material Report（纸板报料单）

用途：把客户订单按供应商、材质、尺寸、数量汇总，发给纸板厂或外协。

关键字段：

- report_no。
- supplier。
- report_date。
- source_sales_orders：关联销售订单明细。
- items 子表：客户、订单号、款号、尺寸、材质、楞型、数量、成本单价、金额、备注。
- status：草稿、已报料、已到料、取消。
- print_format：报材料单 PDF。

### 5.6 Carton Order Warning（订单差异预警）

用途：保存“当前订单与历史档案不一致”的字段差异。

关键字段：

- customer。
- style_no。
- sales_order。
- carton_style。
- warning_type。
- fieldname。
- old_value。
- new_value。
- confirmed_by。
- confirmed_at。
- reason。

### 5.7 Carton Workshop Board（车间生产看板）

用途：车间端只看生产字段，不看金额。

可以做成 Page 或 Report，不一定要建业务表。

显示字段：

- 订单号。
- 客户简称。
- 款号。
- 品名规格。
- 尺寸。
- 材质。
- 数量。
- 图纸/刀模。
- 状态：待料、料已到、生产中、已送货。
- 工序：印刷、开槽、模切、钉箱、粘箱、打包。

## 6. ERPNext 业务流程建议

### 6.1 接单/报价流程

1. 录入客户需求。
2. 先选择客户，再在款号栏搜索该客户的 Carton Style；优先按存货编码/款号搜索，也支持按纸箱名称、规格、材质模糊搜索。
3. 如果找到历史档案，自动带出尺寸、材质、价格、图纸、刀模、报料尺寸和压线尺寸。
4. 如果本次字段与历史档案不一致，生成 Carton Order Warning，要求老板确认。
5. 通过 Carton Quote Calculator 计算报价。
6. 生成 ERPNext Quotation。
7. 客户确认后转 Sales Order。

### 6.2 生产/报料流程

1. Sales Order 提交后，根据 Carton Style 和纸板尺寸公式生成报料建议。
2. 系统判断普通箱、压线箱、模切箱、毛片、净片、双拼箱，生成 Carton Material Report 草稿。
3. 老板或跟单确认后，对纸板厂生成 Carton Material Report 或 Purchase Order。
4. 纸板到料后，更新 Sales Order/Work Order 状态。
5. 车间端看板显示可生产订单。
6. 工序完成后更新 Job Card 或自定义工序状态。

### 6.3 送货/对账流程

1. 选择客户。
2. 从“已到料订单”和“成品库存现货”中选择本次实际要送的纸箱，生成 Delivery Note。
3. 使用自定义 241mm 打印格式打印送货单，送货单明细必须同时显示客户款号/存货编码和纸箱名称。
4. 客户签收后上传回单照片。
5. 如果实际签收数量与送货单数量不符，可以在回单确认时修正送货单数量。
6. 如果送货单开错纸箱款号，可以在回单确认时变更送货单明细款号，并保留修改记录。
7. Delivery Note 进入“已送货、未对账”状态。
8. 按客户、时间范围生成对账单，客户确认后标记“已对账”。
9. 根据客户含税/未税规则和开票主体生成 Sales Invoice，标记“已开票”。
10. 收款用 Payment Entry 核销应收，标记“已收款”。

### 6.4 供应商应付流程

1. 报材料或采购收货后生成 Purchase Receipt。
2. 供应商开票后生成 Purchase Invoice。
3. 付款用 Payment Entry 核销应付。
4. 按供应商、月份输出应付对账。

## 7. 权限设计

| 角色 | 权限 |
| --- | --- |
| Boss/老板 | 全部业务、价格、成本、利润、财务、配置 |
| Sales/跟单 | 客户、报价、订单、送货，不看供应商成本利润 |
| Purchase/采购 | 供应商、报材料、采购、应付，不看客户利润 |
| Workshop/车间 | 只看生产看板、图纸、刀模、数量、工序，不看金额 |
| Finance/财务 | 应收、应付、开票、收付款、对账 |
| Admin/系统管理员 | 用户、权限、基础配置、备份 |

## 8. 数据迁移方案

当前 SQLite 数据量很少，但字段设计有价值。迁移路线：

1. 从 `carton_erp.sqlite3` 导出 CSV。
2. customers 导入 ERPNext Customer。
3. suppliers 导入 Supplier。
4. product_archives 导入 Item + Carton Style。
5. orders 导入 Sales Order；如果只是测试数据，可以不迁移。
6. attachments 迁移到 File。
7. order_warnings 迁移到 Carton Order Warning。

注意：ERPNext 需要先建 Company、Warehouse、Item Group、UOM、Tax Template、Naming Series，再导入业务数据。

## 8A. 客户订单自动识别方案

目标：客户发来的 PDF、Word、Excel 订单能够按客户识别内容，并生成 ERPNext 销售订单草稿。

原则：

- 一个客户通常只有一两种固定订单/合同格式，因此不要一开始做完全通用 OCR。
- 先按客户维护“订单模板解析规则”，稳定后再逐步自动化。
- 自动识别只生成草稿，不直接提交正式订单；老板或跟单确认后再提交。

建议新增 DocType：Customer Order Template（客户订单模板）

关键字段：

- customer：客户。
- template_name：模板名称。
- file_type：PDF、Word、Excel。
- parser_type：Excel 坐标、Word 关键词、PDF 文本、OCR、人工。
- order_no_rule：客户订单号提取规则。
- delivery_date_rule：交货日期提取规则。
- item_code_rule：存货编码提取规则。
- item_name_rule：品名规格提取规则。
- qty_rule：数量提取规则。
- price_rule：单价提取规则。
- remark_rule：备注提取规则。
- sample_file：样例文件。
- active：是否启用。

识别流程：

1. 用户上传客户订单文件。
2. 选择或自动判断客户。
3. 系统找到该客户启用的 Customer Order Template。
4. 按模板解析客户订单号、存货编码、品名规格、数量、交期、备注。
5. 按客户 + 存货编码匹配 Carton Style。
6. 找到档案后自动带出纸箱名称、规格、价格、尺寸、材质、报料尺寸、图纸、刀模。
7. 找不到档案时生成“待建档订单行”。
8. 与历史档案不一致时生成 Carton Order Warning。
9. 生成 Sales Order 草稿，人工确认后提交。

第一期可支持：

- Excel 客户订单：按固定单元格或表头列名解析。
- Word 采购单：按关键词和表格列解析。
- 可提取文字的 PDF：按关键词解析。

第二期再支持：

- 扫描版 PDF / 图片订单 OCR。
- 印刷图稿 PDF 的缩略图预览和图稿归档。
- 按历史样例自动推荐解析规则。

当前样例文件对应的系统用途：

- `驶安特.xls`：客户月结对账单模板，可反推送货单/对账单字段。
- `报价单1.xls`：报价单打印和报价字段模板。
- `发票开具项目信息导入模板 (3).xlsx`：开票导出模板。
- `2026年客户收付明细.xlsx`：多主体收付款和会计统计样例。
- `天明包装厂5月对账单未确认.xlsx`：供应商/纸板厂对账样例。
- `2025年采购单.xlsx`：历史报料、库存、供应商、报价、退货、检验等综合台账。
- `客户订单样例`：客户订单自动识别样例库。
- `客户印刷样例`：图稿/刀模/印刷文件附件库。

## 9. 部署建议：极空间 NAS 局域网运行

新增前提：ERPNext 环境搭建在这台电脑局域网里的极空间 NAS 上。NAS 24 小时开机，工厂电脑、手机、平板在同一局域网内访问系统。

优先使用 Docker 部署 ERPNext，原因是 ERPNext 依赖 MariaDB、Redis、队列、后台任务、Web 服务等多个组件，直接装在 Windows 本机维护成本高。极空间 NAS 如果支持 Docker/容器管理，应把它作为局域网服务器使用。

推荐结构：

```text
D:\纸箱厂erp软件搭建\
├─ erpnext-deploy\          # ERPNext Docker Compose 部署文件和说明
├─ carton_factory\          # 自定义 Frappe App
├─ migration\               # SQLite 导出和 ERPNext 导入脚本
├─ docs\                    # 需求、字段、流程、打印格式说明
└─ legacy-fastapi\          # 当前 FastAPI 原型备份
```

局域网部署拓扑：

```text
工厂电脑/手机/平板
        |
        |  局域网访问，例如 http://nas-ip:8080
        |
极空间 NAS
├─ Docker / 容器服务
├─ ERPNext Web
├─ MariaDB 数据库
├─ Redis 缓存和队列
├─ ERPNext Worker 后台任务
└─ 持久化目录：数据库、站点文件、附件、备份
```

生产环境建议：

- Windows 电脑只作为开发和访问端，不作为 24 小时服务器。
- ERPNext 跑在极空间 NAS 的 Docker 环境里。
- 工厂内优先局域网访问，暂不直接暴露公网。
- 如果外出访问，优先使用 VPN、极空间远程访问或内网穿透，不建议直接把 ERPNext 管理端口暴露到公网。
- 每天自动备份数据库和附件，备份文件至少保留在 NAS 本机和另一个外置硬盘/网盘位置。
- NAS、路由器和电脑都应固定局域网 IP 或做 DHCP 地址保留，避免访问地址变化。
- 建议准备 UPS，避免突然断电导致数据库损坏。

NAS 部署前需要确认：

- 极空间型号和 CPU 架构：x86_64 优先；ARM 机型需要确认 ERPNext 镜像兼容性。
- 内存：建议至少 8GB，数据量增长后建议 16GB。
- Docker/容器服务是否可用。
- 是否支持 Docker Compose，或能否通过 Portainer/SSH 管理 Compose。
- 可持久化挂载目录，例如 `/volume1/docker/erpnext` 或极空间实际容器目录。
- 局域网端口规划，例如 ERPNext 使用 `8080` 或 `8000`，避免和 NAS 管理端口冲突。

建议的 NAS 目录结构：

```text
/docker/erpnext/
├─ compose.yaml
├─ .env
├─ sites/
├─ logs/
├─ db-data/
├─ redis-data/
├─ backups/
└─ apps/
   └─ carton_factory/
```

第一版只做局域网访问：

- 访问地址：`http://极空间NAS局域网IP:端口`
- 示例：`http://192.168.1.20:8080`
- 老板、跟单、财务、车间都通过浏览器访问。
- 车间端可以用旧电脑、平板或电视大屏打开生产看板。

当前实际部署记录：

- 部署日期：2026-05-30
- NAS 局域网 IP：`192.168.1.173`
- ERPNext 访问地址：`http://192.168.1.173:18080`
- Docker Compose 项目名：`cartonerp`
- ERPNext 站点名：`carton-erp.local`
- 容器前缀：`cartonerp-`
- 数据库：MariaDB 11.8，容器 `cartonerp-db-1`
- 调度器：已启用
- 初始账号记录文件：`Z:\sata1-18015598002\docker\erpnext\ERPNext初始账号信息.md`
- 实际部署 Compose 文件：`Z:\sata1-18015598002\docker\erpnext\compose.yml`
- NAS 内部 Compose 路径：`/tmp/zfuse/18015598002/docker/erpnext/compose.yml`

维护命令：

```bash
sudo /zspace/applications/services/zdocker/bin/docker-compose -p cartonerp -f /tmp/zfuse/18015598002/docker/erpnext/compose.yml ps
sudo /zspace/applications/services/zdocker/bin/docker-compose -p cartonerp -f /tmp/zfuse/18015598002/docker/erpnext/compose.yml logs -f
sudo docker exec cartonerp-backend-1 bench --site carton-erp.local scheduler status
```

后续如果要远程访问：

- 优先 VPN。
- 其次使用极空间自带远程访问能力。
- 再其次考虑 Cloudflare Tunnel、Tailscale、Zerotier 等方案。
- 开放公网前必须配置 HTTPS、强密码、管理员双重验证和定期备份。

## 10. 分阶段实施计划

### 第一期：ERPNext 基础可用

目标：先能接单、查客户、查款号、送货、对账。

- 部署 ERPNext。
- 初始化公司、仓库、用户、权限。
- 配置客户、供应商、物料、单位、税率。
- 创建 Carton Style。
- 在 Sales Order 增加纸箱字段。
- 做客户 + 款号自动带出。
- 做订单差异预警。
- 做 241mm 送货单打印格式。

验收标准：

- 老板能录入客户订单。
- 老板选择客户和款号后能带出历史档案。
- 不一致字段能弹出确认。
- 车间端能看到订单但看不到金额。
- 能打印送货单。

### 第二期：生产和报料

目标：把纸板采购、到料、车间进度跑起来。

- 建 Carton Material Report。
- 做按供应商汇总报料。
- 做待料/料到状态。
- 做车间生产看板。
- 配置工序、工作站、Work Order/Job Card。
- 上传图纸和刀模。

验收标准：

- 能从订单生成报材料单。
- 料到后车间看板自动出现可生产订单。
- 车间只看到生产必要信息。

### 第三期：财务和经营分析

目标：应收、应付、利润和对账闭环。

- Delivery Note 转 Sales Invoice。
- Purchase Invoice 记录纸板供应商应付。
- Payment Entry 做收付款。
- 客户对账单。
- 供应商对账单。
- 单款利润、订单利润、客户利润报表。

验收标准：

- 能按客户导出月度对账。
- 能按供应商导出月度应付。
- 老板能看毛利，其他角色按权限隐藏。

## 11. 技术实现路线

### 11.1 自定义 App

创建 `carton_factory`，包含：

- DocType：Carton Style、Carton Quote Calculator、Carton Material Report、Carton Order Warning。
- Custom Field：给 Quotation、Sales Order、Delivery Note、Item 增加纸箱字段。
- Client Script：客户 + 款号自动带出、报价计算、差异预警弹窗。
- Server Script / Python Controller：保存订单时校验历史档案差异。
- Print Format：报价单、报材料单、241mm 送货单、客户对账单。
- Report：车间生产看板、客户款号档案、订单毛利、应收应付汇总。

### 11.2 命名规则建议

- 客户订单：`SO-.YYYY.-.#####`
- 报价单：`QTN-.YYYY.-.#####`
- 报材料单：`CMR-.YYYY.-.#####`
- 送货单：`DN-.YYYY.-.#####`
- 款号档案：客户代码 + 款号。

### 11.3 错误处理要求

所有自定义后端逻辑都要做：

- 必填字段校验。
- 客户 + 款号唯一性校验。
- 数量、单价、尺寸不能为负数。
- 报价计算异常捕获。
- 生成 ERPNext 单据失败时回滚事务。
- 附件上传失败时保留业务单据但提示补传。
- 权限不足时明确提示，不暴露敏感字段。

## 12. 下一步建议

下一步不要继续扩展当前 FastAPI 原型，应先做 ERPNext 最小可用底座：

1. 在当前目录创建 `erpnext-deploy` 和 `carton_factory` 目录。
2. 用 Docker 跑通 ERPNext。
3. 创建 `carton_factory` 自定义 App。
4. 先实现 Carton Style + Sales Order 自动带出 + 差异预警。
5. 再做送货单打印和车间看板。

这样能最大程度利用 ERPNext 的成熟 ERP 能力，同时保留你之前讨论出来的纸箱厂专用逻辑。

## 13. 2026-05-30 原型需求变更记录

本次前端原型已补充三类关键流程，用于后续迁移到 ERPNext 自定义 App 时作为页面和业务逻辑参考。

### 13.1 客户订单文件识别导入

目标：客户发来的 PDF、Excel、Word、图片订单，先进入“订单管理 -> 导入客户订单文件”，系统识别客户单号、客户款号、品名规格、数量、交期等字段，再按“客户 + 存货编码/款号”匹配产品资料。

当前原型实现为前端模拟识别流程：

- 支持选择客户和上传 PDF、Excel、Word、图片格式文件。
- 识别结果先展示在确认表格里，不直接生成正式订单。
- 产品资料里存在对应款号时，显示“识别成功”，并自动带出产品名称、规格、材质、纸板尺寸、压线尺寸、单价等后续报料和生产所需资料。
- 产品资料里找不到对应款号时，显示“未匹配”，该行暂不导入，后续需要先维护产品资料或由有权限人员确认建档。
- 确认导入后，已匹配行生成订单，默认进入待报料、待送货、待回单、待对账、待开票、待收款状态。

正式版建议：

- Excel 优先用表头或固定单元格解析。
- Word 表格和可复制文字 PDF 使用文本解析。
- 扫描 PDF 和图片再接 OCR。
- 每个客户维护 1-2 套订单模板，记录字段提取规则和历史样例。
- 每次导入保存原文件、识别结果、人工确认记录和异常原因，方便追溯。

### 13.2 多订单款式合并报料采购单

目标：报料管理不只是逐个订单点“已报料”，还要能从待报料订单里勾选一个或多个客户款式，选择供应商后生成类似现有纸板采购单的报料单。

当前原型实现：

- “报料管理”分为“订单产品报料”和“采购 / 报料单”两个页签。
- 订单产品报料页支持按客户、状态筛选，并勾选多个订单款式。
- 选择供应商、填写采购单号后，生成报料采购单。
- 右侧实时预览采购单版式，字段包含：序号、长*宽(单位cm)、压线、数量、材料、单价、备注。
- 生成后，相关订单自动标记为已报料，并可在订单行再次撤销。

正式版建议新增 DocType 或表结构：

- Material Supplier Rule：供应商、材质、楞型、克重、纸种、默认价格、是否启用。
- Carton Material Report：报料单主表，记录供应商、报料日期、单号、状态。
- Carton Material Report Item：报料明细，记录来源订单、客户、款号、纸板长宽、压线、数量、材质、单价、备注。

供应商对应材料和克重表到位后，应把“材料”字段从纯文本改成可配置匹配：客户订单材质 -> 供应商可报材质 -> 克重/楞型/价格。

### 13.3 状态误点容错与回退

原则：工厂文员长期使用表格操作，误点不可避免。凡是“标记已完成”的状态，都要能在权限允许范围内退回上一步，并记录日志。

当前原型已补充的容错：

- 报料：待报料 -> 已报料，再点可撤销回待报料。
- 材料到达：已报料 -> 材料已到，再点可撤销回已报料。
- 回单：已回单或异常回单可撤销回待回单，重新修改签收数量和款号。
- 对账：已生成对账单可撤销，对应订单回到待对账。
- 开票：已开票可撤销回待开票，并同步移除前端模拟发票记录。
- 收款：已收款可撤销回待收款。

正式版建议：

- 每次状态变更写入 Status Change Log，记录操作人、时间、原状态、新状态、原因。
- 撤销关键财务状态时要求填写原因。
- 已经真实开票或真实收款的单据，不允许普通用户直接撤销，应走红冲、作废或管理员审核流程。
- 前端按钮文案必须清楚显示下一步动作，例如“标记已报料 / 撤销报料”“确认收款 / 撤销收款”。
