# MAC-SERVER-HANDOFF-20261010

## 2026-10-11 系统引导钥匙串未写入现场续接

老板实际执行a90975b8管理员包：只读预检和两个身份进程通过，pending及home/Library/Keychains已创建，停在create_system_bootstrap。随后root只读诊断确认System.keychain状态2（可读、未解锁），身份/路径保护/内存UID ACL通过，本次精确条目查询-25300未找到，私有Keychains目录为空；没有凭据数据读取，不删除pending或目录来重跑。

本闭环范围app/core/mac_service_keychain.py、mac_keychain_install.py及对应测试/任务/计划。Apple公开实现中GetStatus的写权限位仅随未锁状态设置，实际Keychain操作可按系统机制认证；为固定System域的root引导新增保留实际路径/所有者/ACL/文件检查并交原生SecItemAdd执行无UI授权，失败仍拒绝，不手动锁/解锁系统钥匙串，不修改其设置或旧条目，私有钥匙串仍须解锁状态。新增有限resume-empty只接受已批准旧pending、同一创建记录/身份、严格空目录、精确系统条目不存在和一次性独占续接记录；保留所有旧记录，新进程正例、HMAC、其他UID原生ACL拒绝及最终context发布顺序保持。任何部分已写入、未知状态或重复续接均停止。a90975b8只作中间检查点，最终签名候选待本故障闭合后重建，不能先签旧包。


本闭环整合1792项：1776通过、16既有平台跳过、0失败错误，XML 253.385秒，1条既有依赖告警。整合期间只将两处Path.stat(follow_symlinks=False)换成Apple Python 3.9支持的等价os.stat；最终源码180项定向回归及Apple 3.9 stdlib真实文件/ACL合成空现场检查复查通过，集合重叠不累加。首次原生探针的测试替身漏接verify_python参数，修正探针后通过，原失败日志保留；未执行系统钥匙串写入。服务HMAC/其他UID拒绝/context发布抽取前后AST一致；相对工厂基线差异检查通过。当前仍无keychain-empty-resume-attempt、verified或context；新最小管理员包待老板实际执行，不能将合成测试当专用499系统钥匙串成功。主代理单独实施复核，未声称独立代理验收。

## 2026-10-11 实际身份完成后的系统Python预检修复

老板执行f86b5549续接脚本，实际身份完成回执已返回；root0755系统根和499:499的700 home/instance已只读确认。钥匙串初始化在首次写入pending前失败；原安装现场保留，不重跑身份complete。原预检对/usr/bin/python3错误套用小型配置文件的单链接及64KiB限制；本机Apple启动shim为200560字节、78硬链接，原检查已只读复现拒绝。

本闭环仅修mac_keychain_install.py及对应测试、任务卡/计划：小型代码和记录仍保留原单链接/大小/保护检查；系统解释器另核固定Apple签名、root保护、只读可执行路径及元数据稳定性，子进程核实际Apple解释器。新管理员材料只做钥匙串预检与初始化，不再创建账号；既有钥匙串现场仍拒绝覆盖。真实root只读诊断由老板执行；最小合成测试和系统Python只读验证不冒充初始化、后台注销重启或迁移完成。全部原件、完整真实数据和旧机保留，家庭隔离及工厂正式门禁不变。

老板随后提供NAS《20261011-fa514540-Windows核验未通过-未签名》：15项Windows原生专项通过，但源审计/历史恢复夹具连接未关闭、锁持有中再次读取operation.lock、65,537字节参数自动生成过长测试ID使Windows测试失败，工厂未签名。本轮承接修正test_factory_source_audit、test_credential_restore、共用test_factory_cutover夹具的显式close，事务语义及真实锁保留；锁持有期间核其他文件和锁元数据，释放后仍核全部字节；test_factory_activation_contract仅改短ID，完整边界载荷不减。另清除factory_activation_contract.py/reader_contract.py两个EOF多余空行，语义不变。需新固定源码及配套程序交工厂重跑原范围并完成复核后签名；Mac测试不能替代Windows回执。

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

## 2026-10-11 专用服务后台钥匙串接入范围

本闭环新增app/core/mac_service_keychain.py及专项测试，mac_keychain按真实OS身份路由；不以环境变量选库。System域只保管绑定安装身份的随机引导秘密，实际业务凭据在专用用户文件钥匙串正常新增/轮换；get/put不自动创建、补建或回落个人登录库。显式初始化原语与日常读取分离，完成记录由root保护，真正系统安装器及跨UID/注销/重启实测仍待后继闭合。本轮主写人负责全部文件，独立代理只读查核SDK/Apple OSS和复审；不安装真实账号，不写System keychain，不触及真实秘密或正式数据库。

原生临时钥匙串预检仅用本任务新建随机合成库：创建期间、删除自身测试库后，个人默认库及搜索列表均不变。发现公开Security实现的两个重要边界：显式失效keychain也可能回退defaultKeychainUI；GetStatus的WritePerm并非真实BSD写权限。故服务进程首次使用前一次性选择System偏好域，并回读域/实际默认库/系统文件权限，所有业务操作使用固定私有库且检查实际新增条目落点。System域切换为进程状态，但可能flush此前已脏偏好，因此新服务进程不得预先改默认或列表，不能把每请求切域当无副作用。API使用不得替代实际系统身份正反例验证。

原生合成试验另复现并修正两个文件语义：SecKeychainCreate默认产生644，现仅对该次成功新建的自身文件以FD核对属主/类型/单链接/ACL后收紧600；原服务私有父目录700保持。SecItemAdd会原子替换钥匙串数据库inode，不能把正常写入当路径攻击；现固定并核对私有父目录，钥匙串文件操作前后分别核对权限/归属，新增条目另回读真实目标库与值。当前用户全新合成库已验证UID ACL下同UID不同Python解释器读取旧项及新增新项、旧引用保留、重复拒绝、锁定后错误密码拒绝/正确解锁，用户/System域默认与列表创建期间和删除自身库后均不变。此证据不是专用UID/其他UID拒绝、System引导条目或重启验收；没有读写任何真实System条目。

后台接入源码闭合：新增模块、专项测试及独立原生子进程probe，原mac_keychain只改OS身份分派，不变Windows路由或个人用户行为。每次服务操作验证root保护的非秘密完成记录、实际UID/GID/补充组、固定本机目录、精确System引导引用、解锁及安装绑定HMAC canary；已解锁库即使接受错误解锁口令也会因canary不符拒绝。新增采用单UID进程ACL、无honor-root/应用allow，精确私有库及写后原字节回读；正常改密码仍新增随机引用保留旧项。create_bootstrap/initialize_private只是独立初始化原语，不是完整管理员安装器，不自行发布完成记录。

复核发现旧格式.keychain检测不能用with_suffix移除.keychain-db，已改只去末尾-db并覆盖旧文件/悬空链接。原生缺文件测试增加SecItemAdd哨兵，明确证明在调用写API前拒绝，不能以最终权限报错冒充。最终回归147项：146通过、1项原Windows DPAPI实机跳过、0失败/错误，7.50秒；两个Mac原生钥匙串开关显式启用并实际通过。3条告警为依赖弃用及合成短JWT。独立复核问题已修正，证据mac-service-keychain-reviewed.xml/log；此前42项定向及146项中间回归不相加。原生probe在两个短进程之外另做独立进程偏好指纹前后比较，用户/System默认与列表均未变。测试只新建和删除自身合成库，未读取个人已有条目、未调用真实System条目读写或创建专用账号。

Apple实现依据：SecKeychain.cpp/SecKeychainItem.cpp的失效引用默认库回退、Keychains.cpp的status位并非真实BSD写权限、StorageManager.cpp/DLDBListCFPref.cpp的进程域和脏偏好flush；来源https://github.com/apple-oss-distributions/Security/tree/main/OSX/libsecurity_keychain/lib 。系统库定位采用CopyDomainDefault而非硬编码，依据https://developer.apple.com/forums/thread/738542 。SecAccessCreateWithOwnerAndACL使用CFString授权tag数组；这不等于完整ACL回读或跨UID拒绝已证明。专用服务身份/真实System引导/其他UID/注销/重启仍未验收，后续管理员安装器必须先完成这些核验才能发布root完成记录。

当前工厂正式基线仍只读核对，家庭签名运行包仍3de3be32，未重建、重签或启用factory_cutover_v1。真实四份恢复目录未动，正式库/停启/收单/发信/外部AI/真实签发及旧机清空均未执行。本轮额度接口ordinaryUsageAllowed=true、周已用95%/余5%，没有耗尽证据或兑换/购买；先提交和NAS回读再继续完整系统安装器、LaunchDaemon及目标正式激活。

## 2026-10-11 管理员钥匙串初始化编排范围

新增mac_keychain_install及隔离测试/最小代码包构建，串联已创建专用身份、System随机引导、专用库初始化、同UID新进程锁定与读写验证、其他普通UID的真实钥匙串ACL拒绝，全部通过后才独占发布root完成记录。代码必须先由OS工具独立核对固定摘要并位于root不可写、子进程可读的最小包；以系统Python -I -B -S执行，降权明确清空补充组，不用已初始化CoreFoundation后的fork直接执行Python。先跑两种UID的无秘密自检，再写任何引导条目。完整真实凭据不进入本阶段；不创建账号、不安装/启动ERP、不发信收单。Popen+exec只证明新进程身份和System钥匙串偏好域，不能冒称真实launchd系统会话、注销/重启或工厂激活，回执明确这些为false。主执行者唯一写入，独立代理只读复核。


## 2026-10-11 管理员后台钥匙串初始化候选完成

新增mac_keychain_install及最小Git代码包构建器，固定系统Python -I -B -S、七个标准库代码文件与manifest，管理员先用OS工具独立核验包/计划摘要，再从root保护且两UID可读取的目录执行。源码自检不能代替执行前真实性核对。原账号创建计划、OD完整字段、root回执和本机安装身份均重新核验；任何既有初始化现场（含悬空链接）拒绝覆盖。服务UID和普通观察者UID先在全新exec进程核验代码/导入及身份，之后才独占写pending、创建私有父目录和System随机引导项。随机秘密仅在父内存和服务进程自己的System读取中出现，不进入参数/环境/临时文件/输出。

修正macOS现代os.getgroups查询目录服务访问组而不是进程内核组的语义：显式绑定SDK导出的libsystem_kernel.getgroups，XNU返回中包含有效GID，setgroups→setregid→setreuid后的预期严格为[gid]，其他内核组拒绝；原服务目录成员资格门禁仍保留。当前普通用户只读确认该API可调用，不冒称已经真实降权。依据Python https://docs.python.org/3.9/library/os.html#os.getgroups 、Apple https://github.com/apple-oss-distributions/xnu/blob/main/bsd/kern/kern_prot.c 、CPython https://github.com/python/cpython/blob/3.9/Modules/_posixsubprocess.c 。

专用库初始化与另一个新服务进程的锁定/解锁、旧新合成条目读写分别以秘密HMAC绑定阶段/context/独立挑战；跨UID反例绕过业务身份预拒绝，必须实际精确查询System条目，并仅接受Security原生AuthFailed/InteractionNotAllowed。缺项、导入/路径失败、其他状态不能算权限验证通过。先独占写root验收回执，再最后发布644非秘密context；失败保留现场，晚期错误可能已有context，不能宣称自动回滚。Ctrl-C后继续观察同一worker退出再拒绝完成，不自动强杀或遗留仍写入的进程。默认不设置超时；终端必须等候本次精确进程结束。

最终回归213项：212通过、1项Windows DPAPI原用户实机跳过、0失败错误，7.85秒；本次新增66项全部通过，两个原Mac合成钥匙串测试开关显式启用。3条告警为依赖弃用和合成短JWT。覆盖完整计划/身份/ACL/包清单、两身份早检零写入、错误/重放proof、错误反例、中断回收、部分写入失败与晚期context保留；独立只读代码复核通过。早期39通过/1测试调用方式错误日志保留，修正测试后不把中间结果叠加。证据mac-keychain-install-regression.xml/log。系统Python3.9.6的-I -B -S语法及实际导入闭包通过；新安装包另按最终Git字节构建及回读。

尚未执行管理员账号创建或初始化，未创建真实System引导条目，也未读写运营秘密或真实恢复数据库。回执明确launchd_system_verified/logout_reboot_verified/factory_activated=false；Popen新身份与System钥匙串偏好域不是launchd系统会话证据。候选已有可审阅管理员执行材料，但真实双UID、后台服务、注销/重启/FileVault解锁、最终工厂签名与停写同步、业务/中文实体打印/扫码仍待完成。当前家庭签名运行包仍源码3de3be32，未重建或改factory_cutover_v1；工厂基线只读仍b9a077bc115cc649cdc2753cab3dfb3d75269d1c。旧机与四份完整恢复目录保留，无外部发信/收单/AI、工厂正式库连接或接管。本轮支持额度接口ordinaryUsageAllowed=true、周剩余4%，无耗尽证据、无购买/兑换；完成NAS独立回读检查点后再开始后续闭环。


## 2026-10-11 系统服务不可变执行副本接入范围

现有用户入口依赖pw_dir/gui域，且完整文件验证只在注册时，不能直接改为System即称可靠。本闭环先抽取完整发布树核验，新增专用账号读取root发布清单及不可变执行副本的解析器，接入Manager启动/更新链/真实与预演迁移边界；原instance/releases和packages仍作恢复缓存。root published布局分别保留server（仅固定四个托管数据链接）和migration（无数据链接）的完整已签名代码副本，避免迁移预演通过server链接落到实际共享目录。未批准包/旧运行包先于停服拒绝，Mac专用身份使用隔离脚本入口和固定加载器环境。根发布安装器、稳定root启动入口及实际LaunchDaemon注册仍在后续闭环，不在本轮创建root目录、改服务账号/钥匙串或签署运行包，不启用factory_cutover_v1；全部Mac仍保持家庭预演。主执行者唯一写入，子代理只读复核。

整合回归发现工厂最小测试包门禁仍预期51项，但e037875d已给同一白名单测试文件增加2项目录固定/ACL变化拒绝用例；包内实际43通过/10跳过、零失败错误，却因计数51而拒绝成功。范围补充该runner、README及包测试的明确计数到53，仍严格核对Windows预期51通过/2跳过、Mac43通过/10跳过；不删除计数/平台/来源/业务导入门禁，不重写既有NAS的1bd96e0c旧51项包，不冒称本次执行Windows。另在Mac构建加入EasyOCR只读user_network签名占位，执行前要求两模型与占位文件，原生只读合成目录验证初始化无下载、缺目录拒绝。

## 2026-10-11 不可变执行边界候选验证完成

专用账号仅能从本机root批准的包执行：完整程序树核验类型/所有权/权限/ACL、签名原包及逐文件摘要，server保留固定四个数据链接，migration禁止数据链接。Manager启动、升级链、实际及隔离迁移均接入；未批准包、缺角色入口/离线模型、篡改代码或缓存版本信息在启动/停服前拒绝。元数据从原包验签并严格对比缓存，旧Windows/旧Mac包仍可离线恢复与备份，不能因缓存revision伪造跳过迁移。专用身份恢复写manual_stop=true；中断恢复检查两个解释器，系统迁移Ctrl-C后观察原进程退出并保留现场，不自动强杀。

执行采用-I -B签名脚本与固定环境，保留家庭回环/外连/收单/AI门禁。OCR只从发布树取模型，构建预置签名user_network/.keep，避免EasyOCR在只读程序目录初始化失败。新包声明immutable_execution_v1=1，仅代表此执行协议，不代表系统安装或正式激活。此模块使用包内Python3.12，不能当系统Python3.9标准库启动器。root发布安装器、稳定启动器、LaunchDaemon及目标正式激活仍未实现/未安装。

最终助手全量702项：686通过、16跳过、0失败/错误，62.78秒，3条依赖/合成短JWT告警；新增执行边界40项及实际-I迁移子进程、只读OCR初始化均通过。root权限/专用UID在边界测试中明确模拟；另有本机普通用户文件被真实root所有权门禁拒绝、真实独立Python对合成DB迁移且外连拒绝、EasyOCR无下载只读初始化实测，均不冒称系统部署。Windows/显式实机开关跳过保持原理由。首轮全量的旧kit计数失败及首轮测试漏传chain参数日志保留，不叠加统计。证据mac-published-runtime-final.xml/log；独立只读复审意见已修复，README不再混用旧源码SHA和新53项runner。

实时只读核对M6/16GiB/macOS27.0.1、本机364GiB空闲、NAS独立设备可读；_tianmingerp和system/cn.tianming.erp不存在。官方Alembic命令仍唯一ep1010md（自制仅解析Assign的探针漏AnnAssign所得多head无效，不作为迁移事实）。远端工厂基线仍b9a077bc115cc649cdc2753cab3dfb3d75269d1c。既有四份完整真实恢复和3de3be32签名运行包未改，没有正式数据库连接、收单、外部发信/AI或旧机清空。本轮不索取重复移交凭据，下载附件缺失时依据NAS同名回执核对；文件中的历史步骤不构成新切换授权。提交并完成NAS独立回读后，继续root发布/稳定服务入口/后台备份和目标激活；真实管理员安装、注销重启、工厂最终停写同步与业务/实体打印/扫码仍须后续验证。

## 2026-10-11 管理员程序发布器范围

扩展既有独立SHA审阅的最小管理员代码包，新增stdlib程序发布器和非特权签名计划准备工具。管理员入口先核验root代码包与批准计划，计划绑定完整签名程序包/清单/公钥/本机context；只在新root slot构造server与migration完整副本。程序验签子进程降权到专用UID后才运行已批准的原生解释器；root不导入ERP或程序依赖，不读取运营秘密。全部校验通过才追加root发布清单，保留旧批准版本及失败现场，不启动服务、不更改数据或工厂模式。旧七源码文件钥匙串包/计划保持历史原样；新包与计划另按新摘要交付，不能混用。当前没有root权限或新签名运行包，本轮仅实现与合成验证，不能冒称管理员安装已完成。

实现完成：非特权准备入口必须给出独立交接的工厂公钥指纹，先验证原签名及每个载荷，再生成绑定全部原包字节的七天计划。管理员按独立SHA核对计划和最小代码包后，root只用标准库解析/复制，拒绝路径歧义、大小写/Unicode冲突、特殊成员、业务数据路径和不完整执行清单；两份执行树逐项回读、文件/目录fsync后，专用UID的新exec进程再验签。保留现有全部批准包，最后原子替换发布索引及同步父目录。目录fsync在本机可用，但未进行实际断电试验；替换前失败保持旧索引，替换后同步/回读失败可能已有新索引，统一保留现场人工核对，不能当作自动回滚或盲目重试。

最终助手回归739项：723通过、16跳过、0失败错误，64.48秒；跳过15项Windows原生及1项显式个人launchd开关，3条告警为原依赖弃用/合成短JWT。新发布器37项全部通过，覆盖真实签名和全文件摘要、实际解析器接受生成树、伪造公钥指纹、坏包/坏计划/错误context及权限、重复发布、子进程中断观察退出、原子替换失败仍保留旧批准。root/专用UID/OD/ACL正例使用合成适配器，没有真正降权或安装系统目录；另有真实系统Python3.9.6 -I -B -S标准库导入、真实用户文件被root门禁拒绝。证据mac-release-publish-regression.xml/log；此前定向结果保留但不叠加计数。

最小管理员包现为八个源码文件加manifest共九文件，须从本次固定提交重建，并以新manifest生成新钥匙串初始化计划。原账号创建脚本和身份计划字节不变；7b736ade旧包和旧计划继续成套保留。真实程序发布计划须待本机系统context完成、包含immutable_execution_v1的新工厂签名原生包及完整专用数据目录就绪后再生成，不能用旧3de3be32家庭签名包或虚构context替代。新程序发布器不安装LaunchDaemon、不触发Manager更新/启动、不解除家庭隔离、不执行正式切换。四份完整真实恢复及所有运营凭据保持原状。

