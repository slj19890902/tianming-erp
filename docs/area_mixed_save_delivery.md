# 地图保存混合库存兼容修复 v0.22.343

- 任务：北货架G2层数/格数及区域名称保存，被 EDIT-050 旧用途校验阻止。
- 原因：EDIT-050 为南F货架1，历史策略为 finished，存在5批成品和2批半成品。整层发布逐区校验仍按旧类型白名单拦截，违反总需求9.2混放规则。
- 修改：已绑定/未绑定区域转换校验取消成品半成品原材料互斥；保留非库存用途、操作区、有货布局转换、权限、版本及原有货位安全检查；相关API错误显示实际区域名称。
- 分支 codex/area-mixed-save；代码 8adb4b5f41aabbf6a9b270a04c7c60efb6acb62a；正式版本 v0.22.343。
- 定向验证：7 passed，覆盖混放发布、两种绑定的三种库存类型、功能区/模具用途/有货布局转换阻断、未发布不可用与无库存操作区。git diff --check通过；唯一head rw10v8x9z71。
- 正式数据只读查询：3F _formal_area_publish_blockers 返回 []。LAN和公网 /api/health 均200。启动时公网探测曾失败，随后复查两入口通过。
- 发布报告 docs/migration_reports/release_runtime_20260911_142205.json completed；备份和隔离演练 integrity=ok、FK=0、核心表计数一致；无新增migration，正式source/applied SHA一致，未改库存事实。
- 备份：D:\纸箱厂erp软件搭建\data\backups\carton_erp_before_release_20260911_142209.sqlite3
- 未执行全量回归；本次纯后端修复无前端构建。Chrome连接此前不可用，本轮未声称页面验收。
- 开发验证通过，技术发布完成，正式 ERP 人工验收待完成：刷新地图保存G2层数/格数及区域名称。
