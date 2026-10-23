# CONTROLLED-UAT-ENV-COMPAT-20261023

状态：仅离线兼容修复准备。旧v717签名工具77fbef8a及其管理员包、正式程序、环境配置全部保持；未生成实际计划、未capture。原生诊断证明环境是字符串字典、mtime早于进程，但有内联凭据字段，故原工具uat_export.py:59按原合同拒绝。具体字段与值未输出，不能把字段存在说成已证实某个非空密钥。

目标：使受控开发副本工具兼容既有共享环境里的已知内联凭据字段，同时确保凭据不进入任何交付件，不通过修改正式配置/重启规避来源校验。此任务不是财务修复，不放宽历史事实或普通管理入口。阶段A修订后的凭据处理须形成可审核候选，再取得所需正常管理员准入；阶段B仍绑定实际未过期计划另行执行。

唯一写入负责人：本ERP3根任务；独立复核者只读代码并独占evidence/erp3-next-20261023/controlled-export-v717/environment-compat/independent-review.json。代码范围：desktop_assistant/uat_export_contract.py、uat_export.py、uat_export_sanitize.py、mac_uat_export.py、build.py及对应tests/desktop_assistant/test_controlled_uat_export.py、test_uat_export_sanitize.py、新增环境合同测试、本任务卡。后续冻结新包及新管理员准入另列精确文件，不修改已执行stage-a或失败现场。

拟定合同（实现与独立复核通过前不得执行）：

- 旧整体凭据策略v1与新兼容策略v2分开，能力版本和精确签名代码绑定新策略；不能让旧计划进入新apply。
- 共享environment.json仍只在受保护进程内读取，源摘要/mtime/身份绑定保持。只把批准的原件根和运行地图路径投影供来源检查使用；配置文件、任意配置字段或凭据字段不会进入输出。已知内联ERP_SECRET_KEY、OPENAI_API_KEY、DEEPSEEK_API_KEY仅在私有进程内作为残留检查模式；不打印、持久化或传给用户身份进程，不读取它们指向的文件或Keychain/备份密码/AI配置文件。
- 凭据相关的未知环境键拒绝；已知仅作为文件引用的字段不读取目标。其他非路径配置不影响业务原件根的白名单与实际运行路径比较。大小、重复JSON键、值类型、源变化、路径范围及所有原保护继续拒绝。
- 数据库原有四字段去凭据及业务/结构/外键/完整性验证保持；新增检查已知内联值不残留在SQLite逻辑TEXT/BLOB（含溢出页）或整个交付清单、地图、原件、回执的原始UTF-8字节。命中时拒绝交付，不篡改业务内容来清除匹配项。此为已知凭据残留检查，不宣称通用秘密识别或PII匿名化。
- 服务UID在私有staging检查，管理员在本批root私有payload再次独立检查后才移交目录。管理员再次读取的环境必须和计划摘要一致；最后仍在既有维护锁内核验来源。不给UID501正式目录访问权，不改任何旧目录权限，不启动/停止业务服务。

定向验收：已有真实拒绝证据；合成环境含非空/空/大小写已知字段、文件引用、未知凭据键、重复键和非字符串值；只有六项路径可投影；所有异常不含模拟秘密；已知秘密出现在业务文本、跨SQLite页、地图/原件/回执及跨读取块边界时无交付；篡改回执、旧策略计划、源环境变化、复制期间变化均拒绝；正常输出逐表业务指纹不变且所有配置/凭据字段不可见。只使用合成数据库或后续受控正式副本，不读取实际凭据值来开发测试。

新实际来源仍未生成。业务候选目前为客户补录聊天的82c6d697；新鲜来源还必须包含之后实际开票状态，不能复用旧事实数量。独立原生诊断回执及NAS索引见environment-check-v2；不重放原工具准入或复用失败输出目录。

## 当前离线实现

新整体policy为tianming.uat-credentials.v2，能力controlled_uat_export_20261023_v2；DB四字段转换自身policy保持原v1。环境有限允许集合为六路径、三已知内联、固定ERP_SECRET_KEY_FILE及23个有源码依据的非凭据字段，其他键/大小写歧义均拒绝。唯一文件引用只接受固定shared/data/session_secret.key（不在任何原件根/地图目录中），从不读取它。模式同时含原值、SHARED变量替换值及去首尾空白值，不忽略非空短值/空白，不排除禁用密码标记；总匹配字节超过64KiB就拒绝。

inspect来源回复、实际plan与receipt写出前检查已知值，JSON检查包括解析后的键和值（重复键/非有限数拒绝），因此转义不绕过源路径/文件名检查。SQLite仍检查逻辑TEXT/BLOB及原始字节，地图/回执检查JSON逻辑字符串，所有交付成员检查原始UTF-8表示；root在完整新私有payload上重复检查后才fchown。其他编码、Base64、压缩PDF/图片中隐藏的表示不在此证明范围，不声称通用匿名化/秘密扫描。命中拒绝，不改业务字符串。