## 2026-10-11 系统LaunchDaemon候选范围

本轮接入专用UID监护入口、固定root批准程序的系统plist和管理员安装/停用/重新启用编排。范围mac_system_service.py、service_supervisor.py、mac_published_runtime.py、manager.py、build.py、最小管理员包入口/构建器及对应测试/任务计划。root仅用标准库处理已批准的程序/配置，新进程以专用UID复验；停用先持久禁止再次启动，再在维护锁内manual_stop及正常停止，确认退出后才bootout精确标签，保留所有程序/数据/配置。安装或重新启用仅接受已停止实例，默认仍家庭预演，不以系统注册证明后台凭据、注销重启或正式业务已验收。本机没有root/专用账号和新版签名包，实际安装不在本轮执行；不改变真实恢复目录，不修改工厂库、不发信/收单、不清空旧机。

编排已实现：固定system/cn.tianming.erp，plist直接指向root批准migration树中的原生Python和监护脚本，UserName/GroupName均_tianmingerp、显式InitGroups；运行时再核验实际UID/EUID/GID/内核组、解释器/入口、完整本机私有目录及已有状态/维护锁，之后才构造Manager。新exec专用UID复验签名执行树、manual_stop及无运行进程，安装/启用不解除manual_stop，初始监护仍停止。root不导入ERP/第三方依赖。新system_service_v1仅声明入口协议；系统监护安装后，缺该能力的程序不能跳过持久停用门禁，旧包离线保全继续允许。

停用先原子发布root enabled=false拒绝并发resume/start，专用UID再持维护锁设置manual_stop并正常停止、确认退出，随后才bootout精确归属plist并持久disable。没有kickstart -k、强杀、删除plist/程序/数据或自动回滚。错误/中断保留现场：root门禁可能已关闭而ERP尚未停妥，不能当停用完成或盲目重试。日志位于root保护目录且文件仅给专用组写入权限；重新启用须新计划/已有原字节归属/停止状态复验。

check/run通过System随机引导和私有库HMAC canary验证本次进程实际后台钥匙串访问，不读取运营条目；失败在Manager构造前拒绝。stop不依赖钥匙串，凭据故障不能阻挡停机。监护状态记录实际身份与canary结果，但注册回执明确health/system_keychain_session/logout_reboot/factory验收均false；Popen正例不替代launchd系统会话证明。ExitTimeOut240覆盖75秒锁等待+60秒停止及在途启动余量，不保证无限长维护、断电或OS强制关闭；正式停用必须使用完整正常退出顺序。

最终助手798项：782通过、16跳过、0失败错误，62.87秒；新系统服务53项及6项旧程序/停用回归全部通过。15项Windows原生和1项显式个人launchd开关跳过，3条原依赖弃用/合成短JWT告警。root/专用UID/OD/ACL/注册/后台canary正例为合成适配器；真实签名、文件摘要和目录解析、系统Python3.9.6 -I -B -S导入及launchctl精确标签只读未注册查询实际通过。早期扩大回归206通过/1个夹具漏模拟root元数据权限的失败保留；只修夹具未降低门禁，随后209项定向全过，添加canary后最终全量如上，结果不相加。证据mac-system-service-final.xml/log。

管理员包增至九源码加manifest共十文件，按最终提交生成新包与新钥匙串计划；旧7b736ade/6eef54c6材料成套保留，不能混用。真实系统服务计划待账号/钥匙串、专用身份完整恢复及root发布新版工厂签名包后生成，不虚构context或程序ID。实现依据本机/usr/share/man/man5/launchd.plist.5及Apple官方https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html；真实系统注册、InitGroups身份及日志权限仍须后续原生验收。

下一项为专用身份完整恢复/正常管理入口、独立于个人登录的NAS及备份、受保护目标激活与最终工厂签名。只读发现当前NAS在/Users/kimiyf/Public/ZSPACE下，而个人home为0750；不能由个人可访问推断专用UID可访问，也不放宽个人home权限。家庭签名运行包仍3de3be32，四份真实恢复未改，工厂基线仍b9a077bc。真实安装、后台注销重启、工厂最终停写同步及完整业务/中文实体打印/扫码未完成，旧机保留。

## 2026-10-11 系统服务独立复核修复范围

本闭环修复74350181独立只读复核发现的三项：停用遇在途维护锁即失败且无法新计划重试；首次安装监督包支持系统能力但current未支持时仍通过；stderr的0620不符合launchd文档读写打开契约。唯一写入负责人为主代理，允许文件mac_system_service.py、对应系统服务/发布解析测试及本任务卡和迁移计划。停用有界等待只限获取锁阶段，失败保持关闭门禁及现场；新disable计划仍绑定原控制字节和完整归属，不能自动重试、强杀或重新开启。首次check检查实际current能力，纯离线结构authority不附加本次新要求。stderr改0660，stdout保留0620，父目录和属主仍root控制。先建立失败用例再修复，合成权限不冒充实际专用UID/launchd验收。

并行NAS只读复核确认当前极空间挂载实际NFS server为127.0.0.1:52100，代理链归UID501。此前独立进程F_NOCACHE与SHA只能证明本机挂载视图完整性，不能证明远端设备持久化、专用UID或注销重启可用。已知/vfs/stats当刻队列为空也不倒补过去具体文件。原始回执和本地完整副本全部保留；后续独立闭环处理Mac备份成功门禁及所有消费者，Windows凭据/来源审计receipt契约保持。本次不改挂载、不扩大home权限、不读代理token或重复索取已移交凭据。

第二次复核将范围补充mac_published_runtime.py：已安装后旧schema_authority会被执行解析器附加system能力要求，导致合法实例无法disable后enable。新增专用只读authority验证，仍核对root批准、公钥/context、签名、完整两树、immutable能力和缓存清单一致，只不把离线证明当成实际执行；正常resolve仍要求系统能力，无调用者可关闭的执行参数。回归同时覆盖CONTROL不存在/存在禁用两种阶段、旧authority不得实际运行及篡改/未批准拒绝。

四项修复完成并独立只读复审通过。旧代码先复现三项13失败/2通过及启停周期1失败/11通过；修复后定向126项通过。助手完整825项：809通过、16跳过、0失败错误，63.92秒，3条原依赖/合成短JWT告警。首次沙箱全量23失败为9项sysctl进程查询拒绝和14项Tk桌面初始化失败，日志保留；经自动审批以普通用户运行完整合成回归后全过，不以跳过或改测试规避环境限制。15项Windows原生和1项显式个人LaunchAgent开关仍跳过，root/专用UID/系统注册为合成适配器；日志实际文件IO是测试UID，不冒充专用UID打开root日志已验。

本机再核Apple M6/16GiB/macOS27.0.1，固定系统目录与system/cn.tianming.erp仍不存在，工厂Git基线只读仍b9a077bc115cc649cdc2753cab3dfb3d75269d1c，Alembic唯一ep1010md。最小管理员包需按本提交新建并生成匹配的新钥匙串计划；旧成套材料保留，真实服务计划仍缺前置未生成。没有root安装、真实钥匙串/数据库/恢复目录写入，没有发信、收单、外部AI、正式切换或旧机清空。下一闭环优先收紧Mac备份真实远端证据与历史状态消费者，再解开专用身份首次发布/完整恢复顺序及管理入口；真实NAS合同、系统原生与注销重启、目标正式激活和工厂最终同步/实体打印扫码仍未完成。

## 2026-10-11 Mac备份远端证据接入范围

本闭环唯一写入负责人主代理；新增mac_mount.py和mac_backup_storage.py，允许修改nas_probe、Manager备份段、onboarding前置探针、nightly、GUI备份状态及app/services/managed_recovery_status，对应测试/合成RecoveryTests存储适配器、任务卡与迁移计划。Mac备份正向路径须实际匹配root保护的本机context与固定直连NFS合同；未知、代理、失联暴露的本地目录及未批准目的地在探针写入和停服前拒绝。独立子进程完整F_NOCACHE回读比较生成时已认证摘要，前后核对挂载与实际FD身份；成功后六字段原回执、独立Mac侧车、state依次发布。侧车只证明当次指定NFS服务器回读，不冒充远端物理盘断电持久性或后台注销验收。

定时和GUI/网页共用本地小型证据快路径，绑定receipt原字节、包/大小/路径、context/合同、挂载记录及验证时间；旧Mac回执和last_backup_at不再冒充远端验证，GUI不新增整包读或NFS探测。Windows原六字段/source-audit契约不变；原包认证恢复不依赖新Mac侧车，Mac retention仍不准据此删本地包。合成正例明确替代OS权限/挂载，不能绕过生产门禁来凑测试。没有真实NAS设备/协议合同，当前loopback挂载不能获批准，不改现有挂载/home权限、不读代理秘密、不动完整恢复或旧机。真正系统挂载/合同发布及注销重启仍须后继真实前置。

本范围已实现专用context+固定直连NFS合同门禁、认证原输入摘要绑定、独立F_NOCACHE整包回读及严格历史侧车。对原生ABI/参数、父链FD、ACL、设备、FIFO、字段/时间、Windows兼容和系统工作目录的独立只读复审发现均已修复；Mac探针及回读不依赖仓库CWD，Windows原入口保持。实际备份集成使用基础Manager，不通过RecoveryTests存储适配器；其他既有恢复测试仅用方法范围的显式合成本地存储适配器，生产代码不检测pytest或环境变量放行。

本机只读实测当前个人NAS仍为127.0.0.1:52100代理，before_write拒绝且系统目录前后不存在，没有执行写探针。当前协议仅是候选验证器，真实设备/导出身份、管理员合同发布器/系统挂载、专用UID实测、注销重启和断电持久性未完成。公共verified_at不访问NAS，但Web原有文件可达性stat仍可能等远端；不把本闭环描述成整个Web已无NAS IO。原完整恢复、凭据和旧机保留，家庭隔离及Mac清理门禁不变，未连接工厂库或正式接管。

最终助手全量及Web状态回归954项：938通过、16跳过、0失败错误，63.90秒，3条原依赖弃用/合成短JWT告警。跳过仍为15项Windows原生与1项显式个人LaunchAgent实机开关；没有为本改动新增跳过。原生ACL/FIFO与读取、真实-I脚本导入及CWD/PYTHONPATH隔离通过；Windows缺pwd/fcntl的测试收集用明确缺模块模拟验证，并非实际Windows执行。首轮3个ACL测试夹具用数字UID调用chmod失败、下一轮1个GUI类名误写已修，原日志保留；扩大164通过/1跳过、首次全量936通过/16跳过及最终全量不相加。探针入口修复后96项定向与最终全量全部通过。证据mac-nas-storage-final-v2.xml/log、mac-nas-storage-native-readonly.json，独立只读复审通过。

NAS开发归档仍仅作local_mount_view独立完整回读，remote_device_persistence_verified=false，保留本机所有原件。本轮未改最小管理员包的九个源码，沿用9ee62e24成套包和绑定计划的历史身份，不重签或混用计划；有期效的计划实际执行前仍须复核。下一闭环为专用身份首次发布/空实例完整恢复和管理入口，再接系统NAS合同发布及实际目标激活；尚未达成工厂实际运行目标。

## 2026-10-11 首次空实例发布与恢复输入绑定范围

本闭环唯一写入负责人主代理；允许修改mac_release_publish.py、prepare_mac_publication.py、Manager.restore、restore_cli.py、credential_restore.py及相应发布/恢复测试、任务卡与迁移计划。独立代理只读复核。新增明确绑定批准摘要的首次空实例发布计划类型，普通发布要求不变；首次仅允许已配置且完全为空的固定私有instance、无既有发布/系统服务及失败现场，发布全程固定目录FD并在批准前重查，不补建shared。完整双树及验签保留，server真实数据目录准入不放宽。此步骤仅解决程序就位，不能冒充专用账号完整恢复或已能原生运行。

恢复输入需把批准备份摘要传到核心，与同次GCM认证流摘要在解包、凭据重封装、数据提升之前比较；CLI隐藏输入不允许getpass回显降级，参数错误不回显秘密。外部移交包应使用已认证的同一份原字节，避免多次按路径读取混用。测试全部为合成备份/身份，真实完整恢复、现有签名运行包、钥匙串和旧机保持。本闭环后仍需实现签名专用恢复入口及同revision、reader/marker/schema严格校验的原生绑定，保留Windows来源原包和离线authority；不能通过普通update执行旧Windows程序或伪造reader激活。

本范围已完成并经两项独立只读复核。发布器持有BASE/instance FD，明确绑定系统Data卷、专用UID/GID及私有模式，拒绝隐藏数据/旧发布/系统服务/失败现场和目录中途替换；全量原包与双树保持，空实例不补建shared，真实server解析继续拒绝悬空数据链接。恢复核心及CLI绑定同一次认证流摘要，外部凭据以固定原字节和该摘要验证；A→B→A路径替换也不能混用凭据。三路终端、禁止回显降级和固定参数/异常错误已覆盖，原Manager返回合同保留，CLI最终核对state中的实际恢复摘要。

最终助手及Web状态1002项：986通过、16跳过、0失败错误，70.19秒；15项Windows原生和1项显式个人LaunchAgent跳过保持，3条原依赖/合成短JWT告警。定向125通过/1跳过、返回合同45通过，不与最终全量叠加。首轮GID夹具误改脱离核心的context、次轮外部凭据夹具误用含内嵌包、首次全量新增返回字段破坏旧精确合同，均已修正并保留原失败日志。合成权限/注册正例不冒充真实专用UID/root安装；实际PTY隐藏输入通过。证据mac-pristine-restore-final.xml/log及review-summary，独立复核无剩余本范围阻断。

本机只读仍UID501、arm64/macOS27.0.1，Data卷与候选均16777230、约389.1GB可用，专用账号、固定系统根与系统ERP注册仍不存在。Alembic唯一ep1010md，当前轮工厂Git只读基线仍b9a077bc。九源码管理员包中的发布器已变，须按本固定提交重建新包并生成匹配计划；旧成套材料保留，未执行任何真实安装、恢复、发信、收单、外部AI、工厂切换或旧机清空。下一闭环接签名专用恢复及原生绑定，不能把本次空实例发布当作完整接管。

## 2026-10-11 专用完整恢复、原生绑定及来源保留范围

本闭环唯一写入负责人主代理，独立代理只读。允许新增service_restore_entry.py、mac_service_restore.py、reader_contract.py及restore_provenance.py，修改Manager恢复/备份/兼容段、storage认证公共原语、retention、factory_source_audit的纯reader导入、build和pristine发布能力校验，以及对应测试/任务卡/迁移计划。目标是已批准的不可变migration树由实际专用UID执行完整恢复，并在同一维护锁内完成严格同revision/目标schema/全部原reader及真实激活marker验证，最终current绑定Mac、manual_stop=True；不执行旧Windows、不运行迁移、不伪造reader激活、不启动业务。

输入采用实例外固定restore-inputs全新attempt，由root系统文件工具复制既有加密原件并设root:专用组只读权限；不放宽个人home，不让root导入ERP或处理明文秘密。专用入口核真实UID/GID/组、root批准程序/入口/解释器、Data卷及FD/ACL，持独立恢复锁，三路TTY隐藏输入。流式备份口令认证复用现有GCM格式，认证丢弃输出后才初始化空instance；核心仍按批准SHA完整恢复。失败保留现场，不能删除旧机或真实恢复重试。

原Windows release/authority不仅入state与retention保留集，还应完整进入以后每次备份，并在第二次恢复保持其原始身份。来源声明与固定包成员严格一一对应，旧格式全无声明仍兼容，部分声明/缺包/错误绑定一律拒绝。历史原包仅离线核签，不要求现数据库仍符合其旧revision。原Windows签发的精确成员集合不放宽，新历史来源备份变体暂不作为Windows原端签发输入。测试须覆盖Windows原包加authority→Mac绑定→再备份→第二空实例恢复→后续保留，全程不调用迁移/旧程序/start/resume；真实root/专用UID/运行包签名/系统NAS和工厂现场仍待后续实测。


## 2026-10-11 恢复来源长期保留与认证原语检查点

先完成专用恢复的可独立验证前置：新增restore_provenance_v1及两项完整来源身份，原Windows release与历史compatibility原字节进入后续每次完整备份，第二次恢复继续保留原身份；restored_backup_sha256只指本次认证输入。历史authority核完整签名和preserve_existing_facts_v1/rollback绑定，不要求其旧revision等于后来升级的数据库。全缺失保持旧备份兼容，部分/假值/缺包/额外固定成员/错误摘要或绑定均拒绝。新build在声明reader前检查所需源码存在，尚不声明service_restore_v1。

备份空间按实际归档成员计量，重复身份占用的多个成员也计入；pack_recovery返回清单再次逐一核release/compatibility及历史包身份，避免核验后换包生成不可恢复的成功备份。restore在任何凭据重封装前完整核验当前、活动authority及历史ZIP，保留原模型错误合同。历史原包离线stage，不执行Windows程序。retention先验证完整来源再保护两角色；兼容、start（含已有进程）、单步/多跳更新、回退和中断恢复均接入能力门禁，缺reader在停服或状态改写前拒绝。Windows原来源签发的精确成员及六字段回执不放宽，新历史备份变体仍不作为Windows源端签发输入。

GCM抽取唯一流式实现，authenticate_stream对已打开FD完整认证并丢弃明文，不产生明文预检文件，仍须调用方绑定预期摘要和文件身份。修复decrypt_file原有既存输出open(xb)失败后误删目标的问题；只有本次创建且设备/inode仍相同的输出才可失败清理，替换对象保留。此检查不是原子的按inode删除承诺，恢复临时目录仍由调用方独占。没有改密码格式/密钥派生或绕过tag。

最终助手、Mac凭据和Web状态1100项：1084通过、16跳过、0失败错误，78.77秒，3条原依赖/合成短JWT告警。跳过为15项Windows原生、1项显式个人LaunchAgent，无新增跳过；两个Mac合成钥匙串实测开关开启，只使用测试临时条目，不接触运营秘密。新增52项真实签名/GCM/文件测试含连续两次恢复、全业务事实和非DB文件一致、邮箱位置/配置/秘密等值、retention保留、多跳/中断和晚期换包。初始来源state是合成前提，不表示已执行首次Mac绑定；真实root/专用UID/原生绑定和实际全量正式恢复均不在本轮执行。

初次定向156通过/2跳过/2失败，分别为保留链测试漏替换清理层进程探针和retention提前读取非法current路径，已修；随后69通过/1跳过。独立复核先在旧候选复现start两个反例和晚期换包三个反例失败，修复后定向51通过；首次扩大回归1075通过/16跳过/1失败为完整ZIP检查提前改变原模型错误文字，恢复先模型静态核验后全ZIP且仍先于protect，原测试不变。最终全量如上，中间结果不累加，所有日志保留。两项独立只读复审无剩余本阶段阻断。

本机只读为Apple M6/arm64/16GiB/macOS27.0.1(26A434)、约389.1GB空闲；专用账号、/Library/TianmingERP及system/cn.tianming.erp仍不存在。Alembic唯一ep1010md，工厂Git只读基线仍b9a077bc115cc649cdc2753cab3dfb3d75269d1c。NAS实际NFS4.0为个人UID501的127.0.0.1:52100代理；本轮归档只证明local_mount_view，remote_device_persistence_verified=false，不能当后台存储或远端断电持久性。沙箱硬件/挂载查询受限的原观察保留；经自动审批仅只读补全，不用unknown作正向证据。

本轮曾只读既有真实恢复state与缓存manifest（未读真实DB/运营秘密）；其Windows v0.22.624/ep1010md及reader声明只是缓存观察，最终恢复必须重新认证原包。四份真实恢复、原凭据、旧3de3be32家庭签名包和旧机未改；未连接工厂库、发信、收单或外部AI。九源码最小管理员包本轮未变，继续成套保留67c3c2b1包/绑定计划，不重建、不混用旧计划。Downloads同名移交文件仍缺失，既有NAS同名回执不能视为当前正式切换批准，凭据不重复索取。

