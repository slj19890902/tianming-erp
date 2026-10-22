# 可选客户料号与唯一性闭环（2026-10-23）

状态：开发验证通过，尚未正式发布。承接bc76c12b，正式基线为v716/b19e58a0；不修改正式3973/9881或历史单据，不安装exporter、不切换服务。

首版仅放开API/表单空料号，真实HTTP复审发现同客户同品名、不同内部编码的两款无客户料号产品第二次新增409，而旧内部码代填控制组均201，因此原候选不得直接发布。当前修复保留非NULL空串表示未提供；客户料号唯一性仅对实际非空值生效，内部customer_id/product_code/product_name唯一性不变。前端、API、客户资料快照均保留真实空白；遗漏字段编辑保留旧料号、显式空白可清空。前导零不丢失，不制造占位/隐藏料号。

## 约束迁移

新线性revision fa1023oc，前驱ez1022hc。只把uq_products_customer_material_code_name表内唯一约束替换为同字段partial unique index，条件customer_material_code <> ''。SQLite重建直接沿用原CREATE TABLE，仅移除精确命名及字段的该约束；原其他列、默认值、检查/唯一/外键、显式索引和本表触发器保留，入站FK不改指向。迁移要求独立FK-off连接及原FK无错误，显式真实事务包含DDL/DML/revision；临时legacy_alter_table恢复原值。未知定义、生成列、AUTOINCREMENT、临时对象冲突或非SQLite存储拒绝执行。

升级无业务回填。降级先检查旧唯一组合；已有新允许的空料号同名产品时，任何DDL之前拒绝并保留现库向前修复，不删产品、不改料号、不覆盖旧库。首次启动失败的正式恢复仍须绑定实际签名包、原事实和迁移回执演练，当前没有实测正式Mac发布或授权新的安装能力。

## 验证与边界

42项定向全通过，0跳过：迁移升降升、RESTRICT/CASCADE入站FK、额外索引、自身/外部触发器和view、真实HTTP迁移后同名空白新增/清空、真实/内部重复拒绝、旧不同品名规则、前导零/遗漏字段、CAS、权限/客户范围、冻结历史及提取正式前端方法执行。两处DDL中途故障均整体回滚、原表/孩子/触发器/revision及文件SHA保持；新重复身份拒绝降级文件SHA保持；FK-on连接拒绝且不降低外键保护。

受管历史隔离源另经scripts/uat/run_task.py完成ez1022hc→fa1023oc→ez1022hc→fa1023oc：333原业务表逐表全部字段指纹保持，原字段及所有入站/出站FK、products其余原始定义、772原显式索引/触发器/view保持（含270触发器），每阶段integrity ok/FK0。不是今天正式数据或真实并发性能验收。脚本仅接受有效UAT环境；运行守卫、secret绑定和checkout写入保护均未关闭。共享合成writer夹具只在受管环境使用runner绑定secret文件。

证据：/Users/kimiyf/ERP-Migration/evidence/erp3-next-20261023/optional-code-final.xml、optional-code-historical-verified.json及对应runner回执。早期隔离夹具配置失败记录保留；最终42项没有跳过。成功副本由标准空闲守卫决定回收，无法确认退出者保留；失败现场保留，本轮未绕过清理守卫。

## 发布整合前提

可与ERP3库存读取ed8da91e串行整合；不与独立受控导出/其他未验收migration串包。整合后再次核对唯一head和定向回归。正式仍需真实草稿关闭、实际正式基线、签名包时点备份/恢复演练、旧事实/原件核对和只读接口验收。当前未改版本、未构建/冻结、未认证/停服，人工页面验收没有代填通过。
