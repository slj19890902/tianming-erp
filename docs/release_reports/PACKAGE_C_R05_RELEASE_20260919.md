# R05 统一待报料与采购来源 v0.22.456 发布回执

状态：技术发布完成，待管理员人工验收。R07、R08、R09 继续开发，不以本回执宣称全部完成。

## 正式基线

代码 `38bf64c61172fbc53bdc9293ff907c2ae303a8b9`；分支 `codex/package-c-r05-r08-20260919` 快进正式 `factory-current-baseline`。
签名包 `8c8602ee73b543eb5c11e8670e54f3fb7bc9e865ef51811fefb08955e1d3a96f`，正式运行目录 `D:/TianmingERP/releases/8c8602ee73b543eb5c11e8670e54f3fb7bc9e865ef51811fefb08955e1d3a96f`。
实际入口 `runtime/python.exe -m desktop_assistant.server_entry`；数据库路径仍为 `D:/TianmingERP/shared/data/carton_erp.sqlite3`。
正式 schema 从 `sc0916` 增量升级到 `up0919`：新增空采购来源表和可空补库请求哈希，未回填、替换或改写原业务数据。原 v455 界面修复和 v454 PDF 修复保留。
原 v455 签名包为兼容回滚点；不通过恢复旧数据库回退代码。

## 行为与验证

补库先进入待报料；普通订单、BOM、补库按供应商统一成单，来源、用途、压线、收货身份保留。未采购需求不冒充在途，不允许收货。采购/收货重试复用原事实，已收货禁止直接撤销。
具体文件、行为和前序测试见 `PACKAGE_C_R05_20260919.md`。

最终合入并行 UI 修复后：`python -m pytest tests/test_package_c_r05.py tests/test_workflow_continuity.py tests/test_pdf_save_recovery.py tests/test_fin001_frontend.py::test_inline_javascript_is_syntax_valid -q --disable-warnings --tb=short`：12 passed，12 warnings，17.13 秒。
前序 R05 与保存门禁集合 20 passed；补库预警/撤销集合 39 passed；供应商导出/分页/PDF 集合 35 passed。不是全量测试通过声明。
真实库隔离副本升级、降级、再升级：288 张原表内容摘要一致，完整性 ok，外键 0。托管发布还在停服完整备份后执行签名包副本演练，正式迁移结果与副本 schema、行数、原字段事实和外部资料一致。
签名和 29684 个文件摘要通过；三个服务健康地址 200，两个 LAN 入口/仓库资源与签名包相同，未登录订单和仓库接口 401。
未自动点击正式页面；真实手机、实体打印及管理员正式验收待完成。

## 备份和边界

完整 NAS 备份 `Z:\sata1-18015598002\BoxERP\backups\20260919-214427-c16adcb9.tmbackup`。
备份 SHA256 `eac170d602b545820d617bb7a971361340296e7a74d1b9c5bc2cb9dd0e5d5e38`；停服数据库 SHA256 `1b4220475e6ee85aa322b823807b97eab9bfedadf011d5efd135f2e6c06d8bf2`。
加密备份解密回读、数据库完整性/外键检查通过，备份时数据库未变化。
保留原两份未提交文档；正式配置、地图、安装助手均未变。正式代码目录和 schema 按授权更新，原数据库文件未替换，历史业务事实未改写。没有创建验收订单或库存。
证据目录 `D:/tm-build456-r05-20260919`，NAS `BoxERP/desktop-assistant/release-v456-20260919`。

## 管理员最短验收

1. 刷新待报料；真实需要补库时保存需求，核对先进入待报料。
2. 按真实业务选择同供应商普通订单、BOM 和补库，核对来源/规格/用途后成单，打印与来料页核对同一采购单号。
3. 未收货采购可撤销回待报料；已有收货应拦截。不要为正式验收创建模拟事实。

隔离入口 http://127.0.0.1:18091/ ，仅隔离副本 admin 测试密码 123456；候选代码已更新，服务重启状态需以独立 PID 证明为准。真实 token 用量不可获取。

发布后数据库只读检查曾持续锁等待；确认只有正式服务持有数据库文件，托管停止后完整性/外键通过，重启同一签名版本后只读与健康恢复。未替换数据库，未更改配置，根因尚未确定；证据 lock-recovery.json。Git 推送等待退出超时，但已重新查询远端确认提交相同，未重复迁移。