剩余本任务工作继续保持未完成：签名专用恢复入口及固定root只读加密输入、实际身份/FD/空实例/口令预认证、严格同revision/schema/reader/marker原生绑定；其后系统管理/NAS合同发布、受保护目标激活及新工厂签名。真实管理员安装、后台凭据、注销重启/FileVault、搬入工厂后最终停写同步、完整业务与中文实体打印/扫码验收仍须实测。该检查点不等于ERP已接管，任何阶段不清空旧机。


## 2026-10-11 专用账号完整恢复与原生绑定候选

新增service_restore_entry.py薄入口和mac_service_restore.py；仅在已批准的不可变migration树、实际专用UID/GID及内核组、隔离Python -I -B下运行。固定root只读加密输入位于restore-inputs/<attempt>，父链、Data卷、权限、ACL、普通单链接文件和FD/命名身份持续核验；三路TTY隐藏输入，禁止回显降级。实例外同FD flock覆盖全部流程；canary、凭据认证及完整GCM丢弃式预认证通过后才初始化完全空实例。管理员以后只能用OS文件工具准备加密输入，不让root运行ERP或处理明文秘密，不放宽个人home。

Manager.restore新增可选native_package；默认恢复合同保持。源完整备份、源签名/authority、全库/附件及源reader先验证，再验证目标同实际DB revision的模型结构、全部reader和真实dispatch激活记录；源未声明inventory时也不能由目标补造来源证据。native必须有内嵌或外部已认证凭据记录，缺失在任何运营秘密写入前拒绝。原Windows程序和authority按原字节离线保留，跨后续备份/恢复与retention仍保存原身份。shared提升后复验root批准的server和migration树，最后current绑定Mac、schema_authority/previous为空、manual_stop=true；不执行Windows程序、不迁移、不启用缺失reader、不启动服务。

本轮独立复审发现并修复恢复锁替换的晚期检查、回执身份/原字节回读缺失、凭据输入短读完整性和native缺凭据仍完成四项。完成回执提示前拒绝既有对象，写后在同FD检查保护属性及完整原字节；失败保留输入、数据、凭据和部分回执，不能当作回滚后可直接重试。pristine首次发布和显式native恢复要求service_restore_v1/restore_provenance_v1及两入口源码；普通发布和离线历史authority不附加该新门禁。Mac构建声明新能力前核所需源码；新能力不代表已安装或正式激活。

合成集成覆盖Windows current r0+结构authority r1→Mac同r1绑定→完整再备份→第二空实例恢复→保留清理，全业务事实、非DB文件、三类测试凭据、原始两包身份一致。UID/root权限正例有明确测试适配器；真实FD/签名/GCM/SQL、独立进程锁竞争、普通UID拒绝、原生ACL/FIFO及短读行为有实际验证，不能冒充真实服务账号或系统钥匙串验收。旧候选的锁替换、回执四例、缺凭据反例确实失败；最初测试夹具7项错误修正并保留日志，非产品行为放宽。后续定向46项及扩大166项通过，统计不累加；最终整合结果见本节后续补记。

只读刷新M6/16GiB/macOS27.0.1、约389.4GB空闲；_tianmingerp、/Library/TianmingERP和system/cn.tianming.erp仍不存在，唯一Alembic head ep1010md，工厂Git仍b9a077bc115cc649cdc2753cab3dfb3d75269d1c。Downloads同名交接附件不存在，按NAS同名历史回执核对；已完成凭据移交不重复索取，历史步骤不是当前切换许可。四份真实恢复、运营凭据、3de3be32家庭签名运行包与旧机未动，未连接工厂DB、发信/收单/外部AI或正式接管。

发布器源码已变，九源码最小管理员包须按本次固定提交重建、配套新钥匙串计划，旧成套材料保留，不能混用。尚无新的完整工厂签名程序和root context，不伪造真实发布计划或执行恢复。下一闭环仍需专用身份日常管理入口、系统NAS合同/挂载发布、受保护目标激活及新工厂签名；实际管理员安装、专用账号真实完整恢复、后台凭据、注销重启/FileVault、工厂最终停写同步及业务/中文实体打印/扫码仍待实测。现NAS为127.0.0.1:52100个人NFS代理；归档只证明local_mount_view，不证明远端设备持久化或后台可用。本检查点不是迁移整体完成，任何阶段不清空旧机。

最终整合回归1163项：1147通过、16跳过、0失败错误，98.06秒；本轮新增63项（专用恢复55项、pristine能力8项）全部通过。跳过仍15项Windows原生和1项显式个人LaunchAgent，3条原依赖/合成短JWT告警；两个Mac测试钥匙串开关已开启，仅处理测试临时合成条目。普通UID真实-I -B调用薄入口在ERP导入前拒绝。两项独立只读复审未见剩余本范围代码阻断，差异检查通过；不把合成UID/权限适配器当作实际管理员安装。最终证据mac-service-restore-full.xml/log，旧失败及中间记录保留不累加。


## 2026-10-11 系统autofs归档安装与明确触发范围

本闭环主代理唯一写入，独立代理只读核验。允许新增mac_archive_contract/mac_archive_install/mac_archive_probe及archive_probe_entry，修改mac_mount、mac_backup_storage的受保护autofs触发分支、管理员最小包/CLI清单、Mac build能力及对应测试、本任务卡和迁移计划。保留现有直连合同兼容；新autofs协议固定Data卷物理mountpoint与绝对root map，计划绑定原master完整SHA/派生配置及本机context/批准probe程序。root只安装标准库配置，不运行ERP或读取运营凭据，不改个人挂载、不猜NAS端点。首次通过真实专用UID随机合成写入/rename/独立F_NOCACHE回读后才发布正式归档合同；系统按需触发不等于启动已就绪，失败保留现场，不自动卸载/清理或回退配置。现无管理员安装和真实NAS导出身份，本轮只能实现合成验收及可审阅材料；最终仍需实际后台/注销重启和远端核验。

本范围完成系统autofs候选协议：固定物理Data卷archive路径、/private/etc绝对map及字面IPv4/export，不推断极空间代理的真实设备身份。管理员计划绑定当前context、批准程序/公钥/registry、代码manifest、原auto_master及派生配置完整SHA、attempt与时窗；只在身份预检通过后保存原master和pending，再独占写map、原子发布保留原字节的master增量，并调用固定automount -v。已有配置、挂载、逻辑/物理别名、失败现场或并发master改动均拒绝覆盖；root只扫描archive名称，不访问待核远端，不导入运行时或读取运营秘密。

归档入口先拒绝root/普通用户，实际专用UID/EUID/GID/EGID/内核组与root context严格相符后，复验当前脚本/解释器、root批准包及完整签名migration树。身份阶段没有NAS IO；probe/readback必须绑定固定root pending原字节。只写新的本次attempt目录与128KiB随机合成文件，专用账号核权限/ACL、实际NFS与FD，fsync后rename，再由新隔离进程F_NOCACHE完整回读；前后重复核文件/目录/配置/挂载身份。失败保留全部现场，不自动卸载、删除、回退或强杀，hard NFS可能持续等待，不虚构有界超时。

正常备份保留旧直连v1合同；新autofs合同先读取root保护配置，核唯一正确触发层后显式open，再要求同一autofs身份上叠加唯一批准NFS并核实际FD。单NFS、错误map、未知叠层、本机代理、本地空目录、只读/async/soft/非预留端口及权限错误均拒绝。原生内核观察包含完整trigger，历史侧车同样严格，成功仍仅证明当次指定服务器回读。root在探针结束后复核完整IPC证据与计划有效期，最后发布正式合同和独立安装回执；不改ERP state、last_backup或Windows六字段回执，不把探针当正式备份。

独立只读复核发现并修复叠层身份丢失、配置父链/短读检查、初次触发身份变化、历史trigger字段、探针跨越批准期、root回执挂载验证不足和vacant可能stat待核路径。两项复审本范围无剩余代码阻断。新增117项全部通过；最终完整助手/凭据/Web状态1280项，1264通过、16跳过、0失败错误，99.88秒，3条原依赖/合成短JWT告警。15项Windows原生、1项显式个人LaunchAgent跳过保持；两个Mac测试钥匙串开关开启。既有191项、新流程早期80/103项和定向371项为中间结果不叠加。root/NFS/专用UID正例用明确适配器，真实私有文件IO、新进程F_NOCACHE、普通身份拒绝以及系统Python3.9 -I -B -S仅stdlib导入实际通过；未声称真实专用UID或autofs上线。

2026-10-11T06:04Z只读刷新仍Apple M6/16GiB/macOS27.0.1、约389.3GB空闲；_tianmingerp、固定系统根、system/cn.tianming.erp未创建；NAS仍UID501的127.0.0.1:52100个人NFS代理。工厂Git只读仍b9a077bc115cc649cdc2753cab3dfb3d75269d1c，唯一migration ep1010md。现有auto_master195字节同FD保护读取通过，但无系统配置变更。Downloads交接附件仍缺失，既有NAS历史回执不能作当前切换批准；已移交凭据不重复索取。四份真实恢复及3de3be32家庭签名运行包未改，本轮未读真实DB/运营秘密。

最小管理员包扩为12源码加manifest共13文件，必须按本提交生成新成套包与钥匙串计划；实际账号/批准新版完整签名程序及明确NAS端点未齐，不能生成真实归档安装计划，更未执行安装或探针。新增archive_autofs_v1仅为构建协议声明。现NAS开发归档继续只证明local_mount_view，远端设备持久化未验证。仍须专用管理入口、配置NAS偏好、受保护目标激活及新工厂签名、实际root安装/完整恢复、后台凭据与注销重启/FileVault、最终工厂停写同步和实体打印/扫码验收；不清空旧机、不连接工厂DB、发信、收单或外部AI。

另只读复核既有路径盘点报告及代码：31,027项company_file_index源路径目前用于文件计数，其他Windows路径主要是历史审计、备份记录、幂等证据；尤其adoption_evidence_reference参与精确字符串幂等判断，不能批量替换。888项注册附件含880图纸/缩略图及8份PDF；86个邮件附件名对应DB BLOB。上述不等于历史外部原文件已搬迁，index_sources_accessibility_verified和complete_historical_external_files_verified仍false；需工厂只读清单将原引用、原件/归档、大小/SHA、Mac可取位置对应，不重建退役抽取层，不改历史事实。

## 2026-10-11 专用日常管理与系统定时备份范围

本闭环主代理唯一写入；新增service_manage_entry/mac_service_manage，允许修改mac_system_service/最小管理员CLI、service_supervisor/nightly/Manager备份恢复启动条件、Mac build以及对应测试/任务计划。管理入口只允许固定实例和root批准不可变程序，以实际专用UID执行status/configure-backup/backup/start/stop；root仅标准库降权调度，输出固定非秘密状态，不能传密码/任意目录或命令。配置仅绑定现有批准NAS合同、保留已恢复凭据引用及其余偏好；状态/停机不能因凭据不可用受阻。启动保持家庭隔离，最终正式激活另行完成。

现有系统监护尚未调用nightly，个人LaunchAgent不能代表无人登录定时备份。本轮在已验证系统监护内接独立备份线程，复用维护锁、23点到期与已验证侧车；手动停止/维护失败/root停用不读秘密也不重启。备份故障记录固定公开错误，ERP监护继续；退出先等在途备份，禁止其在退出过程中恢复服务，再正常停止，不强杀、不承诺hard NFS有界返回。Windows/个人任务保持既有语义。本轮仅合成测试与代码/交接证据，不实际管理员安装、不动真实恢复/运营凭据，不连接工厂库、发信、收单或正式接管，不清空旧机。


本范围实现并验证：root固定调度只启动已批准原生程序的专用管理入口，实际身份/脚本/解释器/签名树复验后，在持有完整私有父目录及原维护锁FD期间执行五项管理动作。配置仅更改批准NAS路径，保留全部偏好和已恢复凭据引用；唯一pending记录同FD回读后原子发布，既有pending链接及并发改动保留。状态/正常停机不解密秘密、不做NAS IO；NAS元数据故障只标未配置，备份错误只返回布尔提醒。start须系统注册/启用与定时协议齐全、后台canary通过；所有动作仍家庭隔离，错误/中断保留现场，不能把未确认当自动回滚。

系统监护独立定时线程复用23点到期、维护锁及可信备份侧车；root停用、手动停止、未决维护和退出前拒绝读取秘密。正常退出等在途备份结束，finally不再于退出期间重启ERP，再正常停止。故障固定公开状态并延迟重试，不关闭ERP监护、不强杀。Windows/个人原有语义保留；旧签名监护不能凭历史nightly_schedule显示系统备份已经安装。新service_manage_v1/system_nightly_v1只声明源码协议，尚未部署。

旧代码定时回归先复现1失败；初版108项全过，中间136通过/1测试夹具误传extract_verified参数失败保留，修正后137及最终定向146项全过。最终完整助手/凭据/Web状态1343项：1327通过、16跳过、0失败错误，97.56秒，3条原依赖/合成短JWT告警；新增63项全部通过，中间结果不叠加。15项Windows原生及1项显式个人LaunchAgent跳过保持，两个Mac测试钥匙串开关开启。真实私有FD/ACL/flock、普通UID入口拒绝、合成签名完整备份解密恢复后DB事实及附件比较已验；root/专用UID/系统服务/NAS正例为明确适配器。本阶段主代理顺序源码复核，无新增独立代理复审，不冒称真实管理员安装或后台定时验收。

2026-10-11T06:20Z只读复核Apple M6/arm64/16GiB/macOS27.0.1、约389.1GB空闲；_tianmingerp、/Library/TianmingERP及system/cn.tianming.erp仍不存在，NAS仍UID501的127.0.0.1:52100个人代理。初次沙箱sysctl/mount未知结果保留，随后经自动审批普通用户只读核验成功。工厂Git仍b9a077bc115cc649cdc2753cab3dfb3d75269d1c，唯一head ep1010md。Downloads附件仍缺失，已有NAS同名凭据交接回执存在；交接已完成，不重复索取密码，历史步骤不构成当前工厂切换批准。

最小管理员包仍12源码+manifest，但调度源码变化，须从本次固定提交生成新的成套包及匹配钥匙串计划；旧成套材料保留，已初始化钥匙串不重复创建。真实完整工厂签名包、root安装/完整恢复、独立NAS端点/UID映射、后台凭据/注销重启/FileVault、受保护目标激活、31,027历史索引原件可达性、最终停写同步和业务/实体中文打印/扫码仍未完成。四份完整真实恢复、3de3be32既有签名运行包及运营凭据未触及；无工厂数据库连接、外部发信/收单/AI、正式接管或旧机清空。NAS开发归档只证明local_mount_view，不能当远端设备持久化。


## 2026-10-11 目标正式网络与切换申请范围

本闭环主代理唯一写入；新增stdlib目标申请契约/网络验证、专用申请入口与root申请保存编排，复用现有身份、发布和私有实例维护锁。允许修改factory_cutover的契约抽取与网络绑定入口、mac_system_service/最小管理员CLI及包清单、Mac build和对应测试/任务计划。申请精确绑定真实本机专用身份、批准完整程序、公钥和所有明确网络字段；root保存公开申请/网络原字节，不能接受任意不透明摘要作为部署配置。网络同时覆盖现有HTTPS代理及私网HTTP方案，禁止隐式默认、未知字段、URL凭据/通配和外部直连监听。生成申请不改environment/state、不读取秘密、不停启服务、不写工厂库或执行激活；网络/TLS可用性及后台/现场验收明确不由申请证明。只有未来完成受保护激活实现和新版签名程序后才可实际生成工厂申请，不提前声明factory_cutover_v1能力。旧源端协议、签名域、完整性/reader/权限/幂等与家庭门禁保留。

本范围实现完整14字段公开网络提案，绑定浏览器/健康URL、来源、可信主机/代理、监听、Cookie及手机/货位二维码入口。HTTPS为回环反向代理，私网HTTP仅RFC1918 IPv4/0.0.0.0直连，均明确非特权端口。源端codec与root入口共用stdlib目标请求契约，保持原签名域与请求格式；不将提案当TLS/网络或现场验收。复查现有mobile_qr_origin发现HTTPS货位地址会被实际应用拒绝，新增真实解析器失败用例后修复；货位非空只准批准私网HTTP，手机独立覆盖或明确空值回退。ERP_MOBILE_QR_ORIGIN同时纳入完整网络摘要和快照技术字段，两端须用同版核验器，其他环境/业务事实保留。

管理员入口只读核验网络文件独立SHA和同FD稳定性；固定程序以实际专用UID复验身份、签名树、维护锁及current/manual_stop/进程后返回严格类型公开IPC。root再次核元数据/时间，独占保存网络和最后的目标申请，文件及父目录fsync/完整回读，失败保留部分现场与全部旧记录，不强杀、不重复请求。申请只绑定目标，不改environment/state、不解密凭据、不做NAS/工厂业务IO或服务停启。回执明确网络连通、最终备份、凭据、源端物理隔离及factory_activated均false。factory_request_v1仅申请协议；正式factory_cutover_v1仍不在build提前声明，真实申请须等完整激活实现/新签名包/root实例齐备，本轮没有实际申请或激活。

最小管理员包增加目标契约、网络验证和申请保存三个stdlib源码，现15源码加manifest共16文件，必须按本次提交新建成套包及匹配钥匙串计划，旧材料保留。只读新核Apple M6/16GiB/macOS27.0.1，约389.2GB空闲；实际专用账号/系统根/注册仍不存在，NAS仍UID501的127.0.0.1:52100代理。工厂Git仍b9a077bc115cc649cdc2753cab3dfb3d75269d1c，唯一ep1010md。Downloads附件缺失但NAS历史凭据交接存在，不重复索取凭据、不将历史步骤视为当前切换授权。四份完整真实恢复及3de3be32签名运行包未动；未读写真实DB/运营秘密，无发信、收单、AI或正式接管，旧机不清空。

最终完整助手/凭据/Web状态1444项：1428通过、16既有平台跳过、0失败错误，97.37秒，3条原依赖/合成短JWT告警；新增101项全过，定向309项不重复累加。最初网络绑定入口缺失1失败、扫码解析器实际409失败均保留；扫码修复前完整1422通过/16跳过的中间结果不当最终验收。15项Windows原生及1项显式个人LaunchAgent跳过保持，两个Mac测试钥匙串开关开启。真实有界文件IO/维护锁/普通UID拒绝、实际应用Settings/二维码解析器和系统Python3.9 -I -B -S导入通过；root/专用UID/批准树正例用明确适配器。主代理顺序源码复核，无新增独立代理复审。接下来继续受保护目标激活和最终恢复/凭据验证，再构建新版完整工厂签名包；实际系统安装、后台NAS/注销重启、历史外部原件可达及工厂最终停写同步/业务/中文实体打印扫码仍未完成，总目标保持未完成。

## 2026-10-11 最终恢复目标内容核验范围

本闭环主代理唯一写入；新增factory_target_audit.py及合成集成测试，允许修改Mac build所需源码/能力声明和本任务卡、迁移计划。只实现正式激活之前的专用身份核验上下文：绑定root保护的目标申请、网络、源端签名授权及本次root只读完整备份，重做GCM认证、全数据库事实/结构、附件、reader/marker、来源原包和内存运营凭据等值比较。保持现有私有父链和原维护锁FD，核验前后复查静态库/配置/输入与授权时窗；目标NAS偏好只能是既有批准合同目的地，其余配置保留。审计仅在新私有staging目录解包，成功或失败保留现场，不修改既有目标数据、凭据、环境、state或激活记录，不访问远端NAS或工厂服务、不启动/停止程序。

