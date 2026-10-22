# 可选客户料号：正常新增及编辑保留空白

父任务2026-10-22明确授权的最小候选，基于正式v716 b19e58a0071f。创元3973在正常页面留空客户料号，保存payload却复制内部编码且后端要求非空。目标：内部product_code仍必填，客户料号可显式空白，保存/GET/客户资料快照不伪造客户编码；旧客户端省略字段编辑保持既有值。

仅修改产品保存合同及定向测试，不迁移、不批量清历史、不修正式3973、不发布。保存权限、客户范围、CAS、事务、版本审计、历史冻结订单维持。数据库已有非空字符串列，使用空字符串表示未提供，不变更schema。已有客户料号原样保留，不从内部码自动推断。

验证：真实HTTP新增、显式清空、遗漏保留、已知前导零、内部码必填、CAS/冻结订单不受改档影响；提取正式前端保存方法执行新增/编辑测试；相关客户送货快照回归。仅合成SQLite测试库。

## 2026-10-23 根代理接管：空码同名约束闭环

原bc76c12b候选不能发布：真实HTTP复审确认同客户、同品名、不同内部编码且客户料号均空时第二次新增409，旧前端内部码代填控制组两次201。原“不迁移/不变更schema”仅描述首版范围，已被本节修订；没有正式迁移或历史修正。

根代理独占本worktree的后续实现；协调方停止相关运行文件写入。先完整读取三份迁移规范，已核代码唯一head ez1022hc及受管历史源同revision；正式仍以实时发布核验为准。新建线性revision fa1023oc（须唯一性检查）。新增allowlist：app/models/product.py、该revision、定向迁移/API测试、本卡和独立报告；不改exporter、安装管理程序或正式数据。

设计：客户料号非NULL空串含义保持，将uq_products_customer_material_code_name由表内唯一约束替换成同字段、仅customer_material_code <> ''生效的partial unique index；内部customer_id/product_code/product_name唯一约束原样保留。SQLite需重建products，使用原始CREATE SQL只删除精确命名/字段约束，保留全部其他列/约束原文；原显式索引和本表触发器原样重建，foreign_keys必须已关闭且原FK检查无错误，legacy_alter_table仅在有事务的重建期间临时打开并恢复。原表不rename，避免入站FK改指向临时表；所有DML/DDL与revision处于同一真实SQLite事务，失败回滚。未知结构/临时表冲突/非SQLite方言拒绝，不自动猜测结构。

升级不回填任何行、不伪造料号、不改冻结单据。降级先检查全部旧唯一性组合，发现新允许的空码同名重复则在任何DDL前拒绝且文件SHA保持；安全降级才恢复旧约束。用合成含RESTRICT/CASCADE入站FK、附加索引、自身/外部触发器及view的库验证升降升、失败事务、重复/空/前导零、显式清空/遗漏保留；用受管历史源的另一个副本核全部原表原字段摘要、索引/触发器、FK/完整性。正式迁移仍需实际新时点备份/签名包恢复演练，当前不触发服务动作。

受管验证适配：额外独占tests/test_master_data_versioning_p4_writers.py，仅使共享合成夹具在ERP_UAT_ROOT存在时沿用runner绑定secret文件，不设置旧明文测试secret；生产验证守卫保持启用。迁移合成夹具经prepare_uat_layout绑定新的子目录身份及所有写根，不覆盖外层数据库。

隔离历史往返脚本纳入allowlist：scripts/validation/rehearse_optional_customer_code.py，强制validate_uat_environment，仅修改runner复制品，输出聚合保留证明。

## 2026-10-23 验证闭合

42项最终定向全通过、0跳过；真实HTTP验证迁移后的两款空码同名新增及显式清空，真实/内部身份重复仍409。两处DDL故障原子回滚、非空新身份拒绝降级SHA不变、FK-on拒绝均通过。受管历史源333原业务表/772显式对象/270触发器及products其他定义升降升保持，integrity ok/FK0。详见docs/release_reports/OPTIONAL_CUSTOMER_CODE_20261023.md；尚未正式迁移或发布，原bc76单独候选不再可发布。
