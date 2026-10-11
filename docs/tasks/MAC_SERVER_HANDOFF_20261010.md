# MAC-SERVER-HANDOFF-20261010

## 工厂端续接核验（2026-10-11）

老板在提供 FACTORY_NEXT_STEPS_20261011.md 后明确“继续”。本轮只核验备份及3de3be32家庭预演候选、原用户运营凭据加密移交、既有身份分离签名及Mac跳过的Windows专项；不安装候选到工厂、不停正式服务、不修改正式数据库/配置、不导出发布私钥、不清空旧机。独立工作区mac-factory-review从已核验b9a077bc后继的NAS源码bundle 3d8dec52建立，原脏工作区不动。

Windows首轮定向28项中27通过，1项凭据导出测试夹具未关闭SQLite连接导致WinError32；只为该夹具补显式closing，保留事务和原断言，不修改生产导出/签名逻辑。复验、材料哈希、签名及人工口令输入状态以本轮NAS独立回执为准。仅检查、内存读取运营凭据和新增加密交付文件，不在日志或Git保存任何真实凭据。

复验28项全部通过（含Mac跳过的8项Windows实机专项，launchd项只能留Mac验收）。原备份及候选ZIP哈希与交接清单一致，29307个载荷逐项核对，1167源码与3de3be32 Git对象一致，3个构建验证产生的pyc与源文件新编译代码对象一致；schema/11项reader与工厂v625相同，alembic无变化。既有发布身份已为该家庭预演清单生成分离签名，身份文件不变，未导出私钥。正式服务与远端正式分支保持原状；运营凭据backup/deepseek/mailbox已由原用户在内存只读核验可解，实际加密移交待管理员本机隐藏输入。未把签名或合成测试当作Mac真实恢复及正式接管验收。

## Mac端实施续接（2026-10-10）

工厂2026-10-11 07:18补记：老板本机隐藏输入已完成，运营凭据包和成功回执已生成于NAS factory-cooperation-20261011目录。本轮只读检查格式/大小/完整SHA及回执声明绑定通过，archive_synced确认上传完成；不在Git保存口令或真实密文。具体指纹及Mac下一步见NAS独立回执20261011-ERP凭据移交完成与Mac接手.md。Mac认证解封、内部绑定及真实恢复仍待执行，正式v625未切换，旧机不清空。

用户已在Mac确认只读报告，进一步授权持续推进至工厂ERP完整成功运行，并要求夜间先自主执行，次日由工厂旧Windows原用户协助安全移交凭据。此授权承接下方历史交接任务，取代其“当前没有Mac会话、不实施跨平台改造”的阶段限制；全部数据、门禁、家庭隔离和旧机保护边界保持。

当前基线从实时factory-current-baseline核对为b9a077bc115cc649cdc2753cab3dfb3d75269d1c；独立候选codex/mac-server-native-20261010。当前树11658个文件已逐对象核验，完整历史另行获取，不能称完整离线历史已验收。v625 Windows包及27780个文件、原发布签名已在Mac核验；真实加密备份本地SHA一致，尚未解密或启动服务。

首个最小闭环：修复Manager维护锁在Mac导入msvcrt失败的问题。范围仅desktop_assistant/operation_lock.py、manager.py的lock入口、tests/desktop_assistant/test_operation_lock.py及本卡。保留Windows一字节锁，Mac使用内核非阻塞独占文件锁；验证同进程、跨进程互斥、异常和进程退出后释放；不改更新、恢复、签名、reader或数据库行为。不触及真实库。此阶段通过不代表Mac运行器和迁移整体完成。

维护锁结果：原Mac四项测试1失败/3错误，均因msvcrt不可用；改后四项通过。原有并发备份拒绝、完整加密备份/新机恢复、错误密码及损坏包不激活数据三项回归通过（均为临时合成夹具，无正式数据）。Windows分支保持原算法，未声称在本轮实机Windows验证。唯一head仍为ep1010md。证据留Mac任务目录evidence/operation-lock-*.log。

第二个闭环：让现有PDF样本重定位和附件预检按源平台路径语义读取Windows盘符/UNC/反斜杠。范围扩展到desktop_assistant/source_paths.py、attachments.py、preflight.py及tests/desktop_assistant/test_source_paths.py。仅在复制品重定位已知PDF字段；未知持久路径的全库盘点和审核仍是后续必须完成项，不把此修复宣称全附件迁移完成。所有引用先核验，再事务写入；拒绝越界和目标符号链接逃逸，不改源库。

路径闭环结果：新增六项通过，包含Windows盘符大小写、UNC、相对反斜杠、跨盘/父级/不完整盘符拒绝、符号链接逃逸拒绝、预检只读及失败不部分改写。连同原有PDF恢复、旧上传目录转换、附件预检和完整加密恢复共十项定向验证通过。Manager恢复入口保留原source_shared字符串，避免在目标平台提前改变源路径语义。真实数据尚未解密，未知路径未作自动替换。

第三个闭环：新增只读持久路径清单工具，逐表逐列扫描文本值及嵌套JSON字符串，记录盘符/UNC、POSIX绝对路径、受管引用和路径命名字段的相对值。范围desktop_assistant/path_inventory.py与tests/desktop_assistant/test_path_inventory.py。报告含表/行主键/列/JSON位置及原值，仅保存于受限任务目录，不上传Git；不自动替换文本，不解密字段或运行退役入口，不宣称仅靠正则便完成附件语义验收。要求静态无WAL副本，源SHA前后保持，未扫描二进制值单列计数。

盘点工具四项合成数据验证通过：未知表和嵌套JSON发现、原库SHA保持、WAL拒绝、不创建缺失库、CLI只输出数量、0600报告及重复输出不覆盖。真实备份仍待工厂原用户安全交付解密凭据；未运行真实库盘点，不将候选计数冒充正式结果。

第四个闭环：家庭预演保护。范围app/core/home_rehearsal.py、app/main.py、app/services/email_intake.py、app/services/ai/providers.py、desktop_assistant/server_entry.py、manager.py、test_home_rehearsal.py及test_ai_config.py。Mac受管启动固定预演、回环监听、跳过AI凭据加载；恢复的environment.json不能开启正式接管，原文件不改。保留ERP_ENVIRONMENT=production的权限/安全设置。手动和自动邮箱入口先拒绝，再考虑数据库/密钥；两个AI后端在请求前拒绝；预演不启动三个后台业务任务。

Python socket审计钩子阻止主动连接、UDP发送和外部DNS，仅允许回环监听；删除子进程环境里的继承代理。安装后环境变量变更不能撤销钩子。此机制不是操作系统网络沙箱，原生扩展/子进程仍须在真实恢复启动前逐项审计；工厂正式模式激活入口尚未实现，不能据此宣称已可上线。

预演保护七项验证通过：模式默认与非法配置拒绝、两种AI后端请求前拒绝、原非预演调用分支保留、真实子进程网络拦截及安装后不可通过环境撤销、本机客户端通信、恢复配置不能开启Mac正式模式、邮箱自动/手动入口在访问DB和秘密前拒绝。原AI配置五项回归通过（Windows分支明确模拟，非实机），完整备份恢复和错误密码拒绝两项合成回归通过。受管预演强制asyncio，lifespan拒绝未审计原生事件循环；完整ERP/真实库尚未启动，不声称启动验收完成。应用依赖已完成安装并冻结清单，离线OCR和打印实际运行仍待验证。

第五个闭环：受管运行器平台选择及数据目录连接，范围desktop_assistant/runtime_platform.py、manager.py、test_runtime_platform.py和test_recovery.py。旧包默认Windows，只可在Mac离线保全/恢复，启动在数据库操作前拒绝；新macos-arm64声明选择固定runtime/bin/python3.12，要求Apple Silicon、存在/无链接/哈希一致/可执行。受管启动和中断升级恢复使用归档公钥验证后的元数据选择Python，不信任可写manifest缓存来改入口，不借用系统Python。签名校验后仅给已知Python入口0755，无setuid权限。POSIX受管目录使用符号链接，拒绝覆盖陌生目录/错误悬空链接；Windows junction算法保持。

五项新增验证与原恢复回归共23项：22通过，1项Windows DPAPI实机测试在Mac明确跳过。初轮两项错误已在原b9a077bc基线复现：一项为Mac /var临时目录别名触发原防链接门禁，测试夹具改用预先resolve的真实目录，安全门禁未改；另一项是Windows DPAPI不可在Mac执行。原生签名包构建、迁移子进程、服务真实启动仍待实施，不把运行器选择视为完整服务验收。离线OCR另已用签名包模型在arm64、禁用下载条件下识别合成中文“送货单”/80012752/1600成功，证据offline-ocr-validation.json；非真实单据验收。

第六个闭环：原生迁移子进程。范围desktop_assistant/migration.py、migration_entry.py、manager.py及对应迁移/跨版本/升级链测试。迁移目标必须等于已验签发布元数据；固定发布包Python、代码搜索路径和已存在的绝对DB路径，拒绝父目录/文件链接及空库创建；不继承API密钥/代理/Python重定向。Mac迁移必须经过哈希匹配的安全入口，在导入迁移框架前启用家庭联网限制。旧Windows包保持其原迁移入口，但不能在Mac执行。失败信息保留现场、不再笼统声称正式应用失败时“未修改托管库”。原字段事实、附件、schema、签名桥接、回退和中断恢复校验保留。

