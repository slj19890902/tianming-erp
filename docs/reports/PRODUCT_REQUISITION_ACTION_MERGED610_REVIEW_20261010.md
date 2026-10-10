# 保留正式 v609 后的 v610 合并只读复核

结论 **pass**。根候选 `850e1915bda47b28fa7f32b04772cb97c6bc18a0`，实际正式基线为 v609 / `eb36881d0e2474ba3315f827720e37c6ffdce6b5`，包 `524baff8ca8272bf6bbd018e65ec39f2e266cf17cd6ff7080e691ea96da5b7ba`（根现场事实及本轮发布脚本固定值）。本审者仅读代码和差异，没有新测试、恢复、服务或部署动作。人工验收 **pending**，本任务尚未发布。

## 源码继承

9 个 API/UI 已受检文件与上一合并候选 `90cdddf02f0bb575a2ca9cbc1189e5683d2e6482` 完全相同，包含 index；因此原 API 8c79504e / UI 90b0d4c 的 5/5 独立证据和上轮 3/3 合并入口证据仍按原 SHA 保留，不改标签或重复运行。

新正式仓库 C 架修复的 warehouse.py、WarehouseTwinApp.tsx、地图 bundle/HTML，以及已合并的首页 JS/CSS、stock_replenishment.py，均与正式 eb36881d 原样相同。本轮没有回退这些修复。app/version.py 保留正式 608/609 全部历史，在正式 609 内容后追加本轮 610；仅当前版本标题改为本轮版本。全部 LF 指纹及比较结果见 merged610-verification.json。

## 真实恢复门禁接入

本任务 `product-requisition-action-release-v610/verify_backup_restore.py` 与已审 helper 相比，只改 DRILL_BASE 为独占 `C:/Users/Administrator/AppData/Local/Temp/tm-erp-product-requisition-v610-20261010/restore-drill`，仍在其下创建新的随机 UUID 目录。Manager.restore 和数据库/附件/包身份比较合同不变，密码只在内存传递，无服务启动。

release.py 在调用 manager.update 前先核 C/D 盘余量：C 按共享大小、旧程序 ZIP 和解压体积计算保守峰值并保留至少 12 GiB 需求/额外余量；D 按新程序解压与 ZIP 暂存体积加余量。source、包、签名、迁移契约及差异集合继续严格验证；进入 Manager 锁后再次 CAS 核当前包，不抢另一发布操作。

真实 restore 位于 `_backup_stopped` 包装器：先调用原冷备，验证同次 receipt 与包 SHA，采集服务已停的事实/附件，再将同一包和内存恢复密码交给本任务 helper。只有 returned passed 且 service_started=false 后才写 `backup-restore-verification.json` 并返回备份路径、允许 update 继续；不是只解密比哈希便称恢复。启动新版本前还比较原数据库事实及 shared 文件不变。恢复异常继续上抛，由原 Manager 冷备失败分支启动旧服务并停止更新。

本轮源码审核未执行容量预检或 restore，故“可恢复”最终结论仍须由执行产生的 space-preflight / backup-restore-verification 证据确认。脚本没有清理动作；后续需先保存证据，再对本任务返回的单个 UUID 目录按已审边界清理，不删除 base、正式或其他任务路径。

本回执只证明最终合并无交叉和恢复门禁接入符合预审。根负责等待其他任务收尾、实际同锁冷备/恢复/发布/只读健康与静态核对，不因本报告跳过这些门禁。