当前离线代码已通过独立复核，尚未冻结新包，不能运行到正式来源；新增测试全部是合成环境、SQLite及私有临时目录。真实诊断只证明内联字段存在，未读取或公布其具体字段和值。原stageA和已签名旧包不修改。

## 验证与独立复核修复

独立审查 ENV-COMPAT-001 指出 SQLite 结构定义同样可含溢出页字符串。已增加 sqlite_schema 的 type/name/tbl_name/sql 逻辑检查。默认值SQL、表名、视图SQL三个合成用例修复前均错误放行，修复后正确拒绝且不修改副本。最终四文件回归162项通过，Apple Python3.9静态编译六文件、git diff检查通过；精确源码集合见environment-compat/review-input-r2.json。

扩展回归第一次为158通过、1失败：原管理入口测试在mock Manager后懒导入recovery fixture导致导入顺序问题；同一用例在未改动的99d6ff7c工作树复现。预载原fixture模块后159项通过，增加审查用例后162项通过；未删断言、未修改原测试绕过，初次失败日志与复现均保留。validation-results.json记录完整过程。实际业务数据、配置及服务均未参与测试。

独立复核结论passed_after_remediation、剩余阻断0，回执SHA256为bf00e6bdf47532716c57351307216af79ecf7b78c5af73905af5776f7adaf2ae。复核者独立重现并验证三个结构溢出场景；不把离线审查结论视为新工具包/管理员入口审核、原生执行或阶段B授权。

## 新阶段A冻结范围

离线工具源码提交c32fcafbb03602ca5448cdc4f9fc1345fc63d721；新构建只改原七项工具文件，业务/界面/依赖/结构保持正式v717。新入口分别为scripts/admin/controlled_uat_env_compat_admission_v717.py与scripts/uat/prepare_controlled_uat_env_compat_admission_v717.py；原准入测试tests/desktop_assistant/test_controlled_uat_admission_v717.py参数化覆盖新旧固定入口。三文件及本任务卡由根任务独占修改；独立审查者仅写environment-compat/admission-independent-review.json。新入口沿用原固定stageA流程，只替换新包摘要、来源提交、专用目录和提示语；禁止执行原失败入口或覆盖原现场，不包含capture/迁移/ERP切换。

冻结资产：unsigned SHA256 051e3bc4c03a651230deac414a810bb93ba31771c70a23ac3720fb0c1121ab65，manifest 613a428b90a116b5f83ad370e9ae662ca7513afa1265b0947fdc0281d7119284；admin ZIP e849242a80cdc19774c105999be80f2c64d7e072f5c6002cdc8003927d4e7386，admin manifest 3767d25af76162cc797fb7a6e79d33600f89bfdb0e9b8fb55749add841d469ef；入口ea79c9ef9241b46e4349c67ed8a59931a600b6b94f3595b7c6b8a29aec9ff77e，launcher16ee13810bdfcc7d6d6d93e3bec908101573afcbb0f7a009a32ab7e48a958138。新目录为/Library/TianmingERP-UAT-Export-v717-c32fcafbb036。

新旧入口72项合成测试通过，19个顶层函数AST相同；独立复核已报告无新阻断，核验新unsigned29602成员、admin25源码及manifest、28206runtime成员属性、1396非runtime源码及schema，详情以admission-independent-review.json为准。后续正常macOS认证仅执行已批准阶段A修复，不包含阶段B授权。当前尚未原生执行、签名或生成实际计划。

业务侧最新说明：仪元已在税局实际开票，旧“完全未开票再撤回”前提失效。仪元链和精度修复由客户补录聊天唯一负责；本任务不据旧清单演练或应用。该方已提交20b83995，但新发现常用箱单价三位截断，须等待其补充提交后以完整最小候选审查，不能先发布该单提交。

## c32 阶段A实际执行与新固定只读定位

用户已完成兼容修复正常macOS认证。c32包已签名登记为d464b7fe402ec014bfd2ed236149140f87c058ae1d8aedd7ab7a4250c596a78c，签名和服务UID发布验证通过；inspect随后失败，计划回执未生成，进程已退出，正式v717健康且未停服/capture。失败现场/873字节受保护错误日志保留，具体原因尚未知。前文“未原生执行”仅为当时阶段记录。

下一步限定environment-compat/inspect-diagnostic新目录；根任务独占entry.py、worker.py、authorize.applescript、validate.py及回执。现有admission_review独立只读复核，仅写此目录independent-review.json，不运行正式读取、认证、签名、复制或服务操作。固定入口绑定新已签名工具和c32准入，不重放旧失败入口；以原inspect认证完整执行，关闭逐行trace，仅收集最多32个异常类别/源码位置；同时沿既有固定FD来源守卫读取环境，输出有限公开已知字段的存在布尔、数量和路径条件，不输出任何实际值或任意字段名，不读取密钥文件，不修改配置。复核/合成隐私拒绝测试通过后才请求正常macOS认证。

v718精度候选已离线冻结6bd19225、16项定向通过，但旧恢复模板复制过宽且未完全脱敏，独立边界审查确认阻断，前一静态可行性判断已收回该部分。不能为发布绕过本次未知的inspect拒绝；新恢复源还需完整范围与凭据边界专项闭环。