新增实进程验证复制独立Python基础运行时，执行仅含合成表的迁移框架夹具（没有运行正式迁移链、没有业务数据）；验证实际子进程路径、DB固定、外部连接拒绝、错误revision/缺库/链接/被改写安全入口拒绝。与原迁移、跨版本和升级链回归共29项通过。旧Windows合成运行器的恢复测试仅在TestManager里省略宿主匹配，保持哈希检查，不模拟实际Windows运行成功。

同轮修正cleanup.py的进程名误判：本机pgyvpn-enterpris因“enterprise”中含erp被误认为ERP且无法读取其进程信息，导致安全保留临时目录。改为ERP词首/前缀、Python/Tianming/天明识别；任何进程实际引用目标目录仍拒绝，真实应用不可读仍拒绝，归属/链接/Git门禁不变。新增3项验证包含VPN误判与真实应用拒绝；原清理门禁定向2项通过，Windows 8.3 API实机项在Mac明确跳过。未清理旧机或用户原目录，仅临时合成测试目录按原归属流程释放。

第七个闭环：原生包与分离签名流程。范围desktop_assistant/build.py、native_runtime.py、release_request.py、storage.py及两个新测试模块。Mac包绑定arm64/Python3.12，复制独立运行时和全部已安装依赖，内部文件别名实体化，外部/目录链接拒绝；复制后的Python在-I隔离和家庭网络保护下验证核心依赖导入。离线模型继续按原签名来源与EasyOCR校验信息核验。

新增--signing-request-only模式，输出release.unsigned.zip与含包/清单/git哈希、原公钥指纹的请求。未签名包不能通过原安装验签。工厂原用户用既有DPAPI发布身份、本次已审阅git SHA和manifest SHA签名，私钥不离开旧机、不自动生成替代正式身份；Mac用已有可信公钥验签合包，重新逐文件核验，输出拒绝覆盖。签名清单允许记录可执行文件，解包只给其中已验证文件普通0755，不使用ZIP携带的setuid等权限；未签名执行权限清单拒绝。

定向验证36项中35通过、1项Windows DPAPI明确跳过，覆盖签名请求/错误身份/错误审阅哈希/文件损坏/坏签名/权限升级拒绝/内部链接实体化，以及原源码构建、OCR模型与恢复安全回归。证据native-build-unit-tests.log。单元测试使用临时测试身份，未改变正式公钥或创建替代正式私钥；工厂正式签名尚待执行。

实际构建结果：源提交46477f6a0cee3efb97a20a6591477c53a2cd0904，Mac arm64候选release.unsigned.zip为535144540字节，SHA256 e52982a6e3568544fca8ddb31a3b3ea0e3f376e5c15334e6faf180eedfd59afb；29295文件逐项核验，403项执行权限声明，11项reader能力及schema_contract与已验签Windows v625完全一致，head仍ep1010md。复制后的Python3.12.15及核心依赖隔离导入成功，证据native-runtime-verification.json、native-release-contract-verification.json。包/请求/既有公钥/源码增量bundle已归档NAS mac-candidate-20261011/runtime-candidate-46477f6a，读取副本SHA一致；bundle依赖原b9a077bc，不是独立全历史。此包仅为运行时构建验证候选，未签名不可安装，不是最终迁移验收候选；打印/凭据/服务后续变更后需重新构建最终候选。未启动真实ERP或执行真实库迁移。

后续依次闭合：原生签名运行包与服务、家庭出站/收单隔离、凭据安全重封装、完整路径审计映射、全量真实恢复与事实比对、中文打印/OCR、备份及故障恢复，再在工厂停写同步与人工验收。密码、私钥、完整数据不得进入Git、聊天或日志；任何阶段不自行清空旧机。

## 授权、范围与成功条件

老板要求本厂 Windows 设备 ERP 未推送内容先提交推送，已验证功能完成发布，再交接到全新 Mac mini；Mac 在家准备，随后搬入工厂局域网成为唯一正式服务器。仅 ERP 全部真实数据、附件、配置、代码及可恢复历史资料，不包含微信及其他办公文件。用户提供 M6 / 16GB / 512GB，可在 Mac 打开 Codex；型号和实际可用空间待新机核验。

本轮最小闭环：盘点所有本仓库分支/工作树的未交付源码，区别保全与正式发布；核对现有正在发布的 v625，保全剩余旧候选；输出有证据的正式服务器迁移方案和 Mac 接手清单。当前没有 Mac 访问会话，不宣称转移或新机验收完成。不实施跨平台运行器改造、不擅自开始正式服务器切换。

## 不变量与禁止范围

- 单代理、独立工作树，从实时 origin/factory-current-baseline 1f074e81 建立；v625现有发布任务仍在执行，不抢占正式更新。结束重新核对远端及运行基线。
- 源码保全不等于已通过验证或上线；旧候选不得整分支合入覆盖已发布实现。保留原脏工作区、索引和所有无关改动。
- 数据不脱敏、不删减、不重新导入历史单据，账号/权限/数量/金额/状态/幂等/版本/审计/附件全保留。Mac 家庭预演使用完整真实备份，但不能同时接收生产业务；最终停写后再同步到切换点，正式接管后只允许一个写入端。
- 不重建退役 legacy_ruida_*；发现旧脚本引用该表则只保全、禁止执行。生产库必须在本机 SSD，NAS 仅备份/归档，不让两台机器共用网络 SQLite。
- 密钥和完整数据不得提交 Git，DPAPI 本机密文不能冒充可移植备份；必须用有密码的加密归档、受控凭据移交及异机真实恢复验证。
- 不删除旧机数据，不运行清理/磁盘擦除，不重试曾被策略拒绝的删除路径。旧机清空须所有迁移验收及用户最终确认。

## 最新业务口径（用户本轮明确）

- 80012752 每套 1A + 2B + 2C，BOM 组合套装。
- 80012753 每套 1A + 2B + 2C + 1三角；BC三角作为配片组一起存放。A模每小片出3A，另一模出2B+2C+1三角。
- 375×226 理论小片，一开二，供应商 750×236 的多出10mm是修边余量。1600套无损耗/无抵扣：A模 ceil(1600/(2×3))=267张；BC三角模 ceil(1600/(2×1))=800张。
- 旧候选文档中的3A/套和2×3分切不再作为本款当前事实。记录事实不等于已修改正式主档；混排共同产出尚须报料、库存和页面端到端实现，第二块实物模具身份须确认后绑定。

## 文件及验证

仅本任务卡、docs/plans/MAC_SERVER_MIGRATION_20261010.md、本任务审计脚本和报告，以及跨端交接必要的HANDOFF简短索引；不改变应用运行代码/数据库结构。审计脚本只读源数据，输出任务所有权目录；源码备份推送前做路径和敏感信息检查，远端逐项核验；最终差异检查，NAS独立回执。正式页面不自动点击，打印/手机/仓库操作由管理员验收。

## 本轮收口

已核验现有任务v625正式发布和备份恢复；102个分支提交、60份遗漏未提交源文件、4个内部快照/stash引用已安全归档推送，本机Git历史另加密归档。迁移方案已提交；Mac尚无操作会话，跨平台适配和最终切换未执行。未完成的共同模切完整运行链及12752编辑修复没有冒充上线。详见迁移方案第10节和NAS独立回执。

## 2026-10-11 中文 PDF 与标签闭环

Mac 默认使用系统 STHeiti Medium.ttc 的 Heiti SC（face 1），合同和工程图嵌入字体，印刷文字使用同一字面。明确配置的缺失字体继续拒绝；仅已知系统 TTC 使用 64MB 上限，其他字体保留原 50MB 上限。像素、输出大小、字体/生成结果哈希门禁保持；旧发布原件继续按原字节复用，旧草稿字体或字面变化需 409 拒绝并要求重新保存核对。字体文件不随代码或运行包再分发。

定向验证：中文 PDF/文字/工程图 22 项通过；40×30 和 40×80 标签的 1/2/100 页及像素边界 7 项通过；送货打印布局契约 8 项通过。Mac 字体会将“片”提取为兼容部首，测试只对提取文本做 NFKC 比较，不改 PDF 或业务文字。测试使用任务目录中的官方 Chrome for Testing headless shell 155.0.8059.39，静态合成 HTML、独立临时配置、关闭背景联网及外部 DNS；其代码签名为 ad hoc，不声称 Google Developer ID 验签。

三份合成样张（合同 2 页、工程图 3 页、40×80 标签 2 页）已由 Poppler 渲染并逐页目视检查：中文可读、无方框和截断、工程图尺寸标注完整、标签旋转及二维码位置正确。证据 mac-chinese-pdf-tests.log、mac-label-pdf-tests.log、mac-delivery-print-contract-tests.log、native-print-samples.json、native-print-visual-review.json。没有使用真实订单或访问正式数据库；实体针式/标签打印机的驱动、走纸、二维码扫描及现场签字尚未验收。原 46477f6a 未签名构建仅证明原生运行时，必须在后续变更完成后重建，不能作为最终候选签署。

