# v0.22.339 仓库成本发布回执

- 2026-09-11 13:27正式技术发布完成，代码9798b2776e7b7ff48016f83bdd604321edf95681，factory-current-baseline与功能分支均已推送。承接v338的71批已授权参考成本和20条实收价格补录，没有重复执行这些数据动作。
- 新盘点成品/半成品/原材料使用人民币材料价，按可靠展开尺寸、箱型公式、组合部件和外购换算自动冻结；缺资料拒绝无价入库。既有成本在报价调整、移货、拆分时不重算，出货无实际采购来源时以已确认批次成本补充财务材料成本，不虚构采购应付。
- 成本入口统一为货位中的折叠成本与/factory-twin-assets/warehouse-costs.html；仅有效admin/boss，finance及员工角色不能读取仓库成本API；保留客户可见范围，禁止缓存成本响应。
- 识别旧估算14批单位异常，10批按可靠现行规则补定：156、185、538、579、581、583、584、585、619、622。每批原6项成本字段、公式、报价版本、前后值保留；10条审计，正式批次warehouse-cost-unit-fix-20260911-v339。其余正数成本、采购价、历史出货事实未改。
- 正式只读结果：312批现存实物，304批计价，8批待核价，已定价部分166872.77元。251张业务表逐行比较一致（inventory_lots只排除6个成本字段及version/updated_at，operation_logs单独核对）；全行改变的inventory_lots恰好为上述10批。完整性ok、外键0；幂等预览0项可补，不重复写入。
- 未完成业务事实：8批的真实展开尺寸、白板纸有效报价或EPE采购价仍缺。详见WAREHOUSE_COST_ACCEPTANCE_20260911.md，已经给出逐项缺失资料和计价建议；不能宣称所有库存已定价。

## 验证与备份

- 工作树隔离验证：82项成本、仓库、财务来源及相邻客户搜索回归通过；最后权限一致性/单位纠错增强后32项重跑通过；发布元数据1项通过；前端客户搜索2项通过，TypeScript检查及Vite构建通过，git diff --check通过。未运行全量测试，按任务最小相关范围。
- UAT旧副本曾演练40批补价，随后发现正式v338已补71批，弃用旧预览；改用最新正式副本current-v338-uat.sqlite3重新预览演练10批，正式与演练指纹完全一致：5bdb1690f960cc96ccd6e23bacf535a3a9dfe89f9dbb439d2dedf3905293f636。
- 隔离副本Chrome桌面及390px手机宽度成本列表核对，外购两片/件3.94元、参考成本和8批待核价正常显示。正式成本页面已打开，但Chrome控制两次Debugger unattached，未取得正式页面点击验收。正式LAN健康通过，成本JS与发布文件逐字一致；未登录的成本/版本接口受认证保护。真实业务人工验收未代替老板完成。
- 发布报告docs/migration_reports/release_runtime_20260911_132624.json=completed，13:27:07启动成功。唯一revision rw10v8x9z71，无新增结构迁移。
- 发布备份data/backups/carton_erp_before_release_20260911_132625.sqlite3；独立成本纠错备份data/backups/carton_erp_before_cost_unit_fix_20260911_1327.sqlite3。两者SHA256均fd7b32003d4f9f00792df91df81a7afb9ccd892085b1d4e8047fc76cba5a87f6，integrity ok/FK0，核心表计数与当时正式一致。
- 成本预览及结果：docs/migration_reports/inventory_cost_preview_20260911_1327.json、inventory_cost_applied_20260911_1327.json。回退仅对这10批成本字段逐项审核下游引用后恢复旧值；禁止整库覆盖或抹除审计。
- UAT测试密码只改独立副本的已核验管理员，正式用户、权限和密码未改。所有正式地图、草稿、location_id、库存数量、产品主档及订单均保留。