只声明核验协议，不提前声明factory_cutover_v1。当前真实运行包、四份真实恢复和已移交运营凭据不参与开发测试；测试使用临时合成加密备份、签名、SQL和附件，root/专用UID正例用明确适配器。后继仍须受保护激活写入/所有实际运行入口闭合、新工厂签名、实际系统身份与NAS/注销重启、工厂最终停写同步和实体业务验收；核验结果本身不是正式接管许可，任何阶段不清空旧机。

本范围已完成核验上下文：绑定root保护的本机申请/网络/签名授权/加密原件和已有专用恢复回执，完整认证备份后比较原始库及全事实/附件/结构/reader/marker/原签名程序保留。现有静态目标保持停止和同revision，逐用途内存核对Windows→Mac秘密/配置/邮箱版本，只允许批准NAS位置差异。环境必须仍是备份原字节，实际网络应用留给受保护激活；当前包和两份历史来源缓存原件均重新核SHA。关键私有FD/原维护锁全程持有，反复核静态树/原包/秘密/授权时间，凭据读完后再查附件元数据防止晚期变化。返回证据为复制，不允许调用者修改内部基线，关闭上下文后不能将旧字典当实时许可。

主代理顺序复查，无新增独立代理。新增原生专用位置/能力、实际静态文件保护和内容差异反例；原维护锁竞争拒绝、签名有效但内容不一致/认证tag错误拒绝、上下文内中途变化拒绝，保留现场。初始缺模块测试确实失败；首轮2项与62项通过，中间扩大385项通过（在最后晚期附件复查补丁前完成，不冒充最终源码结果）；补丁单例通过，最终全量见后续补记。没有root安装、真实目标核验/授权签发、实际运营秘密或DB读取；真实恢复及旧机未改，未发信/收单/外部AI/工厂业务连接。

最终整合回归1518项：1502通过、16既有平台跳过、0失败错误，156.38秒；本轮新增74项全部通过。跳过仍15项Windows原生和1项显式个人LaunchAgent开关，两个Mac合成钥匙串测试开关开启，3条既有依赖/短JWT测试告警。中间385项与初始62项不累加；晚期附件复查已包含本次最终源码与回归。差异检查通过，真实root/专用UID/NAS正例仍未实测，本轮无新独立代理复审。

本机2026-10-11T06:51Z只读刷新Apple M6/16GiB/macOS27.0.1，389309272064字节空闲；专用账号、系统根和system/cn.tianming.erp仍不存在。NAS仍为UID501的127.0.0.1:52100 NFS个人代理，auto_master原SHA未变；工厂Git只读基线b9a077bc115cc649cdc2753cab3dfb3d75269d1c，唯一head ep1010md。15源码管理员包与0373e433成套材料逐文件一致，无须本轮重建；实际完整签名家庭包仍3de3be32。NAS开发归档只作local_mount_view独立完整回读，不代表远端持久化；保留全部本机原件。未进行工厂DB连接、真实凭据/恢复目录改写、外部发信/收单/AI、正式接管或旧机清空，整体任务保持未完成。

## 2026-10-11 受保护激活发布与原维护锁交接范围

本闭环主代理唯一写入；新增标准库factory_activation_contract/mac_factory_activation_install、专用factory_activation_entry/mac_factory_activation，修改mac_service_manage原维护锁FD接收、factory_target_audit上下文、最小管理员CLI/包清单及build能力声明，并补对应合成测试、任务卡/计划。root仅读取公开输入和维护锁控制字节，不导入ERP运行依赖、不读取数据库或运营秘密；它持有原维护锁并把同一open-file-description通过pass_fds交专用UID。核验子进程退出后root继续持锁直到独占写入/回读激活记录完成，覆盖核验与发布之间的时序空档。

显式管理员命令绑定申请ID、输入attempt、已审阅授权SHA和本机批准程序。子进程完成上一阶段全部认证核验并回读服务canary，仅返回严格公开证明。root再次检查元数据/时窗，保留pending和所有旧材料，最后独占创建固定激活记录；失败不自动覆盖、重试、强杀或回滚，仍保持manual_stop且不启动业务。原FD继承不能成为跳过flock的参数：无论来源都实际取得同一锁，核验身份/模式/ACL/原命名inode和字节；借用FD退出不得解锁父进程。

本阶段不改变Manager/server/应用/监护的家庭模式执行，构建不提前声明factory_cutover_v1或factory_execution_v1；真实入口必须等待完整执行门禁与新版工厂签名。全部root/专用UID正例使用明确测试适配器与合成备份/身份；当前完整真实恢复、运营凭据、旧机保持，不进行真正安装/激活/工厂连接、发信、重复收单或外部AI。下一闭环接所有实际运行入口，不能把root记录候选单独当作ERP已正式运行。

范围补充factory_cutover纯形状校验抽取至factory_activation_contract供root标准库和源端codec复用，原请求/授权格式、签名域和核验行为保持；标准库形状通过绝不等于Ed25519认证。正式发布命令还要求显式当次切换/旧端隔离声明，专用子进程核系统监护已安装启用、后台canary以及既有Mac完整备份本地侧车；这些声明和元数据不能冒充实体网络/旧端物理检查。本轮不会实际提供这些声明执行正式切换。

本范围完成标准库激活记录契约/root发布器与固定专用核验入口。root原维护锁通过同一open-file-description继承，借用方仍核身份与实际flock但不解锁父进程；原锁持续覆盖完整认证、子进程退出与激活记录发布回读。root只读公开输入及控制字节，不导入ERP/数据库/运营秘密。专用进程在家庭/内核断网保护下完成全部源签名/GCM/全事实/附件/凭据核验，并核系统注册启用/备份协议、后台canary及晚于本次恢复的Mac完整备份侧车。root复核严格IPC、原字节绑定、元数据与时窗，独占创建pending和固定记录，文件/目录fsync并回读。

已有记录、重放、损坏输入、锁替换、中断和晚期变化均拒绝；失败保留现场，不自动覆盖重试、强杀或回滚。最后回读失败时记录可能已发布，manual_stop仍保持且不启动服务，不能声称已回滚。源端纯形状校验共用但密码学认证不省略；管理员声明、系统注册和备份侧车不替代实际物理隔离/网络/注销重启验收。本阶段仍只声明factory_activation_v1，缺少factory_execution_v1及reader factory_cutover_v1时发布器拒绝；真实运行消费者下一闭环接通。最小管理员包新增三源码，现18源码加manifest，必须按本次固定提交重建并配套新计划；旧材料保留。

最终完整助手/凭据/Web状态回归1618项：1602通过、16既有平台跳过、0失败错误；XML耗时193.05秒，终端193.06秒，3条既有依赖/合成短JWT告警。新增100项全部通过；定向276项、早期49项/106项不另累加，无新增跳过。两个Mac临时合成钥匙串测试开关开启；15项Windows原生与1项显式个人LaunchAgent仍待各自平台/开关实测。真实合成GCM/签名/SQL/文件、sandbox-exec跨进程原锁、普通UID拒绝和系统Python3.9 -I -B -S标准库导入通过；root/专用UID/系统注册与备份侧车正例为明确适配器。主代理顺序复核未见本阶段剩余代码阻断，无新增独立代理复审；差异检查通过。

本机2026-10-11T07:13Z只读仍Apple M6/arm64/16GiB/macOS27.0.1，389315493888字节空闲；专用账号、系统根及system/cn.tianming.erp不存在。NAS仍UID501的127.0.0.1:52100 NFS代理，auto_master未变；工厂Git只读基线b9a077bc115cc649cdc2753cab3dfb3d75269d1c，唯一head ep1010md。Downloads指定附件仍缺失，NAS同名历史凭据交接回执已核对；凭据不重复索取，历史步骤不是当前切换授权。四份真实恢复、运营凭据、旧3de3be32家庭签名运行包及旧机均未改，本轮未访问真实DB或运营秘密、未连接工厂DB/发信/收单/外部AI。NAS开发归档只证明local_mount_view，不证明远端物理持久化，保留全部本机原件。未执行真实安装、申请、激活或正式接管，整体目标继续未完成。

## 2026-10-11 正式运行入口与受保护模式准入范围

本闭环主代理唯一写入：新增mac_factory_runtime及合成验证，允许修改Manager环境/启动、server_entry、app.main启动前准入、home_rehearsal外联检查、published运行器能力门禁、系统管理/监护公开状态、build能力及对应测试、任务卡和计划。仅在已有root激活记录经过完整形状、源端Ed25519、硬件/专用身份/实例/原生恢复来源绑定核验后应用已批准网络覆盖；原environment与业务数据保持。批准记录缺失仍为家庭模式，存在但损坏必须拒绝正式启动，不把错误降为成功；历史激活时窗不变成运行到期停机。后续已批准升级必须保留正式执行能力，不能回退到忽略门禁的程序。

真正服务入口先核隔离解释器与批准不可变server树、当前包和控制/数据库/网络绑定，再建立只在本进程有效的准入状态。专用身份直接导入应用或仅设置ERP_HOME_REHEARSAL=0不得跳过；正常个人/Windows开发与既有家庭保护保持，已安装的家庭网络审计钩子不能降级。状态/停机仍不读取运营秘密，损坏激活记录可报告attention而不阻断停机。全部正式模式正例使用合成签名/数据与明确OS身份适配器，不触发真实外联或收单；真实安装、签名、最终同步与工厂业务/打印扫码仍需后续实测。本轮没有工厂切换授权，不激活真实实例，不清空旧机。

本范围已接通Manager环境/启动、固定server入口、应用导入与lifespan、外联判断、批准程序能力及管理/监护状态。受保护记录缺失继续家庭模式；已有坏记录拒绝正式启动，状态/停机仍可用。只对真实专用身份应用此新准入，保留个人/Windows开发路径；不声称抵御同一数据库所有者任意恶意本机代码。签名历史激活时间不作为运行到期日；后续root批准升级可改变current与registry，仍须保留原生恢复来源、原申请包批准及完整正式执行协议。Mac build现在声明factory_execution_v1与factory_cutover_v1，实际完整程序必须重新构建和工厂签名，旧家庭包不会自动获得这些能力。

范围补充：回归提交后，允许从固定Git字节、本机既有arm64运行时/依赖/OCR模型构建全新完整未签名Mac包与分离签名请求，并生成与改动后的标准库管理员包匹配的新计划。构建/请求仅供工厂复核，不读取发布私钥、不签名、不部署、不生成真实切换申请或授权。新构建现场与旧材料保留；只按既有构建器回收其自身可重建临时payload，任何阶段不清空旧机。具体构建与NAS独立回读结果保存到任务外部证据及本阶段独立回执。

顺序复查发现直接已批准服务入口和环境构造还可绕过系统注册/启用/定时备份协议及后台canary。新增四个合成反例在修复前全部实际失败，修复后五项通过（含已有进程分支）；现在Manager启动、正式环境交付和固定服务子进程都核这条门禁。状态/停机不调用该准入，激活损坏仍可查看并正常停止。原env/业务数据与家庭网络保护未改变。

最终整合1684项：1668通过、16既有平台跳过、0失败错误，终端247.58秒；新增66项全过，3条既有依赖/合成短JWT告警。两个Mac临时合成钥匙串开关开启；15项Windows原生及1项显式个人LaunchAgent跳过保持。最初102通过/3失败为测试标志适配和状态字段断言，已修；109/256/7/3项中间通过与修复前1663通过/16跳过的整合结果不累加。最后整合包含全部应用/lifespan、系统服务与canary修复，旧失败与日志均保留。主代理顺序复核，未新增独立代理；差异检查通过，不把适配器/进程替身当实际系统或业务验收。

07:26Z只读观察为Apple M6/arm64/16GiB/macOS27.0.1，389316476928字节空闲（构建前）；专用账号、系统根及系统ERP服务不存在，NAS仍UID501的127.0.0.1:52100 NFS代理，auto_master未改。首次观测脚本的输出重定向造成空文件，保留后重新只读成功，不把空输出当证据。工厂Git只读仍b9a077bc115cc649cdc2753cab3dfb3d75269d1c，唯一head ep1010md。Downloads指定附件缺失，NAS历史凭据交接已核，不再索取密码、不把旧回执当切换授权。四份真实恢复、旧3de3be32家庭签名程序与旧机未改；没有真实DB/运营凭据读取、工厂DB连接、发信、收单、外部AI或正式接管。固定提交后的完整未签名构建及成套管理员材料/工厂复核交接结果保存外部证据，整体目标仍未完成。


## 2026-10-11 实际管理员安装失败后的身份兼容与有限续接

老板实际执行fa514540管理员脚本后，root目录及00-plan/01-groups/02-users已落盘，专用UID/GID 499已创建，但home/instance及完成回执未创建。普通身份只读复现_check_identity拒绝：本机打印组100通过localaccounts61继承，共享组701通过everyone12继承；不是额外直接管理员授权。AuthenticationAuthority普通用户读不到，root创建流程已通过原字段比较后写02-users，不依据普通用户缺字段删除或重建账号。

本最小闭环修正真实组拓扑识别和消费者的一致检查，只认可固定打印组及严格everyone嵌套的sharepoint组，管理员/未知/直接成员/不符拓扑仍拒绝；保留UID/GID、禁用登录、私有目录、签名与全部业务门禁。新增仅对原三份root创建证据完全匹配、尚无后续文件的有限续接；不接管外来账号、不改旧记录/系统组、不清空现场。旧原身份计划须保留，后续钥匙串计划绑定新固定代码。只做此次安装故障修复及合成/只读验证，真实继续安装交老板在Mac终端交互执行；不连接工厂库、不发信/收单/外部AI，不清空旧机。


本修复实际只读身份检查及系统Python3.9 -I -B -S仅标准库导入通过，未修改真实账号/组或root现场。身份定向76项、关联304项、消费者24项、修正测试隔离后的127项均通过，集合重叠不累加。第一次全量1679通过/19失败/16既有跳过，19项都是两处合成测试读取真实root私有system-service.json导致PermissionError；改为各自临时控制路径后127项全部通过，生产拒绝行为保留。固定提交后的最终完整回归、新管理员包/原计划绑定、未签名完整原生包及NAS交接另存外部证据；未实际执行complete或钥匙串安装，不冒称本机系统就绪。


本轮最终整合1730项：1714通过、16既有平台跳过、0失败错误，XML耗时247.434秒；新增12项，3条既有依赖/合成短JWT告警。310项关联验证集合重叠不累加。22处合成连接的closing与原事务上下文已逐AST核验；参数收集最长ID96字符，65537字节反例保留；相对工厂基线diff检查通过。根身份完成与旧预检故障由老板真实只读回执确认；新钥匙串初始化及新Windows实机复验仍待执行，不冒称成功。固定提交后的新管理员包、完整未签名原生程序、NAS归档及工厂复核材料另存外部证据。

## 2026-10-11 已发布托管链接权限故障及发布器修正范围

老板实际 v3 只读回执确认：真实专用实例已有 325 张表，完整性/表计数和同 revision 原生结构通过；shared 已提升、凭据配置已落位，state 与专用完成回执均缺失，ERP 未启动。已发布 server 四个 root 所有托管链接实际为 0700，非属主 readlink 被拒；原发布器在 077 umask 下创建链接但未设置链接自身权限，降权 worker 只核签名未核链接可读性。已发布 migration 完整核验通过。

本最小闭环唯一写入者为主代理，允许修改 mac_release_publish.py、tests/desktop_assistant/test_mac_release_publish.py、本任务卡与迁移计划。新发布明确将四个链接自身设为 0755，保留 root 所有及固定绝对目标，并在降权验签 worker 中实际读取全部链接后才允许批准。覆盖 077 umask、错误模式/目标/非链接/读取失败与子进程回执缺失拒绝。此处不添加晚期恢复完成入口，不改业务库、凭据或空实例恢复门禁；后续完整续接需另设有限检查点闭环。

当前已发布签名代码及程序原包保持。任务外管理员工具只允许在固定包/计划/公钥/context、既有锁及严格四链接现场通过后记录原件、对链接自身执行不跟随目标的 chmod，并由 UID 499 复验完整 server/migration/cache。既有数据目录权限、inode、内容和所有者保持；不补写 state、完成回执或 reader 激活，不启动服务、不重跑恢复，不删失败现场或旧机。真实工具执行状态记录 NAS 独立回执，候选源码改动不等于新签名包已发布。

实际管理员执行已返回成功：四链接均已为0755，root所有者/目标保持，业务目录元数据不变；UID499完整server、migration及缓存清单复验通过。state及专用恢复回执仍不存在，恢复未完成、服务未启动、工厂未接管。候选发布器关联205项全部通过；任务外修复22项检查及真实管理员回执分别留证，不混作同一测试集合。

## 2026-10-11 已落位恢复的有限完成闭环

唯一写入负责人为主代理。允许新建 desktop_assistant/mac_restore_completion.py、desktop_assistant/service_restore_completion_entry.py 及 tests/desktop_assistant/test_mac_restore_completion.py，修改 desktop_assistant/build.py 和必要构建测试，以及本任务卡/迁移计划。只为已有专用恢复锁、唯一原暂存槽、shared已提升、凭据配置与原包缓存完整、state/完成回执均不存在的停止现场增加独立完成入口。原空实例恢复入口和全部既有门禁保持不变。

新入口必须来自原发布公钥签名且root已批准的完整migration程序，完整核正在执行的模块、入口、解释器及专用内核身份；可核验并完成另一个仍已批准的原恢复目标包，不能将候选执行包冒充原恢复来源。固定根目录、不接受任意恢复路径，绑定context/输入attempt/原暂存槽摘要/目标包/备份/原公钥/外部凭据密文摘要。先独占保留本次尝试，再在新私有工作目录认证完整密文及解包；不改原暂存区、业务库、配置或钥匙串条目。

来源Windows原程序/结构权威/历史来源、同revision原生结构、reader及真实dispatch marker、全表计数/完整性、所有事实/附件、PDF来源映射、environment原字节、配置除凭据封装外原语义、逐用途秘密与邮箱版本均需比较。外部移交口令仅本机隐藏输入；重读凭据只用于内存相等判断，不能打印秘密。先复验完整server/migration与缓存，再独占写入manual_stop=true的state及原格式恢复回执；不覆盖已有state、不删除pending/尝试或失败现场、不激活reader/服务/工厂。

合成测试必须真正制造“shared提升后、state前”失败点，覆盖内容/签名/身份/路径/模式/锁/凭据变化、重复执行和中断不覆盖；正例不能调用原restore或重新保护凭据。新代码完成构建后须由原工厂身份复核分离签名，不能自行签名或修改现有签名树。后续实际恢复完成、后台服务/NAS/注销重启、工厂停写同步及人工业务验收仍独立记录。

本闭环最终整合1907项：1891通过、16既有平台/显式开关跳过、0失败错误；终端336.16秒，完整发布/恢复/切换与临时合成Mac钥匙串、中文字体均在范围内。新增107项定向通过与整合集合重叠不累加，最终整合包含最后的GCM失败私有核验文件保留修正。首轮86通过/1失败为签名能力拒绝错误文字断言，已修；所有原日志保留。顺序复查由同一主代理完成，未声称独立代理验收。新模块未接触真实私有数据/运营凭据，未写state或安装程序，Mac实际完整恢复仍待新候选工厂复核签名及本机终端执行。工厂只读参考基线仍b9a077bc115cc649cdc2753cab3dfb3d75269d1c、唯一head ep1010md。完整固定提交构建/包摘要/签名请求和NAS独立回执另存外部证据。

## 2026-10-11 隐藏输入前台检查及解密前有限续接

主代理唯一写入；本闭环限 mac_restore_completion.py、build.py、对应 completion/build 测试及本任务卡/迁移计划。ea6aa5c9 已经工厂原身份复核签名并实际追加发布；老板执行有限完成时在口令提示前退出，专用只读回执确认旧尝试 SHA 为 3f56b7951725c719f222064638476f8944a3b6095df984a03a4874f774cb09aa，state/pending/恢复回执/新解密槽全部缺失，staging 仍只有原槽。旧尝试和所有旧现场不得删除或改写。