第九个闭环范围：新增 app/core/mac_keychain.py 和 tests/test_mac_keychain.py，先实现并验证本机钥匙串后端。通过系统 Security/CoreFoundation API 在当前用户钥匙串保存凭据，普通配置只持带用途的随机引用；旧 DPAPI 值明确拒绝，不能假装跨机解密。禁止将秘密放入子进程参数或日志，不修改默认钥匙串/搜索列表/访问控制。实机验证仅在本任务新建的独立临时钥匙串中保存合成值并验证锁定拒绝，不读取个人钥匙串条目；邮箱/AI/备份接入及异机加密移交另作后续闭环。

钥匙串后端定向 8 项通过，包含独立临时钥匙串实机中文/NUL 字节往返、同引用重复拒绝、锁定拒绝及解锁恢复；测试只删除自身刚创建的临时钥匙串，搜索列表前后相同，交互设置恢复原值。生产 API 每次请求设置禁止交互；实机测试另禁用交互防止无人值守弹窗，因此不把该测试扩大为所有运行上下文的无弹窗保证。用途不符、旧 DPAPI、空值/过大值/无效编码均先拒绝；私钥用途不支持。未读取个人钥匙串已有条目、未存正式秘密、未接入业务入口。证据 mac-keychain-tests.log。

实现采用 Security.framework SecItem API 的当前用户文件型钥匙串，并限制到该用户 Library/Keychains 和当前 UID；不设置 iCloud 同步、默认钥匙串、搜索列表或 ACL。Apple TN3137 说明后台 daemon 无法使用 data-protection keychain，且新版 macOS 钥匙串文件可能依赖受保护熵文件，故不将复制钥匙串当作异机备份；后续仍须应用级加密凭据导出与异机恢复演练。来源：https://developer.apple.com/documentation/technotes/tn3137-on-mac-keychains 。当前后端是开发验证结果，服务身份及重启后解锁尚待验证，不代表凭据迁移完成。

第十个闭环范围：desktop_assistant/credential_store.py、ai_config.py、backup_settings.py、gui.py、app/services/email_intake.py 及定向测试。将邮箱、两种 AI、备份密码读写按宿主路由到带用途的 Mac 钥匙串引用或原 Windows DPAPI，签名身份继续原 Windows 分离签名；保留家庭邮箱/AI调用拒绝，不改业务表、收单游标、权限、调度或正式模式。备份计划的 launchd 注册与可移植凭据包尚未实现，不能把凭据路由当作全备份恢复通过。

凭据路由验证：17 项通过（新增 5 项含原邮箱 API 权限/版本/明文隐藏契约、原 AI 配置 5 项、家庭隔离 7 项），另备份设置定向 9 项通过。均为合成数据；路由/API 测试使用内存钥匙串替身，真实 Security.framework 往返另由第九闭环覆盖。API 回归发现旧实现先加密后检查版本，会在 Mac 钥匙串留下已过期请求的多余条目；范围补入 app/api/email_intake.py，在存储前增加现有版本预检，并保留原 SQL 乐观锁和唯一插入/审计事务门禁。修复后过期配置不会创建新钥匙串条目。

新引用不覆盖旧条目，旧凭据保留便于回滚；并发失败或进程崩溃产生的未引用条目不自动清理。Windows 分支只做模拟路由回归，不冒充工厂原用户 DPAPI 实机读取。未调用邮箱/AI 外部服务；Mac Manager 仍固定家庭预演并跳过 AI 载入。证据 credential-routing-tests.log、credential-backup-settings-tests.log。下一步继续应用级加密凭据移交/完整备份恢复和 launchd/服务用户配置；目前还不能断言无人值守重启及异机恢复完成。

第十一个闭环：desktop_assistant/credential_transfer.py 与合成定向测试，实现受控收集和应用级加密凭据移交。只读当前根目录的备份/AI 配置及邮箱凭据单行，由当前平台原用户解密；保留原配置和原密文指纹，AES-GCM 加密全部内容后独占写出，口令仅 getpass 交互输入。源配置前后核对，格式/用途/文件大小有界，错误口令/篡改不返回任何秘密。不包含发布私钥，不自动写恢复库、钥匙串或开启业务；与完整备份绑定和恢复应用是下一闭环。

加密凭据移交模块 9 项合成检查通过：四用途往返、源库/原配置哈希前后不变、输出无明文、POSIX 0600、重复输出拒绝、错误口令/密文与头部篡改/截断/超限拒绝、未知用途/路径/来源指纹不符拒绝、原用户解密失败不产出文件、导出期间配置变化拒绝、缺库不创建、链接拒绝、空配置明确保留为空、CLI 禁止口令管道且不回显。证据 credential-transfer-tests.log。AES-256-GCM，随机16字节盐/12字节nonce，PBKDF2-SHA256 600000 次；头部参与认证，文件最大256KiB，解密认证完成后才解析并返回内容。未调用真实 Windows DPAPI，未导出真实凭据。

工厂执行准备：原 Windows 服务用户在核验后的独立候选工作区运行该模块，--root 指向 D:\TianmingERP，--output 指向 NAS 上一个全新 .tmencrypted 文件；移交口令只在 getpass 本地交互终端输入两次，不能进入聊天/参数/日志。模块只读已知配置及邮箱单行，原密文和配置保留在加密内容中，发布私钥不纳入此工具。必须继续完成与恢复副本匹配及安全应用，当前只完成导出/认证解封，不宣称凭据已转移或完整恢复完成。

第十二个闭环：desktop_assistant/credential_restore.py、manager.py 及定向恢复测试。新增备份内的加密运营凭据包，恢复时先验签/验库/验附件，再核对邮箱原密文 SHA 与版本，重封装并回读目标凭据；只在未激活暂存副本更新 encrypted_secret，版本、自动收单设置、游标和业务事实保持。恢复配置仅独占写入空目标，不覆盖既有设置，状态最后激活且不启动服务。旧备份可以另传已认证移交包；无移交包保持显式未包含状态，不冒充完整凭据恢复。错误匹配在钥匙串写入前拒绝，后续失败不激活副本。

完整备份现内置单独认证加密的运营凭据包，外层仍为原完整加密备份；旧正式备份保持不变。外部独立移交新增 --backup 参数，必须绑定目标 .tmbackup 的完整 SHA256，恢复同时检查邮箱原密文指纹与版本；此前开发原型未绑定的外部包明确拒绝，不能作为正式移交。内置包通过外层备份认证绑定，不做循环自哈希。

验证结果：凭据准备/导出及原完整恢复合计38通过、1项Windows DPAPI实机项在Mac跳过；原跨版本/升级链备份回归18通过。新增恢复检查覆盖旧格式备份＋独立绑定包、新格式内置包、错版本/错密文/缺失状态/WAL拒绝、钥匙串回读错误不改库、目标配置不覆盖、数据库触发器造成额外业务写入时事务回滚、原收单位置和自动设置/版本保持、宿主拒绝时不激活、源库SHA保持。证据 credential-restore-tests.log、credential-update-backup-regression.log。宿主凭据在这些整链测试中使用合成替身，真正Security.framework独立实机验证见第九闭环，不冒充真实Windows到Mac凭据恢复验收。

新建安装根目录使用0700，引用配置用0600，已有根目录权限不擅改。状态在库、附件、发布包、凭据验证及独占配置落盘之后才写入，恢复不启动服务。失败暂存现场与新建未引用凭据保留，不自动删旧凭据；配置落盘中断可能留下未激活目标，须核查后使用新的空恢复目录，禁止覆盖重试。旧无凭据包明确返回not-included，不冒充凭据完整。真实正式备份尚未解密，服务用户/重启/GUI安全输入及工厂最终停写同步仍待完成。

第十三个闭环：desktop_assistant/restore_cli.py 和对应测试，为实际家庭恢复提供本机隐藏口令输入入口。必须核对完整备份 SHA256 与既有公钥指纹，目标为当前用户本地磁盘下全新目录，不接受已有目录、路径链接或 NAS 目标；凭据包绑定匹配后才创建目标。可在内存中从已认证移交包读取备份密码，口令不通过参数/环境/文件/管道输入。恢复调用原 Manager 验签/验库/附件/凭据流程，强制家庭联网保护且不启动服务；只输出不含秘密的回执。

交互恢复入口6项通过，包括真实macOS伪终端隐藏口令输入、完整合成备份解密/发布验签/恢复后DB SHA一致、无服务进程、口令不出现在终端记录和回执；错误公钥/备份指纹在口令提示之前拒绝，外部口令错误不创建目标，已有/链接/目录外目标拒绝，stdin管道拒绝。另对已授权NAS路径进行只读落点检查，本机home与NAS设备号不同，NAS目标被拒绝且未创建目录。证据 restore-cli-tests.log、restore-cli-nas-preflight.json。没有使用真实恢复密码或正式数据；完整移交后的当前服务用户钥匙串及后台服务运行仍待实证。

第十四个闭环：desktop_assistant/service_supervisor.py、mac_service.py 及测试，实现签名原生包的用户 launchd 监护。只在Mac当前用户登录会话使用、固定家庭预演，不创建root daemon或正式接管；共用原维护锁，尊重人工停止/迁移失败，异常退出后可由launchd重启，服务停用只通过现有身份/nonce优雅停止，不能强杀数据库进程。登记前核验归档、公钥、原生Python和全部发布文件，plists只含非秘密参数。测试仅注册任务自有短期合成launchd任务，不注册正式ERP。服务尚要求登录，不能称未登录冷启动或现场断电恢复通过。

