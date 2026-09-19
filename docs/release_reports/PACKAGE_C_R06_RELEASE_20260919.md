# R06 v0.22.453 正式发布

技术发布完成，待管理员人工验收。本回执仅覆盖R06；R05、R07、R08、R09尚未完成，继续开发。

代码 ee4168e4001b68f39e4c7771d13c2fe70d416483；分支codex/package-c-r05-r08-20260919已推送并快进factory-current-baseline。签名包 45e9a7cc12326cf1c30afd106772ea3a6b6ea448f3e5bfa4b8d2772e48f42f40；实际运行目录D:/TianmingERP/releases/45e9a7cc12326cf1c30afd106772ea3a6b6ea448f3e5bfa4b8d2772e48f42f40，入口runtime/python.exe -m desktop_assistant.server_entry。

正式数据库D:/TianmingERP/shared/data/carton_erp.sqlite3，唯一revision sc0916，没有迁移。发布没有自动改写库存、主档或其他正式业务事实。停服备份及解密回读、文件摘要、完整性/外键通过，停服期间数据库摘要不变；配置、地图、已有两份未提交文档保持。旧v452包保留可回退。

NAS完整备份：Z:\sata1-18015598002\BoxERP\backups\20260919-203734-5eed8501.tmbackup
SHA-256：9def6c143afab94e759fb3e6cee498e927c95e28c37c689c5a1c2087dd6d7787
停止时数据库SHA-256：0a0bf52c44d979cba8dda624a038b65c54be2653b4f0f12ef9f13cfe5856a95f

三个地址/api/health返回200。两个LAN页面和本轮仓库HTML/JS资源与签名清单一致；未登录订单及库存资料接口均401。正式页面没有自动点击，没有创建验收业务数据。证据D:/tm-build453-r06-20260919及NAS BoxERP/desktop-assistant/release-v453-20260919。

隔离定向验证36通过、3排除；最终当前名称写回复验9通过；TypeScript、Vite构建和差异检查通过。三项旧测试在原v452基线同样失败，分别是旧预占顺序、成本估计口径和手机材质当产品编码的预期；详见PACKAGE_C_R06_20260919.md，不声称全仓库通过。真实手机和纸质打印未验收。

正式入口http://192.168.3.80:8000/warehouse-twin.html。管理员：1.选择确有资料错误的批次，成品点修正资料，片料点匹配产品再展开修正资料。2.核对原来源，填名称/用途/客户/备注和原因；部分修正填数量，保存后核对同位置拆分及总量。3.确需同步常用箱时额外选择并预览，默认不改主档，核对历史订单送货快照不变。不要为验收修改正确的正式库存。