普通 UID 的独立私有 PTY 已复现：后台进程收到 SIGTTOU、监护将其转前台并继续后，标准 getpass 在提示前抛 GetPassWarning；原始前台启动成功。真实 UID499 三种 descriptor 模式合成通过，原 sudo heredoc 与参数启动的实机对照仍待回执，不把合成模型当已证实现场根因。

新增隐藏输入前台门禁须在任何完成尝试写入前执行，后台/无控制终端明确拒绝，保留禁止回显退化的原策略。显式解密前续接须由新签名能力授权、绑定已核实旧尝试原字节 SHA，完整核旧执行包仍获 root 批准及签名能力、同一输入/原恢复包/context/完整备份；要求仍无新解密槽和完成状态，创建独立且仅一次的续接记录。旧尝试通过原 FD 持续固定并逐阶段复验；先前已开始解密、已有续接、任何不符/缺失或后续变化都拒绝。重新执行全部密文认证、全事实/附件/凭据比较后才能写停止状态/恢复回执；不跳过原完整验证、不复用未认证明文、不重跑原 restore、不改现有签名树或清理旧机。新候选仍需完整工厂复核签名及本机实机验收。

随后老板实际执行固定摘要的 readonly-launch-comparison：heredoc 的 UID499 进程组94097、终端前台组94096，getpass 在进入 _raw_input 前报 GetPassWarning；参数方式进程组与前台组均94110，真实中文提示写出、内存假值读取通过。两例TTY属性复原，无真实输入/凭据/钥匙串/业务访问。这已实机复现原启动方式故障，后续管理员调度必须使用保留前台的参数方式；不再把三路 isatty 当完整隐藏输入证明。

本闭环整合1969项：1953通过、16既有平台/开关跳过、0失败错误，385.07秒；2条依赖弃用和2条合成短JWT告警。定向167项及2项真实私有PTY通过，均已含于整合集合，不累加。覆盖真实中断恢复、不同签名执行包续接、旧记录原字节/inode/权限不变、原/新记录晚期变化、错误摘要/字段/签名能力/链接/权限、已解密/已有续接/完成状态拒绝。两个临时Mac钥匙串测试开关开启，15项Windows原生及1项个人LaunchAgent仍在Mac明确跳过。主代理顺序复查，未声称独立代理或真实UID499恢复完成。

本机系统daemon仍不存在，sudo无助手授权；约336GiB空闲。公开字段重建旧尝试的canonical字节SHA与老板实机回执完全一致，无需读取秘密。本轮远端Git查询未完成，GitHub连接器亦传输失败；最后已验证工厂参考仍b9a077bc115cc649cdc2753cab3dfb3d75269d1c，交工厂重新核最新基线，不冒称已刷新。新代码只在候选工作区验证，已发布程序、真实数据/密文/凭据/旧尝试全部保持；完整构建、bundle、NAS回读和原身份签名交接另存独立回执。

## 2026-10-11 管理员 SMB 后台归档：原生协议与身份核验

原恢复候选00982184及其完整包正在等工厂签名，保留/Users/kimiyf/ERP-Migration/candidate不变。本闭环在独立Git工作树/Users/kimiyf/ERP-Migration/nas-smb-candidate、分支codex/mac-nas-smb-20261011进行，主代理唯一写入。老板已明确选择现有NAS管理员账号，不再创建tianming_erp_backup；旧研究文件“等待专用账号创建”是已过时历史，不能继续作为前置条件。密码后续仅本机隐藏输入，不从聊天、个人钥匙串或浏览器取出。

先完成可验证的原生SMB层：允许新增desktop_assistant/mac_smb_contract.py、desktop_assistant/mac_smb_mount.py及对应测试，更新本任务卡/计划；必要ABI定义放在本模块并注明Apple原始来源。固定本机服务身份、明确直接NAS地址/用户/共享/固定本地挂载点及专用远端备份目录，拒绝本机代理/guest/匿名/SMB1/2/未实际保护的会话/本地回落/身份变化。共享和用户名只用当前系统工具可精确核验的规范形式，不能凭广告“支持加密”当正在加密。优先版本/尺寸有界的系统接口与受保护系统工具，不采用原研究用无长度share-attributes指针ABI。只读元数据应绑定实际内核挂载及服务端地址/会话/账号，不能仅解析调用者URL；中文来源无法可靠核验时明确拒绝。

此阶段不接入或放宽现有NFS备份门禁，不宣称SMB备份/安装签名能力；后续必须完成隐藏输入入服务钥匙串、root批准配置、真实UID499/launchd独立挂载、完整加密备份与独立回读、断网拒绝/重连、注销重启/FileVault及工厂现场验收。测试仅合成元数据/隔离任务目录；必要真实探针只用已开放guest只读共享核接口及拒绝行为，不读远端文件、不写NAS业务内容，不读取管理员密码。未知或失败现场保留，不接管/卸载个人代理，不启动ERP、不触碰真实恢复数据或旧机。


## 2026-10-11 SMB 后台挂载连接器候选范围

71e4444c已完成工厂签名、Mac独立验签和实际完整真实恢复；原恢复候选等待签名的旧段落仅是历史。当前主工作树fb0b6f33只增加完成记录，已恢复实例manual_stop=true；系统launchd停止态验收材料已准备并交老板执行。此独立SMB工作树继续保留其原基线与全部未提交元数据草案，不改已发布程序、恢复数据或旧机。

本闭环主代理唯一写入，扩展allowlist为新增desktop_assistant/mac_smb_client.py及对应测试，以及本任务卡/计划和既有SMB元数据模块必要相邻修正。使用Apple原生SMBClient的有明确标量参数的打开/挂载/释放接口；仅内存URL携带认证材料，不放入子进程参数、配置、输出或回执。调用者必须为实际专用UID，固定root保护的本地空挂载点，禁止重用现有会话、个人偏好、口令提示、guest/匿名回退；明确请求会话及共享加密，成功后仍以已有尺寸有界的内核元数据和smbutil复验实际身份/SMB3/加密。失败保留挂载现场，不自动卸载、覆写或退回本地写入。

只交付底层候选与合成回归，不修改build能力或接入定时备份。必要真实接口探针仅现有guest只读共享/普通UID/独立测试挂载，核ABI及guest拒绝，不读取远端文件。后继仍需root批准合同/服务钥匙串隐藏输入/专用UID后台实测/完整备份回读及断连重启；本阶段不能作为这些实机验收。家庭隔离及现有NFS门禁全部保留。

本阶段底层候选已完成：精确合同、1432/144字节有界FSCTL会话身份、statshares实际SMB3加密状态、单次原生标量打开/挂载/释放及固定空挂载点FD核验。Apple接口ABI已在本机普通UID501的独立guest只读共享探针执行成功，open/mount/release均0，读取实际peer/session身份并由合同拒绝guest；未读写远端文件、未用管理员口令、未实测UID499或加密管理员会话。仅自有探针挂载已卸载，所有证据保留。真实数据/旧机/个人NAS挂载均未改。

最终客户端与合同/观察器/既有挂载关联229项全部通过（0.17秒）；原元数据扩大397项属于早期相关集合，不与本次叠加。实际源码顺序复审由主代理完成，无独立代理审计声明。密码只出现在调用内存URL，错误不含秘密；原生调用不设自动重试，失败后的已挂载现场保留。调用者仍必须在受保护worker中核root合同、持原安装/备份锁并限制直连网络；此原语不单独授予备份或安装权限，也不能单独防止协议内DFS重定向。尚未集成消费者/构建能力/凭据入口，禁止当作已可运行的后台备份。

主任务最新实际状态：71e4444c原签名程序已完整恢复，系统监护UID499/后台canary通过；老板夜间授权后的家庭运行在127.0.0.1:8000及两个明确家庭LAN地址可访问，工厂192.168.3.80留待搬入。主工作树4222a984仅更新文档；本独立NAS分支基线00982184保持，后续整合须精确合并，不能覆盖主任务的新事实。实际NAS后台仍未配置、自动备份告警保持，Windows原身份新签名及真实管理员加密会话/完整备份验收仍待完成。

## 2026-10-11 输错移交口令后的认证续接

00982184 已由原工厂身份复核签名，Mac 独立验签及实际追加发布成功，签名包51ac295889a680cb07fa320f532c0f67eb241a7c1011e059fb4eb50562676b95。老板实际看到了隐藏口令提示，返回认证阶段失败，并明确说明输错口令。再次运行原 create-only 包装器已退出；本机查无续接进程，系统daemon仍不存在，公开 registry 包含原三包。旧原始尝试与首次resume均保留，私有解密前现场须在新入口实时核验，不能凭聊天推断后续状态不存在。

主代理唯一写入。本闭环仅修改mac_restore_completion.py、build.py、对应completion/build测试及本卡/计划。新增独立签名能力和显式前次resume原字节SHA，要求同时核原始尝试、前次resume及两个旧执行包的完整批准签名/能力与输入全部绑定，尚无完成状态/新解密槽/认证续接记录。两个旧记录均用原FD持续复验。新入口在任何新尝试或解密工作目录写入前，以实际前台TTY最多三次隐藏输入认证原外部凭据包；每次前后重查现场。输入错误、取消或三次失败不消耗新尝试，不删除/改写旧记录。认证成功且完整绑定检查通过后才独占创建认证续接记录，随后仍执行全部GCM/数据库事实/附件/凭据比较和停止状态回执门禁；任何真正解密后的失败仍保留且禁止覆盖重试。

旧初次完成和首次resume行为保持，不修改已签名程序树、不代填口令、不导出或重封装凭据、不重跑restore。需新工厂复核签名后才能执行真实认证续接；后台NAS、注销重启、工厂停写同步和人工业务验收仍独立待办。NAS SMB 未完成候选保留在独立工作树，不混入此包。任何阶段不清空旧机。

本闭环最终整合2010项：1994通过、16既有平台/开关跳过、0失败错误（420.83秒），两个临时Mac钥匙串开关已开启；15项Windows原生及1项个人LaunchAgent仍明确跳过。中间定向215项通过不是最终快照，最终整合已覆盖后加的输入后前台检查和不同签名执行包认证续接。另有3种普通UID501真实私有PTY验证输错再正确/三次输错/EOF取消：调用真实getpass、输入时ECHO关闭、终端设置复原、合成输入不回显；不冒称UID499或真实恢复验收。首次PTY证据解析因EOF提示与JSON同一行失败，修正解析后通过，旧日志保留。主代理顺序复查，无独立代理复审声明。

本轮Mac远端只读查询成功，工厂参考仍b9a077bc115cc649cdc2753cab3dfb3d75269d1c；工厂接手时须再次核最新正式基线。新候选只含本闭环五文件，结构/reader/原安全门禁不变。完整构建、候选提交推送、bundle与NAS回读及Windows原身份签名要求另存独立回执。真实认证续接、系统后台/NAS/注销重启/最终工厂停写同步尚未完成，任何阶段不清空旧机。


## 2026-10-11 专用身份完整恢复实际成功，转系统后台验收

71e4444c已获原工厂身份签名（Windows375通过/11跳过、15原生全阶段通过），Mac原公钥独立验签和29346成员核验通过。实际追加发布包22bd55b3e9b4ec0842040295c305ec15eee384d0fc0096cfa2854e2196830be5，四包注册SHA562ed097e9cf4a84b3ea1c3bc5a4d8fce899940c0eee0cdb7e8816fab101450f。任务外认证派发77项通过，NAS67项回读一致（local_mount_view，原件保留）。

老板本机执行已返回完整成功：核两份旧记录，口令认证通过后创建新认证尝试；完整备份GCM认证、全数据库事实/附件/配置/凭据核对通过，停止状态和恢复回执已发布。备份SHA14d81de312e1d4b58e9bbdc849a6fd2d21f6e13212728594a9d2977f50097185，数据时点2026-10-10T20:12:10.643701+08:00；原恢复目标acb2f921b953b5478afe49f32dbf8465367f2073c55785c44bf4a0aa40daaf6e，v0.22.625/ep1010md，schema_migrated=false；backup/deepseek/mailbox恢复成功，邮箱版本保持。manual_stop=true、started/factory_cutover均false。原样结构化实机证据保存于evidence/mac-restored-stopped-71e4444c/actual-restore-receipt.json，助手未冒称已直接读取私有回执。恢复闭环已完成，不再重跑恢复/认证/链接修复命令，全部旧记录和现场保留。

下一闭环主代理唯一负责，仅准备任务外系统监护安装和只读验收材料，复用已签名root程序及18源码管理员包，不修改ERP源码/签名树。严格实读恢复回执和停止state，固定计划/注册，先由UID499签名worker检查停止和canary，再安装system launchd；核真实PID/父进程1/UID499/固定解释器、后台canary和家庭停止状态，前后state及恢复回执SHA必须不变。监护仅写控制状态，manual_stop继续阻止业务启动与定时备份。注册不是业务健康/注销重启证明。

仍待系统后台实际验收、既有NAS管理员的直连SMB后台完整备份/回读/断连、注销重启/FileVault、历史外部原件对应、实体中文打印扫码、工厂最终停写同步/唯一写端/人工业务验收。家庭端不连接或覆盖工厂库、不发信/收单/外部AI，不重建退役层或删除门禁；任何阶段不清空旧机。


## 2026-10-11 system launchd 日志打开权限实机修复范围

系统注册实际成功，但验收等待超时；只读launchctl显示spawn failed、EX_CONFIG(78)，stdout/stderr均零字节，不能当后台运行通过。本机普通UID两个独立/usr/bin/true launchd探针复现：stdout仅可写时78，改为可读写时0；均已注销自有合成任务并保留文件，没有ERP访问。stdout原root:499/0620不满足本机launchd实际打开方式，stderr已为0660。

本闭环主代理唯一写入，源码allowlist为desktop_assistant/mac_system_service.py、tests/desktop_assistant/test_mac_system_service.py及本任务卡/计划。统一新安装的两路日志为root:服务组/0660，root父目录与独占创建保持；定向回归必须覆盖实际打开方式。既有已签名程序不改字节。任务外管理员修复只绑定已注册71e4444c、原计划/控制/plist、实际恢复回执和原state摘要，在专用worker停止态/canary核验通过后对固定stdout原FD执行0620→0660；不重装、不开启ERP、不改程序树/数据库/凭据/旧记录，不强制重启或清理。等待原launchd自然重试并重新核专用UID、parent1、固定解释器、后台canary及state/恢复回执不变。修复与验收完成记录另建，失败保留现场。

顺序复查同时发现原任务外观察器将launchctl嵌套coalition的state=active也算作服务状态，后续修复观察器应按顶层字段解析并增加实际结构回归；原已执行材料保留。NAS SMB工作树暂停实现，不混入此实机修复。完整恢复仍有效、服务后台验收未完成、工厂未接管、旧机不清空。

新安装器两路日志模式已统一为0660，最终相关155项全部通过（2.48秒）。早期新断言缺stat导入、后续夹具旧四字段事件断言失败均保留，修正后最终通过；本机两组真实launchd探针独立证明写-only/读写的78/0差异，不把合成root身份当专用UID实机验收。任务外修复仅变现有stdout模式，已发布签名代码不改；真实管理员修复、后台canary/停止态验收仍待执行。候选源码修复尚未重新构建或工厂签名，后续并入下一完整候选；不以此更新受保护运行树。此前fb0b6f33文档推送因GitHub缺少可用认证而失败，本地提交及NAS留存，不声称远端已推送。

## 2026-10-11 家庭邮箱预览只读诊断与现有测试修复范围

主代理唯一写入。本闭环只修改 tests/email_queue_loading.cjs、本任务卡和迁移计划；修正现有测试对已移除 pdfSaveStorageKey 的引用，并接入当前恢复未确认保存记录的前端方法，覆盖真实方法组合中的成功、网络失败、错误响应、空队列和权限拒绝。不改已签名程序、应用源码、业务数据、会话或浏览器状态；不提取浏览器 Cookie，不生成真实系统登录令牌，不执行收单/发信/外部 AI。任务外仅做健康与静态响应观察及合成传输测试。后台 preview 的 HTTP 200 与 Safari Network Error 尚不足以确定根因，合成测试通过不能代替实际浏览器验收。

定向后端队列/终态/资源测试16项通过（9.07秒，15条既有依赖与合成短JWT等警告）。原前端测试实际因不存在的pdfSaveStorageKey在提取时失败，尚未运行页面；修正为当前reloadPdfSaveRecovery后通过，并覆盖待确认原保存请求保留、包含条目的草稿水合、明确网络失败、三种错误响应、空队列及两个权限拒绝。仅测试文件修复，不改变实际页面行为。

23:43本机对回环、Wi-Fi、网线各做健康/前端/完整工作区/匿名身份共12项只读请求：200/200/200/401均符合预期；三路完整工作区gzip响应均644396字节，解压3323520字节且SHA一致，无截断或解码错误。未使用登录会话、业务API、数据库或钥匙串。此结果只证明这些HTTP响应正常，不证明邮件preview响应或Safari已恢复；实际Network Error继续待确认。证据位于evidence/mac-email-preview-diagnose-20261011，失败日志保留；NAS回读仅local_mount_view，工厂接管及旧机保留边界不变。

## 2026-10-11 系统验收通过与夜间家庭运行授权

老板回读日志修复完成记录：系统launchd父进程1、PID1326、UID/GID499及后台canary通过，home/manual_stop=true，ERP未启动，state和恢复回执摘要不变。助手随后取得老板本次明确的Mac管理员执行授权，直接只读核验原root回执SHA为2a619c04b383940eebf259ae12176f64e2e73497503818c3b04ea83b9a1b37d5，原签名管理status同样通过。已完成的安装/恢复/日志修复不重跑。注销重启、后台NAS、工厂接管仍未验收。

老板要求夜间自主推进Mac运行及局域网访问，并确认192.168.3.80:8000是工厂搬入后的地址；当前家庭网线192.168.163.157、Wi-Fi192.168.1.243，不修改当前网关或IP，不把本次授权当工厂停写切换。密码不进入脚本、配置、回执或Git。主代理唯一写入；本闭环允许任务外家庭准备/验收、固定目标LAN转接器和测试，以及本任务卡/计划，不修改已签名ERP程序。

先保持专用实例停止并持原维护锁，完整保留shared、state、preferences、control及恢复回执的私有检查点，逐文件哈希核副本。只调整已有环境的家庭网络字段，使用原生产lan_http模式、明确来源、原账号和权限。ERP继续通过签名管理入口以UID499启动，内核出站禁令和Python粘性家庭门禁保持。若提供LAN转接，仅允许固定本机私网IP:8000到127.0.0.1:8000，不提供任意目标代理、不改变ERP监听或出站门禁；转接不读业务数据或凭据。原环境和SHA保留，最终工厂同步和全事实核验仍须重新执行。

NAS后台未就绪时如实保留备份告警，不关闭或伪造门禁。仅做健康、静态资源、只读接口核对；其他电脑、业务、打印扫码及注销重启验收按实际证据记录。旧机和失败现场始终保留。

本次实际家庭运行已完成：root私有检查点2991文件、3437528916字节逐文件一致（含shared/control/state/preferences及另存原恢复回执）；仅环境网络字段变更，原环境SHA34bb233226342a5b8ee2572726cc6aa89a123c95d5fe6df7736412c16e543152，家庭配置SHA45a7ddcb1c691a39ef28a56bf2e1831499fa30cdcb793af84a153d3be2c7830d。随后通过原签名22bd管理入口启动acb2原恢复目标，UID499实际ERP进程PID2857，系统监护PID1326状态running/home、manual_stop=false，未正式激活。配置准备没有改业务库或凭据；应用启动及用户原浏览器活动与此准备阶段分开记录。

