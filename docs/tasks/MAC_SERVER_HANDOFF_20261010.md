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
