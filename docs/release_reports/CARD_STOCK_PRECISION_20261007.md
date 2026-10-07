# CARD-STOCK-PRECISION-20261007 发布回执

状态：技术发布完成，待管理员人工验收。单代理；未自动点击正式页面、未调用IAB、未提交实体打印。

## 结果与正式基线

- v0.22.560，源提交 f58e67604fa6289f9beb5617faf496bf5d3c6c84，签名包 9bf128dd0de21ff58961de30a2ca6cdb6d373d119ed6e94bddc78c0975861c0e。
- 正式schema：ee0930ba → ef1007cp。保留已上线v559的订单/预送货Excel导入修复，正式Git此前为5c8fcd3b（业务源ff7d8f41）；合并时版本文件唯一冲突已按两项功能完整保留。
- 此前v556完成的模具新货架格位编辑、B2短号回显保持；v558卡纸小数支持和本版版本保护已上线。
- 常用箱J列更新累计149款，YKE优先KEW，YL未修改；压线公式行不据J修改原报料长度，组合父件不套子件尺寸。
- 80011929：宽540、长318、一开二；M954仅为放大采购长度。
- 80020548/product3718：真实材质650、layer1/NONE，宽889→444.5、长1194→298.5、一开二→一开一，正常API版本6→7。用户确认卡纸允许精度，纸板整数。

## 实现与验证

小数最多两位，拒绝非有限值、布尔值和瓦楞小数。整数字段JSON保持整数，API/ORM/订单快照/纸板报料/补库/片料匹配/打印去除尺寸截断。SQLite沿用INTEGER affinity存储REAL，不重建任何历史业务表；其他方言NUMERIC(12,2)。数量字段与历史采购抽取不改。

- 本次最终合并后：卡纸18项、补库保存7项、预送货Excel相关7项，共32 passed。
- 先前定向：尺寸/压线/模型25 passed，普通纸板/手工尺寸/订单供应商打印35 passed，补库与采购8 passed，层楞校验15 passed；导入恢复Node及pytest通过。
- 既有半成品预占测试31 failed、1 passed，与未改基线完全一致，均为旧成本报价fixture缺失；未放松正式成本校验，不宣称全套测试通过。
- 隔离副本升降升通过，306表事实、schema、索引和触发器不变；存在小数时降级被拒绝且数据库哈希不变。
- 最新签名包再做停服隔离演练后正式迁移，原306张业务表事实、附件/配置和仓库发布/草稿布局完全一致，integrity ok、FK0。
- 正式三地址health200，两LAN入口index.html与签名包一致；模具位置/正视图只读API通过，A/B共70/31档案的打印次数与数据库一致。未授权模具接口401。
- 版本兼容实测：v560允许，保留精度的v559允许；会截断的v557拒绝。不得用旧库覆盖新数据或强降schema。
- 80020548写入前后仅products、operation_logs、master_data_object_versions变化；只有3718产品的尺寸/开数/版本时间字段变化，价格、BOM、订单、库存及其他产品不变。最终149款逐条匹配来源计划。

## 备份、空间问题与回退

完整NAS恢复包：Z:\sata1-18015598002\BoxERP\backups\20261007-164702-35a1300f.tmbackup。已认证解密、明文哈希、独立NAS副本哈希与验证回执全部通过。
80020548数据更新前另有独立验证备份：Z:\sata1-18015598002\BoxERP\backups\card-stock-precision-20261007-165143\before-final-product.sqlite3。

NAS虚拟盘临时写入受D盘缓存空间影响，原解密函数把IO错误也显示为“恢复口令错误或备份已损坏”；第一次本轮演练明确抛出Errno28。未进入正式迁移。将仅本任务失败演练目录和两个未发布构建ZIP归档C盘，恢复v559服务，再把本任务临时备份与隔离演练移到C盘。仅部署辅助脚本替换临时路径，全部正式NAS、加密、哈希、停服、文件、schema、事务与回退门禁保留，未修改系统默认备份偏好。成功发布之后NAS完整备份仍是正式恢复来源。

隔离演练报告：C:/Users/Administrator/AppData/Local/Temp/TianmingCardPrecisionMigration-20261007/migration-0138de27f92a465aaca2cd4482cadfbe/report.json。

## 管理员验收

1. Ctrl+F5刷新常用箱，打开研光80020548，核对报料宽444.5、长298.5、一开一，关闭重开值一致。
2. 抽查80011929宽540、长318、一开二；正常卡纸新单据打印保留小数，实体纸张仍需现场确认。
3. 模具编辑选择实际B架格位时应显示B2等短号；不自动移动现场未搬动的模具。

证据目录：D:/.codex/workspace_artifacts/card-stock-precision-20261007。release560/release-result.json、compatibility-result.json、frontend-result.json、feed-result.json；product-formal-result.json；149-products-verification.json；报料尺寸更新完成明细.txt。源Excel只读未改动。
