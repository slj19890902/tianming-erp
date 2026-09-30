# P0-5 恢复必要结构契约返工回执

2026-09-30 最新状态：老板批准的前轮五项已技术发布v0.22.539（源码00e20835，包91daa06f，ed0928ml无迁移），不再等待人工验收；下文“待批准/未发布”为历史状态。正式数据未修复。完整发布/备份/清理证据见 docs/release_reports/ROUND_UPGRADE_20260929.md。原隔离体验实例已停止，关键证据迁C:/ERP-OPT10-20260930/retained-evidence。

状态：独立复审发现缓存和动态 Base 缺口后已返工，98 项集成回归通过；最终d6c15d66候选真实受管备份/恢复/启动/登录、18路径/5阻断及Chrome验收通过，待人工验收/待上线。未执行正式发布、正式恢复、正式停服/重启、正式配置修改、数据库迁移或历史数据写入。

## 问题与红灯

旧恢复校验仅要求 `users`、`customers`、`products`、`sales_orders` 四张表。隔离用例从完整恢复库删除 `sales_order_items`，再用该残缺库重新生成完全一致的 revision/counts 恢复清单；修改前 `Manager.restore()` 没有抛错并激活了残缺结果，红灯为 `1 failed: DID NOT RAISE ValueError`。

这证明恢复包内部数据库清单只能证明包内文件前后一致，不能由待恢复库自己证明“必要结构完整”。

## 实现边界

- 新增 `desktop_assistant/schema_contract.py`，只用 Python AST 解析已经签名的发布包模型源码，不 import、不 exec、不运行包内 Python。
- 恢复直接读取恢复包内的 `release.zip` / `compatibility.zip`，验证发布签名、完整文件清单及每个被消费模型文件的 SHA-256；不信任 `stage_release` 已提取缓存中的可修改 manifest 或源码。
- 静态解析 `app/models/__init__.py` 的注册模块闭包，再收集所有已注册模块中的直接 `Base` 表类、静态 `__tablename__`、`mapped_column` / `Column` 和无显式赋值的 `Mapped[...]` 隐式列；静态位置参数或 `name="..."` 关键字列名均绑定到真实数据库列名。
- 对当前契约语法可能漏收结构的已知旁路明确拒绝：动态/条件导入、星号导入、未注册模型模块、非顶层或条件内表类、类内条件表名、动态表名、mixin/未知继承、`Base` / `Column` / `Mapped` / `mapped_column` / `relationship` 导入或赋值别名、SQLAlchemy 模块别名、动态列名、列工厂、`**kwargs` 列参数及未知 `Mapped` 赋值。
- 当前源码得到 263 张必要 ORM 表、3829 个必要列；结果与当前受信源码的 `Base.metadata` 逐表逐列相等，并包含两个隐式 `tax_included` 列。
- 新构建包由 `desktop_assistant/build.py` 从干净提交快照生成 `schema_contract` 并写入签名 manifest。没有该字段的旧签名包仍可从逐文件验哈希的模型源码派生同一契约；无法静态解析的旧包拒绝，不执行其代码。
- 普通恢复使用 active release 的签名结构契约。程序回退但数据库停留在新 revision 时，保留原 `compatible()` 的业务规则，直接用验签后的 authority manifest 核对 revision、`preserve_existing_facts_v1` 和 `rollback_package_sha256`，不读取可修改缓存来裁决兼容；结构契约取自该 authority 包。
- 初次必要结构校验在任何恢复库写入前执行；PDF 路径重绑定和附件预检后，在 `shared.rename()` 与 state 激活前再次执行 `database_info()`（integrity、foreign key、唯一 revision、counts）和完整必要表列校验。
- 不新增或修改 migration，不改变数据库结构或业务数据。

## 测试证据

隔离 runner 会清除 `ERP_*`、`TM_ERP_*`、OpenAI/DeepSeek 密钥，将数据库、附件和临时目录固定到 `D:/ERP-AUDIT/20260929-comprehensive/synthetic-tests/*`，阻断边界外文件/数据库、socket 和普通子进程。因生产实现的 NAS 探针使用子进程，测试适配器在同一隔离进程内直接执行原 `nas_probe.probe()` 的创建、写入、`fsync`、读回和删除；没有跳过 NAS 读写行为。