独立家庭LAN转接cn.tianming.erp.home-access以nobody运行（PID3700、parent1），仅192.168.1.243:8000和192.168.163.157:8000转127.0.0.1:8000；内核策略只允localhost:8000出站，实测其他端口EPERM，原ERP进程仍全部禁止主动出站。没有修改原程序签名/认证/权限或开启工厂模式。原防火墙为关闭、系统睡眠为0，本次保持。安装过程中命令终端长行损坏导致语法失败，尚未建日志或注册；准确核现有两份root文件SHA后以短命令和独立续接脚本完成，旧现场保留，无覆盖重试。

56项家庭配置/原隔离/生产传输检查通过，另1项真实独立loopback转接大消息与半关闭测试通过（15项工具测试已含于56，不累加）。本机对三个地址的health、前端、匿名身份共9项实测符合预期；健康和前端200、匿名401，非信任Host400；Wi-Fi来源CORS预检200、精确来源，无通配。两项入口JS/CSS均200并记录SHA。另一电脑尚未参与实测。Safari已有会话显示订单列表，但邮箱导入面板有Network Error，后台对应preview记录200且无异常栈，原因未确认；助手没有点击或代登录，不把页面展示当完整业务验收。自动备份真实状态failed（NAS尚未配置），告警保留。桌面新增两个家庭入口快捷文件，未改其他浏览器会话。证据evidence/mac-home-run-20261011；完整私有检查点留在/Library/TianmingERP-HomeRun-20261011。NAS、注销重启、邮箱预览异常及工厂验收继续待办。


## 2026-10-11 SMB 备份路径与独立回读接入范围

主代理唯一写入，独立NAS工作树先合并主线b3557549，保留两边文档和全部恢复/日志修复。允许新增desktop_assistant/mac_smb_storage.py及对应测试，修改mac_backup_storage.py、Manager备份包装入口、SMB契约说明及必要相关测试、任务卡/计划。仅将已批准且已挂载的SMB位置接入现有完整备份及独立回读，不在此层读密码、自动挂载或创建远端目录。固定root合同、真实专用身份、直连NAS/SMB3加密/非guest、逐级不跟随链接及挂载/目录FD连续核验；写入期间保持目录FD，回读重新打开并逐字节核SHA和最终身份。挂载/目录/合同变化均拒绝成功，保留文件和原记录；NFS与Windows既有行为及原完整恢复格式保持。

本阶段不宣称后台SMB安装或签名能力已完成。仍需后续批准安装、独立挂载worker、专用钥匙串NAS口令、原工厂签名、真实UID499/launchd完整加密备份及断连/重启验收。不得放宽原ERP出站门禁、连接工厂库、发信收单或清空旧机。

本阶段已实现受root合同约束的SMB位置检查、固定路径逐级FD持有、真实专用身份/实际加密会话复验与完整备份消费者接入。新SMB回读证明单独标型并绑定三个目录身份、文件身份/大小/SHA及合同/context；独立回读仍用原隔离子进程协议，成功状态只在完整认证与本地回执后写入，历史状态查询不访问NAS。没有自动挂载、密码读取、远端目录创建或本地回落。

最终相关整合800项：799通过、1项既有平台跳过，159.16秒；包含32项新SMB位置/回读/完整合成加密备份/停服前拒绝测试。早期101与24项、457通过/1跳过集合均重叠不累加。首轮扩大测试12失败/445通过/1跳过，原可移植恢复测试适配器漏接新目录保护入口，导致读取公开服务身份后被UID门禁拒绝；只补其任务私有路径的显式合成适配，生产身份门禁不变，旧失败日志保留。测试使用真实临时FD/文件/密码学和明确模拟的root/UID499/SMB元数据，不声称真实NAS管理员加密会话或跨进程正例已验收。源代码差异检查通过，主代理顺序复查，无独立代理复审声明。

已发布树、真实业务库/凭据和旧机未改，Mac家庭健康接口仍200。新SMB候选尚未构建、签名或部署；仍需受保护安装/隐藏输入/独立挂载worker、工厂原签名、真实完整备份及断连和注销重启验收。完整证据位于evidence/mac-smb-storage-work-20261011，NAS归档仅local_mount_view，保留全部本机原件。


## 2026-10-12 SMB 后台受限连接入口范围

主代理唯一写入，继续独立NAS工作树290688df。允许新增mac_smb_runtime_contract.py、mac_smb_runtime.py、service_smb_entry.py及对应测试，修改mac_backup_storage/nas_probe/nightly/mac_service_manage的连接前置调用，以及必要合成适配、任务卡/计划。运行记录必须root保护并绑定原公钥批准的不可变签名程序、context、固定SMB合同和专用NAS钥匙串条目。仅固定地址445可出站的sandbox-exec子进程可执行原生连接；ERP原出站隔离保持。

父进程/worker分别验证批准记录、执行路径/解释器、真实服务身份及完整程序能力；记录在启动/回执间改变则拒绝。已有正确挂载不读取口令；冷启动只在固定空本地挂载点通过后读取唯一专用条目并单次挂载，失败不卸载、修权限、清理或自动重试，不创建远端目录/业务文件。新的公开历史安装记录结构不代表它已经存在；后续root安装器、隐藏输入和真实NAS验证完成前不声明构建能力或安装成功。NAS登录口令与原备份加密凭据分开，不纳入Windows运营凭据移交，不改变已有keychain用途或读取其他秘密。

本轮实机否定了原网络策略假设：macOS 27.0.1 的sandbox-exec解析器对remote ip/tcp中的192.168.1.245:445均返回65（host must be * or localhost）；localhost及通配形式仅编译探针返回0。探针只运行/usr/bin/true，没有网络连接、NAS认证或文件访问。原TCP测试在规则编译时退出，未执行连接，空network.json保留；不得把合成协议测试写成真实精确NAS网络边界通过，也不改用通配地址作为回退。

运行协议草案保留并加入实际策略预检：批准资料及空挂载点核验后，先让系统编译精确规则，失败就在任何凭据worker启动前明确拒绝。build仍不声明archive_smb_v1，无root安装、NAS口令写入、新签名或部署。当前冷连接在本机明确不可运行；后续必须先完成另一种可实测的精确地址隔离方案，再做安装和完整备份验收。已有ERP与家庭LAN入口保持，三个健康地址本机只读检查均200/ok；不冒称另一电脑实测。证据目录evidence/mac-smb-runtime-work-20261012。

最终相关整合855项：854通过、1项既有平台跳过、0失败错误（156.39秒），含55项新运行协议/真实临时FD锁/策略拒绝测试；233项早期关联、54项扩展和55项策略门禁集合重叠不累加。协议正例显式模拟策略被接受，不冒称真实UID499、NAS认证或网络边界通过。本机UID501四种解析器探针证实精确NAS规则被拒绝；build能力未开启，旧签名运行树和个人挂载未改。主代理顺序复查，未声明独立代理审计。全部候选及本机失败证据保留，NAS回读仅local_mount_view。


## 2026-10-12 原生SMB网络边界实测范围

主代理唯一负责，仅任务外证据探针和本卡/计划；先验证原生SMBClient连接是否实际受进程sandbox网络规则限制，避免继续建立在普通socket测试假设上。只用此前已开放的guest只读共享、普通UID501、单次session-only打开/释放，无管理员口令、挂载、远端文件访问或业务调用。对照无放行、仅loopback与445端口规则的效果只存在短命探针进程，不改变ERP、NAS、系统防火墙/PF或个人挂载，不将通配规则部署到实际凭据worker。明确记录无法成立的安全假设，再决定后续可行方案。

本机证据闭合：普通UID501的三组session-only guest探针，无放行/仅localhost均返回3221226038且无句柄；仅445端口探针打开/释放均0。无挂载、密码或远端文件访问。netstat见kernel_task:0；普通身份pcblist未给出匹配，不能据此断言没有连接。老板已授权的管理员只读sysctl观察确认原生会话socket为UID0/PID0，旁边的用户预连接为UID501/探针PID。故仅以服务UID499编写PF规则不足以证明限制原生SMB全部通路；本轮没有修改PF、网关或系统网络规则。

替代路线已有可执行证据：父进程仅把一个预先连接的FD传给sandbox-exec全出站禁止子进程，合成本机通道完整传输1441792字节并核SHA，而新建连接得到EPERM。随后以相同结构连接真实NAS固定192.168.1.245:445，仅做SMB协商（无Session Setup/账号/共享/文件），smbprotocol 1.17.0协商SMB3.1.1、AES-128-GCM、SHA-512预认证，签名要求保持；尝试另连同一NAS仍EPERM。此为普通UID501通道及协商通过，不能冒称实际加密登录、UID499、备份或回读成功。协议依赖两个纯Python wheel只在任务证据目录按PyPI摘要核验并zip导入，未改变ERP依赖或发布树。

后续实现采用独立SMB流归档合同和不可再连接的FD适配，不伪造原生挂载、文件系统inode或旧回读证明：已有完整备份先在受保护任务目录生成原格式完整密文，上传新文件后以第二独立已连接通道全量回读并认证，才能发布NAS成功状态。禁止DFS/符号链接自动解析、会话池回退或网络重连；远端失败保留现场，NAS登录与备份加密凭据继续分离。仍须受保护安装、既有NAS管理员隐藏输入、Windows原签名、专用UID后台及注销重启实测。旧原生候选和全部证据保留，不启用archive_smb_v1或部署未完成后端。证据evidence/mac-smb-egress-discovery-20261012；本阶段仅诊断/方案文档变化，无需重复整套应用回归。


## 2026-10-12 SMB流归档合同与单通道传输实现范围

主代理唯一写入，允许新增mac_smb_stream_contract.py、mac_smb_stream_transport.py及对应测试、独立依赖清单和本卡/计划。采用独立合同，不复用原生挂载证明；固定私网NAS/445/专用身份、Unicode共享名及唯一目录，不接受口令字段。传输只能接管一个已连接FD，复验对端，不创建socket连接、不重新连接；SMB帧长度/超时/部分收发/EOF/关闭并发明确处理。适配固定版本smbprotocol的传输接口，并保留操作系统全出站拒绝。仅本机合成通道和真实NAS无认证协商验证，不执行NAS登录、文件上传或发布能力。后续加密会话、完整备份上传/独立回读、安装签名及实机验收继续完成。

本阶段新增独立tianming.mac-smb-stream-archive.v1合同，保留中文共享名，固定私网IPv4/445、400至499服务UID、context、NAS GUID和唯一目录，排除口令字段、guest、路径/域歧义与别名。新增不创建连接的Direct TCP帧适配器：接管并复核单一已连接FD，单次claim，16MiB以内帧和有限总超时，部分收发/EOF/中断/关闭并发处理，传输错误后关闭而不重连。仅固定smbprotocol 1.17.0/pyspnego 0.12.4版本可绑定，作用域内禁止第二传输工厂，退出复原，不改任何已有运行依赖；单独依赖清单不代表安装能力。

最终相关347项全部通过（1.01秒），含67项新增合同/真实loopback socket/传输工厂测试。首轮13失败/42通过，原因是本机不支持SO_ACCEPTCONN；改用AF_INET/SOCK_STREAM/getpeername及SO_TYPE连续复核，新增真实监听socket拒绝用例，没有放宽“必须已连接”要求。失败日志保留。组件候选在实际全出站拒绝子进程中完成一次NAS未认证协商：SMB3.1.1/AES-128-GCM能力/要求签名，新连接EPERM；没有密码、登录、共享读写或实际备份。普通UID501验证不替代UID499/launchd。

当前只完成流合同与传输组件。加密登录、远端目录/文件级上传及第二连接完整回读、现有Manager接入、受保护凭据入口、build能力、原工厂签名、实机备份及注销重启均未完成。原生SMB候选与历史证据继续保留，不将其不支持的能力开启。老板已被异步询问既有NAS管理员登录名和共享名，未索取聊天密码，不把Mac密码当NAS密码。证据evidence/mac-smb-stream-transport-20261012；主代理顺序复查，NAS回读仅local_mount_view，无独立代理审计声明。

跨平台顺序复查追加允许修正test_mac_smb_runtime.py的真实POSIX锁夹具：该前序候选漏标Windows不可执行条件。流传输测试同样只把真实POSIX socket-FD用例标为Windows跳过，合同/协议合成分支仍跨平台运行；不以Mac测试代替Windows原生验收，不改生产门禁。

上述平台夹具修正后的最终相关集合仍347项全部通过（1.00秒，final-v2.log/xml）；Mac真实FD用例均执行，没有新增Mac跳过。此前final.log/xml保留，不把两次重叠集合相加。Windows实际专项仍待原工厂环境，当前不宣称其通过。

## 2026-10-12 SMB流加密会话与共享连接范围

主代理唯一写入，延续47d82403。允许新增mac_smb_stream_session.py及对应测试，给已有单通道传输增加可选帧验证器，并更新本卡/计划。固定SMB3.1.1/AES-128-GCM、原合同端点/GUID，协商通过后才调用一次NAS凭据提供者；显式NTLM、拒绝空凭据/guest/null，验证最终认证响应签名，认证后双向只接受同会话加密帧。共享连接禁止DFS，所有响应禁止符号链接自动解析。异常关闭通道、清除可达口令引用，输出通用错误；不重连、不读真实密码、不访问真实共享或上传文件。此会话组件尚不是root授权入口；后续安装/UID499/完整备份与第二通道回读/签名/实机验收均保持待办，不声明构建能力或NAS成功状态。

会话组件已实现，最终相关418项全部通过（1.06秒），含71项新增用例；初始128项与最终集合重叠不累加。合成协议状态测试覆盖协商不符时不取秘密、只取一次、空密码拒绝、guest/null等不可用密钥拒绝、最终认证签名、DFS标志/能力与非磁盘共享拒绝、失败/中断清理、超时和禁用链接解析。实际固定版本库生成AES-GCM消息，在真实loopback FD双向传输并解密一致，修改密文或标签均被密码学认证拒绝；会话前后的明文/加密帧边界另测。修复原传输适配器只接受bytes、不能发送库返回的SMB2TransformHeader对象的问题。协议日志在独立worker作用域内抑制，避免输出认证token；未声称Python内存可彻底擦除。

最终候选源码SHA绑定的NAS协商探针通过：普通UID501、deny network-outbound、唯一预连接FD，SMB3.1.1/AES-128-GCM及签名要求一致，新建连接EPERM。没有Session Setup、真实密码、Tree Connect或文件读写，因此真实NAS登录和备份仍待验收。git远端正式基线查询12秒超时，本次未取得新正式SHA或推送成功证据；不改已有运行程序、工厂库或原公钥签名。主代理顺序复查，证据evidence/mac-smb-stream-session-20261012，NAS仅local_mount_view回读。下一步为固定目录/新文件独占上传与独立全量回读，再接Manager及受保护签名worker。

## 2026-10-12 SMB流文件上传与独立回读范围

主代理唯一写入，延续1ae06a2b；允许新增mac_smb_stream_files.py及对应测试，更新本卡/计划。只在已验证加密会话内，固定共享根/BoxERP/mac-server-backups，逐级打开既有非重解析目录并持有禁止删除共享的句柄，不创建目录。上传私有本地完整密文FD到独占新建的带attempt标识.partial文件，分块写、flush、摘要/长度核对、不覆盖改名，失败保留全部现场。第二独立连接重新打开最终文件，全量回读到新的私有FD，核目录/文件身份、长度与SHA；返回传输证明不能代替原备份完整认证或Manager成功状态。本轮仅隔离合成协议/实际密码学与本地FD测试，无真实NAS登录/文件操作，不改变运行程序或工厂数据。

文件组件已实现，相关475项全部通过（1.51秒），含57项新增；初始42项与最终集合重叠不累加。使用真实smbprotocol 1.17.0请求/响应结构及Open实现，连接对端为显式内存模拟，不是实际NAS。真实本地FD、部分读写、原格式AES-GCM密文往返和原解密器认证执行通过；明文为合成测试字节，不冒称已验证完整ERP数据库/附件备份。

固定目录逐级持有无DELETE共享的既有句柄；FILE_OPEN_REPARSE_POINT、不跟随链接、不创建目录，持续核对SMB文件索引与创建时间（不伪造POSIX inode）。本地源/回读FD须当前身份所有、0600、单硬链接且按只读/读写模式打开；源前后身份/长度/时间和SHA一致，回读另对实际写入的本地文件逐字节核SHA。一次连接只允许一个归档实例；回读拒绝上传连接GUID。远端仅FILE_CREATE临时.partial，短写继续剩余数据，flush后按ReplaceIfExists=false改名；已有备份、失败部分文件和未知结果均保留。传输证明明确backup_authenticated=false，必须退出目录上下文成功后交上层继续原完整备份认证，不能直接发布成功。

补充错误用例覆盖同名文件、目录缺失/重解析/删除中/替换、远端创建/写/flush/改名/关闭确认丢失、错误写入计数、本地源变化、远端文件替换/硬链接/长度/摘要变化、同连接回读、非空/非私有回读目标和关闭失败。生产组件、测试与本卡/计划共四文件；已签名运行树、真实凭据和工厂数据库未改。本轮没有NAS网络探针或真实共享读写。下一步接完整备份构建器、独立签名worker和成功状态协议，再做真实NAS/UID499/launchd/注销重启验收。

## 2026-10-12 完整备份构建复用与流回读认证范围

主代理唯一写入，延续1ab55cfc；允许提取Manager现有完整备份输入/构建私有方法，新增mac_smb_stream_backup.py和对应测试，更新本卡/计划。既有NAS挂载/Windows路径仍在所选NAS创建所有临时包，禁止自动本地回落。仅显式SMB流入口可在固定instance/staging下新建0700任务目录，复用原数据库/附件/签名/reader/provenance/全部凭据核验及原格式加密；独立回读后再次GCM认证并与原完整明文包SHA一致，原数据库/状态及私有目录复验。此层不取NAS口令、不建立网络或发布成功状态，传输回调后续由受保护签名worker提供；失败保留现场，原ERP必须已停止且原维护锁由调用者持有。完整合成备份、恢复、凭据和旧NAS行为必须回归，不修改实际运行树、真实凭据、工厂库或旧机。

整合回归暴露一项旧迁移预演测试环境依赖：无法确认宿主ERP/Python进程退出时，原清理门禁正确保留release/shared，但测试无条件期待删除。单独重跑复现；新建合成预演诊断报告status=passed、两目录均以“无法确认 ERP/测试进程已退出”保留，证据及合成现场保存在本轮evidence，不绕过清理或删除。追加允许修改tests/desktop_assistant/test_migration.py：回收正例显式模拟空闲进程清单，新增AccessDenied保留正例；生产cleanup/migration代码和安全门禁不变。

本轮已提取原完整备份输入和构建私有方法，旧NAS路线继续在所选NAS进行全部暂存与最终发布，无本地回落。新显式流候选入口持有现有私有目录Lease，在instance/staging新建0700任务目录，按0600创建所有凭据包、完整原包和加密包；只调用一次传输适配，双连接证明核身份/长度/SHA后再次认证实际下载密文，明文SHA与原完整备份包一致，并复验源文件、目录、数据库事实和state。返回candidate-verification且state_published=false，不更新last_backup/清除告警/写正式回执。所有工作材料继续保留，待签名worker最终发布及引用生命周期处理。

新增24项测试通过，其中完整合成ERP经真实协议结构/内存NAS、实际FD和原密码学备份往返后，由原restore恢复到新实例；数据库表计数、附件、邮箱版本/开关/读取位置以及backup/mailbox/openai凭据保持，源数据库SHA与state不变。没有真实NAS认证、文件访问或凭据读取。

