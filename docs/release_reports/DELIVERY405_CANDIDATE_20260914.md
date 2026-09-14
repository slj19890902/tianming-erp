# DELIVERY_SAVE404 / v405候选：送货单保存外键冲突

2026-09-14。状态：开发验证通过、候选已推送，尚未正式发布。正式仍为v404。

## 原因与现场证据
用户：YL最新送货单新增00205数量300，导入后保存失败。日志为取消发货后PUT /api/deliveries/104连续409。正式库只读SQLite Backup到独立副本，确认YL-20260914-001、pending、原当前明细454～459，关联6条status=reversed的delivery_inventory_allocations。
修复前即使原样保存也复现sqlite3.IntegrityError: FOREIGN KEY constraint failed；SQL为DELETE FROM sales_delivery_items WHERE delivery_id=104 AND is_current IS 1。旧代码删除当前明细再新建，但取消发货没有、也不应删除历史出库分摊，RESTRICT外键正确阻止删除。不是新增300本身造成，也不是需删除外键保护。

## 实现
app/api/deliveries.py按现有is_current/revision_number机制将旧明细归档，再创建新的当前明细；保留库存分摊/撤销/成本等原引用，无DDL。未撤销完毕的订单及无订单出库数量仍阻止替换。保留原权限/客户范围/状态/CAS/幂等/事务和财务锁；笼统编辑异常分为关联、唯一性或数量状态校验提示，后台记录具体约束，不向用户暴露SQL。创建送货单的另一处旧通用提示不属本轮修改。

## 验证
- 新匿名订单用例修复前409；修复后取消→新增另一明细→保存→过期版本拒绝→发货→重复发货不重复库存流水通过。
- 故障注入外键异常，全部明细版本及单据版本回滚，用户显示关联冲突；已作废无订单单据继续不能编辑。
- 加受控已发货修订、同键重放、失效版本、回单限制、修订失败回滚、无订单及混合来源修订等，共9项定向pytest通过，合入同期STAGING404后再次9项通过。
- 最新正式隔离副本加入order_item_id=10353（Z.001.000205，300）后保存成功104；同一副本执行前后逐行对比inventory_lots、inventory_reservations、inventory_movements、delivery_inventory_allocations、warehouse_locations、sales_order_items全不变，integrity ok、FK0。没有代用户修改正式送货单。
- 两份不同时间复制品中location656曾不同，是并行STAGING404授权登记；未把跨时点差异当成本修复变更，后续同副本前后验证已通过。
- 唯一head mq0912，diff检查通过。未运行全量、正式自动点击或正式业务写入。无前端改动，不需Vite。

## 提交与发布阻塞
分支codex/delivery-save404-20260914；工作区D:/erp-delivery-save404-work；候选f35d44e997c5d5aebfd3b30e399a0fc3a3df364e（合入4435a2ff/440313fe当前正式基线）。版本预留v0.22.405，尚未签名打包/正式整合/停服/发布。
正式根D:/TianmingERP，包3f18d5e24b780f07deb6ecc0c201968595ddf38757bcafd9f40429ed23101668；主工作区仍4435a2ff，未修改正式业务事实。
D盘约12.2GB空余，现有验证备份与暂存安全门禁需约15.5GB，按16GB以上准备；C盘约3.6GB，亦紧张。拟清理本任务旧构建缓存的命令在执行前被环境拒绝，未清理、未迁存、未删除任何文件。不得降低门禁或改用未经验证的旧备份发布。需用户先释放D盘约4GB并给C盘留构建空间，再重新核对最新正式基线、版本冲突、磁盘、备份及健康后发布。不能称已上线。

## 继续入口
诊断脚本D:/纸箱厂erp软件搭建/data/audit/delivery404-20260914/reproduce.py；私有复制品在C用户Temp delivery404-*-copy.sqlite3，不向NAS导出数据库。既有托管发布流程见desktop402-20260914/deploy403_managed.py，只作参考；必须替换版本/完整SHA/当前回滚包/最新基线，不能直接复用旧断言。
上线后管理员刷新并重新打开YL-20260914-001，新增00205数量300保存，再核对明细与合计。未上线前旧页面仍可能失败；不在正式环境自动代填提交。

