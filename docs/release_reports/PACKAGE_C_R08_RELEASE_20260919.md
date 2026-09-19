# R08 v0.22.459 正式发布

技术发布完成，待管理员人工验收。R01–R08已技术发布，R09继续；不表示整轮全部完成。

代码 `ea600910121ef06ec88cdb9a7535666e3def762e`，签名包 `6db3f190b38a201e4ee8439f7e494667ef563a90738e81eab8efd3ca91697f64`。实际运行 `D:/TianmingERP/releases/6db3f190b38a201e4ee8439f7e494667ef563a90738e81eab8efd3ca91697f64/runtime/python.exe -m desktop_assistant.server_entry`。已快进并推送正式分支，保留v458订单余料转产改动。

正式库 `D:/TianmingERP/shared/data/carton_erp.sqlite3` 已按签名契约从up0919升rp0919，只新增4张空关联/成本来源表，无历史回填。包内演练和实际迁移比较通过，289张原表事实、原地图/配置及两份他人未提交文档保留。独立升降升及有事实时拒绝降级也通过。

NAS完整恢复备份 `Z:\sata1-18015598002\BoxERP\backups\20260919-232218-418e1b7e.tmbackup`，SHA256 `1da92b1ecea511d41c76ca186b39f833e439904c1f5e09b3a04557b40e52f404`，解密回读验证通过；备份期间停服源库SHA `0d32053aadc269e4852a69b034710b4d5f6e490e701292ceef24c08ca13955d5`保持不变。上版v458包cf0fa94145fbe8dfd46eeeb1be6a293ca43c74dc6c033d2767c2738abb1194a6保留，管理器核验旧程序兼容新增结构；不得通过删除新业务事实降级。

40项定向测试、tsc/Vite、内联JS和差异检查通过（明细见PACKAGE_C_R08_20260919.md）。签名与29693个包内文件摘要通过；三入口健康200、首页/仓库资源匹配包摘要，订单/仓库/新采购接口未登录401；正式库完整性ok、外键0。未自动点击正式网页，未生成正式业务测试数据。

证据 `D:/tm-build459-r08-20260919`、NAS `BoxERP/desktop-assistant/release-v459-20260919`，包内演练 `D:/TianmingERP/staging/migration-2f7c4db111cd42be9a32cad4d8532c8b/report.json`。隔离测试 `http://127.0.0.1:18091/`，独立库 `D:/tm-uat/package-c-20260919/run/uat.sqlite3` 已rp0919，实际DB/写根/PID隔离核验通过。仅隔离admin密码123456，正式账户未改。实际Chrome业务点击、实体打印和手机仍待人工验收。

1. 待报料选择真实盖20、底20需求，采购方案调整填45张与实际尺寸、楞向，核对40分配+5备用及原压线。
2. 供应商打印预览核对实际无压线原片；展开厂内分配保留最终要求，实收后应为40预占待加工和5张客户专用原片。
3. 实际加工完成后从订单详情确认，核对20套、5张备用、原片消耗与成本来源。不要为验收虚构业务。