验证分开记录：首轮88通过/1跳过（9.39秒）；整个助手模块2441项为2424通过/16跳过/1失败（417.47秒），唯一失败即上述原测试的宿主进程依赖。单项重跑复现、合成诊断保留，修正测试后迁移/清理/保留及新流备份复查57通过/1跳过（5.13秒）。中间一次复查误指定不存在的test_cleanup.py，0测试、退出4，日志也保留；实际修正后运行test_cleanup_process_identity.py与test_retention.py，不将0测试记作通过。仅测试夹具修正后未重复整套整合，不冒称有一次全绿的2442项执行。所有集合重叠不累加，3条既有依赖/合成JWT警告保留。

后续接入发现现有_update_locked和中断恢复需要backup可本地读取并再次sha，不能把远端SMB地址冒充Path；签名后台入口须同时绑定NAS完整回读回执与更新所需私有备份副本，保留迁移证据与清理引用门禁。当前尚无CLI/构建能力/安装或后台路由，原签名运行树、工厂数据库与旧机不变。证据evidence/mac-smb-stream-backup-20261012，主代理顺序复查，NAS仅local_mount_view；真实后台备份及最终工厂验收继续待办。

## 2026-10-12 SMB流受保护派发与双进程传输范围

主代理唯一写入，延续926bc3e5。允许新增mac_smb_stream_runtime_contract.py、mac_smb_stream_runtime.py、service_smb_stream_entry.py及对应测试，更新本卡/计划。独立root控制记录绑定原公钥批准包、context、流合同及专属NAS凭据引用；实际专用UID与固定程序入口复验。父进程只向合同私网IPv4/445建立一次连接，将该socket与固定staging任务内的单个私有密文FD交给全出站拒绝子进程。上传、独立下载各用新进程/连接，禁重连、DFS、覆盖和静默回落；凭据仅子进程协商通过后从本服务钥匙串按批准引用读取。两次传输证明均完整才返回原完整备份GCM认证层，本层不发布成功状态。

现有system supervisor本身没有ERP子进程的全出站沙箱，home_process只包裹业务入口；此设计不放宽业务隔离。仍不启用build能力、不安装、不读取真实NAS口令、不登录共享或改实际服务。测试使用合成root记录/凭据、私有临时文件及本机socket；真实UID499/launchd和NAS凭据配置/原签名/注销重启另行验收。父进程维护锁和停止控制由后续Manager接入负责，此传输入口不单独证明完整备份成功。

已完成独立root流控制合同、固定入口和父连接/子传输派发。实际身份、原公钥批准包、context与root记录前后复验；F_GETPATH只接受固定staging槽内的源文件，子进程另以父目录Lease核继承FD同一身份、权限及长度。父进程不读取NAS口令，只连接批准IPv4/445，并只传两个FD；子进程以固定全出站拒绝规则运行，先实际检查新建连接EPERM，再在协议协商核验后按专属引用取口令。上传与独立回读使用不同子进程/连接，响应绑定挑战、包、身份、合同与文件证明；中断不重启子进程、不发布成功。最终完整GCM认证继续由上一层执行，本层返回state_published=false。

最终关联352项全部通过（4.79秒），含78项新增检查；本机普通UID501真实子进程证明继承socket可往返、新建连接被系统拒绝，不能替代UID499/launchd。实际协议结构与内存NAS完成两次worker文件往返，认证/root身份是明确合成夹具，不冒称真实NAS登录。初始70项通过；扩大后先350通过/1失败（共享名夹具不一致），修正后351通过/1失败（模拟sys.flags缺少import所需字段），单项诊断与失败日志保留；两处仅修测试夹具，随后最终352全绿。各集合重叠不相加。

本机只读健康复核127.0.0.1、192.168.163.157、192.168.1.243均HTTP200/ok=true，家庭地址未改；没有第二台电脑的访问证明。工厂192.168.3.80按老板确认留待搬入，未设置。远端只读基线查询本次成功，factory-current-baseline为b9a077bc115cc649cdc2753cab3dfb3d75269d1c，现场仍须重查。源码/测试证据evidence/mac-smb-stream-runtime-20261012；主代理顺序复查，没有独立代理审计声明。仍待受保护安装与NAS隐藏输入、成功回执/Manager和更新副本生命周期接入、build能力/原工厂签名、真实完整NAS备份、断连/注销重启和工厂最终验收；全部原数据及旧机保留。

## 2026-10-12 SMB流完整认证成功回执与本地恢复证据副本

主代理唯一写入，延续38c202ee。允许新增mac_smb_stream_receipt.py及对应测试，修改mac_backup_storage.py的历史成功证据路由、mac_smb_stream_runtime_contract.py的必需模块清单，并更新本卡/计划。独立候选入口在原维护锁/ERP停止的调用契约下执行真实完整构建及流回读认证，再将已认证下载密文独占复制到instance/backups，核完整SHA/长度及私有文件身份；备份更新器后续可读取此副本，不能用远端URI冒充本地Path。创建唯一类型的本地回执，绑定原批准包/context/root合同、两个传输证明、完整包SHA、数据库事实、验证时间和本地副本身份；回执实际回读一致后才以原字节并发检查替换state，清除本次备份告警。失败保留副本/回执/暂存，绝不提前标成功。

历史状态检查只读root批准元数据、小型回执和本地文件身份，不访问NAS、读取口令或全量哈希；当前state必须同时绑定回执摘要和本地副本。原NFS/原生SMB/Windows回执及备份路线保持。本闭环不启用构建能力、安装或夜间路由；整体Manager/更新副本保留回收、NAS凭据安装、原签名及真实后台验收继续推进。所有测试为合成ERP/凭据/内存NAS，不改真实运行树或旧机。

独立内部候选入口现已复用完整构建/双连接回读/GCM认证，在固定backups目录独占保存已认证下载密文，完整SHA/长度及FD身份复验后创建专用流回执，再用已有状态原字节并发检查替换state。last_backup仍是可恢复本地密文Path，last_backup_stream绑定对应回执名称和原字节SHA；它不把本地副本当NAS证明，回执另包含原合同、批准包、两次不同连接的远端文件身份/摘要和完整原包SHA。复制、回执或最终state失败，以及认证后密文/回执/状态并发变化均保留且拒绝记成功。历史状态检查只取小型本地记录和副本身份；没有取口令、NAS访问或整包hash，过期root批准/替换副本/改写回执均拒绝认可。老路线后续产生不同last_backup时，旧流索引不会把它错误路由成流备份。

首轮33项通过；最终相关339项为338通过、1既有Windows 8.3 API平台跳过、0失败错误（21.45秒），包含47项新增检查，集合重叠不累加。完整合成ERP归档后的私有证据副本用原restore恢复到另一独立实例，数据库事实/附件/backup-mailbox-openai凭据保持，未启动服务。相邻旧NAS、管理、夜间、更新链与保留回归通过，不将旧更新链测试冒称为尚未接入的新流更新路线。源代码差异检查及只读顺序复查完成，证据evidence/mac-smb-stream-receipts-20261012。

下一步还需显式流目标选择、停止前NAS预检、Manager/夜间/更新调用路由和副本保留生命周期；当前成功入口没有CLI或自动消费者，真实运行未改变。受保护NAS隐藏凭据安装、构建能力/原工厂签名、UID499后台全量真实NAS备份/断连/注销重启及最终工厂验收仍待完成。主代理单独执行，无独立代理审计声明；未读取真实运营/NAS凭据，未连接工厂库、未清空旧机或清理原失败现场。

## 2026-10-12 SMB流停止前读写预检范围

主代理唯一写入，延续5407e7b7。允许新增mac_smb_stream_probe.py及对应测试，修改mac_smb_stream_files.py的私有创建句柄读权限参数、mac_smb_stream_runtime.py的专用probe动作、其批准必需文件清单及对应测试/内存NAS夹具，并更新本卡/计划。先完成独立预检原语再接现有调用点，不能用连通端口代替NAS读写门禁。固定目录内独占新建随机名字、无运营内容的16KiB测试文件，核写入/读回、同目录不覆盖改名、文件身份与目录身份；只在这些核验通过后通过原独占句柄标记删除并确认两个测试名字均不存在。未知失败不按名称删除、不重试，不操作任何已有备份或业务文件。

按原始Microsoft MS-FSCC FileDispositionInformation与FileFsFullSizeInformation，以及本地已固定1.17.0协议源码实现；查询调用者和实际可用空间并取较小值，不把配额总量误作整卷容量。此动作只传网络FD，无数据库或本地备份FD；保留原全出站拒绝/加密登录/原签名/context门禁，成功仅是预检，不产生备份成功。父进程对自有预检worker设置有限总时限，超时结束该临时进程、观测退出并拒绝结果，ERP未因此停止，可能的远端测试现场保留。仅合成协议/内存NAS测试，不取真实密码或对真实共享执行预检；未启用或部署后台路线。

独立预检已实现并接入同一受保护worker的probe动作：只允许继承网络FD，拒绝附带本地数据FD/任意作业目录；核批准身份/协议会话后执行随机测试文件完整读写、原句柄改名、名字消失/目录身份检查，全部通过才标记删除并核关闭及最终缺失。删除返回不确定时不补发请求或按名字清理；NAS可能已完成删除，结果一律不标通过。实际空间采用min(CallerAvailable,ActualAvailable)乘单位大小，支持配额总量小于整卷空闲，拒绝负值/溢出/不足及预检后空间下降。自有probe子进程120秒总时限，超时只终止并收割原进程，不重启、不触碰ERP或完整备份worker；此分支目前是合成进程接口测试。

最终相关388项全通过（16.25秒），含44项新增。初始177通过/1失败，失败是溢出夹具只把sector数量设为32位最大值，乘积仍在63位范围内，生产拒绝条件没有触发；将两个乘数字段组合成真正溢出后通过，原失败日志保留，不收紧或绕过协议门禁。测试使用真实固定版本SMB结构和内存NAS、合成认证/批准记录，覆盖旧文件碰撞/保留、写读错、文件与目录身份变更、改名旧名残留、删除未实际完成、配额、空间下降、请求/证明绑定及超时单次进程处置；不是实际NAS/UID499验收。

源码差异检查与主代理顺序复查完成，证据evidence/mac-smb-stream-preflight-20261012；无独立代理审计声明。没有调用真实NAS探针、读运营密码或修改已发布服务。本原语还未接入Manager/nas_probe/夜间调用点；下一阶段显式流目标及调用路由，随后受保护配置、原工厂签名及真实NAS/注销重启/工厂最终验收。旧机、正式库、原备份与全部历史失败现场保持本轮未操作。

## 2026-10-12 SMB流目标与完整调用链接入范围

主代理唯一写入，延续c087f2d1。允许新增mac_smb_stream_target.py及对应集成测试，修改Manager、nas_probe、mac_backup_storage、mac_service_manage和流运行合同必需文件清单，按需补充相邻测试并更新本卡/计划。显式目标使用绑定root合同摘要的逻辑选择值，不冒充NAS挂载目录；匹配流前缀的损坏配置必须拒绝，不自动回落旧后端。停止前验证批准程序/专用身份/固定实例、公钥、完整输入与本地容量，再执行真实协议读写/改名/删除canary及NAS容量预检；全部通过后沿用原维护锁和停止/恢复启动逻辑。手工备份、夜间任务和更新均接入已认证回执入口，更新所需密文Path仍为私有证据副本，发布源目录不得由逻辑目标拼接。设置与只读状态只核本地批准元数据，不能因配置存在自动取NAS密码或连接网络。

保留原Windows/NFS/原生SMB行为、关机不重启和失败告警，不新增清理或删除原备份。全部测试使用合成实例/凭据/协议对端，不启用构建能力、不部署、不改真实运行服务，不读取真实NAS密码。实际NAS/UID499/launchd全量备份、签名、断连与注销重启及工厂最终停写同步仍需后续验收。

显式目标已接入prepare_destination、停止前NAS预检、Manager完整备份/更新及现有nightly消费点。逻辑值绑定root合同SHA，目标/批准程序/固定私有实例/公钥不符一律拒绝；不把选择值传给旧挂载文件操作。停止前在同一维护锁内核完整输入和本地空间，再执行SMB canary/容量，最后复核原root记录与目录Lease。失败发生在stop之前；停服后的传输/认证失败保留现场和告警，按原运行状态及关机门禁决定是否恢复启动。成功仍使用原完整认证回执和可读取的私有密文副本，真实多版本合成升级链保持迁移备份SHA和原业务事实；专项迁移实际阶段失败则保持停止、不回写旧数据。

configure-backup在流批准记录存在时选择流目标，部分安装不能回落旧合同；既有保存的旧路线状态仍按原选择验证，不因新root记录出现而自动重定向。状态读取仅小型批准元数据，无NAS连接/取口令；显式prepare验证完整程序批准但不连接NAS。更新链以明确输入程序包所在目录寻找已签名中间版本，不用逻辑备份目标拼路径；保留清理使用同一实际发布目录，原清理代码及备份/暂存保留行为未改。

新增25项路由集成测试。最终相关606项为605通过、1既有Windows 8.3 API跳过、0失败（26.19秒）；另做旧恢复/凭据/迁移/库存reader及原生SMB兼容147项，146通过、1相同平台原因跳过、0失败（6.23秒），分开记录不与早期重叠集合累加。初始21通过/1失败：错误root夹具没有control目录，先被原维护锁拒绝；修正夹具后测试真正进入实例校验。扩大执行曾重复列入新文件，631项中627通过/1跳过/3失败：同一多版本测试被执行两次，夹具未设置真实专用入口的077 umask，原Manager写state后600检查拒绝；另一旧Mock未接新预检方法。按实际服务Umask和真实方法补齐测试夹具，生产权限门禁不改，最终去重执行通过。所有失败日志保留，没有把重复执行累加成独立验证。

02:04本机只读/api/health在127.0.0.1、192.168.163.157、192.168.1.243均HTTP200/ok=true；一次先请求了错误的/health路径被HTTP拒绝，该诊断也保留。两张网卡地址未改，无第二台电脑验收证明。老板确认192.168.3.80为搬入工厂后的地址，未提前配置。证据evidence/mac-smb-stream-routing-20261012；源码差异检查及主代理顺序复查完成，无独立代理审计声明。候选未推送、未签名或部署；NAS实际登录/备份、配置安装、build能力、完整暂存保留策略及注销重启/工厂最终验收继续待办，旧机及原失败现场未操作。

## 2026-10-12 SMB流受保护首次配置入口范围

主代理唯一写入，延续c96bf0cc。允许新增mac_smb_stream_setup.py、service_smb_stream_setup_entry.py及对应测试，更新mac_keychain_install最小管理员代码清单、mac_smb_stream_runtime_contract批准文件清单及必要相邻测试，并更新本卡/计划。首次配置计划绑定原公钥批准包、context、registry、审核过的管理员代码与明确NAS合同，有有效期及唯一尝试ID。root保留pending并以真实专用身份启动批准程序；管理员仅连接合同中的私网IPv4/445，把单个网络FD交给固定全出站拒绝worker。worker核前台终端和隐藏输入，在协议协商通过后取NAS现有密码，登录及canary读写/改名/删除确认通过才向专用钥匙串新增唯一引用；第二独立进程验证可读。密码不进入计划、argv、环境、管道、输出或回执，不读其他运营凭据。

原root记录/批准身份与有效期复核通过后，独占发布流合同、控制记录和配置完成回执。失败保留pending、可能新增的钥匙串条目及未知远端测试现场，不覆盖、删除、自动重试或声称已回滚；旧NAS配置和preferences保持，后续显式configure-backup选择新路线。此入口不启动/停止ERP、不执行完整备份、不启用构建能力或真实安装。测试使用合成root记录/身份/钥匙串与协议对端；真实NAS账号/共享/密码和UID499前台隐藏输入、后台备份/注销重启仍待实机验收。

整合检查发现管理员最小代码包构建器另有同序白名单，追加允许更新scripts/uat/build_mac_keychain_installer.py使其与运行时核验清单一致；继续只打包已提交的明确文件，不接受工作树或额外文件，不改变原可信发布公钥或应用构建能力。

首次配置候选已实现。计划含有效期、唯一尝试、审核代码及原公钥/context/registry/批准包/固定NAS合同；实际root入口在发布锁内保留原字节pending，使用三个独立专用进程核身份、输入/验证/新增、再次读取。provision的父进程仅向固定私网IPv4/445连接一次，子进程仅继承该socket及前台终端，固定deny network-outbound且实际核新连接被拒绝。协商匹配后才提示隐藏密码，账号来自批准合同；读写16KiB canary、flush/回读/不覆盖改名/原句柄删除及缺失确认通过，退出会话复核后才向独立NAS命名空间SecItemAdd一次，原条目不更新或删除。另进程只读该新条目；回执没有密码、密码摘要、长度或认证token。

旧挂载配置、preferences、ERP、完整备份及运营凭据未触及。先发布合同和验证记录并回读，再最后发布启用控制记录作为提交标志；前面失败或缺少控制记录不能通过正常流运行绑定。失败保留pending/可能新增条目/远端未知测试现场；不自动重试、回滚或清理。隐藏输入保留先前修复的前台进程组门禁，禁止getpass降级回显；协议握手采用有限600秒窗口容纳人工输入，不把普通后台传输的时限扩大。

新增61项检查。首轮41通过；整合767项766通过/1失败，失败暴露构建器白名单未同步新增3个管理员模块，修正生产构建清单后最终768项全部通过（22.10秒）。随后只改善PTY测试的waitpid/最终输出排空竞态，61项定向复查全部通过（0.53秒）；生产代码未再改。真实普通UID501伪终端中，以实际sandbox-exec、隔离Python、继承stdin和捕获stdout/stderr运行getpass，合成密码不回显、终端设置恢复、新连接EPERM；该检查未读取用户实际终端，不能替代UID499/安装后会话验收。Apple Python3.9.6在-I -B -S下导入新管理员模块通过，未执行apply；没有第三方依赖流入管理员最小代码包。

本轮不读取真实NAS或运营凭据，没有真实网络/NAS认证，不部署或启动/停止服务、不清理旧机。应用构建能力仍未启用，正式原公钥签名、真实账号/共享/隐藏密码配置、完整工作材料保留策略、NAS全量备份及断连/注销重启/工厂最终验收继续待办。源码差异检查和主代理顺序复查完成，证据evidence/mac-smb-stream-setup-20261012；NAS仅local_mount_view回读，不声称上传队列或远端持久化验证。

## 2026-10-12 SMB成功备份工作材料回收范围

主代理唯一写入，延续f5ae20ab。允许新增mac_smb_stream_work.py及对应测试，修改流构建器/回执/批准模块清单、mac_service_manage和GUI的保留提醒及相关测试，更新本卡/计划。仅在本次完整NAS独立回读/GCM认证、本地密文副本、成功回执及state均完成后，回收本次新建staging槽中的六个明确工作文件；不扫描或清理旧槽，不碰backups密文、迁移引用、数据库/附件、NAS归档或任何失败恢复现场。保留小型开始/完成记录及原槽目录。

构建完成时固定六文件摘要/完整身份与原槽身份。回收必须持有现有维护锁、ERP已停止，两个传输子进程已正常结束；复核固定私有父目录、所有文件FD/权限/摘要、成功回执与本地副本完整SHA，逐次删除前复核原state/批准记录及剩余清单。仅对原槽dir_fd执行明确叶文件unlink，不递归rmtree，不调用其他目录的清理或删除原有清理门禁。状态先记录本次材料待处理引用；回收完成后才清除此引用，异常/中断保持引用和未删文件并提示管理员，不能把有效备份误报为已丢失或把未清理状态显示为完全正常。历史待处理引用不因下一次成功而自动清除。实际运行数据不操作，全部以合成备份和故障注入验证。

本闭环已完成：成功状态先保存本次待处理引用，之后核本地密文完整SHA、当前状态、批准记录、回执及六个工作文件的FD/身份/摘要，逐个复核后通过固定dir_fd删除。开始与完成日志独占创建、回读并持有；只留下两个小日志和原任务目录。异常或并发变化保留引用及剩余材料，后续成功不会扫描旧槽或消除旧提醒；可恢复密文、NAS归档、迁移引用和全部原失败现场不操作。管理状态和GUI增加独立待核查提醒，不把有效备份误报为丢失。

