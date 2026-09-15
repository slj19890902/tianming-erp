# COST-CONTRACT v429 候选补充

状态：代码候选，未正式上线；线上仍为v428。无新增迁移，schema保持cc0915。历史补充尚未执行。

## 闭环

- 已复现：旧原料成本为空，备库完工仍返回200并创建无成本产出；新保护返回409，指出来源批次，原料与计划保持不变。
- 已复现：旧批次只有单价数字、没有可确认的冻结来源，BOM组套仍接受；改为核对完整来源依据，不将参考成本提升为实际采购成本。
- 备库组套逐项核对，第二个子件缺成本时整笔回滚，前一个子件也不得被扣。
- 正常已冻结材料参考成本、加工和组套金额继承仍可通过，不改库存数量或采购应付事实。

## 检查记录

46项定向测试通过，覆盖备库、组套、库存估值、销售契约和BOM本体；包含负向事务回滚。命令：`python -m pytest tests/test_stock_preparation_flow.py tests/test_stock_preparation_disposition.py tests/test_inventory_valuation.py tests/test_cost_contract_20260915.py tests/test_multilevel_bom_body_assembly.py -q --tb=short`。差异检查无错误。

另外两项test_multilevel_bom_external_assembly失败，在独立bb43f646基线同样复现（产出0而期望1；可用0而期望2），不归因为本次新增校验，未修改业务规则掩盖失败。test_bom_subkit_inventory的11项需要显式准备的隔离源，默认跳过，不计作通过。

## 发布阻塞与后续

极空间Z盘已有latest.json读取返回不存在，先前NAS备份也未完成校验。不可据此认定远端数据已删除，不重建空发布目录，不删除任何备份。ERP持续运行v428；NAS恢复后重新验证备份，再进行v429发布与历史修复。历史修复仍须新的停止态副本预演、指纹比对和逐行审计。

本回执暂存本地，NAS独立回执待连接恢复后写回。未进行正式浏览器点击验收。
