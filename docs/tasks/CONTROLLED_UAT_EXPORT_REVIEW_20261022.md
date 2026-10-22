# 固定在线 UAT 导出候选交接

当前仅本地候选代码和合成临时库验证，未正式安装、认证、捕获、导出或运行真实副本验收。独立于 GT142 事实追加候选及 fa1022df 迁移。v715 正式来源由发布负责人反馈为 80e2fff6b786d2bb460435d37d3786a01e8e45ff / ez1022hc；发布时必须重新核对，不把该事实固化为未来授权。

## 新增受控入口

在重新审查、构建的完整管理员最小代码包 `mac_keychain_install.py` 中增加两个动作：

- `uat-export-plan --package-sha256 <已签名并root批准的exporter包SHA>`：通过原有正常管理员认证，以固定服务 UID 运行签名 worker 的只读 inspect；读取当前实际应用、内核进程、数据库revision、路径配置及独立supervisor事实。只生成 root 644 的固定计划文件，不捕获业务。输出 attempt 与精确计划SHA，1小时有效。
- `uat-export-apply --export-attempt <同一32位attempt> --approved-sha256 <审查通过的计划SHA>`：再次正常管理员认证，核验计划与所有身份，再捕获并交付。本入口不接受自由目的地、源路径、UID、脚本或其他通用管理参数。

普通 manage 的五个动作及其权限/回执合同保持原状。新 worker 必须同时具备原管理协议与 `controlled_uat_export_20261022_v1=1` 签名能力及精确六文件集合。当前应用、已安装supervisor、执行exporter包分别绑定；只发布可执行exporter包不表示切换当前应用或升级独立supervisor。

管理员代码清单新增 contract、broker、sanitizer 三模块，必须连同原精确 FILES 完整重建及独立 SHA 审查。不能将新文件临时塞入已安装管理员目录、用临时 root 脚本调用，也不能只添加能力名。

## 原生操作顺序（尚未执行）

1. kimiyf 解锁 Mac。由 UID501 在自己的 workspace 中准备固定新父目录 `/Users/kimiyf/ERP-Migration/isolated-sources/delivery-fulfillment-20261022`，其路径必须无链接、不在Git工作树、无写权限放宽。root 入口不会代改这些目录权限。
2. 发布负责人按现有受审查程序包/管理员最小包流程审阅本候选，构建签名 exporter 包与独立管理员包；用户在正常 macOS 认证窗口批准精确代码包的 root 发布。此步骤不运行新ERP应用、不停服、不迁移。
3. 用户通过正常原生管理员认证运行上述 plan 动作，取得一份可审查实际计划。确认任务、UID501、固定输出目录、当前应用/监护/exporter SHA、预计原件范围与“业务PII未匿名化”事实。
4. 用户通过正常原生管理员认证批准同一计划 SHA 的 apply。任何包、进程、源状态、配置、ACL或目标变化都需要新的计划；旧 attempt 不覆盖。失败保留服务UID私有捕获目录或root私有交接目录，没有自动清理其他历史目录的动作。
5. 接收者复核 `receipt.json` 和全部文件 SHA，随后仅从新收到的 `database.sqlite3` 与 `layout.json` 经 `scripts/uat/run_task.py` 创建自己的 owned copies。PDF源路径已在私有副本按行单独记录重绑；run_task 只自动隔离PDF训练源，其余导出原件未自动接入所有UI。GT142迁移/业务验收另在其候选树执行并把证据保存到job外。

以上是交接顺序，不是已经取得的批准。没有真实副本前不能报告 fa1022df 正式来源迁移验证通过。

## 数据和交付保证

- 服务 UID 使用只读 SQLite backup；维护锁仅排斥维护，业务数据库继续正常写入。冻结的是一致快照，不承诺在线数据库停止变化。
- 运行地图来自已核验 server/data→shared/data 的受管链接或精确受管配置，文件不存在不回退模板。原件运行根若配置覆盖到其他目录则拒绝，不能从默认目录残留文件冒充实际原件。
- 原始快照留在服务UID私有 staging；正式配置、session_secret文件、Keychain、备份密码、AI配置、日志和程序不导出。配置文件仅为核对受管路径在内存读取，内联 ERP_SECRET_KEY/OPENAI_API_KEY/DEEPSEEK_API_KEY 存在即拒绝，不读取任何引用的秘密文件。
- 只修改 users.password_hash/auth_version、email_intake_settings.encrypted_secret/automatic_enabled。schema、trigger SQL与非凭据业务摘要相同；临时去除触发器再原样重建避免额外业务写入。VACUUM生成新紧凑库，并拒绝在业务值或残留页中仍存在已知原凭据。未知凭据列fail closed。
- root独立核验清洁报告完整字段及数据库SHA，按清单经FD复制到受保护新root0700外层；全部验证完成且最终来源锁检查通过后，仅移交本批新payload，再以macOS renameatx_np(RENAME_EXCL)原子发布，不覆盖现有目标。输出文件0600、目录0700。root不改变正式目录/安装/身份权限。
- 明确保留业务PII、客户信息和价格；不宣称匿名化。原件范围见任务卡，不代表全量附件恢复，也不包含第二个twin库的原子快照。

## 未过的实际门槛

- 新exporter签名包构建/发布及管理员完整代码包独立审核未执行。
- 正常macOS认证、专用UID真实online capture、root→UID501真实交付和实际失败保留演练未执行；隔离测试不代替这些事实。
- 未取得真实去凭据副本，因此GT142候选的非测试迁移/往返/业务验收仍未完成。
- GT142迁移引入后独立已装supervisor reader更新仍属另一候选的硬门槛，本导出功能不会自动解决它。
- 所有当前schema/路径/原件缺失会fail closed，不能保证现有正式来源一定满足本固定合同。具体失败不得临时放宽目录/凭据/副本门禁。

本worktree为raw git，勿调用Codex managed archive清理。

## 开发验证记录

- 新协议/在线捕获/FD交付/来源锁/认证输入及去凭据测试共70项；连同原mac_service_manage 63项，最终133 passed（1.30秒）。原恢复夹具须在 pytest 收集前预载 `tests.desktop_assistant.test_recovery`，避免该旧测试在 Manager 被mock期间懒导入导致测试基类变成Mock；未改旧业务测试或运行代码规避此问题。
- 管理员代码包、系统管理、当前Mac构建和通用构建定向回归：214 passed（2.92秒）。首次测试抓到最小包builder与bootstrap精确清单不一致，已同步新模块及原本遗漏的publisher trust模块，回归通过。
- 4个受审查管理员模块通过Apple Python3.9语法核验；sanitizer另有Apple Python3.9实际sanitize/verify执行通过。home标准deny-delete ACL仅只读原生核验通过。
- 原子发布测试真实调用macOS renameatx_np(RENAME_EXCL)，覆盖目标并发出现时拒绝覆盖；测试当前UID临时目录不等于root→服务UID→用户UID现场认证已验收。
- 独立子代理复核已关闭清洁哈希/清单关联、服务UID不能读home、home标准ACL、用户父目录mkdir/open竞争、交付最后来源锁、运行存储根配置与PDF白名单问题。当前未发现新的具体代码阻断；上节真实发布/副本门槛仍未完成。