## 2026-10-23 辅助工具被维护回收后的恢复闭环

已认证的退出后诊断入口在登记检查前失败。独立只读审查确认维护程序在12:53–12:56将d464签名工具归类old-expansions-original-kept，保留原包而删除两执行树和登记；不是新的签名或来源拒绝结论。正式v717未改变。恢复准备限定evidence/erp3-next-20261023/controlled-export-v717/environment-compat/helper-reconstitution新目录；根任务是entry.py、原样worker.py、launcher、合成验证和候选回执唯一写入人。现有backend_audit独立复核，仅写该目录independent-review.json，不操作正式系统。

固定管理员入口经新正常认证后，仅对精确d464包按原发布验证器重建：原包、清理证据、信任记录、管理员包、服务身份和正式v717均固定核验；原包及失败现场保留；两执行树必须完全不存在，已有/部分现场拒绝覆盖。原发布解包、完整树/托管链接、专用UID验签、最终身份和原子登记门禁全部保留。不重新签名，不修改信任，不停服或复制业务。恢复前在既有维护锁→发布锁内，以原哈希CAS将精确d464加入已有protected_helper_packages；其他策略字段不变，不关闭清理。写策略后要求没有旧cleanup进程，否则保留新pin并停止，以阻止锁外已缓存旧配置的进程继续回收；后续新进程读取新策略。任务完成时再以独立受控回执撤销本任务新增pin，不自动取消其他保护。

登记成功后仅重跑已复核退出后inspect诊断；来源通过才调用原make_plan。实际capture仍要求计划生成后单独确认。若仍失败，仅输出错误类型/源码位置，不读取配置值、日志正文或业务行，不因此绕过任何来源门禁。签名原包重建不是完整恢复演练，不能据此发布财务候选。

### 系统 Python 3.9 实际兼容补充

D3 已正常认证，但在任何策略/登记变更前因 Path.stat(follow_symlinks=False) 的 Python3.9 不支持参数失败；只编译和3.12模拟测试未能发现此运行时问题。当前策略/登记摘要均保持原值，原包、失败目录仍在。最小修复只将 uat_export_contract.py 的两处及 mac_uat_export.py 输出父目录的一处改成等价不跟随链接的 Path.lstat()，不改检查条件、来源、权限、计划或凭据策略。新增 scripts/uat/check_export_python39.py 使用真实合成文件及真实 no_acl/目录描述符运行，旧代码已由系统3.9重现失败；修复后同时运行3.9/3.12，覆盖普通读取、替换、硬链接/软链接、可写权限、大小/身份、维护锁及目标父目录换位。

本次只为管理员代码包准备兼容变体，固定 d464 运行包仍使用其已签名 Python3.12，原包和旧管理员现场不改。新 D4 固定入口在独立目录安装新版最小管理员包；相对旧25文件只有上述两文件三处等价调用及新install-manifest变化，完整哈希及源码提交固定，旧管理员准入仍先核验。恢复流程保持D3合同，旧D3不可重试覆盖。现有backend_audit独立复核允许读取新包和真实合成测试，仅写python39-admin-compat/independent-review.json及后续指定D4复核回执；不执行正式操作。

### D4 已恢复工具与二维码入口兼容

D4 正常认证、真实系统3.9执行成功。d464原包重建两执行树、完整回读和服务UID验签后恢复登记，维护策略只增加精确d464保护；其他策略/信任/正式v717保持。最终inspect真实拒绝定位到环境未知字段，有限条件回执确认恰为ERP_MOBILE_QR_ORIGIN和ERP_SHELF_LABEL_ORIGIN，其他未知字段0、值均字符串、无歧义、固定原件目录匹配、配置早于进程启动；未输出配置值、未capture。

两字段已由factory_network.py及app/services/mobile_qr.py源码确认用于既有扫码链接来源，不是业务文件根。最小修复只加入固定忽略集合；不改变六项文件路径投影、凭据处理、任何导出文件范围或未知字段拒绝。新增两项合成场景先复现拒绝，修复后要求真实合成capture不带字段/值、源DB/config不变、原凭据扫描仍生效、大小写变体和_TOKEN后缀仍拒绝。原3.9路径兼容一并保留。

下一新工具包需要包含此固定白名单及3.9兼容，来源提交冻结后按既有七文件unsigned构建合同准备；业务界面/依赖/结构仍为正式v717。新固定stageA独立目录，沿原签名/信任/发布验证流程，在维护锁→发布锁内为精确新签名包增加helper保护后再正常发布。原d464及其pin/失败记录保留；新工具计划通过后才评估旧pin的独立移除，不删除程序原包。若make_plan失败，同一受审入口可运行绑定该新包的退出后位置诊断，只输出类型/位置，不输出日志正文或配置值，不包含capture。现有backend_audit独立复核新源码/包/固定入口，仅写origin-compat下指定回执；唯一实现写入人为根任务。