- 修改前红灯：缺 `sales_order_items` 且 manifest 自洽，`1 failed`，未抛错。
- schema/恢复/备份/迁移/跨版本/P0 入口组合组：`88 passed`，JUnit 为 `errors=0 / failures=0 / skipped=0`，隔离证据在 `D:/ERP-AUDIT/20260929-comprehensive/synthetic-tests/231004/`。
- 新增定向覆盖：当前 263 表/3829 列、签名 contract、旧包 fallback、隐式列、静态 keyword 列名、动态 keyword 列名拒绝、结构符号/模块别名拒绝、非顶层及条件表定义拒绝、缺表、缺列、签名后模型字节篡改、包内顶层写文件代码不执行、rebind 后最终复核、正常旧包恢复、跨 revision authority 正反例，以及所有失败保持 `current=None`、`shared` 为空。
- 构建源测试使用清空 ERP/助手/模型密钥环境的独立进程，临时目录固定在 `D:/ERP-AUDIT/20260929-comprehensive/build-tests-p0-5-schema-20260929-2312`；仅允许测试夹具本地 Git 操作，结果 `6 passed`。
- 只读核验正式 `state.current` 指向的签名运行包 `9bd7b2cd86d8a00a2da5b4265430d647e5b8ad48aafbd35ddae296a649183f7f`：包文件 SHA-256 与身份一致，使用已安装助手内嵌发布公钥验签并逐个核对 78 个模型文件哈希，旧包 fallback 得到 `v0.22.538 / ed0928ml / 263表 / 3829列`。未恢复、未启动、未读取数据库、未写正式目录；证据为 `D:/ERP-AUDIT/20260929-comprehensive/formal-package-schema-readonly-evidence.json`。

## 未覆盖与最终门禁

- 本轮没有用正式数据库、正式完整备份或迁移后的正式副本执行恢复。
- 2026-09-30补证：`dc8dadac` 包含完整结构和独立返工，已在r6真实执行 `Manager.restore -> Manager.start -> 登录/业务读取`，完整记录在 `D:/tm-uat/round-upgrade-20260930-r6/recovery-runtime-evidence.json`。集成其他业务后的最终包仍须再次验证；旧四表版本的证据不替代本修正。
- 旧包 AST fallback 保证 ORM 必要表和列存在，允许额外历史表/列；它不宣称逐项验证所有索引、CHECK/UNIQUE/FK DDL 与默认值。原有 SQLite integrity、foreign key、唯一 revision、表计数、附件哈希和发布兼容门禁继续保留。
- 本轮只核验了现场当前 v538 签名包，没有遍历全部历代包；合成旧包及跨 revision 回退包提供真阳性。解析器只保证上述直接声明式模型语法及已列明旁路，未宣称识别任意未来 ORM 框架或自定义元编程；未来若引入命令式 `Table()`、自定义映射装饰器或其他生成方式，必须先扩展离线解析与反例，不能让构建/恢复降级回四表门槛。
- 安全回退不得完整撤销 P0-5 或重开网页恢复。若结构解析器误拒绝旧包，保持空目标和停服状态，前滚补充解析/签名契约；不得用残缺库自证或执行备份内代码。

## 2026-09-30 独立复审返工

- Terra 独立复现签名包正确但已解压 main.py 被改后仍可恢复；另发现动态 Base、继承列及装饰器/元类可能漏字段。根代理保留失败用例，`round-tests/070331075852` 为8失败1通过。
- 恢复的 `stage_release(..., verify_existing=True)` 对整个签名包重新解包验哈希，并核对已有缓存的全部文件清单、manifest、逐文件SHA和链接；缓存异常明确拒绝，不覆盖缓存，更不激活数据库。正常已验证缓存仍可复用。
- 必要结构与附件的全部校验完成后才提升程序缓存，缺表拒绝时 releases/packages 均为空。staging 作为失败现场可以保留，不显示成功。
- Base 限定为唯一顶层、无成员、无装饰器的 DeclarativeBase；结构符号重定义、模型装饰器/元类均拒绝。现行263表3829列逐项等于运行模型，独立确认现行v538静态旧包仍兼容。
- 第二轮完整测试暴露隔离runner对Python3.12 `_fallback_socketpair`误拦及NAS适配异常措辞差异，记录79通过19失败；只修runner，保留业务和测试断言。
- 最终同组 `98 passed / 0 skipped`，证据 `D:/ERP-AUDIT/20260929-comprehensive/round-tests/070812094521/results.xml`。覆盖结构、恢复、备份、迁移兼容、受管状态、服务入口和网页权限。
- 独立复审 `/root/p015_terra_rework` 检查实际差异后未发现剩余阻断；r6及保留P0保护的独立回退候选已实际恢复/启动/登录，最终集成包验收继续进行。

最终运行证据：`D:/tm-uat/round-upgrade-20260930-final/recovery-runtime-evidence.json`、`final-http-evidence.json`及`final-browser-and-persistence-evidence.json`。当前模型合成库，不是正式数据库迁移演练；全部历史版本、SQL约束/索引/默认值覆盖未作承诺。
