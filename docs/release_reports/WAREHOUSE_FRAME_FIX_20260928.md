# 仓库内嵌连接修复 v0.22.526

状态：技术发布完成，待管理员人工验收。

## 原因与修复
点击仓库恢复上次库存列表时，/warehouse-ledger.html?embedded=1 请求虽然 HTTP 200，但安全中间件遗漏该路径，仍返回 X-Frame-Options: DENY，浏览器拒绝嵌入。仓库地图本身已允许同源内嵌。仅将台账精确路径加入同源内嵌白名单，继续要求 embedded=1，返回 SAMEORIGIN 和 CSP frame-ancestors 'self'；独立台账、embedded=0/true、首页及其他路径仍禁止嵌入。权限/API/客户范围和业务逻辑不变。

分支 codex/warehouse-frame-fix-20260928；正式基线 c95d6b7d；运行源码 d7dde3c578490d45cddabe098ccf993ce18a260a；仅 app/main.py、app/version.py、tests/test_n031_transport_security.py 变更。

## 验证
新增安全头用例在修复前确实因 DENY 失败；修复后目标用例通过。安全测试文件30项通过、1项既有health测试失败（预期503实得200），已在原c95d6b7d基线单独复现相同失败；没有扩入无关修复。仓库主壳Node14项全部通过；Sol只读独立复核确认恢复list触发链与最小白名单修复充分。差异检查、签名构建、唯一head ed0928ml通过，无迁移。未运行全量模块回归，未执行正式页面自动点击。

正式本机/办公网192.168.3.80/车间网三入口：健康200；地图与台账 embedded=1 均200/SAMEORIGIN/CSP self，独立台账、embedded=0和首页保持DENY；页面哈希与签名包一致；未登录库存API401。证据 D:/ERP-UAT/warehouse-frame-fix-20260928/health-acceptance.json。

## 发布与数据
包 cd61ba89ebbb6f93090bd861fd9852833ccb4e6a9bf2780b50b2d9c666de4357；上一包 0bc8c80a1d5fb4df4ead329924464a2e3cee4322fc75698f6ce47007cb275149 保留。
NAS备份 Z:\sata1-18015598002\BoxERP\backups\20260928-182017-7989af0d.tmbackup；SHA256 07918e8024949d516aa4379d5c21324d0359b585b839b948598d48dc1f927a97，加密解密及完整回执验证通过。
核对305表：停服备份及更新启动前原事实保持；启动后仅email_intake_settings的last_sync_started_at/last_sync_completed_at自然变化。完整性ok，FK0，原head不变，无业务数据修正，送货打印校准文件不变。

## 人工验收
刷新ERP确认v0.22.526，点击仓库并在地图、库存列表之间切换，确认无拒绝连接。当前未代填人工验收。

Astra修复集成，Sol独立复核。真实token用量不可获取。
