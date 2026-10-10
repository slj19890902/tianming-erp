# YL 28 条常用箱补充正式导入报告

## 结论

- 执行日期：2026-08-05
- 正式客户：`YL / 苏州工业园区驿力机车科技有限公司 / customer_id=136 / customer_number=131`
- 本次新增：28 条，全部启用
- 原有记录：3 条，逐字段与导入前备份一致
- 导入后 YL 常用箱：31 条，全部启用
- 未导入：60 条；原因是未填写、字段不完整、重复或无法安全确认
- 正式服务：本机及局域网 `192.168.3.80:8000` 健康检查均为 HTTP 200

## 冻结输入

- 来源工作簿：`C:\Users\Administrator\Desktop\天明合作纸箱\YL_YKE_KEW常用箱_全量预导入核对表_最终映射复核版.xlsx`
- 工作簿 SHA-256：`8DB04E36039E5D9617D54C8AC91B4F5155B7C0C587A16E8F14163B6B6E4D4AC1`
- 冻结清单：`outputs/019fcc60-32ce-7ac2-8df7-0e6751e4195c/YL_28条_补充导入冻结清单.json`
- 清单 SHA-256：`C75C4B7FE3C3EDFA4B9A15C132F51701E27D0B572ED2905716F282192EBB1184`
- 范围：普通箱 27 条、刀卡 1 条；A1 20 条，其中单拼 10 条、双拼 10 条，沿用 YL 现有 35mm 舌头口径

## 特殊归类

`Z.001.000130` 的原资料明确记录模切内盒、三段压线 `103/220/103`。ERP 的模切内盒压线类型只允许“净料/毛片/其他”，因此受控导入将类型归类为“其他”，保留三段数值、原始文字和审计备注；没有把该条改成 A1，也没有重算人工报料尺寸。

## 演练与正式执行

- 隔离副本演练前：产品 3467、YL 3；`integrity_check=ok`、外键异常 0、revision=`dk93v8x9z82`
- 隔离演练后：产品 +28、主数据版本 +28、操作日志 +56；客户、材质、订单、报料、库存均无变化
- 幂等复跑：28 条全部为 `already_applied`，新增数 0
- 正式库导入前 SHA-256：`EF1BED45AAC488A8187D6E5AD415C4F3655EE186C52B6F85EBF9AD408821A74E`
- 正式库导入后 SHA-256：`5CC9A6E634BA577C33A6E67349F043EB4A8FE6499B5CF52C5E3172C851A8B84D`
- 正式备份：`D:\纸箱厂erp软件搭建\data\backups\carton_erp_before_YL_supplement_20260805_20260805_184832.sqlite3`
- 备份 SHA-256：`EF1BED45AAC488A8187D6E5AD415C4F3655EE186C52B6F85EBF9AD408821A74E`
- 备份校验：与停机后的正式库导入前哈希一致；`integrity_check=ok`、外键异常 0
- 正式新增产品 ID：3469 至 3496
- 正式结果：产品 3495、YL 31、主数据版本 594、操作日志 3788
- 受保护表：客户 134、材质 616、销售订单 73/明细 288、供应商报料 74/明细 196、库存批次 213，前后不变

## 验证

- `tests/test_yl_common_box_supplement_import.py`：7 passed
- `tests/test_yl_yke_kew_import_safety.py`：3 passed
- `tests/test_p1_13_box_type_rules.py`：16 passed
- 正式库重复只读核验：28 条全部 `already_applied`
- YL 原有 3 条：与备份逐字段一致
- 正式库：`integrity_check=ok`、外键异常 0、revision=`dk93v8x9z82`
- 正式服务：本机和局域网健康检查 HTTP 200

## Git 与验收状态

- 正式代码仍为 `factory-current-baseline@cbc51201223542f9b45ba0b88bf6e744ecea4314`
- 本次只执行受控数据导入；专用导入程序、测试、清单和报告位于独立候选分支 `codex/yl-common-box-supplement-20260805`，尚未提交、推送或合并
- 技术导入已完成；仍待老板在正式常用箱页面筛选 YL，确认 31 条的显示、搜索及现场使用