用户服务监护18项通过：尊重人工停止、首次接入待办与迁移/reader失败；维护锁忙时不启动或强停，独立监护锁防重复，进程退出后重启，已校验运行身份不每两秒重读大型发布包。登记校验原签名/原生运行器/全部发布文件，错误公钥、篡改代码和Windows包拒绝；同名任务不接管，外来标签即使两份plist内容相同也不得停用。明确停用设置manual_stop，经原nonce优雅停止；只移除已核对的本安装LaunchAgent，原plist按SHA归档保留，不删除其他任务。证据 mac-service-tests.log。

实机launchd检查使用本任务临时目录中的合成Python任务（非ERP）：首次exit 7后产生第二个不同PID，bootout触发SIGTERM并写优雅退出标记，最后launchctl确认标签不存在；未向用户Library/LaunchAgents写入正式ERP任务。正式定义30秒重启节流、90秒退出等待、umask077，程序参数无密钥；Interactive用于HTTP用户请求而非后台限速。AbandonProcessGroup保留监护异常退出时的ERP子进程，再由监护恢复核验，避免launchd直接强杀数据库；明确停用仍通过维护锁/Manager.stop验证。

来源为本机launchd.plist手册及Apple官方 Creating Launch Daemons and Agents（https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html）。这是当前用户登录会话能力；不冒充专用非管理员账户、注销后运行、FileVault解锁前冷启动或工厂断电恢复已通过。服务入口固定到已签名包，更新入口需先核验停用、归档配置再登记，不覆盖运行定义；完整ERP原生服务/真实库/自动备份调度仍待验证。

服务完整性复核扩展：mac_service准备时另拒绝整个发布目录链接、被改写的缓存manifest、未签名额外文件及非本安装数据链接，防止额外json.py等导入遮蔽；仅允许四种已绑定shared目标的数据链接。范围补入manager.py和migration.py：受管家庭服务及Mac迁移不生成未签名字节码，监护解释器使用-I -B；Windows正式分支不改变。服务/家庭隔离/原生迁移进程联合30项通过，含额外模块和错误链接拒绝，证据mac-service-integrity-tests.log。未删除任何未知文件来通过门禁，未接管正式服务。

第十五个闭环：mac_nightly.py、nightly.py、nightly_entry.py、mac_service.py共用验签入口、backup_settings.py宿主分派、gui.py调用及manager.py备份锁内函数抽取，配套定向测试。Mac用户LaunchAgent每小时检查本地23点到期，登记不立即运行；钥匙串读取和NAS可用性核对在停服前，所有到期复核/完整备份共用维护锁。Mac不启动原本未运行的服务；Windows原“到期备份后启动/已有备份时启动”保留。原备份失败恢复原运行状态、加密回读和人工暂停门禁不删。只做合成验证，不登记正式计划。


自动备份闭环验证：夜间到期检查、完整备份/恢复和首次接入回归44项通过、1项Windows专用实机检查跳过；备份设置定向9项通过；共享签名入口/服务/家庭隔离回归20项通过、1项需显式启用的launchd实机检查跳过（此前第十四闭环已有独立合成launchd实证）。证据mac-nightly-tests.log、mac-nightly-settings-tests.log、mac-nightly-service-regression.log。新增合成全备份往返验证未运行状态不会被自动启动，并发到期检查由原维护锁拒绝重复执行；NAS不可用时先拒绝再停服。

Mac计划每小时整点检查北京时间23点的备份是否已完成，错过时在后续运行补做；当前实现需要该用户保持登录，睡眠/注销不冒充连续后台运行。未运行原生包时可先安全保存NAS及备份凭据，明确显示定时任务尚未安排，以便先备份再安装原生包。运行包升级后旧计划不自动覆盖，须在维护锁内核验停用、归档原配置，再为新签名运行时登记；同一版本改密码可复用经指纹和归属核验的已登记计划。没有登记实际ERP备份任务。

原首次接入测试使用Windows合成发布包，在Mac先被宿主运行器门禁拒绝，已以原提交604e2cf4复现同样行为（self-service-platform-baseline.json）。仅调整该测试的运行器模拟以单独验证首次接入门禁，生产Manager.start及Windows运行器拒绝保持。真实正式备份、真实钥匙串凭据、正式调度及工厂最终停写同步仍未执行。

第十六个闭环范围：desktop_assistant/home_process.py、manager.py、migration.py及定向测试。在当前Mac家庭服务和迁移子进程启动前追加系统进程出站限制，弥补Python审计无法覆盖原生socket和后代进程的缺口。只作为家庭预演额外门禁，保留Python网络/回环限制，不改变Windows正式行为，不修改系统防火墙；系统工具不可用时拒绝降级启动。Apple已弃用自定义sandbox-exec接口，必须记录当前OS实测限制，不冒充长期受支持的完整安全沙箱。


家庭进程联网隔离验证：14项通过，包括原生libSystem TCP connect/UDP sendto与后代进程均返回EPERM（不安装Python审计钩子的真实子进程）、未限制的合成对照成功、受限服务仍可接收本机浏览器并回复、exec后PID不变、原生迁移实进程及原Python网络门禁回归。测试只连接本机合成监听器，从未访问外部/工厂端点。Manager/运行器/服务相关26项通过、1项独立launchd实机检查未在本轮启用。证据home-kernel-egress-tests.log、home-kernel-manager-regression.log。

仅受管Mac家庭服务及原生迁移使用/usr/bin/sandbox-exec固定出站拒绝策略，不提供自定义策略覆盖，不改防火墙、NAS挂载或Windows生产启动；系统工具缺失/非root所有/链接替换时启动前拒绝，执行策略失败时原启动/迁移失败门禁继续保留。原Python审计仍负责禁止外部域名与非回环监听。此系统策略未限制文件/IPC，也不声称能隔离恶意代码借助其他系统服务间接联网；直接手工Python启动不自动获得该系统保护。

Apple DTS明确说明自定义sandbox语言不作为第三方支持接口，sandbox-exec已弃用（https://developer.apple.com/forums/thread/661939）。本方案只是当前macOS27.0.1实测的家庭进程额外门禁，升级系统后须重验，不能推广为长期受支持或完整安全边界；正式服务仍需单独明确启用设计与验收。没有在真实数据上启动服务、解密备份或接管工厂。

第十七个闭环：助手334项全量Mac回归发现21项失败，逐项核对后修正测试夹具，不放宽运行门禁。链接测试在Mac创建真实POSIX目录链接，在Windows保留真实junction；模拟Windows命令的测试显式提供SystemRoot/creationflags/FUSE API，仅真正Windows PowerShell及DPAPI/ACL实机检查加平台跳过。NAS维护锁路径Mock绑定真实_backup_locked；恢复结果核验新增完整凭据状态；启动状态夹具补齐发货保护契约，避免把不可撤销Python审计钩子安装到pytest主进程。腐坏凭据配置应拒绝完整备份和升级，单独验证原库/配置/当前版本保留，保留有效偏好下发布源选择回归。生产应用代码本闭环不变。

助手全量Mac回归修复后335项：326通过、9跳过、无失败，27.65秒。新增一项损坏凭据配置拒绝升级检查。9项跳过包含Windows PowerShell3项、发布身份Windows ACL/DPAPI3项、运营凭据Windows DPAPI1项、Windows8.3路径API1项、需ERP_TEST_LAUNCHD显式启用的实机launchd1项；此前合成launchd实证仍保留，不替代原Windows用户验收。3条告警来自依赖弃用及测试短合成JWT密钥，未涉及正式凭据。证据assistant-suite-mac-final.log。

全部修改仅在测试和任务卡，没有改变应用/Manager生产安全逻辑；原生候选b仍绑定3de3be32，源文件字节不变，不因测试修复重复打包或替换其签名请求。真实正式325表/2983文件、原账号权限、真实凭据和业务链仍待完整真实备份恢复后核验，不能以335项合成测试替代。


## 2026-10-11 完整真实副本首次 Mac 服务验证

原始已核验恢复目录 factory-20261011-0718 保持不启动，另建完整家庭工作副本 factory-home-20261011。保留 production 权限校验，按既有 lan_http 配置校验改为本机回环地址，并启用家庭收单、队列和出站保护；已验证8条PDF绝对路径在工作副本重绑定，其他历史路径不批量改写。

通过原工厂公钥核验的 Mac 包 SHA256 `4e0653a6a40e75ad78f8fada0851411c4efbef668540a945ca353062eb104ae9`，运行代码仍为3de3be32；测试/交接文档后继不冒称已重新构建。Manager.update 在真实325表副本上先核对签名、reader及ep1010md结构兼容，再执行完整加密NAS备份，解密回读和最终副本SHA验证后激活 v0.22.625；没有执行schema迁移。

实际原生服务成功启动：健康接口200、frontend-v2页面200、未登录auth/me为401；随后按原nonce流程正常停止，未改账号密码、未提交业务单据。原始恢复基准数据库SHA保持不变。证据 real-home-preparation.json、real-home-service-summary.json。家庭工作副本不代表工厂正式接管。

