# 受控在线开发副本导出设计（只读设计，未实现）

用途仅为DELIVERY-FULFILLMENT-FACTS-20261022候选取得有来源证明、已去ERP凭据的新鲜副本。不属于fa1022df迁移，也不混入v715。本文未执行认证、读取凭据、改变正式权限或运行导出；不是可直接运行的root脚本。

## 已确认缺口及可复用接口

- `desktop_assistant/data_recovery.py:139`的`sqlite_snapshot`以只读连接执行SQLite在线备份，有时间上限和完整性检查，可复用数据库捕获；不能单独代表跨文件一致性、去凭据或跨UID交付。
- `desktop_assistant/mac_system_service.py:33,414`的动作仅status/configure-backup/backup/start/stop。`manage`已有受审查管理员代码、固定专用UID/GID、挑战和回执校验；没有向UID501开发任务导出的动作。
- `desktop_assistant/mac_service_manage.py:31,55,79`的`authenticated/instance/maintenance`可复用签名运行器、专用身份、目录句柄与维护锁。必须新增严格请求合同，不能接受任意命令或任意正式源路径。
- `desktop_assistant/preflight.py:9`的`inspect`核对图纸、PDF训练源、共享文件版本及发票附件路径/摘要；不是所有外部配置的导出器。
- `scripts/uat/run_task.py:27`及`app/core/uat_isolation.py:1073`可创建隔离副本并成功回收。当前只接数据库/地图，自动原件复制仅PDF训练源；不能宣称图纸、发票和共享文件已全部导入。
- `data_recovery.create/included`默认会带environment.json和config，不是去凭据白名单；backup可能进入停服/备份凭据路径，verify-pair恢复会自动清理。不得借用这些动作、改目录权限或复活已收尾恢复目录绕过缺口。

## 单次计划和请求合同

先生成可审查计划，不捕获数据。计划固定task_id、随机唯一attempt_id、短有效期、管理员代码包摘要、当前应用包/git/revision、独立supervisor包、publisher/context/registry/control/plist摘要、运行实例身份、recipient_uid=501、固定原件选择和去凭据策略版本。身份必须在计划生成时重新读取，不将v714旧包摘要当作未来导出时的当前身份。

输出只能为明确的新目录，例如`/Users/kimiyf/ERP-Migration/isolated-sources/delivery-fulfillment-20261022/<attempt_id>/`。目录不得存在，不得位于Git工作树或正式实例；必须以目录句柄/no-follow检查全部父路径、所有者和替换竞争，禁止链接、覆盖或任意目的地。重试同attempt必须回读原结果，不能复用已失败半成品冒充成功。请求只引用批准计划摘要及挑战，不携带shell命令、凭据或自由源路径。

## 正常认证后的固定流程

1. 通过现有正常macOS管理员认证批准精确计划摘要。受保护入口核对已审查代码和计划，以既有专用UID/GID启动导出worker。不赋予UID501正式实例访问权限，不改正式目录ACL/所有者，不读取Keychain、备份密码或签名私钥。
2. worker取得现有短维护锁，仅排斥并行维护，不停止业务进程。通过固定实例目录句柄核对应用、supervisor、revision和进程归属；未决迁移、身份变化、过期计划或锁冲突均拒绝。`sqlite_snapshot`只读捕获到专用身份私有staging，捕获后再次验证身份；不调用backup/restore/verify-pair。
3. 复制实际运行地图，禁止回退工作树模板；从捕获数据库生成批准的必要原件清单，仅允许已核验业务存储根。逐项拒绝链接、越界、缺件、摘要/大小不符；地图或可变原件在捕获窗口内变化则本次失败。独立twin数据库如必要，单独在线快照并标明时间/摘要，不宣称多库全局原子。
4. 只在私有副本去ERP凭据，保留业务主键/关联/角色/数量/金额。基于精确schema和版本固定白名单处理认证字段，未知结构先拒绝。输出数据库重新紧凑生成，不能把原始快照、残留空闲页、WAL/journal交给UID501。副本绝对路径重绑逐表/字段/行数/前后摘要记录，不能混入业务修正。
5. 验证integrity/FK、业务表指纹、允许转换字段及原件清单。回执绑定计划、来源身份、快照/转换/输出摘要和时间，声明服务未停止、未替换、未写正式业务。只将通过验证的数据库、真实地图、批准原件和回执一次性交付新输出目录；目录0700、文件0600，只移交本批新输出给UID501。失败留在受保护staging且无成功交付标记。
6. 接收端重新核对回执与全部摘要，经`run_task`复制进自己的owned copies，再运行`delivery_fulfillment_acceptance.py`。验收所需附件如超出PDF训练源，须单独实现受清单约束的UAT导入适配，不能让候选程序沿数据库旧绝对路径访问正式附件。成功按所有权规则回收本任务副本、保留job外证据，正式源和其他任务目录不清理。

## 去凭据与业务个人信息的区别

仓库模型明确存在`users.password_hash/auth_version`和`email_intake_settings.encrypted_secret/automatic_enabled`。现有UAT会生成独立会话密钥、拒绝继承ERP_SECRET_KEY，但不清除数据库中的密码哈希，旧capture仅清邮箱配置也不充分。下一候选必须审查用户认证禁用/会话失效及邮件秘密移除、禁用自动外联的固定转换，保持角色和业务身份；输出不包含正式environment/config、密钥、凭据包、备份包、控制记录、日志或运行器，也不能调用可能读取AI凭据的`Manager._environment()`。

上述字段仅是已知最低项，不代表通用秘密扫描已完成。实现前应针对准确schema和业务文件白名单检查所有认证载荷，测试未知认证字段时fail closed；不能通过打印、日志或回执证明秘密值。原件的客户姓名、联系方式、地址、员工信息、价格和发票内容可能仍保留，当前没有通用匿名化器。回执必须写“按固定ERP凭据策略处理；业务个人信息保留，未匿名化”，副本继续是私有业务资料，不公开或外传。

## 下一独立候选的最小范围与验收

建议新增`desktop_assistant/uat_export_contract.py`（严格计划/请求/清单/回执）和`desktop_assistant/uat_export.py`（固定来源在线捕获、原件、去凭据、交付）；仅扩展`mac_system_service.py`/`mac_service_manage.py`的`export-uat`动作与身份校验，保留既有动作。`desktop_assistant/build.py`及管理员代码包清单必须包含并完整绑定实现，不能只增加能力名；相应隔离测试与任务卡独立提交。若需额外原件，再独立限定run_task清单导入适配，禁止泛化文件复制权限。

必须验证身份/计划变化、路径替换/链接/覆盖拒绝，数据库并发写入下快照一致，原件变化拒绝，原凭据不进入收到件或残留页，白名单外业务事实不变，失败不交付，服务进程不停止/替换，UID501只读收到件。最后通过真实受保护Mac入口做一次管理员演练。当前尚无这项实现或验收；本轮仅给可审阅设计，不新增提升权限，不据此执行认证。
