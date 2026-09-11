# MULTILEVEL-BOM 家庭接包与隔离验证 2026-09-10

## 代码与数据边界

候选克隆：D:/tm-worktrees/bom-home-20260910，HEAD 8686da336b9d712af9b5210d09107e9edce1d1ff。原主目录及既有改动未动。NAS交接49载荷大小与SHA256全部回读验证通过。
隔离源：D:/tm-uat/bom-home-20260910/order-graph-source-isolated.sqlite3，SHA256 f604beab03de2751f4985ab96718afabfff2918fe6b6a9299174b8049f721eed。
配套发布地图SHA256 c8123ee4ba37a342365b354959d3c32d367f32e98c847d7d7727807858e5f6c2。源库与地图测试后哈希未变。每个真实数据测试从只读隔离源再备份到临时库，核对备份后实际升级到se17v8x9z79，结束检查integrity/FK。没有正式迁移、发布、停服、归位或库存写入。

## 位置与本次修正

交接证据明确area80/EDIT-028、policy77、published revision563f19179f32a8ee、地图feature f1612fcc-b406-456f-a248-9aee520a7354及slot.location_id关联到1203；空间序号10为1203，1206为序号8。货位自身placed不代表货物已归位。
初轮测试24通过、6失败：旧测试读取tracked地图，与真实数据库发布revision不同，被原门禁拒绝。未放宽生产门禁。factory_copy现在要求同目录配套published map并复制到各临时库的私有目录；00205测试目标由历史假设1890改为老板确认并经证据核验的1203。所有业务写入仅在临时测试库发生。

## 本次实际结果

- 常用箱原子保存与真实BOM主档：初轮24通过；原子新增/修改、循环、版本和失败回滚已有用例本次执行。
- 00205真实转换writer：7通过，16.86秒。300套输出在1203，原1800/已送1500不变，旧消耗4500/6000不改，只释放剩余900/1200并成套预占300；重放不重复流水；无效待归位目标、非管理员、末端审计失败和外层回滚受保护；发1套/取消恢复通过。正式系统仍未归位或转换。
- 主档转换与前端原子请求：18通过，23.80秒同轮另1条新验收断言失败，后续修正并单测通过如下。前端为Node执行实际方法，不是Chrome登录验收。
- 新增其余16款隔离保存/重读验收：最终1通过，2.56秒。原真实子件ID、用量、单位保持，订单/库存/预占/流水/货位不变，停用3494保持停用。用manufactured+accompany测试配套保存能力，不是对全部产品采购/制造来源作正式转换决定。最初全边字段相等断言包含更新时间和旧display_mode归一化，超出本用例身份/用量合同，改为明确检查边ID、父子ID、用量；不宣称所有边元数据逐字节不变。
- 真实产品编译及转换只读review：7通过，13.06秒。源库/配套地图哈希保持。
- git diff --check通过。上述组不合并宣称全仓或完整业务验收。

修改文件：tests/test_multilevel_bom_factory_compile.py、tests/test_bom_cutover_writer.py、新增tests/test_bom_home_handoff_acceptance.py。未改应用运行代码，未提交/推送。
日志：D:/tm-uat/bom-home-20260910/initial-tests.log、cutover-tests.log、master-acceptance.log、other-sixteen-final.log、source-review.log。

## 未完成及下一步

旧订单转换仍只有convert_reserved_legacy_order服务，无管理员公开操作入口；现有服务仅支持全预占原片转assembled父件，不能冒充所有未完成BOM版本切换都支持。需补明确影响预览、目标选择、校验及审计执行入口，采购/本体/配套等不支持路径必须保持拒绝。
Chrome连接器库存读取报nodeRepl.fetch request failed，browsers为空，未使用IAB或任何finalize。管理员从界面创建/修改多级BOM、真实配套/组装闭环及紧凑布局仍未验收。实体打印扫码、附件/CAD相关用例也未完成。完整目标未完成、未上线。