更新前备份20261011-073238-e23e58e1.tmbackup的SHA256为`a9348fd2b998af3b127cf727b71481b73049e45056d59d00b7fc3806b9540d69`。复核发现工作副本初次复制未带入control/deepseek-provider.json，因此该次备份不作为三类凭据完整恢复验收依据；原始基准中的AI配置始终保留。已按原字节补齐控制目录配置，并核对backup/deepseek/mailbox均能由本机当前用户读取；正在另行生成Mac原生完整备份、恢复到第二个空目录，成功证据另记，不能预先勾选通过。

历史company_file_index的31027条源路径分属原C/D/E/F/K盘，已全部保留；运行代码搜索仅见旧main.py客户文件数量查询，没有发现源路径打开调用。这不证明历史外部文件已全部可访问，仍须独立映射/档案核验。真实业务附件888项完整可读不等同全部历史外部引用已验收。

当前仍缺原账号人工登录、完整业务代表流程、真实服务身份登录/重启、另一可写备份介质、工厂最终停写同步、唯一写入端、手机/旧二维码和实体打印验收。不自动推送正式分支，不清空旧机。


## 2026-10-11 Mac原生完整备份第二空目录恢复通过

家庭工作副本已补齐control/deepseek-provider.json；三类运营凭据backup/deepseek/mailbox分别在当前用户维护环境和工厂签名包内原生Python中读取成功，未输出值。原生解释器检查启用家庭内核出站限制，启动后的29307个签名发布文件哈希全部一致；这不是launchd登录/重启后解锁验收。

Manager.backup生成NAS `mac-home-rehearsal-20261011/20261011-074214-0dbc0a3d.tmbackup`，1233356238字节，SHA256 `267a87bfcc413af1f23d82eb45df6cdd259d248a32d1a2859449fa1ab3b1f56f`。完成认证解密、归档回读、最终NAS副本SHA核验后，Manager.restore恢复到新的本机空目录mac-home-backup-20261011，程序v0.22.625、schema ep1010md、三类凭据全部恢复。未启动第二恢复目录。

独立比较通过：325表计数相同；结构/索引/触发器相同；全表全部原字段业务事实一致（PDF路径重定位和邮箱密文重封装字段单独检查）；全部非数据库文件SHA一致；888项登记附件ok、缺失/外部/哈希不符均0；三类凭据值在内存逐项相等，邮箱版本未变。家庭工作副本与原恢复基准相比，业务事实不变，非数据库文件只有environment.json按回环配置修改，无新增或丢失文件。两份目录均处于停止状态，原始Windows备份恢复基准始终未启动。证据real-home-second-restore-summary.json、packaged-real-credentials.json。

此备份中的数据仍源自2026-10-10工厂冷备，时间较新的Mac打包时间不代表已同步工厂后续交易。工厂最终停写后的新备份/全量事实校验和唯一写入端门禁仍不可省略。Mac保持FileVault开启；另一可写介质尚未落实，不能将只读NTFS盘计为已验证恢复副本。未配置工厂正式激活、未连接工厂数据库、未发信/收单、未清空旧机。


## 2026-10-11 当前用户launchd真实服务与凭据检查通过

使用既有工厂签名Mac包，在已完成完整备份与第二空目录恢复的家庭工作副本上实际注册用户服务。签名文件、受管数据链接和服务入口归属门禁全部通过；launchd启动真实副本成功，健康和页面200、未登录401，监听仅本机回环。按原mac_service.disable流程正常停用，手动停止状态保持，服务及本次登录项均已解除，其他登录项不变；工作数据库和原恢复基准数据库完整SHA均保持不变。未使用root daemon，未重启或注销当前用户。

另由launchctl注册一次性独立凭据检查进程，使用同一签名原生解释器和家庭内核出站限制，backup/deepseek/mailbox三类运营凭据均在当前已登录GUI用户的launchd上下文中读取成功，值未输出。一次性检查不是ERP后台业务任务，不把它冒称重启后解锁证据。

原mac_nightly注册流程、同配置重用和归属停用在实机通过；人工停止状态下触发一次计划检查，签名nightly_entry返回skipped，没有启动ERP、读取业务邮箱或执行新备份。检查后计划注册已解除，其他登录项及数据库SHA不变。实际新备份和恢复已由上一闭环单独证明；本轮不把跳过状态当作到期备份成功。证据real-launchd-service-summary.json、real-launchd-credentials-schedule-summary.json。

远端factory-current-baseline本轮只读核对仍为b9a077bc115cc649cdc2753cab3dfb3d75269d1c，唯一Alembic head为ep1010md。当前仍须实际重启后的用户登录/FileVault解锁和服务验收、业务代表流程及工厂最终切换；服务安装验证不等于已获准家庭承接生产业务。保留原始恢复基准与二次恢复目录用于后续迁移/人工验收，未按阶段检查成功提前清理。


## 2026-10-11 原生业务回归中的应用工厂重复初始化修复范围

当前已签名运行包与真实预演目录保持原字节。签名原生Python运行候选源码的56项业务契约测试，55通过、1失败；现有test_create_app_can_reinitialize_after_middleware_has_started复现BusinessVisibilityMiddleware在清空Starlette已启动堆栈前注册，导致第二次create_app失败。初次完整UAT环境与该组合成fixture的cookie/启动凭据/文档路径要求冲突，失败日志保留，不作为业务故障或通过证据；按仓库普通pytest隔离规则使用全新合成fixture后定位本问题。

本闭环仅修改app/main.py中该中间件的重新配置顺序、现有tests/test_phase2_auth.py的单层保留断言及本卡/迁移方案。权限投影中间件必须始终存在且只有一层，保持原相对顺序，不能删减敏感字段防护。复验56项原生业务契约和既有scoped_business_approvals回归；不修改业务数据库、签名包、原账号或正式部署。源码修复后旧3de3be32签名包仍是旧代码，后续须重新构建并按工厂身份审阅签名，不在原包就地打补丁。


## 2026-10-11 原生业务契约100项通过与源码修复

原生Python运行普通pytest独立合成fixture后，原56项中55通过，现有应用工厂重复初始化测试失败：BusinessVisibilityMiddleware在Starlette中间件堆栈重建之前注册。修复仅将该注册移到原重建位置，并在重建时清除旧同类层后注册一层；保留原相对顺序及敏感字段过滤。既有回归增加单层保留断言。没有删除权限或过滤功能，没有修改业务数据、schema或签名包。

复验100项全部通过、0失败/错误/跳过，用例覆盖phase2_auth、auth_revocation、delivery_dispatch_commands、order_inventory_reader_contract、finance_manual_cas_idempotency、scoped_business_approvals、transport_security。运行解释器及依赖来自工厂签名原生包，应用代码是当前候选源码；证据mac-business-contracts-fixed.xml/log及运行所有权回执。源恢复基准数据库SHA仍不变。

早期run_task完整UAT入口正确拒绝venv符号链接；改用签名原生解释器后，普通合成fixture与UAT专用Cookie/路径/启动证据配置冲突。保留两份失败任务现场和日志，不把它们当作业务完成或忽略门禁的理由；本轮通过结果仅为普通隔离合成用例，不冒称完整真实业务人工验收。

当前已安装和实机核验的包SHA仍4e0653a6a40e75ad78f8fada0851411c4efbef668540a945ca353062eb104ae9，运行源码仍3de3be32，未就地改动。上述修复尚待与后续Mac正式激活工作合并构建，再按工厂既有身份审阅签名。当前Mac管理器继续强制家庭预演，正式激活流程尚未实现，不能手动改环境变量当作完成迁移。


## 2026-10-11 正式切换凭据与全量比对基础层

当前完整目标仍包括独立于用户登录的系统级launchd、专用低权限账号、该上下文钥匙串、最终停写备份/单写切换及现场验收。已通过的当前用户LaunchAgent不能替代这些要求。先实现desktop_assistant/factory_cutover.py及对应测试：严格版本/用途隔离的Ed25519票据格式，绑定目标硬件摘要、独立安装ID、服务UID/GID/专用账号/根目录、已审阅程序包和网络配置摘要；最终备份必须晚于目标申请，有限激活时窗与运行后验证分开，防止票据过期误停已激活服务。

同模块只读捕获全量静态事实：schema/索引/触发器、所有表原字段（含sqlite_sequence）、全部共享文件、保留配置和规范化PDF引用；只允许已知PDF根路径、邮箱密文及明确网络配置差异，邮箱版本/其余配置仍参与指纹。注册附件缺失、WAL/热日志、链接或读取期间变化拒绝。凭据内容恢复仍须单独通过认证加密包校验，不能用排除密文列代替秘密一致性。

该闭环只实现协议及只读证据基础，不接入Manager正式模式、不签发真实切换凭据、不停工厂服务、不安装系统账号/Daemon、不修改真实库。源端停写/最终备份核验和签发入口、Mac最终接收/安装、系统服务身份及后台凭据继续分别接入并验证；全部完成前家庭门禁不变。测试私钥仅限临时合成身份，既有工厂私钥不来Mac。


## 2026-10-11 正式切换协议与只读完整事实核验结果

新增factory_cutover协议基础层，尚未接入正式启动。票据使用既有发布身份的Ed25519签名及独立用途前缀，严格拒绝重复/未知JSON字段、错误身份或签名、不同目标/账号/组/安装目录/程序/网络申请、早于申请的旧备份和不明确停写声明。目标固定专用_tianmingerp账号及同名组，UID/GID不得为0；本机身份采集要求非管理员、真实/有效UID一致、目录在本机数据盘、归账号独占且祖先不能被其他账号改写。没有创建该系统账号或真实目标申请。

