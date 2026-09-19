# R10 旧 ERP 证据化对比（只读）

本轮发现真实存档：`Z:/sata1-18015598002/纸箱ERP新建`。实际列举了该目录、legacy-client-瑞达中奇、erp_audit、report，以及 NAS BoxERP/archives、D:/ERP交接备份、D盘根目录和桌面快捷方式。未触及照片等无关目录。

- `boxdb20_full.bak` 当前存在，563803648字节，文件时间2026-06-16 14:50:27。本轮只查元数据，未执行 RESTORE/VERIFYONLY，不能将历史还原成功当本轮验证。
- 旧程序目录有瑞达中奇管理软件.exe、Box.Data/Entity/Rule DLL、FastReport组件、报表及配置。未启动程序、未输出配置口令、未反编译全部后端。
- `erp_audit/01_tables.tsv`、`02_columns.tsv`、`08_sql_modules.tsv` 是可读历史结构导出。本轮查看表清单、列头以及 Orders3、OrdersForChengPin、SongHuoPaiChengDanPrint、V_CaiGouDanDetailForShow 等视图定义位置。历史报告 ERP_REBUILD_AUDIT.md 标明2026-06-16的241表、3711列、193主键、9外键、13存储过程、16函数、180视图。这些是历史导出报告，不是当前旧库实时统计。
- 本机只发现SQLWriter，未发现运行中的MSSQL实例，未连接BoxDB20_REPRO。桌面未见旧ERP快捷方式。历史报告提到的 D:/0rueida20天明 不作为当前实际运行路径。
- 本次没有恢复或刷新退役 legacy_ruida 抽取层；erp.db不作为历史订单来源。

| 模块 | 旧系统可验证证据 | 当前ERP证据/本轮处理 | 可借鉴点与限制、结论 |
|---|---|---|---|
| 客户产品主档 | Customers、OrderXLs_common历史表，客户132/常用箱3407为当时导出值 | models/customer、product及主档权限接口 | 可借鉴稳定客户/存货编码；不能据表名证明客户隔离，保留当前门禁 |
| 常用箱材质供应商 | 常用箱、GongFangCZ及报价类表；历史审计列基本资料菜单 | products、supplier_master及BOM主档版本 | 保留现有唯一主档；不拷贝旧口令或重复新建资料 |
| 订单导入 | Orders、OrderXLs；历史字段映射计划有数量/单价/金额映射 | api/orders、A包幂等及库存请求测试 | 借鉴单头明细追溯；旧导入取消/幂等实现未完整读取 |
| 组合分项计价 | 没有足够静态证据确认真实父件分项规则 | R03三条实物订单/发货/结算隔离通过 | 按本轮明确规则实现，不能宣称旧系统支持或不支持 |
| 双单位换算 | 尚无可确认YL业务换算的旧代码证据 | R04冻结BOM separate/parent、2片对应1只 | 使用当前数量模型，旧实现优劣未知，不增加第二数量账 |
| 报料采购 | CaiGouDans/Details及采购显示视图 | requisition、supplier_requisition_order；R05/R08待本轮实现验证 | 借鉴一单多行和来源关系；静态表不能证明合并/撤销事务 |
| 来料工序半成品 | ZhiBanRuCangDanDetails、ChengPinRuCangDanDetails | incoming_receipts、production_workflow；R07待专项 | 旧入仓链可追溯；不能据报表推断自动完工规则 |
| 库存库位盘点 | CunLiao、ChengPinGongYong及历史仓库菜单 | InventoryLot/Movement与stocktake；R06待专项 | 当前唯一库存流水继续沿用，旧盘点剩余批次改名隔离未验证 |
| 送货待补送回单 | SongHuoPaiChengDanPrint视图、出货/回签历史菜单 | deliveries、tianhua_pre_delivery；R09待专项 | 借鉴待交/排程展示；无法证明旧跨批FIFO或撤销恢复 |
| 对账开票收款 | 历史菜单有客户月结/应收/收款 | finance回单及statement真实行结算，B包隔离验证 | 保留实际回单事实；不迁入无来源金额和审批 |
| 打印报表 | FastReport组件、报表目录和报表SQL视图存在 | 当前冻结报料/送货/对账PDF | 借鉴业务字段，不把存在模板当现场打印已验证 |
| 权限撤销 | Role_Model、SystemLog表；历史9外键说明约束稀疏 | 当前权限/客户/CAS/事务/审计门禁 | 旧程序可能有应用层保护，未完整审查，不能断言无权限/事务 |

缺少当前可运行旧库连接、完整规则反编译及实际交互证据，故不能完成旧后端全覆盖或断言某能力绝对不存在。此报告完成本轮可访问材料的证据化对比，不用未知旧逻辑覆盖老板已明确R03—R09规则，也不将恢复旧系统作为后续包前置条件。后续包发布结果写各自回执。
