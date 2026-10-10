# MOBILE-FIELD-SEARCH-20261010 / v0.22.615

状态：技术发布完成，待管理员人工验收。签名源码已同步GitHub候选分支与factory-current-baseline，NAS更新源已核对；正式页面未自动点击。

## 结果

- 库存流水通过收料、备库与真实移库关系显示和搜索来源编码，独立保留适用款号。80012273原先仅查询适用绑定而漏掉报料来源；现在可追溯，未新增适用绑定。
- 手机统一单输入的找产品、认纸板、查货位；同一查询仍能展开订单、收料、生产和无常用箱关联的模具。主档近似尺寸覆盖无订单和无库存产品，材质楞型按正式资料筛选，分页前按精确与实际有货排序。
- 产品编码与模具短格均26px，实存位置22px；打开产品先看生产资料，BOM子件各自列模具位置。保留全部上传图纸和全程状态。
- 默认应放和实际货物分开；原盘点移货执行器和幂等权限不变，返回恢复原查询、产品、页码及纸板现状/材质/楞型/尺寸范围，不缓存库存事实。
- 修复来源片料拆分移库后新批在产品查询中遗漏，仅沿真实Transfer且同客户/同来源/半成品身份追溯，不凭编码或尺寸认定可抵扣。

## 正式身份与边界

- 分支：codex/mobile-field-search-20261010；签名源码：5b726452cac0709259e74b520b95e3364f7ddf05。
- 正式包：c9b4c3a524779beb1a92a9c7a2e6e84854f15b306c6f73ec6b38b1f5d83a84c2；版本v0.22.615；唯一schema head：eo1010pi，无迁移。
- 实时承接v614 / 5be5273f93522a90df2a5f5e23b3a42dacf6d368 / 96e7e2a4642c4844478c59be6f4990b9c2c7bb9e66d3e5d1fba4e5045277ddfd，保留补库保存恢复与v613冻结采购规格合同。
- 原始工作区及无关修改保持；未回填正式业务数据。冷态325张表和2982个共享文件验证不变；完整性ok，外键异常0。
- 新NAS冷备经过真实空目录隔离恢复，服务未在演练目录启动；备份：Z:\sata1-18015598002\BoxERP\backups\20261010-114933-7a0c2d71.tmbackup。原程序v614为回退点。
- 收尾时原开发工作树被外部移除，已从同一签名源码提交恢复到独立工作树；功能代码未再修改。电脑12:02重启后既有8000端口转发未监听，恢复IP Helper后原两条转发重新监听，未改变转发目的地、端口或防火墙规则；重新完成HTTP核对。

## 定向验证

以下为相交定向组，不累加冒充独立总数：

- 根：流水8通过；产品和多次上传图纸54通过；真实Chrome/移库/净片最新组3通过；新增分页前移库有货排序1通过。
- API负责人：最终产品、地图、移库组38通过；客户越权、隐去库存、不虚构默认绑定为实存、BOM子模具、共享身份与反例通过。
- 根：Node80通过，最后新增返回条件后相关36通过；合入v614后其相邻保存恢复21通过。
- Chrome只接合成隔离库，320/390px、编码/模具同字号、产品近尺寸、认纸板、货位查询、地图返回和条件跨刷新恢复通过；无业务POST，无页面异常。
- 正式只读：80012273的lot1410、流水4076/4077显示来源，500张实存、500张预占、可用0、适用绑定0保持，模具B7可见；11批同类来源可追溯。
- 发布后本机/局域网健康通过，18项HTTP静态资源哈希与签名发布包及Git源码字节一致，差异检查和Python编译通过。Windows检出CRLF与Git发布LF差异已明确，未通过放宽内容校验来掩盖差异。
- 未跑全仓测试。真实手机、扫码相机与实体现场操作仍由管理员验收，不用自动验证代替。

## 文件

- app/api/mobile_erp.py
- app/api/product_workbench.py
- app/services/product_activity.py
- app/services/product_workbench.py
- app/services/warehouse_movement_read.py
- app/version.py
- static/index.html
- static/mobile_erp.html
- static/mobile_stocktake.html
- static/ui/mobile-field-return.js
- static/ui/mobile-field-search.js
- static/ui/product-workbench.css
- static/ui/product-workbench.js
- static/ui/warehouse-movement-view.js
- static/warehouse.html

完整日志、照片与签名发布证据：D:\.codex\workspace_artifacts\mobile-field-search-20261010。

## 人工验收

1. 刷新库存流水并搜索80012273，核对报料来源编码与原数量。
2. 手机打开现场查询，按编码/尺寸查产品，核对模具短格、实存位置、工艺及全部图纸。
3. 进入查货位与原盘点/移货页面后返回，确认仍是原查询及纸板条件；按实际业务操作，无需为验收改动库存。

真实token用量环境未提供，不作估算。