目标申请有效期最多7天，切换激活时窗最多8小时；安装年龄检查与持续签名绑定核验分离，不能把票据过期实现成运行八小时后自动停机。该层只核验绑定和只读事实，签名codec不是源端停写证明器；源端真实停写/备份认证/隔离确认、目标真实凭据验证、受保护激活记录及正式启动仍须后续接入，不能只调用codec就宣称可切换。

65项定向验证全部通过，0失败/错误/跳过，含新协议及原发布签名、Windows路径语义和家庭隔离回归。用临时合成身份测试签名，不读取既有工厂私钥；生产环境未签发任何切换授权。证据factory-cutover-contract.xml/log。

新比对器在三个已核验Mac目录执行只读实测：每份325表、2983个非数据库共享文件、888项登记附件，revision ep1010md；全表原字段摘要（含sqlite_sequence）、完整结构、全部共享文件、规范化PDF引用及保留配置一致，数据库原SHA读取前后不变。只对既有PDF根路径、邮箱密文重封装及明确网络配置作技术差异处理；邮箱版本等其余列仍参与比较，真实秘密的一致性必须另经认证加密备份验证。此结果不是工厂最终停写事实，也不证明历史外部文件引用全部可取回。证据factory-cutover-real-snapshot-comparison.json。

正式部署继续要求系统级LaunchDaemon及专用低权限服务身份，不能把当前用户LaunchAgent验收替代独立于登录的服务。Apple说明用户agent仅在用户登录期间运行，系统daemon由系统launchd加载；系统上下文钥匙串须使用file-based实现。当前原型的个人用户钥匙串/LaunchAgent不因此获得正式部署资格，FileVault保持开启；系统级凭据和重启链尚待实现/实测。来源：https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html 与 https://developer.apple.com/documentation/technotes/tn3137-on-mac-keychains 。

下一步依序接入：源端已停写状态及最终备份的认证核验/签发；专用服务身份与系统级后台凭据；目标恢复/本机身份/配置/服务安装的原子接收；最后在工厂明确停写和旧端隔离后启用唯一正式写入端。家庭Manager、出站和队列门禁本轮未改。没有安装或启用系统Daemon、没有改变钥匙串/个人账号、没有连接工厂正式库、没有清空旧机。


## 2026-10-11 切换完整性检查收紧与额度检查点

在接入工厂最终停写备份核验之前，对既有切换基础层复审并修复五处漏检：文件枚举改为遇到目录扫描错误立即拒绝，特殊文件不得静默略过；全表列清单和事实读取使用table_xinfo，纳入生成列；仅邮箱id=1可按现有认证恢复入口重封装，其余行密文及类型继续参与事实摘要；Mac目标目录及全部祖先必须同时满足原所有权/mode规则并无扩展ACL。ACL读取使用已打开目录FD及对象身份核对，缺失目录和真正读取错误不能当无ACL，原权限不修改。范围为desktop_assistant/migration.py、factory_cutover.py、新mac_path_security.py及test_factory_cutover.py，另更新本卡、迁移计划和任务回执。独立只读复审不读取正式数据、不写文件，所有代码/数据操作仍由主执行者串行负责。

原HEAD 6ff9e01d下新增5项合成回归均复现失败；修复后切换、迁移、结构约束、完整备份恢复合计80项通过、1项Windows DPAPI实机项跳过。macOS ACL真实API验证仅操作pytest新建目录：无ACL通过、增加合成ACL后拒绝且检查前后ACL原样，目录不存在和5类读取错误拒绝。证据factory-cutover-strict-inventory-before.xml/log、factory-cutover-strict-inventory.xml/log。

三个已停止的Mac完整恢复目录只读重验通过：每份325表、2983非数据库共享文件、888登记附件；结构、业务事实、全部共享文件和保留配置一致，三份DB原SHA及state.json均未变。证据factory-cutover-strict-real-snapshots.json及verify_strict_cutover_snapshots.py。事实摘要新增非主邮箱密文范围，旧摘要保留为历史证据，后续两端必须使用同版核验器，不能跨算法比较。此处仍是10月10日冷备派生的Mac副本，不是工厂最新业务或最终停写验收。

已接收协调任务提出的额度与可恢复检查点要求。每轮先完成风险相称验证、差异检查、独立候选提交和NAS回读核验回执，再启动下一闭环；异常时保存当前阶段、未完成步骤、活动进程和安全续接点，不强杀复制/写入/迁移进程，不假设额度归零后仍有能力收尾。相关额度确认为0或返回明确耗尽错误时不再启动新工作；恢复前通过支持接口核对实际可用额度，再从最新回执核对工作区/活动任务后续接，避免重复运行。普通断线、capacity或接口兼容错误不当额度耗尽。此次支持接口实测Codex周窗口已用87%、剩余13%、ordinaryUsageAllowed=true；没有耗尽证据，没有购买、兑换重置或改变计费，也没有创建定时唤醒。证据factory-cutover-strict-inventory-usage.json。协调消息本身不扩大正式停写或上线授权。

本轮仅完整性修复完成。源端认证最终备份/凭据比较和签发入口尚未实现，本轮未签发真实切换票据。下一项先在维护锁内核验已停止Windows来源、认证解密最终备份、原始DB SHA/全部文件/已签名程序及实际凭据一致；保持旧端停止，不能仅凭布尔声明调用签名codec。随后仍须完成系统级LaunchDaemon、专用账号/后台凭据、Mac接收及工厂现场验收。当前签名家庭运行包仍源码3de3be32，以上新代码未在运行包中，后续重建并由原工厂发布身份另行签名；不得就地改签名包。家庭保护不变，工厂停写/最终同步/旧端隔离/正式接管仍须工厂当次明确授权，不清空旧机。


## 2026-10-11 完整恢复保留订单库存reader门禁

源端切换核验接口审查发现必须先修的实际缺口：order_inventory_activation只存state.json，原完整备份/恢复没有携带。源数据、附件及schema一致也不能证明这一持久reader门禁保留。本轮范围desktop_assistant/order_inventory_contract.py、manager.py、storage.py及对应恢复/reader测试；未接入真实切换签发入口。

新备份把原订单库存reader保护记录完整放入认证加密包；恢复逐字段原样保留历史包身份、激活时间、原Windows备份路径及其他出处。旧备份缺记录而原签名包明确声明整数1的order_inventory_v1时，增加“依据已认证备份签名reader保留门禁”的恢复证据，明确original_activation_record_included=false，不伪造历史激活时间或包谱系。恢复后同一revision仍拒绝不支持该reader的程序。损坏/未知reader声明、损坏保护记录、标记与签名能力不符、可变缓存manifest与签名manifest不符均拒绝；发货reader及其他原门禁保留。

storage.decrypt_file从同一打开FD实际读取的header/ciphertext/tag计算SHA，只在GCM认证成功后返回。Manager.restore在认证后及凭据配置/shared提升前复核路径内容未变化，并把实际认证摘要写入restored_backup_sha256及旧备份reader证据。输入被替换时不激活目标，不误把A的数据标为B的备份。

原2f3b3b9d下3个新增完整恢复用例均复现失败；改后助手全量及原订单库存reader契约共422项：413通过、9跳过、0失败/错误，30.74秒。跳过8项Windows专用实机和1项显式launchd开关；此前实机证据仍保留，不冒称本轮重跑。3条告警为依赖弃用及合成短JWT密钥，未使用正式凭据。独立只读代码复审通过，证据order-inventory-recovery-before.xml/log、order-inventory-recovery-full.xml/log及test-summary.json。

真实只读核查确认：原基准和第二恢复目录的签名包均声明该reader，但state缺标记；家庭工作副本有标记。这三份历史目录不就地修写。随后从既有Mac完整加密备份20261011-074214-0dbc0a3d.tmbackup（SHA267a87bfcc413af1f23d82eb45df6cdd259d248a32d1a2859449fa1ab3b1f56f）恢复至新的私有空目录restores/mac-reader-guard-20261011。全部325表、结构/业务事实、2983非数据库共享文件、888登记附件及backup/deepseek/mailbox真实秘密内存比较均通过；仅既定PDF根路径/邮箱密文重封装技术差异。新state保留reader要求及实际备份SHA，原激活历史不可得明确记录；目标未启动，原工作副本DB及state SHA不变。证据order-inventory-real-recovery.json/log、order-inventory-recovery-real-readonly.json、verify_real_reader_recovery.py；job记录已由实际会话退出0更新completed，不依据文件状态猜测进程结束。

本轮没有创建新工厂冷备、连接工厂库、外部发信/收单、签发真实切换授权或清空旧机。真实恢复的数据仍来自10月10日冷备，不代表工厂后来交易已同步。运行包仍原签名3de3be32，代码修复须后续重建并由工厂原身份签名。系统服务/专用账号/后台钥匙串、正式切换及现场验收仍未完成。