最终相关603项为602通过、1既有Windows平台检查跳过、0失败错误（29.06秒），其中新增工作材料23项、管理及GUI各1项。完整合成备份在回收后由原restore再次恢复，附件及backup/mailbox/openai凭据保持。初始115通过/1失败/1 teardown错误：一项测试未期待密文变更导致外层Lease拒绝，另一项注入的全局unlink钩子误作用于测试临时目录收尾；已修正测试预期和钩子身份/单次范围，生产门禁未削弱，初始日志及未完成的合成临时现场保留。没有执行真实工作材料回收，也没有扫描旧失败目录。

02:35只读/api/health在127.0.0.1、192.168.163.157、192.168.1.243均HTTP200/ok=true，未改家庭网卡或配置工厂地址，无另一台电脑的访问证明。证据evidence/mac-smb-stream-work-20261012；主代理顺序复查及差异检查完成，无独立代理审计声明。候选未启用build能力、未签名或部署；NAS账号/共享/隐藏密码配置、原签名、真实UID499后台全量备份与断连/注销重启、工厂最终停写同步及打印/扫描仍待验收。另发现迁移state包含完整文件清单，而专用管理读取上限为64KiB，需下一闭环以真实规模合成清单核验，尚未宣称已复现或修复。

## 2026-10-12 完整迁移状态规模兼容范围

主代理唯一写入，延续893de5fa。允许修改mac_service_manage.py及专用管理/流备份相关测试，更新本卡/计划。先以2991个合成附件的原migration.files清单复现64KiB状态读取限制，不能读取或修改实际业务附件。只为state.json设置独立、有界的完整记录读取/严格JSON解码与写后回读；小型preferences、恢复回执及root安装/请求记录继续原64KiB限制，权限/FD/原字节并发比较/重复字段拒绝等门禁保持。不删减migration_files、历史字段或摘要，不以截断清单或清理历史作为修复。超界新状态须在创建pending前拒绝，完整大状态应可查询、启停、备份和安全并发拒绝；以真实文件FD及完整合成流备份验证，实际运行和工厂数据库不变。

扩大回归暴露test_mac_factory_activation.py中的管理员最小包检查仍要求18个源码文件；f5ae20ab已审核增加SMB合同/运行合同/首次配置三个纯标准库模块，实际构建及校验清单均为21项。追加允许修正该测试的明确数量并核这三项包含、导入SMB配置且无第三方运行依赖；不改变生产构建清单、签名或安装入口。

已用2991个实际合成附件通过原migration.files生成完整清单，状态超过64KiB；修复前三个状态/启停/配置备份用例均在原长度门禁失败，证实会阻断完整迁移后的管理入口。现在仅state.json采用16MiB独立上限（与既有签名程序manifest大小界限一致），小型记录与root请求仍走原64KiB解码。完整字节FD读取、严格对象/重复字段/非有限数拒绝、600权限/父目录/身份及原字节并发比较保留；不删清单或历史字段。新提案先检查编码字节长度及格式，再创建pending，写后同界限全量回读。

新增22项：完整大状态查询不泄露清单/不触网/不读秘密，启停和配置备份保留历史，失败迁移仍禁启动，并发改写保留新事实，超界文件读取前拒绝/超界写入前不留pending，UTF-8字节边界和严格JSON；完整流备份另新增2991个真实合成附件，独立回读、回执、工作文件回收后由原restore恢复，数据库事实、完整附件清单和backup/mailbox/openai凭据一致。实际业务文件、程序、系统服务和钥匙串未修改。

测试分开记载：复现3失败（0.89秒）；修复后定向119全通过（15.35秒）；扩大969项为968通过/1失败（236.84秒），唯一失败为上述旧18项清单断言。仅修正该测试后，本次代码/全部大状态与流回执及失败断言合计120项全部通过（15.77秒），无跳过。未重跑整组969项，不宣称有一次969全绿，不累加重叠集合。所有原始日志/XML保留，主代理顺序复查、差异检查完成。原工厂Git基线只读查询159秒无返回，确认仅本次query进程组后结束并保留记录；没有取得新正式SHA或推送/签名证据。

证据evidence/mac-service-state-scale-20261012，NAS按local_mount_view独立回读；不等同远端持久化/上传队列归零。下一闭环是构建中显式纳入已固定的SMB协议依赖、批准模块及能力，形成可交原身份签名的完整候选；真实NAS账号/共享/密码、UID499后台全量备份/断连/注销重启和工厂最终同步/唯一写入端/打印扫描/管理员验收继续待办。不能清空旧机或清理原失败现场。

## 2026-10-12 完整SMB流Mac构建范围

主代理唯一写入，延续a3d120a8。允许修改desktop_assistant/build.py，新增mac_smb_stream_build.py及构建测试，更新本卡/计划。显式Mac专属构建选项接受两个既有固定版本/固定SHA的本地wheel，核原提交所含全部流入口及依赖声明；只将经过摘要核验的wheel装入本次新payload，不联网安装或修改原venv/已发布程序。拒绝缺项、版本/摘要不符、路径/链接/成员碰撞及Windows误用。包内Python以-I -B在固定全出站拒绝沙箱内核实际模块位置、版本及新连接被拒绝，成功后才声明archive_smb_stream_v1。无该选项继续原构建行为。

完成源测试及提交后，以原可信公钥构建完整未签名请求，核源码/版本/唯一revision、运行依赖、完整包清单及公钥身份，保留失败场景。仅生成待原Windows DPAPI身份复核签名的候选，不生成替代密钥、不部署或切换、不读取真实NAS或运营凭据；原正式签名门禁不跳过。所有新增构建产物有本任务目录和回执，旧机/旧失败材料不清理。

显式--smb-stream-wheels入口及离线构建模块已实现。仅Mac接受该选项，先核当前提交完整流模块和requirements精确版本，按原固定SHA读取两个本地wheel并验证ZIP路径/类型/成员；装入新payload前拒绝已有同名模块或distribution，不调用pip、不改原venv。包内Python实际导入协议及完整流运行模块，核模块均来自本包、实际版本匹配，并在固定deny network-outbound下检查新建连接PermissionError；证明写入构建记录及包manifest，随后才声明archive_smb_stream_v1。未选该参数保留旧能力集合。

新增26项构建检查，首轮相关39项全部通过；完整流组件、构建、分离签名、发布与平台兼容共588项全部通过（28.89秒），不累加重叠集合。构建主入口有明确模拟依赖拷贝的接线测试，不能替代实际包内验收。已再次核对真实缓存wheel共67个成员及固定SHA；完整真实运行依赖打包和包内验证由evidence/mac-smb-stream-build-20261012/build-complete-runtime.py在提交后执行，其结果须以native-build-verification.json及NAS独立回执为准，未执行前不宣称完整构建成功。代码差异检查及主代理顺序复查完成，无独立代理审计声明；实际系统未改，原签名与实机NAS/重启/工厂现场门禁保持。

## 2026-10-12 Windows解密输出HOLD修复范围

用户交回7bb7a564工厂HOLD：375通过/11平台跳过、15原生专项通过，但独立合成复现decrypt_file沿预存悬空符号链接创建明文目标，未签名。Mac已搬入工厂，当前Wi-Fi 192.168.3.102/24、网关192.168.3.1；旧ERP仍在网内，本轮不占用192.168.3.80、不解除家庭隔离、不停旧机或接管。NAS重新登录后已读原回执及复现，旧失败现场保留。

主代理单独实施，允许修改desktop_assistant/storage.py、tests/desktop_assistant/test_storage_authentication.py，新增test_windows_exclusive_output.py，并更新本卡/计划及外部证据/构建交接。Windows输出必须原生CREATE_NEW配合FILE_FLAG_OPEN_REPARSE_POINT，既有目标检查不能代替原子创建；保持非继承句柄和原不共享删除的语义，失败不得回落Python普通open或删除他人目标。Mac路径原语保持，密码/GCM/全量恢复算法不改。测试明确区分原生Windows与Mac上模拟API，不把模拟通过当Windows实机通过；Windows实际文件锁语义采用明确断言，不跳过文件安全问题。仅修末级输出创建边界，不宣称任意父目录并发重解析问题已获通用解决。

所有测试只用合成文件/口令。新程序包须绑定新准确源码与原公钥，再交原Windows身份复核签名；旧7bb仍HOLD，不复制旧签名或部署。实际业务库、运营凭据、系统服务及旧机不修改。

扩展检查另确认四个Mac专用测试模块缺少平台标记（keychain_install、published_runtime、smb_client、smb_mount），两个已有Mac标记模块在标记生效前导入fcntl（factory_activation、factory_runtime），混合模块runtime_platform两项与home_rehearsal一项实际进入Mac权限/身份路径。追加允许上述测试文件标注真实平台范围，并给两项超大JSON参数使用短ID，载荷和断言不减。Mac全量运行这些检查；Windows仍必须运行共享storage真实安全用例及新增原生输出检查，不能以平台跳过掩盖解密缺陷。

本轮输出修复已完成：仅Windows的decrypt_file使用原生末级CREATE_NEW和OPEN_REPARSE_POINT，非继承句柄转交CRT，转换失败精确关闭原句柄；不跟随既有或竞争加入的末级链接。保留原父目录受控责任、读写共享但禁止删除共享、GCM认证算法及失败身份核对；其他storage既有函数AST不变。共享测试在Windows明确核持有句柄时禁止改名、关闭后失败清理，Mac仍核替换文件保留；悬空链接目标必须仍不存在。新增12项中9项为API/句柄边界合成验证，3项必须Windows原生执行。

Mac首轮27通过/3 Windows原生跳过；恢复/凭据/SMB/签名/构建相关685通过/4平台跳过（43.36秒）。平台范围409项首轮407通过/2失败（94.13秒）：我调整测试导入时误删两段子进程字符串中的sys导入，已补回并对原字符串常量逐项比较。随后两个受影响完整模块113项全部通过；原失败日志保留，集合重叠不累加，不宣称重新跑过完整409项。当前没有Windows实机修复通过证据，7bb的HOLD不撤销。

工厂现场只读：2026-10-12本机127.0.0.1健康200，系统launchd监护运行且ERP_HOME_REHEARSAL=1；新工厂地址192.168.3.102:8000连接被拒绝，家庭旧地址转发未适配，不能称工厂LAN已验收。用户确认NAS仍在家里，旧192.168.1.245/192.168.2.3的445均超时；NAS客户端远程挂载恢复只用于交接，不等于后台直连SMB通过。需要工厂至家庭NAS的可验证私网连接或重新确定备份架构，未开放端口、更改网络、安装配置或连接正式库。新候选继续待Windows复核与原身份签名，后续工厂最终停写同步与唯一写入端仍须完成。

## 2026-10-12 工厂目标审计与远程备份衔接

老板确认工厂和家庭没有VPN，只使用极空间远程挂载；随后明确同意NAS留在家里，安装官方蒲公英并配置仅Mac和家庭NAS的专用私网，不购买套餐。当前挂载为UID501的回环NFS代理，不能视为UID499无人值守备份或远端持久化证明。网络连接、认证、独立回读和重启验收仍须以实际结果为准。

本轮最小源码闭环允许修改desktop_assistant/factory_target_audit.py、tests/desktop_assistant/test_factory_target_audit.py及新增tests/desktop_assistant/test_factory_target_archive.py，由remote_backup_review负责代码与测试，主代理负责本卡/迁移计划/证据并独立复核。目标审计必须按已配置目的地选择现有受保护SMB流合同或旧挂载合同，持有并重复核对原绑定，不联网、不读取NAS口令、不回落其他后端；完整备份认证、数据库/附件/运营凭据比较、维护锁和原签名门禁不变。同一审计发现state.json仍错误套用64KiB小记录限制，允许复用已有16MiB严格状态解析器，只扩展state.json，其他授权/配置大小限制保持。先保留失败用例再修复，所有测试仅合成资料，不部署或修改真实实例。

首次空实例恢复入口不能在现有实例与system-service记录上重跑。最终同步的保留旧实例、最新完整冷备接收、授权文件绑定和原位置提升流程先做只读设计及隔离演练；当前不让旧机停写，不清空/重命名真实运行实例或删除安装记录。6633a2f8仍是独立已交付待Windows复核候选，本轮后继源码不得冒用其签名。

老板随后要求优先解决工厂局域网登录。主代理在任务专有evidence/mac-factory-preview-lan-20261012准备可复核操作，仅调整现有Mac预演的可信主机/来源/二维码为实际192.168.3.102及原127.0.0.1，并把原nobody转发的固定家庭地址更新为192.168.3.102、只接受192.168.3.0/24。先保留原环境/转发代码/plist，使用原已签名管理入口正常停止和恢复Mac预演；保持ERP_HOME_REHEARSAL=1、回环8000服务和出站/收单门禁，数据库、运营凭据、旧Windows及192.168.3.80不改。当前副本时点仍为10月10日，不作为当日正式业务账。只读健康、静态资源、Host/Origin拒绝和认证保护验证后，由另一台局域网设备人工登录确认；不能用本机请求冒称异机验收。

老板最新确认：10月10日备份后没有新增业务，不需要重新数据同步。因此保留并继续使用已经完整恢复核验的10月10日副本，不把再次恢复/最新全量同步设为本次LAN登录的前提。前述最终恢复事务仅作为未实施备选，不继续开发或运行；正式切换仍需核对唯一写入端、地址归属、原签名和备份/现场验收，不能清空旧机。

目标审计候选修复已完成并独立复审：按明确preferences.nas选择已批准stream Binding或旧挂载合同，在初次、事实审计回调及凭据比较后重复核对；错误不回落、不触NAS、不读取NAS密码。只有state.json采用既有16MiB严格解码。修复前两项正例失败、凭据比较期间审批变化用例先失败；最终9模块421通过/2因本地缺smbprotocol跳过（188.47秒）。使用原固定SHA两个离线wheel仅加本次PYTHONPATH补跑两个协议模块103通过/0跳过（4.19秒），未改venv；与421集合重叠不累加。独立复审及禁止协议依赖/IO的非stream导入探针通过。没有构建或部署此后继候选，原签名门禁保持；完整日志evidence/factory-target-archive-audit-20261012。

工厂LAN实际闭环已完成：原签名管理入口正常写入manual_stop后，非父进程的psutil.wait因监护持有已退出的子进程817（UID499、PPID315、kernel Z）60秒等待未成功；未强杀、未删进程记录。初始和首次续接均在网络变更前拒绝并保留。第三份独立续接严格固定原8文件、两代源码SHA、原环境/relay/plist/恢复回执与最新签名停止状态，在维护锁下确认原子进程不存在或精确Z且父PID吻合系统监护，才继续。独立复审均无阻塞，旧程序的僵尸等待问题作为后继源码问题保留，本轮未改签名程序。

09:53已完成四个网络配置键、固定relay地址及plist脚本路径更新，原root私有记录保留于/Library/TianmingERP-FactoryPreview-20261012/preserved；原签名入口重新启动成功。192.168.3.102:8000健康200、首页跳转与JS/CSS资源200、未认证me为401、不批准Host400、批准Origin预检200/外部Origin预检400，共8项只读检查通过。随后老板明确回报另一台工厂电脑“已能打开并登录”，异机人工登录验收完成。

沿用原acb2f921 ERP程序和22bd55b3系统监护，保持ERP_HOME_REHEARSAL=1及原全部外发/重复收单门禁；不重新恢复/同步数据，不改变业务账号或运营凭据。192.168.3.80未配置，正式激活未执行，旧Windows及全部失败现场保留。局域网登录完成不等于固定地址、NAS后台备份或正式唯一写入端验收完成。证据evidence/mac-factory-preview-lan-20261012。

## 2026-10-12 系统监护停止与子进程退出闭环

延续6aab19c2。工厂LAN人工登录完成后，继续处理实际复现的停止误报：原监护保留自有子进程为Z，独立管理进程等待PID消失超时。允许cutover_gap_review唯一修改desktop_assistant/manager.py、desktop_assistant/service_supervisor.py及其专有进程生命周期测试；主代理负责本任务卡/计划/证据、整合和后继完整构建。先合成真实跨进程复现；Manager只登记和非阻塞回收自己创建的Popen，监护在维护锁之前回收，避免与外部stop持锁相互等待。停止仍保留原PID/创建时间/可执行文件身份、nonce优雅退出和总超时；已退出僵尸或原进程消失不误报运行，活进程、暂停、权限拒绝和身份冲突不可放行，不强杀、不waitpid(-1)、不全局SIGCHLD回收。只运行隔离合成子进程和测试，不重启当前Mac ERP、不访问真实数据库/凭据或旧机。修复须进入后继完整原公钥签名候选，不能直接修改已发布程序。

相邻正式运行审计发现mac_factory_runtime.configuration仍把state.json套用64KiB合同上限。追加允许remote_backup_review唯一修改desktop_assistant/mac_factory_runtime.py及tests/desktop_assistant/test_mac_factory_runtime.py；只将精确state.json复用现有16MiB严格解析，其他root合同仍64KiB，字节/权限/签名/context/时间及native绑定不放宽。先用完整大状态合成复现再修复，核运行入口不会引入不必要依赖或循环导入。仅候选源码，实际应用/数据库不动。

用户无需再次同步的正式激活路线先做设计：保留原恢复回执/旧密文输入/数据快照，新增显式复用已恢复快照的签名证明类型，将历史备份时间与当前旧机停写复核时间分开；保留原DPAPI、公钥、新申请7天/新签发8小时、双方完整事实/附件/凭据及唯一写入端验证。禁止修改旧日期/回执、整体忽略审计日志或重新覆盖业务实例。此段仅记录拟议路线，尚未实现或批准实际激活。

两项源码闭环完成并分别通过独立只读复审。进程修复先保留真实跨父进程僵尸导致60.04秒超时的失败，再实现仅自有Popen登记/短锁非阻塞poll、维护锁之前回收、固定原记录及nonce的0.25秒间隔状态复查；原60秒总限和活进程身份/权限拒绝保持，不增加强杀或全局回收。最终8模块204通过/1显式launchd实机跳过，Mac真实合成僵尸已执行，Windows接口仍是模拟覆盖。证据evidence/mac-managed-process-exit-20261012。

运行state修复先由2991个真实合成文件清单复现原64KiB拒绝，仅精确实例state.json改用已有16MiB严格解析，其余合同/同名文件保持64KiB。新增28项及相关4模块最终242通过/0跳过，延迟导入在三种首导顺序、禁止SMB依赖/网络/子进程IO下验证通过，依赖源码SHA首尾一致。证据evidence/mac-factory-runtime-state-20261012；未部署或改变实际激活记录，不能以这些测试代替Windows原生或系统重启验收。

专用私网本轮已实际建立：云管理网络7121934、客户端网络267204169，名称“天明ERP备份专网”，仅Z4S-L342（172.16.3.96）和Mac Mini（172.16.0.87）；原7061663网络仅保留iPhone，未购买套餐。移入完成由云管理成功结果和新旧成员页分别核验。新NAS地址路由经utun4，445可达；候选只进行无认证SMB3.1.1/AES-128-GCM协商，GUID与原家中NAS相同，子进程新建连接仍EPERM。未进行NAS认证/共享读写，未读取密码或执行备份。ERP 192.168.3.102健康仍200。

Mac蒲公英开机启动/自动登录/登录连接/强制证书校验均已勾选；真实root系统守护早于GUI启动，但不能据此声称注销或重启后的自动认证已通过。官方“仅以后台服务方式运行”仅说明隐藏任务栏图标，未将此选项当无人值守证明。真实NAS登录、固定共享目录、UID499全量加密上传与独立回读、注销重启和免费转发链路实际耗时继续待验收。证据evidence/mac-dedicated-vpn-20261012；原数据、签名程序、Windows旧机及失败现场保持。