下一闭环：源端已停写后的认证核验与签发。先验证既有源目录再构造Manager（其构造器会创建目录）；全程持维护锁，不调用会恢复运行的backup/_backup_locked/pause_after_backup。核对manual_stop、真实进程及异常状态，验证本安装回执/last_backup/独立完整SHA，读取实际认证输入摘要及manifest.created。逐项比较原始DB SHA、完整shared清单与环境原字节；release.zip/compatibility.zip/目标签名包都要全文件inspect_package，AST结构验证不能替代全包验证。authority仅在源revision与DB不同且原签名preserve_existing_facts_v1授权有效时使用，直接兼容时允许元数据规范化为None。核对发货共享marker/状态索引、订单库存保护记录及目标全部readers。内置凭据与原用户fresh collect的完整用途/配置/密文版本/实际秘密仅内存比较。Windows明文任务目录在写入前须建立并回读仅当前SID+SYSTEM的DACL；不能只用0700或NAS共享临时目录。只在全部核验通过后加载原DPAPI发布身份，拒绝PEM，独占输出和原公钥回读；失败保持旧端停止。实际源端停写、隔离、同步及正式接管仍须当次明确授权。


## 2026-10-11 源端切换私有暂存与发布身份前置约束

本闭环文件范围：新增desktop_assistant/windows_private_workspace.py与定向测试，修改desktop_assistant/signing.py并补发布身份测试；任务卡、计划和执行回执同步。主执行者唯一写入负责人，独立代理只读复核。目标是在解密真实备份前以Windows原生API原子建立仅当前原用户+SYSTEM可访问的本地私有目录，并从句柄回读受保护DACL、所有者及ACE；遇到NAS/非ACL磁盘、链接/重解析、已有目标和权限错误即拒绝，失败现场保留，不改父目录权限、不自动清理。期间持有目录句柄防止路径被替换。签名身份增加同次读取内的DPAPI-only受限载入，并核对既有公钥，不允许先检查后重新按通用PEM入口读取。验收为合成失败/成功边界和原签名回归，Windows真实ACL/DPAPI项单列，Mac跳过不得宣称实机通过。此模块尚不签发真实票据、不解密正式数据、不调用旧端停启；完整来源验证/系统后台服务仍继续实现。


前置接口结果：private_workspace先固定祖先目录，再从parent句柄核对所有者及仅当前SID+SYSTEM的受保护DACL；既有父目录不满足直接拒绝，不修改原权限。然后CreateDirectoryW附带安全描述符独占创建新job，回读两条OICI/FullControl ACE及owner；持有FILE_LIST_DIRECTORY/READ_CONTROL/READ_ATTRIBUTES且不共享删除的句柄，保持期间正常创建子项但拒绝目录重命名。退出正常流程再核对ACL，成功/失败现场由调用者按所有权另行处理，接口不自动删除。原用户SID限定普通本地/域用户S-1-5-21；其他服务/系统身份不伪装为原用户。

load_dpapi_key在单次有界读取内严格拒绝PEM、未知/缺失字段、错误原公钥/指纹，只有格式和既有身份吻合才DPAPI解封；解封错误使用固定消息，私钥不导出。原通用构建load_key保留兼容，仅共用同一个DPAPI公私钥核验函数。独立代码复核发现并修正父目录DELETE_CHILD窗口和属性句柄不参与一般share判定两处边界，复核通过。

定向验证106项：96通过、10项Windows专用跳过、0失败/错误，0.51秒。覆盖父目录不安全时零创建、子目录权限或回读失败时不交付明文路径、旧目录不覆盖、失败现场保留、平台/链接拒绝、DPAPI-only单次读取与解封前拒绝、原构建签名和切换契约。Windows原生的ACL继承/真实文件权限/目录重命名与junction/真实DPAPI及PowerShell仍待原平台运行；未把合成适配器当实机证据。证据factory-source-primitives-tests.xml/log、test-summary.json。该前置模块尚未接入最终来源核验/票据签发。

工厂独立验证交付将绑定本次固定提交及源代码ZIP SHA，选择4个测试文件共51项，Windows预期49通过、2项POSIX专用跳过。只运行合成数据与一次性测试身份，所有写入限D盘专用任务目录；一次性公钥不被任何真实系统信任，不注册账号/访问，不读取真实发布私钥和运营凭据，不改正式运行包。测试临时文件仅按pytest本任务所有权回收，失败目录保留，禁止自行清理旧机。准确路径/摘要/命令见同轮NAS工厂隔离验证交接单。

实现参考微软官方CreateDirectoryW、GetSecurityInfo、CreateFileW及IoCheckLinkShareAccess文档：https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createdirectoryw 、https://learn.microsoft.com/en-us/windows/win32/api/aclapi/nf-aclapi-getsecurityinfo 、https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew 、https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/wdm/nf-wdm-iochecklinkshareaccess 。


## 2026-10-11 最小Windows合成测试交付包

范围固定为scripts/uat/build_factory_primitives.py、factory_primitives_runner.py、factory_primitives_README.md、factory_primitives_pytest.ini、factory_primitives_requirements.txt、factory_primitives_windows.ps1、factory_primitives_windows_wheels.json及tests/desktop_assistant/test_factory_test_bundle.py；原应用模块和四个专项测试字节不改。用明确allowlist从固定Git提交取必需模块/测试/运行说明，排除数据库、真实凭据、发布身份、历史附件/业务资料及第三方运行库；通过每文件SHA和源码路径映射绑定提交。专用runner不加载通用tests/conftest或外部pytest插件，只使用包内原测试；设置全新任务TEMP/合成ERP环境、结果和失败保留，不向原父目录或正式目录写入。必须独立解包运行原51项并验证篡改/额外文件/已有工作目录拒绝，才产生NAS小包回执。

协调端转达用户已批准仅在工厂新临时文件夹调整访问权限和构造目录链接、只用虚构数据。本机仍不连接工厂或更改其权限；该授权不包括正式目录、上级权限、真实发布私钥、运营凭据或持久授权。CodexSandboxOffline的合成DPAPI通过只证明该身份的临时合成往返。旧66MB全源码包保留历史证据，本小包用于最小传递，工厂不自行拆改源码。


最小包锁定原源码82f0a79c和独立harness提交：12模块、4原测试、6说明/入口/配置，加manifest共23文件。完整Windows CPython3.12依赖锁定10个公开wheel共5,998,804字节，官方PyPI摘要/大小、ZIP完整性及METADATA完整依赖闭包已核验，只下载未安装/执行。Windows新venv安装仍待工厂端明确批准；Check只读，Install必须显式开关及获准目录，Run只用该任务venv。无新Python安装，不使用原ERP requirements全集，不把测试身份加入任何真实信任。

独立复审修正三项边界：pip --isolated仍读global/site配置，故清理子进程PIP_*并以严格小写nul禁用全部配置，安装前从实际venv pip证明有效配置为空、核对sys.prefix及--require-virtualenv；root/工作目录先检查无重解析再规范化，拒绝..或短路径别名进入源码包；unittest临时夹具按原测试自身清理，失败亦可能删除，不能承诺保留，任务根/结果和pytest失败目录保留。PowerShell路径用ASCII JSON往返以兼容中文路径。未改机器执行策略；PowerShell与pip实际运行仍待Windows原生测试。实现依据pip官方配置文档与25.0.1 configuration.py，不能用isolated参数替代配置为空证明。

打包和拒绝回归7项全部通过，含独立解包运行原51项（Mac41通过、10项Windows原生跳过），以及篡改/额外文件/提交不符/旧工作目录/带..的源码内输出零写入拒绝；原源码和原测试字节保持不变。最终提交后再次从提交构建独立ZIP并解包实跑，结果和准确SHA/大小另存新NAS最小包回执，不覆盖旧66MB交接单。Windows预期49通过、2项POSIX跳过，尚未执行，不冒称完成。

本轮支持接口实测ordinaryUsageAllowed=true、周额度已用91%/剩余9%，非0；没有兑换重置或购买。协调端曾报告Mac短暂断线恢复，属于协调证据而非额度耗尽；本轮exec、NAS读写和usage接口实际成功。完成测试、提交及NAS独立回读检查点后才开启下一闭环。仍无正式库操作/发信/收单/真实签名/停写/接管/旧机清空，现有家庭签名运行包源码继续3de3be32，不含本轮工具及此前恢复修复。


## 2026-10-11 已停写Windows最终备份认证审计

本闭环新增factory_source_audit及合成测试，并为windows_private_workspace补只读数据库句柄租约。目标是在现有维护锁内验证原目录完整且已manual_stop、进程退出、NAS最新成功回执/独立SHA和同一次GCM认证输入、备份时间晚于目标申请、原始DB/全部shared/源程序包/reader标记、完整运营凭据与原用户fresh collect逐字节一致。私有暂存DACL从创建到最终退出均成功后才返回非秘密证据；全程不调用backup/_backup_locked/pause_after_backup/restore/start/stop、不重封装凭据、不签发真实票据。Windows数据库句柄拒绝写入与删除共享，避免只信process.json遗漏独立写者；所有源前置先于Manager构造，原目录不补建。

此审计是后续正式签发入口的必要组成，不代表Mac系统服务、正式激活、源端隔离或签发流程已经完成。最终票据必须在原私有暂存上下文成功退出、仍持维护锁并再次验证源事实后才能发布；不能在退出可能失败时先产生可用票据。本轮仅使用合成数据，所有生产操作仍需工厂当次明确授权。

实现结果：新增factory_source_audit.py与两份专项测试，修改windows_private_workspace.py、operation_lock.py、credential_transfer.py及本任务卡/计划。审计不调用Manager目录创建构造器，重复核对所有既有目录；维护锁以existing_only读取已初始化文件，缺失/截断均拒绝而不补建。Windows只读租约固定数据库、state、回执、源/目标包、缓存manifest、现有运营配置和必要兼容授权包，拒绝既有写者以及核验期间写入/删除/父目录替换。NAS备份以同次GCM认证读取的完整SHA为准，再核对路径摘要和只读上传状态。

独立复审发现SQLite普通mode=ro仍可能生成WAL/SHM，现将全部结构/业务SQL移至已认证且与源DB原始SHA一致的私有副本。只有源凭据读取使用immutable_database=True，前提是已停写、无任何旁路日志且持原生只读租约；普通在线导出默认行为不变。源shared全文件及配置原字节前后核对；备份内完整用途/配置/版本/实际秘密与原用户fresh collect仅在内存比较。目标必须通过全包验签、四个运行入口、同revision/schema及所有原reader能力检查，并明确声明factory_cutover_v1；现有家庭3de3be32运行包没有该能力，仍不能正式接管。参考SQLite官方说明：https://www.sqlite.org/wal.html#read_only_databases 与 https://www.sqlite.org/uri.html#uriimmutable 。

助手整合回归519项：504通过、15跳过、0失败/错误，50.58秒。跳过为14项Windows原生及1项显式当前用户launchd开关；3条告警为依赖弃用及合成短JWT。之后新增Windows immutable SQLite/WAL租约用例，单文件补跑1通过、3项Windows跳过，不与整合结果相加。测试均合成，未使用正式库/真实秘密；独立只读复核通过，差异检查通过。证据factory-source-audit-full.xml/log、factory-source-audit-native-lease.xml/log及factory-source-audit-test-summary.json。Windows实机仍待验证；旧1bd96e0c最小测试包不包含本轮新增模块/用例，不修改或冒称已交付这些新测试。

本轮只交付上下文内的非秘密认证证据，没有真实签发入口、没有加载工厂私钥。后继签发必须在该上下文仍持锁/租约时验证身份和独占发布并用原公钥回读；物理隔离与工厂当次授权仍须另外证明。真实Mac恢复、现有签名运行包与已停止目录均未重跑或改动。远端正式基线核对仍b9a077bc；本轮支持接口ordinaryUsageAllowed=true、周额度已用92%/余8%，未兑换重置或购买。提交及NAS独立回读后再进入下一闭环。

## 2026-10-11 来源签发入口接入范围

本闭环新增factory_source_issue.py与合成测试，并补已有Windows私有目录的无写入固定/权限回读接口。主执行者唯一写入，独立复核只读。入口必须核对当次授权/旧端隔离的操作员声明、独立批准的目标申请摘要及既有公钥指纹；声明本身不能冒充物理隔离证明。完整已停写来源审计成功后，仍持维护锁/源文件租约时才加载原用户DPAPI发布身份、签发、原公钥验签、独占写入并回读；拒绝覆盖旧输出，失败保留现场并明确可能已有签名文件，不能自动恢复源端。CLI不接受秘密参数/环境/管道，所有测试用合成身份。本轮不执行任何真实签发，目标正式激活/系统服务仍另行接入。

接入已完成：issue只接收原始申请字节与独立批准SHA、原发布公钥及批准指纹，不接受调用者自称已认证的evidence或私钥对象。先拒绝缺授权/隔离声明、过期/不同身份/摘要、相对路径、源目录内输出及已有输出；受保护输出父目录只固定和回读、不创建或改权限。verified_stopped_backup完成最终明文ACL检查并yield后，仍持来源维护锁和租约才固定publisher.json并调用DPAPI-only载入。签名原公钥回验同时逐字节绑定request与本次source evidence，独占二进制写入/fsync后重新打开、完整字节比较、验签和实际窗口检查；输出可能已写成时的任何错误均保留现场，不宣称“未产生凭据”，不自动重试/删除/启动旧端。正常退出全部上下文后才返回非秘密回执，operator_isolation_attested与physical_isolation_proved明确区分。

CLI仅本机交互隐藏输入最终备份口令，无密码/时钟覆盖参数，不读密码环境或管道；GetPassWarning转为异常，禁止标准库无法隐藏输入时回退到可能回显的输入。参数错误不回显未知值，底层异常只显示固定失败说明。此行为依据Python官方getpass文档：https://docs.python.org/3/library/getpass.html 。不会要求通过聊天提供口令，本轮没有执行实际交互入口。

定向46通过/3项Windows跳过；随后来源审计、协议、签发、锁、私有目录/文件租约和发布身份扩大回归190项：180通过、10项Windows原生跳过、0失败/错误，25.13秒。全部一次性合成数据/Ed25519身份，Windows原生API及DPAPI适配器在Mac合成测试中明确替代；原生验证仍未执行。测试包括审计失败零身份载入、私有暂存最终ACL失败、正确签名但错误来源、已有输出和独占创建竞态、写入/fsync/回读/最终ACL/过期失败现场保留、管道/回显降级/错误信息不泄密。独立只读复核和差异检查通过；证据factory-source-issuer-focused.xml/log、factory-source-issuer-regression.xml/log及test-summary。没有重建/签署家庭运行包，也没有真实签发、正式库操作、停启/发信/收单或旧机清空。

下一闭环为Mac专用系统服务身份及后台凭据接入，再完成受保护目标激活；仅源端签发实现不能启用factory_cutover_v1能力位或解除家庭门禁。新源码仍待最终整包构建/原工厂身份审阅签名，真实Windows检查、完整业务/打印/扫码与工厂最终同步/人工验收继续未完成。此前旧最小Windows测试包保持原样，不能冒称包含本轮新用例。

## 2026-10-11 Mac系统服务身份准备范围

实时只读确认_tianmingerp不存在、对应系统服务未注册、sudo非交互调用需要管理员密码。本闭环先新增mac_service_identity与合成测试，提供绑定本机/批准摘要的专用账号及固定本地目录create-only配置器；采用OpenDirectory原子CreateRecord，不用逐字段dscl覆盖已有账号。原用户账号、钥匙串、权限和全部恢复目录保持。主执行者唯一写入，独立复核只读；本轮不执行真实账号创建/系统服务安装，不索取或绕过管理员密码。只读原生OD检查及合成创建/拒绝流程须可复验，管理员实际配置和系统域运行后才可证明独立于登录。

配置器已完成，独立于应用依赖，仅用标准库和原生CoreFoundation/OpenDirectory，系统Python3.9.6实际只读计划生成通过。固定_tianmingerp及/Library/TianmingERP，计划绑定本机摘要、脚本摘要、原字节批准摘要、UID/GID、安装GUID和七天期限；执行前拒绝既有账号/组/目录及ID冲突。真实及有效UID必须为0，源码和全部祖先须root所有、不可被普通用户修改、无链接和扩展ACL。源码自检不是真实性认证：管理员须先以OS工具复制到/Library下全新root暂存目录，独立比较已审阅固定SHA后才执行；交接命令已备且sh -n通过，未执行特权动作。

组/用户一次性提交完整属性，逐字段OD回读后另经pwd/grp核对唯一UID/GID、home、false shell、空显式组成员和非管理员补充组；新home/instance以FD设置专用属主和700。完成回执独占600写入、fsync及原字节回读。部分失败不删除或接管：早期根700，允许穿越后的晚期失败可能保留根755，子目录700及记录600；后续再执行会拒绝既有现场，不假装回滚。原生创建属性兼容性仍待管理员实测，不能把代码审阅当已配置。

定向及相邻回归111项，110通过、1项需显式开启的当前用户launchd实机检查跳过，0失败/错误，1.99秒；身份模块46项均通过。覆盖实际身份检查的错误/重复ID、额外组、home/shell、全部早期和晚期失败、摘要/期限/已有对象/竞态拒绝；原生检查仅只读OD、非管理员及用户可写脚本拒绝、pytest自有目录ACL。独立只读复核通过，证据mac-service-identity-regression.xml/log、test-summary、native-readonly和admin-handoff。只读实机仍Apple M6/macOS27.0.1，服务专用身份及固定根目录不存在；没有账号/钥匙串/系统服务安装，没有真实凭据或正式库操作。当前家庭签名运行包仍3de3be32，未重建或改能力位。

下一项后台凭据拟采用两层：System keychain只保管随机引导秘密，按专用UID控制访问；服务专用文件钥匙串保存实际邮箱/AI/备份条目并保留修改/轮换能力。不能把全部运行秘密直接塞进只有管理员可写的System keychain后称功能完整。必须明确系统域查询、非交互、初次创建与日常读取分离、缺失拒绝、服务身份正例/其他UID反例、锁定/进程重启和轮换验证。此设计尚未实现或实机证明，不扩大当前个人用户钥匙串证据；Apple TN3137及System keychain权限讨论为依据：https://developer.apple.com/documentation/technotes/tn3137-on-mac-keychains 、https://developer.apple.com/forums/thread/738542 。受保护目标激活、最终工厂签名/同步和现场人工验收继续未完成，旧机不清空。
