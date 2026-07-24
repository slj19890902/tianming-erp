# Codex 项目交接

## 2026-07-24 N081 两人使用场景现场盘点简化候选（人工 UAT 已通过）

- 本轮恢复紧急修复前暂停的 N081 库存建账闭环，在独立 worktree
  `D:\tm-worktrees\erp-n081-field-stocktake-simplification-20260724` 和分支
  `codex/n081-field-stocktake-simplification-20260724` 工作；候选基线为
  `f7680f1cadb730751d5baa458f9743ed11776ba8`。没有修改
  `origin/factory-current-baseline`、`origin/main` 或工厂正式目录。
- “首次盘点入库”改为面向两人使用场景的低字段流程。导出的工作表前 8 列只保留
  现场序号、系统位置、可空的大概位置、大概客户、产品或存货编码、系统数量、
  现场数量和备注；既有成品已带出客户、产品、数量和系统位置，现场只在事实不同
  时修改。新发现成品只需填写客户、产品或存货编码和数量，位置可以留空、写
  “大门右边第3垛”或“？”，系统不要求盘点人员记尺寸、材质或库位编码。
- 同格式 XLSX 继续保留隐藏技术列以供安全回传，技术列不可见但用于精确绑定既有
  批次、版本、可用量和库位。未知数量或“？”不计入盘点；新库存数量 0 被阻断，
  既有库存明确填 0 则作为实际盘亏处理。位置写成有效空库位时按版本门禁安全移动；
  自由文字或未知位置只标记“位置待确认”，绝不猜测库位。同一栈板在一张表中指向
  多个目标位置会被拒绝。
- 回传后系统自动完成匹配和检查。已有库存只走盘点数量调整，不重复创建批次；
  新库存自动采用成品、客户专用、个、当天盘点日期等默认值，并自动生成待确认库位
  和栈板事实。正常行不要求人工逐条确认、填写原因或二次输入，只在确有错误时标红；
  最终保留一次“确认盘点入库”按钮，并复用现有权限、版本、幂等、库存流水、
  操作日志和三楼库位移动门禁。
- 迁移链已线性接到当前正式候选：
  `cp72v8x9z61 → cj66v8x9z55 → ck67v8x9z56 → cm69v8x9z58 → cn70v8x9z59`，
  Alembic 只有一个 head `cn70v8x9z59`。工厂派生 SQLite 的再次复制件
  `D:\tm-uat\n081_field_stocktake_simplification_20260724\factory_replica_ci65_to_cn70_uat_v2.sqlite3`
  分步演练 `ci65 → co71 → cp72 → cn70` 后 `integrity_check=ok`、外键异常 0，
  关键业务表行数与升级前一致。迁移前备份 SHA-256 为
  `CA7E5889892D7E3B2D7DBBC759D1928BC53E6503DFACABD6148C4F774D79DD1F`；
  升级完成、写入隔离测试账号前的副本 SHA-256 为
  `A8FBDC0D5C02C16C773DA19F3B1240E5B393B78E920E73C20BC5E93994B63E48`。
- 隔离浏览器 UAT 为 `http://127.0.0.1:18096/`，账号 `codex_uat`、密码
  `123456`。基于 110 条真实成品库存的导出表重新上传后，页面显示“检查完成”：
  本次盘点 110 条、已识别已有库存 110 条、可直接新建 0 条、需要处理 0 条；
  没有重复创建库存，浏览器 Console 无 error/warn。可导入工作簿及截图仅保存在
  worktree 的 `outputs\n081-field-stocktake-simplification-20260724`，包含真实库存
  参考数据，不纳入 Git 提交。
- 自动验证共 `184 passed`：库存盘点 API/服务/前端及小批入账 61 个，三楼库位、
  库存语义、生产余货、半成品和仓库基础保护回归 113 个，迁移 10 个。Python
  编译、仓库页内联 JavaScript `node --check`、Alembic 单 head 和
  `git diff --check` 均通过。
- 家庭侧只写入隔离 UAT 副本中的测试账号、盘点上传记录和自动检查结果，没有点击
  “确认盘点入库”，没有连接、迁移或写入工厂正式数据库。用户已于 2026-07-24
  确认人工 UAT 通过；实现提交
  `724fb2882b8741dbca2b8d161642879b8fd817e6` 已推送到远端候选分支
  `codex/n081-field-stocktake-simplification-20260724`，未更新正式基线。
  将来工厂发布需要重新核对候选最终 SHA、备份并验证正式库、在备份副本演练
  `cp72 → cn70`、获得明确发布授权后迁移并重启 ERP。

## 2026-07-24 组合 BOM 合并报料先审明细、再生成正式采购单

- 本轮继续在独立 worktree
  `D:\tm-worktrees\erp-composite-business-modes-20260724` 和候选分支
  `codex/composite-business-modes-20260724` 工作；没有更新
  `origin/factory-current-baseline`、`origin/main` 或工厂正式目录。
- 根因是组合 BOM 路径绕过了普通报料草稿：点击“合并报料”后直接二次确认、
  调用正式批次接口并打开打印页。现在统一改为先打开“报料明细草稿”，同时列出
  父件和已选择组件；页面核对库存抵扣、采购张数、尺寸和开料方式后，只有点击
  “确认生成正式采购单”才正式入账并进入打印页。
- 草稿中的采购张数默认等于库存抵扣后的系统最低值，允许按供应商实际要求增加。
  如需减少，用户先点击组件行“自动使用匹配库存”；系统只使用既有同客户、
  同组件、同规格库存门禁，更新成品/半成品抵扣和剩余需求后自动重算最低张数。
  后端再次校验确认数量不得低于当前最低值，不能靠前端改数隐藏缺口。
- 隔离浏览器 UAT 仍使用
  `D:\tm-uat\composite-business-modes-20260724\browser-uat.sqlite3` 和
  `http://127.0.0.1:18095/`。实测点击“合并报料”只打开草稿，显示父件
  3000 张和内衬组件 1350 张、库存匹配入口及最终确认按钮；没有新建正式采购单，
  没有打开打印页，Console 无 error/warn。截图：
  `D:\tm-uat\composite-business-modes-20260724\screenshots\03-composite-requisition-review-draft.png`。
- 组合计价、订单快照、BOM 报料、前端语法和库存相关回归为
  `36 passed`；Python 编译、Alembic 唯一 head `cp72v8x9z61`、
  `git diff --check` 均通过。隔离 UAT 库 revision 为 `cp72v8x9z61`，
  `quick_check=ok`、外键异常 0；仅保留原有 2 张“已取消”测试报料单。
- 本轮不新增迁移。家庭侧只读取和操作隔离 UAT；没有连接、复制、迁移或写入
  工厂正式数据库。用户已于 2026-07-24 确认本轮人工 UAT 通过，并授权推送
  `codex/composite-business-modes-20260724` 供工厂 Codex 同步。工厂正式发布
  仍需按门禁核对候选 SHA、备份验证、升级 `co71v8x9z60 → cp72v8x9z61` 并
  重启 ERP；家庭侧不得代替工厂执行正式迁移或更新正式基线。

## 2026-07-24 组合产品两种计价模式与组件库存自动抵报料候选

- 上一阶段组合 BOM 报料修复已在家庭人工 UAT 通过后，将提交
  `7792f6f246a1ef8428beebebd0d8075e91ee9f4c` 快进推送到
  `origin/factory-current-baseline`；家庭侧没有部署、迁移或写入工厂正式库。
- 本轮从该唯一远端基线建立新的独立 worktree
  `D:\tm-worktrees\erp-composite-business-modes-20260724` 和分支
  `codex/composite-business-modes-20260724`。本候选尚未更新正式基线，
  禁止在统一人工验收前直接发布。
- 常用箱新增两种明确模式：
  `parent_priced_set`（整套统一计价，默认且推荐）继续只生成一个父件销售明细，
  组件仅作内部 BOM；`component_priced`（组件分别计价）在新订单选中组合产品时
  自动展开为真实组件销售明细，每个组件可分别填写数量和单价，并独立参与送货、
  回单和对账。订单明细冻结组合父件、BOM 组件、分组、每套比例和计价模式，
  修改常用箱不回写历史订单。
- 整套统一计价仍允许订单级调整某个组件需求，不改父件套数和 BOM 模板。
  待报料组件行直接显示订单需求、客户专用成品库存、半成品库存、剩余需求和
  理论报料张数；用户只需点击“自动使用可匹配库存”，不要求填写原因或再次确认。
- 自动抵扣严格限于同客户、同组件和同规格签名：先使用正式成品库存，再使用
  客户专用半成品库存；不同客户和不匹配规格不会静默使用。无合适库存时只提示，
  不创建空需求或空草稿。成品与半成品使用统一覆盖量门禁，禁止重复占用；
  已有覆盖、生产或送货事实时，组件需求不能下调到事实数量以下。
- 新迁移 `cp72v8x9z61` 线性接在 `co71v8x9z60` 后且为唯一 head。它增加产品
  计价模式、订单明细组合来源快照，并将半成品需求唯一性拆为普通订单明细与
  BOM 组件两类局部唯一索引。downgrade 在存在组件分别计价、组合来源订单事实
  或 BOM 组件库存需求时 fail-closed。
- 隔离迁移目录：
  `D:\tm-uat\composite-business-modes-20260724`。工厂派生副本的再次复制件从
  `co71v8x9z60 → cp72v8x9z61` 后 `integrity_check=ok`、外键异常 0、逐表行数
  与升级前一致；迁移前副本 SHA-256 为
  `FDD5E2F4035F5055A6A8DC69EC5AF3D4CBD6CE173AC3B9B8C1BFBACECA639B`。
  空新版事实副本可降回 `co71`；含新版模式事实的副本降级被正确拒绝。
- 浏览器 UAT 使用同目录 `browser-uat.sqlite3`，账号 `admin`、密码 `123456`，
  服务为 `http://127.0.0.1:18095/`。页面已验证整套模式与组件分别计价选项、
  父件 3000 张、订单专用内衬 2700 个按一开二换算 1350 张、库存覆盖栏及
  一键自动匹配；无匹配库存时保持 2700 且没有新增库存需求。Console 无
  error/warn，截图在同目录 `screenshots`，不提交仓库。
- 定向 API、服务、迁移、前端静态契约及内联 JavaScript 共 `31 passed`；
  Python 编译、Alembic 单 head 和 `git diff --check` 均通过。扩大回归中的
  3 个失败可在未改动基线复现，分别为一个旧索引名称断言和两个缺少必填库位的
  旧生产测试，不由本候选引入。
- 本候选正式发布时需要先对工厂库做一致性备份，在备份副本演练
  `co71 → cp72`，得到明确发布授权后迁移并重启 ERP。家庭侧只写入隔离 UAT
  测试数据，没有连接、复制、迁移或写入工厂正式数据库。

## 2026-07-24 组合 BOM 订单专用组件数量、报料展开与整批作废候选

- 独立候选从
  `origin/factory-current-baseline@f97bb5014986422155c975c323a6121aab7a6204`
  建立；worktree=`D:\tm-worktrees\erp-bom-requisition-demand-hotfix-20260724`，
  branch=`codex/bom-requisition-demand-hotfix-20260724`。家庭侧未修改
  `origin/factory-current-baseline`、`origin/main` 或工厂正式目录。
- 根因有三处：订单 BOM 快照只有“按套数变更”的读取口径，无法表达客户要求
  某组件少送的订单级件数特例；待报料生成把父件与组件折叠成同一结果，并在开料
  换算中误用订单主项数量/`pieces_per_box`；组合 BOM 生成后的正式报料批次又被
  “已报料”页面统一标成“旧报料单”，没有保留来源事实的整批作废入口。
- 常用箱/BOM 模板继续保持“每套 1 个内衬”。新订单仍冻结父件套数和模板比例；
  本订单把内衬从 3000 调为 2700 时，只追加订单级组件需求事件，不改模板、不改
  其他订单。已有有效正式报料历史时禁止静默重算，并给出中文受控撤销提示。
- 报料展开现在同时保留父件与组件两条不可混淆的来源：父件 3000 个、
  470×600、一开一、3000 张；内衬 2700 个、575×550、一开二、每张产出 2 个、
  `ceil(2700/2)=1350` 张。待报料按当前订单和不可变 BOM 来源过滤；重复提交不会
  重复生成来源。
- 刀卡与模切内盒、隔板统一支持一开一至一开五；新订单冻结常用箱默认值，修改
  常用箱不回写旧订单或旧报料。A1、A3 等无关箱型仍强制一开一，不复用
  `pieces_per_box`。
- “已报料/已入库”将新组合批次标为“组合 BOM 报料单”。在尚未入库且明细仍有效
  时，具有报料权限的用户可一次确认后整批作废；主单、父件、组件和不可变来源均
  保留为“已作废”历史，订单自动回到待报料。重复作废幂等；已有入库事实时拒绝。
- 新迁移 `co71v8x9z60` 线性接在 `ci65v8x9z54` 后且为唯一 head。它冻结组件开料
  方式，并记录报料来源是“按订单套数”还是“订单专用件数”；旧事实默认保持原口径。
  downgrade 在存在新版快照或订单专用件数事实时 fail-closed。
- 隔离迁移演练副本：
  `D:\tm-uat\erp-bom-requisition-demand-hotfix-20260724\migration-rehearsal.sqlite3`；
  `ci65v8x9z54 → co71v8x9z60` 后 `integrity_check=ok`、外键异常 0。迁移前备份
  SHA-256 为
  `2FE72AD9ED58FA3496F2566F74D5FDE1D4318A32E329213783618AD2E4E1BF47`。
- 匿名浏览器 UAT 使用
  `D:\tm-uat\erp-bom-requisition-demand-hotfix-20260724\browser-uat.sqlite3`，
  账号 `admin`、密码 `123456`。页面实际完成“查看 3000/2700/1350 → 生成组合
  报料 → 来源识别 → 整批作废 → 父件与组件回到待报料”；最终 revision 为
  `co71v8x9z60`、完整性 `ok`、外键异常 0，Console 无 error/warn。截图保存在
  同目录 `screenshots`，不提交到仓库。
- 扩大自动回归为 `136 passed, 1 deselected`；最终幂等加固后的订单与报料回归
  `77 passed`，旧组件单兼容与前端专项 `9 passed`。排除项是当前基线已有的移动
  入库 PDF 路径表现断言，与本候选无关。另行完成内联 JavaScript `node --check`、
  Python 编译、Alembic 单 head 和 `git diff --check`，均通过。
- 本候选需要在正式发布后重启 ERP；正式数据库需要由工厂 Codex 先备份并验证，
  再在备份副本演练 `ci65 → co71`，取得明确授权后才迁移和重启。家庭侧没有连接、
  复制、迁移或写入工厂正式数据库。

## 2026-07-24 送货单客户缩写编号与针式打印版式（人工 UAT 已通过）

- 当前独立候选从工厂正式基线
  `f11d616f419b0bbf9ea47bfa68b41cfd9aedb53e` 建立：
  worktree=`D:\tm-worktrees\erp-delivery-print-f11d-clean-20260724`，
  branch=`codex/delivery-print-f11d-clean-20260724`。旧候选误从 N081
  小批确认入账提交 `99b223a6e2493b77695b1fe1c088c62a55233386`
  建立，导致 N081 迁移通过祖先链混入；旧候选不得用于工厂发布。
- 本候选仅重新应用送货单客户编号、针式打印版式和定向测试，不包含
  N081-B0/B1/B2 的模型、服务、页面、报告或测试，不新增或修改 Alembic
  migration。用户已于 2026-07-24 完成送货单页面和打印预览人工验收，
  并授权门禁通过后提交、合并和推送；未修改 `origin/main` 或工厂正式目录。
- 新送货单统一使用
  `{客户资料中的 customer_code 大写}-{YYYYMMDD}-{当日三位全局流水}`；
  例如天华超净为 `TH-20260724-001`。天华预送货与普通送货复用同一生成服务；
  历史 `TM-*`、`DH-*` 不改写。
- 编号前会拒绝大小写归一后重复的客户缩写，并提示在客户资料中分别配置
  `TH/THC/THX` 等唯一代码；空代码稳定回退到 `KH{客户ID}`，前缀过长在消耗
  流水前阻断。隔离副本 132 个客户只读审计结果为：空代码 0、超过 27 字符 0、
  大写归一后冲突 0；其中正式现有代码已经区分天华超净 `TH` 和天华新能源
  `THXN`。
- 针式打印页删除客户资料、数量区、联次说明和普通签名项的不必要横线；签名区
  调整为 `送货人 → 经手人 → 收货单位(签章)` 同一行，仅收货单位保留签章横线。
  打印字体整体放大一级，短单据页脚固定在纸张底部。
- 分页测量和打印均使用 241 × 139.5 mm 纸张及相同有效高度。实际 PDF 验证：
  1 行单据为 1 页；历史 11 行长备注单据为 6+5 两个有效页面，均无底部裁切或
  额外空白页。视觉证据及对比保存在
  `D:\tm-uat\delivery_print_customer_prefix_20260724\evidence`，不得提交。
- 无数据库结构变更，Alembic head 完全继承 `f11d616`。家庭 UAT 仅使用
  `D:\tm-uat\delivery_print_customer_prefix_20260724\delivery_print_uat_backup.sqlite3`；
  测试创建待发货送货单 ID 10，没有确认发货，不影响工厂正式库。
- 家庭端重拆后核验：候选父提交精确为 `f11d616f419b0bbf9ea47bfa68b41cfd9aedb53e`；
  `alembic/versions` 差异为空，唯一 Alembic head 为 `ci65v8x9z54`；
  N081-B0、B1 和小批确认入账提交均不是本候选祖先。送货单打印页面、客户编号服务
  和天华预送货相关运行文件与已人工验收版本的 Git blob 完全一致；唯一不一致的
  `app/api/deliveries.py` 仅删除旧候选祖先带入的 N081 FIFO 排序改动。
- 自动验证：客户编号与针式打印专项 `14 passed`；送货回归在排除
  `f11d616` 本身即可复现的旧超量送货契约断言后 `23 passed, 1 deselected`；
  天华预送货 `1 passed, 1 skipped`。相关 Python 编译、打印页内联 JavaScript
  语法检查和 `git diff --check` 均通过；未连接或写入工厂正式数据库。
- 最新 UAT 服务：
  `http://127.0.0.1:18091/static/delivery-print.html?id=10`；
  账号 `n081_post_uat`，密码 `123456`。用户已确认送货单验收通过；本次授权仅含
  独立候选提交和推送，工厂正式发布仍须另行执行备份、候选核对和发布授权。
## 2026-07-24 N081-B2 收缩版当前状态（最新权威）

- 用户已验收旧 B2 试盘界面，但明确要求不把大厂式逐行勾选、确认词、原因输入、二次确认和专用回滚流程带入正式使用。旧 B2 分支与提交 `2466e3360c08468adf5b716034467e58f236d1c0` 只保留为技术证据，不推送、不部署。
- 当前候选从 B1 精确提交 `a1ec412891756a609de5bb19a1a3cc3ee841d1d4` 新建独立 worktree `D:\tm-worktrees\erp-n081-small-batch-posting-20260724` 和分支 `codex/n081-small-batch-posting-20260724`，没有合并旧 B2 分支。
- 收缩后的现场操作只有一个动作：有盘点复核权限的人员打开已提交批次，核对页面现有逐行明细和汇总后点击“正式入账（N 行）”。系统自动处理该批次全部 `create_new + ready` 行；不选行、不填原因、不输入确认词、不弹二次确认，也不限定固定的成品/半成品栈板数量。
- 精简仅发生在操作层。后端仍强制复核私有源文件 SHA-256、冻结批次版本和指纹、客户/产品/材质/库位/栈板事实、单一区域、最多 100 行、完整栈板组、库位占用和来源唯一性；整批单事务，任一行失败则全部回滚，重复请求返回同一入账结果而不重复建库存。
- 正式库存继续复用现有成品/半成品入库服务、库存流水、栈板绑定和操作日志；不建立第二套库存余额。入账后的日常纠错走现有库存调整流程，不新增 B2 专用回滚页面或 API。
- 新迁移 `cm69v8x9z58` 线性接续 `ck67v8x9z56`，当前 Alembic 只有一个 head。隔离演练副本已完成 `ck67 → cm69`，`integrity_check=ok`、外键异常 0；只读源副本 SHA-256 为 `E6588578879DD8A0192BDFCF6733F36AD53815C8A907044722C9205536CCF59F`，迁移副本 SHA-256 为 `30E9C2BD51CD43318CBB079CEE725F8AF8EC4A90AD889F801EA921A29354F455`。
- 人工 UAT 使用上述迁移副本的再次复制件，服务为 `http://127.0.0.1:18089/warehouse.html`，账号 `n081_post_uat`，密码 `123456`。用户已点击同一 E1 区域的 3 行批次；后端一次成功生成 1 份入账回执、3 个正式库存批次、3 个栈板和 3 条入库流水，`integrity_check=ok`、外键异常 0。首次成功响应中的 `posted_at` 未按项目 RFC3339 规则携带时区，导致前端把成功结果误报为红字失败；现已统一使用 `utc_naive_to_api()` 并增加 API 回归。修复后页面正确显示“已正式入账”、3 行、3 个栈板、3 个正式库存批次和北京时间，按钮隐藏，Console 无 error/warn。
- 用户已确认修复后的成功页人工 UAT 通过，允许将当前候选形成独立提交并只推送 `codex/n081-small-batch-posting-20260724`。最终有效回归为 `169 passed`；Python 编译、JavaScript 语法检查和 `git diff --check` 通过，只读提交前审查未发现阻断性正确性、安全或数据完整性问题。工厂发布仍须按候选 SHA、备份、隔离迁移演练、明确授权、停服迁移和启动复核顺序执行；家庭侧没有连接、迁移或写入工厂正式数据库，也没有修改 `origin/main`。
## 2026-07-23 当前统一总需求与执行状态（权威快照）

### 文档用途与优先级

- 本节是当前继续开发、家庭 UAT 和工厂发布的统一入口；下方按日期保留的内容是历史证据，不能覆盖本节的当前事实。
- 发生冲突时依次服从：用户最新明确指令 → `天明ERP_Codex安全收口与统一总任务书执行提示词_20260722.md` → `天明ERP_当前生产保障优化_统一Codex总任务书_20260722.md` → 本文件历史记录。
- 系统定位继续是“三级纸箱厂状态流转 ERP”：订单、报料、来料、生产、送货、回单、对账、开票、收款优先；库存只做支撑现场真实数量和上述闭环所必需的范围，不扩展为复杂多仓、复杂财务或大型生产排程。

### 当前正式事实与安全边界

- 唯一正式远端基线：`origin/factory-current-baseline@aebf9b23d3200840395baf29202491bfbfa97414`。其中工厂正在运行的功能代码为 `a3001568c56b941a3c0c0935f2fa51ddc6c44f85`，`aebf9b2` 是正式发布记录提交。
- 工厂主机：`PC-20250926DZYH`；正式目录：`D:\纸箱厂erp软件搭建`；正式数据库：`D:\纸箱厂erp软件搭建\data\carton_erp.sqlite3`；正式 Alembic：`ch64v8x9z53`。
- 家庭电脑只使用工厂数据库副本的再次复制件进行迁移、造数和页面验收。测试账号密码统一可用 `123456`，但仅限隔离 UAT；不得修改、复用或推断工厂正式账号密码。
- 家庭侧只交付 `codex/` 候选分支、完整提交 SHA、迁移 revision、测试和 UAT 证据。工厂 Codex 才能在显式授权后执行备份校验、停服、正式迁移、重启和发布后复核。
- 禁止家庭侧连接、写入、迁移、替换或覆盖工厂正式数据库；禁止修改 `origin/main`、强推、覆盖正式目录、删除安全分支或清理工厂保留的 stash。

### 当前不可变业务规则

- 盘点/栈板快照不是正式库存，不能直接抵扣订单；必须通过受权限、版本、幂等和二次确认保护的现有转换流程形成正式批次。
- 已形成正式库存批次的真实货物即使栈板仍显示“待归位”，仍可按客户/产品规则抵扣和送货；“待归位”只是位置复核状态，不得把真实货物当作不存在。
- `placement_status=unplaced` 的库位保留在台账，但不能新入库存、不能承载当前栈板、不能发起盘点，也不能出现在生产入库候选；正式 `placed` 库位和空位绑定继续使用 N081 单一空间事实。
- 订单数量是客户需求，不因来料超收、生产多产或超量送货被改写。来料实收、实际投入、计划产出、合格产出、损耗、订单覆盖和客户余货必须分别留存。
- 订单 200、实收并投入 203、合格 203 时，只为订单预占 200，余下 3 形成同客户、同产品的正式成品库存。其他客户不能使用客户专用余货。
- 普通送货默认不超过订单待送数量。超量送货必须有 `deliveries.over_delivery` 权限、二次确认、原因、客户专用余货和完整审计；回单、对账、开票沿用实际送货/签收数量。
- 常用箱 `default_cutting_mode` 与 `pieces_per_box` 独立；合法值为一开一至一开五。仅“模切内盒（兼容旧平卡）”和“隔板”显示默认开料方式；订单冻结快照，报料可改单但不回写常用箱。
- 一开二表示一张报料纸产出两个同款成品：100 个成品默认采购 50 张；报料改回一开一则为 100 张。不同产品的一模多出以后必须走 BOM/多产出流程。
- 当前页面性能基线必须保留本地固定版本 Vue/Axios/pinyin、按模块取数、30 秒缓存、搜索防抖/取消旧请求、`Server-Timing` 和脱敏慢接口日志；仓库页面保持在 ERP 外壳内切换。

### 发布与阶段状态

| 范围 | 当前状态 | 正式数据库 revision |
| --- | --- | --- |
| P0-A 启动/发布/备份/UAT 隔离门禁 | 已正式发布 | 不新增迁移 |
| P0-B 私有上传和统一文件校验 | 已正式发布 | 不新增迁移 |
| 快照转正式库存、常用箱开料方式 | 已正式发布并人工验收 | `cg63v8x9z52` |
| N081-0、三楼归位/空位绑定、N081-A1 | 已正式发布并人工验收 | `ch64v8x9z53` |
| 页面流畅度 P1 | 已正式发布并人工验收 | 不新增迁移 |
| 来料超收、生产余货、超量送货 P0 | 当前唯一进行中候选；家庭隔离 UAT 已复验，尚未发布工厂 | `ci65v8x9z54` |
| N081-B0/B1/B2 盘点语义、草稿与小批正式入账 | B0/B1 已完成；收缩版 B2 家庭人工 UAT 已通过，待候选推送与工厂发布 | `cm69v8x9z58`（候选） |
| N066、N080-B、N083、N082、N062-B | 按总任务书排队 | 未开始 |

### 当前唯一进行中候选

- 独立 worktree：`D:\tm-worktrees\erp-p0-production-surplus-rebase-20260723`；分支：`codex/p0-production-surplus-rebase-20260723`；基线精确为 `aebf9b23d3200840395baf29202491bfbfa97414`。
- 本轮不是合并旧分支，而是按补丁顺序重新应用 `2dcb40ad2a5d9b8cc8ff68d742fb4be5d07f12b4` 与 `ef0b4134d0282a6054ba037851a417c8195bf209` 的业务改动。
- 必须保留 `ch64v8x9z53`、N081-A 功能/报告/测试、页面性能中间件和 `static/vendor`。`ci65v8x9z54.down_revision` 已改为 `ch64v8x9z53`，目标 Alembic 只能有一个 head。
- Windows 检出会把 vendor JavaScript 的 LF 改成 CRLF，导致运行文件哈希与锁定清单不一致；新增 `.gitattributes` 将 `static/vendor/*.js` 固定为原始字节，不得通过修改测试期待值绕过。
- N081 集成复核补充门禁：明确标记 `unplaced` 的库位既不返回生产库位接口，也被生产入库服务拒绝；旧测试 fixture 的 `NULL` 仅作为迁移前兼容，不改变 `ch64` 正式库必须为 `placed/unplaced` 的事实。
- 隔离源副本：`D:\tm-uat\p0_production_surplus_rebase_20260723\source_verified\carton_erp_ch64_source_verified.sqlite3`，SHA-256 `89EF26FD9782506CAFD1BDDD5D832096AD330686DE1E8741A9CAF5473621A816`，revision=`ch64v8x9z53`、`integrity_check=ok`、外键异常 0。
- 迁移再次复制件完成 `ch64 → ci65 → ch64 → ci65`；每一步完整性为 `ok`、外键异常 0，现有 12 条生产完工记录没有被误判为历史余货。
- 页面 UAT 再次复制件：`D:\tm-uat\p0_production_surplus_rebase_20260723\working\carton_erp_p0_rebase_uat.sqlite3`；服务：`http://127.0.0.1:18087/`；账号 `uat_p0_admin`，密码 `123456`。
- 页面复验已完成订单 200、实收/投入/合格 203、A1-L05 入库、送货并发出 200。最终订单已送 200，正式批次消费 200，A1-L05 保留客户专用余货 3；页面显示“订单待送 200 / 可用成品 203 / 可超送 3”，仓库保持同一 ERP 外壳，Console 无 error/warn。

### 当前 N081-B 工程候选（不能越过 P0 独立发布）

- B0 独立 worktree：
  `D:\tm-worktrees\erp-n081-b0-inventory-semantics-20260723`；分支：
  `codex/n081-b0-inventory-semantics-20260723`；开发起点精确为上方 P0
  候选 `f11d616f419b0bbf9ea47bfa68b41cfd9aedb53e`。
- B0 新增 `stock_date_accuracy=exact/estimated/unknown` 与日期原文；
  `cj66v8x9z55` 线性接在 `ci65v8x9z54` 后。迁移使用 SQLite 原地
  `ADD COLUMN`，不重建库存大表；历史批次统一标记 `unknown`。
- 不明日期在库存列表与库存洞察中不显示伪造的精确天数；手工新增库存明确
  保存精确日期；仅修改数量、归属或库位不会把历史不明日期偷换为精确日期。
- 工厂 `ch64` 副本的再次复制件已完成
  `ch64 → ci65 → cj66 → ci65 → cj66`。最终 110 条历史库存全部为
  `unknown`，核心表计数和库存余额汇总不变，`integrity_check=ok`、外键异常
  0；insert/update 两个日期来源门禁触发器均存在，演练副本最终 SHA-256 为
  `A60437AA761300CB9DF006E27B66B5790C26039AFC7F2BA49EF0435DDF61B6D4`；
  只读源副本 SHA-256 仍为
  `89EF26FD9782506CAFD1BDDD5D832096AD330686DE1E8741A9CAF5473621A816`。
- B0 定向回归 `56 passed`，成品库存候选接口集成测试 `1 passed`；全部 SQL
  与 Python 内存 FIFO 路径均按日期可信度排序，未知技术日期最后使用。既有
  技术日期只有在日期被修改或操作员显式勾选确认后才会转为精确日期。实施证据：
  `docs/warehouse_reports/N081_B0_INVENTORY_SEMANTICS_IMPLEMENTATION_20260723.md`。
- B1 独立 worktree：
  `D:\tm-worktrees\erp-n081-b1-onboarding-dryrun-20260723`；分支：
  `codex/n081-b1-onboarding-dryrun-20260723`；开发起点精确为 B0
  `91a888777cdf5aee97c6ed5a83dc756524e23e98`。
- B1 新增私有 CSV/XLSX、持久草稿、原始行、精确匹配、修正、错误 CSV、
  dry-run 和提交冻结；没有 `/apply`，全过程不创建或修改正式 lot、流水、栈板、
  栈板明细或预占。`ck67v8x9z56` 线性接在 `cj66v8x9z55` 后。
- B1 对原本不存在的栈板、空闲库位、已有栈板明细和正式批次余额/详情执行
  dry-run 漂移复核；半成品必须核对完整物料与尺寸签名，且禁止误走仅支持成品的
  快照转换。客户/产品/材质 ID 与编码或名称冲突时阻断；提交前重新核对私有
  源文件存在性、大小和 SHA-256。客户编码可在草稿解释中单独修正，原始上传编码
  永久保留；修正、重新匹配、dry-run 和提交均留操作日志，排除行必须填写原因。
- 工厂 `cj66` 副本的再次复制件完成 `cj66 → ck67`，迁移副本 SHA-256 为
  `E6588578879DD8A0192BDFCF6733F36AD53815C8A907044722C9205536CCF59F`；
  成品和半成品私有 CSV 行均在流程再次复制件完成修正、dry-run 与冻结，正式库存
  五张核心表计数保持 `110 / 157 / 115 / 132 / 17`，提交后改写被触发器拒绝，
  `integrity_check=ok`、外键异常 0。成品快照还必须与盘点行数量、单位完全一致，
  否则专用错误阻断；同一新栈板跨库位或同一空库位出现多个新栈板也会在
  批次级阻断。B1 专项 `70 passed`，相邻受保护回归 `130 passed`。实施证据：
  `docs/warehouse_reports/N081_B1_INVENTORY_ONBOARDING_IMPLEMENTATION_20260723.md`。
- B1 继续只负责私有上传、持久草稿、匹配、修正、dry-run 和提交冻结。旧 B2
  的固定 `3～5` 个成品、`2～3` 个半成品、逐行勾选、确认词和原因输入方案已
  被 2026-07-24 最新指令取代；当前只允许已提交批次由有权限人员一键将全部
  合格行正式入账。工厂发布仍须独立授权，禁止家庭侧直接部署或写正式库。

### 固定 UAT 与工厂发布交付清单

1. 锁定基线、候选 SHA、迁移链和受保护功能，不用旧分支覆盖新正式基线。
2. 只在工厂副本的再次复制件上执行 upgrade、测试造数和浏览器 UAT；记录副本来源、SHA-256、revision、完整性和外键。
3. 运行定向服务/API/前端测试、受保护 N081/性能回归、Python 编译、内联 JavaScript 语法、vendor 哈希和 `git diff --check`。
4. 家庭验收后只推送独立 `codex/` 候选；不得直接改 `factory-current-baseline` 或 `origin/main`。
5. 工厂 Codex 先核对候选 SHA 和祖先关系，再创建并验证正式库备份，使用隔离副本演练迁移，取得明确授权后才停服 Apply。
6. 工厂发布后复核代码 SHA、数据库 revision、`integrity_check=ok`、外键异常 0、健康接口、登录与订单/报料/来料/生产/送货页面；失败即停在现场并按完整备份回滚。

### 当前 P0 完成后的顺序

1. 工厂发布并验收本次 `ci65` P0 数量闭环。
2. 恢复 N081-B0/B1：盘点草稿、dry-run、差异复核和小范围试盘；禁止直接全量改库存。
3. 依次推进 N066、N080-B、N083、N082-A、N062-B、N082-B；每阶段仍只做一个最小可验证闭环。
4. P0-C/P0-D/P1 剩余安全治理和约 950KB 首页物理拆包分别建独立分支，不混入业务候选。
5. 工厂局域网 HTTP、防火墙和 HTTPS 代理属于基础设施待办，继续记录风险，但不得反复阻断无关的家庭业务开发与隔离 UAT。

## 2026-07-23 客户双录单模式、订单 UI 与全局分页（待排期，未实施）

- 本节只保存已确认/待确认需求，不覆盖上面的 P0 与 N081 顺序；当前候选发布前禁止开始实现。
- A1 销售报价统一使用毫米口径：`(L + W + 80) × (W + H + 40) × 2 ÷ 1,000,000 × 客户平方价`，最终单价按客户配置的 `0–4` 位小数（默认 2 位）使用 `ROUND_HALF_UP`；客户平方价当前统一按含税 13% 口径保存，金额、对账和开票仍保持财务两位小数。
- 建议客户录单模式使用 `catalog/dimension_quote` 枚举，并维护“业务名称（外箱/内盒/衬板）→ 结构箱型（A1/隔板等）→ 受控公式代码/平方价”的报价档案；订单必须冻结规则版本、平方价、原始计算价、小数位和最终价格来源。
- 单价优先级：本单人工修改价 > 已匹配常用箱销售价 > 客户箱型公式自动价。选中常用箱后修改编码、名称、规格或价格，保存前明确选择“仅本订单 / 覆盖常用箱 / 另存新常用箱”；默认仅本订单，覆盖要求版本与原因，另存重复编码必须拒绝。
- 新建订单常用箱查找使用“存货编码 / 产品名称 / 规格”三个输入框联动；任一框选中候选后回填另外两项，下拉脱离横向滚动容器。材质隐藏全部克重；供应商仅做显示别名，不改标准名或历史快照。
- 常用箱后续恢复“压线类型 + 压线尺寸”同行，并缩小数量、客户单价和图纸列；不得破坏已经验收的默认开料方式显示规则。
- 客户合同 N041 的已验收实现位于旧独立提交 `9cce9fa`；以后必须在正确的新正式基线上恢复并回归，不能重新实现第二套。
- 订单、送货单、对账单和开票记录已有服务端分页；待报料、已报料汇总、待入库、今日实收、历史入库、待生产和完工历史后续统一 `page/page_size/keyword/customer_id/inventory_code/date_from/date_to/status/sort_*`，筛选变化回第一页，批量全选只作用当前页。
- 送货与回单主列表后续改为摘要 + “查看明细（N）”，详情表格独立显示存货编码、产品名称、发货数量、签收数量和差异；原则上不需要数据库迁移。
- 待当前 P0 与 N081-B 阶段边界确认后，建议顺序为：恢复 N041 → 客户录单模式/报价预览 → 双模式订单与搜索 UI → 仅本单/覆盖/另存原子保存 → 送货回单明细 → 历史增长列表分页。

## 2026-07-23 N081-A1 + 页面流畅度 P1 工厂正式发布完成

- 用户明确确认 N081-A1 与页面流畅度 P1 均已完成人工验收，并授权一起发布。远端 `factory-current-baseline` 使用旧 SHA `7d8dc5e87a25b7c2632ef76ff4eb052f1dc4e0a6` 租约快进到候选 `a3001568c56b941a3c0c0935f2fa51ddc6c44f85`；`origin/main@9be70622683535f476a353860cf1f2cb32b399b0` 未修改。
- 工厂主机为 `PC-20250926DZYH`，正式目录为 `D:\纸箱厂erp软件搭建`。发布前服务使用该目录、`0.0.0.0:8000`、单 worker，健康接口为 200；正式代码从 `7d8dc5e` 严格 `--ff-only` 到 `a300156`，没有普通 merge、reset、强推或文件覆盖。
- 两阶段发布报告为 `docs/migration_reports/release_runtime_20260723_160149.json`。SQLite Backup API 备份为 `data/backups/carton_erp_before_release_20260723_160151.sqlite3`，大小 `219,447,296` 字节，SHA-256 `88EBC249768863442F6F0D37277C229F3EAE17C105B371CC4B0218B652839A61`，revision=`cg63v8x9z52`、`integrity_check=ok`、外键异常 0。停服后的正式源库 SHA-256 为 `1668BB535B4059CDCC262444124937F6104023EA322C1567449FDBFD4B97D379`；Backup API 重写 SQLite 页布局，因此源库与备份字节哈希不同。
- 隔离演练副本为 `data/release_rehearsals/carton_erp_release_rehearsal_20260723_160151.sqlite3`，已成功从 `cg63v8x9z52` 精确迁移到 `ch64v8x9z53`，`integrity_check=ok`、外键异常 0。源、备份、演练的 15 张核心业务表计数完全一致后才执行正式 Apply。
- 正式库已从 `cg63v8x9z52` 迁移到 `ch64v8x9z53`；迁移后 `integrity_check=ok`、外键异常 0，400 个库位中 `398 placed / 2 unplaced`。正式迁移后的 15 张核心业务表计数与迁移前完全一致，没有新增、删除或修改订单、报料、来料、送货、财务、用户或操作日志业务行。
- 发布后 ERP 已恢复为正式目录、`0.0.0.0:8000`、单 worker；`/api/health` 返回 200 `{"ok":true}` 并带 `Server-Timing`。首页及全部本地 Vue/Axios/pinyin/time-utils 静态资源均返回 200，首页无外部 CDN 依赖。浏览器在现有管理员会话中确认首页正常、订单模块成功切换并显示数据，Console 无 error/warn。
- 本地安全分支 `factory-local-handoff-20260722`、`factory-local-baseline-before-update-20260722` 与 `stash@{0}` 均继续保留，未 pop、drop 或清理。

## 2026-07-23 页面流畅度专项 P1 | 依赖本地化、按需取数与短时缓存（隔离 UAT 待人工验收）

- N081-A1 已形成独立提交 `0ab7d10` 并推送 `origin/codex/n081-a-space-readiness-gate-20260723`。页面流畅度专项另建 worktree `D:\tm-worktrees\erp-frontend-performance-p1-20260723` 和分支 `codex/frontend-performance-p1-20260723`，基线为 `0ab7d10`，没有混入 N081 新业务规则。
- 首页 Vue `3.5.40`、Axios `1.18.1`、pinyin-pro `3.26.0` 已改为仓库内固定版本静态资源，记录来源、许可证和 SHA-256，并对 `/static/vendor/` 返回一年 immutable 缓存；首页不再产生公网 CDN 请求。
- 登录后的全模块 `loadBase()` 已删除，改为只加载当前模块数据；已访问页面使用 30 秒短时缓存，顶部“刷新”强制重新取数。客户、常用箱、订单搜索使用 300ms 防抖，列表请求用 `AbortController` 取消旧请求，首次客户列表仍限制 25 条。
- 新增性能观测中间件：响应包含 `Server-Timing`；超过阈值的 `/api/` 只记录方法、路径、状态和耗时，不记录查询值、请求体或响应体。默认阈值 500ms，可由 `ERP_SLOW_REQUEST_MS` 调整。
- 隔离 UAT 再次复制件为 `D:\tm-uat\frontend_performance_p1_20260723_153042\carton_erp_uat.sqlite3`，创建时 SHA-256 `8226E12DC27495C9182E20BC210BD24CE56AF8D08666BD983F9DD5D89D7C1613`、revision `ch64v8x9z53`、`integrity_check=ok`、外键异常 0；没有执行迁移。服务地址 `http://127.0.0.1:18085/`，账号 `codex_uat`，密码 `123456`。
- 浏览器禁用缓存冷刷新实测 `121.6ms`（工具侧完整导航 `133ms`），首屏公网请求 0；登录状态下首页只请求身份、概览和 KPI。30 秒内返回客户模块绘制约 `19ms` 且不重复请求；手动刷新会重新取数，搜索等待 300ms 后只发 1 次请求；Console 无 error/warn。
- 自动验证：专项与 Phase 12 `57 passed`；扩大前端组合 `344 passed, 18 skipped, 1 deselected`；5 个旧失败均在未修改基线 `0ab7d10` 原样复现。Python 编译、内联 JavaScript 语法、vendor 运行契约和 `git diff --check` 通过。详细报告：`docs/performance_reports/FRONTEND_PERFORMANCE_P1_20260723.md`。
- `static/index.html` 的约 950KB 物理拆包尚未纳入本最小闭环；当前局域网指标已经达标，待 P1 人工验收后再在独立阶段逐模块拆分。工厂正式数据库和正式目录未被本轮连接、迁移、替换或写入。

## 2026-07-23 N081-A1 | 库位空间放置就绪门禁（隔离 UAT 已通过）

- 已按用户验收结论将 `7d8dc5e87a25b7c2632ef76ff4eb052f1dc4e0a6` 快进推送到远端 `factory-current-baseline`；未修改 `origin/main`、未强推。随后从 `origin/factory-current-baseline@7d8dc5e` 建立独立 worktree `D:\tm-worktrees\erp-n081-a-space-readiness-gate-20260723` 和分支 `codex/n081-a-space-readiness-gate-20260723`。
- 本轮只做 N081-A 的最小空间主数据闭环：为库位增加显式 `placement_status=placed/unplaced`，未放置库位仍保留在台账中，但不能入库存批次、不能承载当前栈板、不能进入盘点库位候选；没有新建第二套库存余额，也没有开始 N081-B1 盘点导入。
- 新迁移 `ch64v8x9z53` 线性接续 `cg63v8x9z52`。迁移把既有完整空间位标记为 `placed`，把 `C1-R12/C1-R13` 补入三楼 C1 固定地面位并扩展右侧布局为 13 格；`SF-TEMP` 保持显式 `unplaced`。数据库触发器防止正式库存、当前栈板落入未放置/停用库位，并阻止仍有引用的库位被改为未放置。C1-R12/R13 一旦承载库存、栈板或人工布局，降级将 fail-closed。
- 库位台账新增“存储方式”，并显示“已放置”或“未放置，禁止入库”；成品/半成品入库选择器过滤未放置库位。N035 盘点后端同样过滤未放置库位并返回专用 409。N081 只读报告现在区分 `location_unplaced` 与主数据损坏，停用历史行不再计为待整改活动库位。
- 隔离迁移探针使用 `D:\tm-uat\n081_floor3_placement_20260723_125337\carton_erp_uat.sqlite3` 的再次复制件；复制前后 SHA-256 均为 `8B475C78A793BA5C5C130CDE8D19BF22922A4B40B70BB521398C12DC733836AC`。升级后 `398 placed / 2 unplaced`、`quick_check=ok`、外键异常 0、C1 右侧布局 13 格。
- 可人工验收的再次复制件位于 `D:\tm-uat\n081_space_readiness_20260723_145244\carton_erp_uat.sqlite3`，已从 `cg63v8x9z52` 升级到 `ch64v8x9z53`；本机 UAT 服务为 `http://127.0.0.1:18084/warehouse.html`，账号 `codex_uat`，密码 `123456`。浏览器已确认 C1-R12/R13 均出现在 C1 平面图和入仓候选，SF-TEMP/3D00001 在台账显示“未放置，禁止入库”且不出现在入仓候选；Console 无 error/warn。
- 自动验证覆盖迁移升级/安全降级/降级阻断、数据库触发器、入库服务、盘点 API、库位 API、只读报告和前端 JavaScript，共 `81 passed`；Python 编译、Alembic 单 head 和 `git diff --check` 均通过。警告仅为既有 `datetime.utcnow()` 弃用提示。
- 用户已确认 N081-A1 人工验收通过，同意进入独立提交与推送阶段。本轮未连接或写入工厂正式数据库；性能优化必须另建分支，不得混入本提交。

## 2026-07-23 P0 UAT 验收优化 | 生产库位两级选择与仓库内嵌

- 本轮继续使用 `D:\tm-worktrees\erp-production-overreceipt-surplus-delivery-20260723` 和 `codex/production-overreceipt-surplus-delivery-20260723`，只在 P0 家庭隔离 UAT 副本验证；没有连接、迁移、替换或写入工厂正式数据库，没有新增 Alembic migration。
- 生产确认原先把所有三楼空库位放在一个长下拉框内，区域很多时难以选择。空库位接口现在返回 `area_code`；待生产入库和完工历史“转入成品库存”都改为先选区域、再只显示该区域的空库位，区域切换会清除旧库位，未选完整时禁止提交。
- 仓库库存管理原先是独立 `warehouse.html`，主 ERP 通过整页跳转进入，返回 `/` 时 Vue 主应用会重新初始化，所以看起来像另一个网站并丢失首页/模块状态。现在仓库首次进入时才在 ERP 主框架内同源加载，切到来料、生产等模块时保留仓库当前标签、筛选和页面位置；仓库独立标题栏在内嵌模式隐藏，旧书签直接访问 `/warehouse.html` 仍兼容。
- 生产历史“定位库存”继续携带批次和库位深链，但先检查 `warehouse.view` 权限，不再绕过主菜单门禁。退出登录会卸载仓库页面，避免换账号后保留旧 DOM。
- 生产安全响应头默认仍为 `X-Frame-Options: DENY`；只有精确的 `/warehouse.html?embedded=1` 改为 `SAMEORIGIN`，并同时返回 `Content-Security-Policy: frame-ancestors 'self'`。因此工厂生产配置可以同源内嵌，外部站点仍不能嵌入 ERP。
- 浏览器 UAT 已确认：生产历史先显示“先选区域”，选 A1 后库位只剩 `A1-R03/A1-R04`；仓库页保留 ERP 顶栏和左侧菜单，独立“返回 ERP”不可见；切到“仓库来料入库”再返回后仍停留在仓库三楼视图，顶层地址保持 `http://127.0.0.1:18086/`，页面日志为空且服务端没有 500。
- 隔离副本仍为 `ci65v8x9z54`、`integrity_check=ok`、外键异常 0；源副本 SHA-256 复核仍为 `A9B0732506453BB86C7267395866B0ADE6EC07600D7F3BE0A06EBBCCA8D871FC`。本轮页面操作没有点击转库存、绑定、移动或其它业务写入按钮。
- 自动验证：生产、库存、仓库入口、权限、幂等和前端组合回归 `107 passed`；生产响应头及相关页面专项 `20 passed`；Python 编译、内联 JavaScript 语法和 `git diff --check` 均通过。

## 2026-07-23 P0 | 来料超收、生产余货与超量送货完整闭环（旧分支历史 UAT，已被当前权威候选取代）

- 独立 worktree：`D:\tm-worktrees\erp-production-overreceipt-surplus-delivery-20260723`；分支：`codex/production-overreceipt-surplus-delivery-20260723`；基线：`origin/factory-current-baseline@7d8dc5e87a25b7c2632ef76ff4eb052f1dc4e0a6`。本轮没有连接、迁移、替换或写入工厂正式数据库，也没有修改 `origin/main`。
- 根因是旧生产、送货和库存口径都以订单数量为上限：来料超收并选择“全部投入生产”后，生产任务、完工、可送数量和库存消费仍被订单数量截断，超出的实物没有稳定的库存、送货与财务事实。
- 旧分支当时把 `ci65v8x9z54` 接在 `cg63v8x9z52` 后；该迁移父级仅是历史 UAT 事实，禁止用于当前发布。当前权威链已在本文件顶部改为 `ch64v8x9z53 -> ci65v8x9z54`。生产任务与完工记录分别冻结订单数、实收投入数、计划产出、实际产出、损耗、订单覆盖和客户余货；主完工幂等，已有主完工后的补录仅管理员可执行并保留独立事实。
- 来料 203、订单 200、全部投入生产时，一开一计划产出 203；完工入库 203 后只自动预占订单需要的 200，余下 3 作为同客户、同产品的正式成品库存保留。固定货位与三楼临放位均可用于超出订单的实物入库；占用货位拒绝写入。
- 送货候选同时显示订单待送、实际可送成品和可超送数量。送 200 后订单完成、余货 3 保留；送 203 必须具备 `deliveries.over_delivery`、二次确认并填写原因，送货明细冻结订单数、实际送货数和超量数。其他客户不可使用客户专用余货。
- 回单、对账、导出和发票沿用实际送货/客户实收数量，并展示订单数量与超量数量；没有直接修改历史已过账生产或送货事实的入口，正式样本数据修复仍须另行授权和独立方案。
- 隔离 UAT 源副本：`D:\tm-uat\production_overreceipt_surplus_20260723\source_verified\carton_erp_factory_copy_verified.sqlite3`，SHA-256 为 `A9B0732506453BB86C7267395866B0ADE6EC07600D7F3BE0A06EBBCCA8D871FC`。只在再次复制的 working 和 migration rehearsal 数据库迁移及写入；迁移往返 `cf62 -> cg63 -> ci65 -> cg63 -> ci65` 后为 `ci65v8x9z54`、`integrity_check=ok`、外键异常 0。
- 页面 UAT 使用明俊德订单 `UAT-MJD-200-203-A`：订单 200、实收 203、全部投入生产；页面完工入库 A1-L05 后显示订单覆盖 200、客户余货 3。送货页面显示“订单待送 200 / 可用成品 203 / 可超送 3”，已通过页面生成并发出 200 件测试送货单 `TM-20260723-002`；最终订单已送 200、库存批次已消费 200、A1-L05 余货 3，库存流水完整。
- UAT 样本脚本 `scripts/uat/seed_p0_overreceipt_surplus.py` 强制隔离路径、revision、完整性和外键门禁，账号 `uat_p0_admin`、密码 `123456`。首次页面核对发现样本客户漏填客户编号导致客户列表 500；脚本已补充固定 UAT 客户编号并在幂等重放时自动修复旧样本，重新核对后明俊德客户和待送明细均正常出现。
- 自动验证：生产、库存、送货、财务和相关前端 `92 passed`；内联 JavaScript 语法 `3 passed`；Python 编译与 `git diff --check` 通过。隔离 working 数据库最终 `ci65v8x9z54`、`integrity_check=ok`、外键异常 0；源副本 SHA-256 复核保持不变。

## 2026-07-23 N081 试盘 | 三楼固定货位归位与空闲位绑定 UI（家庭隔离 UAT 已通过）

- 独立 worktree：`D:\tm-worktrees\erp-n081-floor3-placement-bind-ui-20260723`；分支：`codex/n081-floor3-placement-bind-ui-20260723`；基线：`b7281f4e1108b368ea66310841d2ab16b5823076`。本轮没有新增 Alembic migration，也没有连接、替换或写入工厂正式数据库。
- 工厂 N081-0 返回报告确认 DE1/E1 的 25 个固定货位栈板是实际物品，但多为 `semi_finished` 现场快照，而这些货位历史上标为 `finished`，所以旧逻辑把它们全部强制显示为红色待归位且禁止清除。新逻辑为固定货位增加“确认已归位”，要求登录权限、栈板版本和显式现场确认；F12/F34 等过道临放仍必须先移动到固定货位，不能直接确认。
- 业务口径已按用户补充冻结：“待归位”只表示位置待现场复核，不影响已经形成正式库存批次的订单抵扣和送货出库；尚未转为正式批次的盘点快照仍不能抵扣。专项测试覆盖快照不可抵扣、转正式批次后即使栈板仍待归位也能作为候选并成功预占。
- 空闲位详情精简为“当前位空闲 + 绑定货物”；绑定表单打开时替换详情卡，不再上下叠加。右栏和绑定面板移除嵌套滚动，字段、候选与当前货物标签均自动换行；当前货物完整显示客户、存货编码、订单、货物类型、数量和库存来源。
- 人工验收补充发现全局三楼平面图与区域聚焦页使用两条右栏渲染路径，导致全局点击 E1-R01 空位时只有空闲提示。现已让全局空位复用同一个绑定面板：点击“绑定货物”后表单直接显示在当前位置右栏，关闭或保存后安全归还原容器；没有增加第二套保存流程。
- 隔离 UAT 使用工厂副库的再次复制件 `D:\tm-uat\n081_floor3_placement_20260723_125337\carton_erp_uat.sqlite3`，复制前后 SHA-256 相同，revision `cg63v8x9z52`、`quick_check=ok`、外键异常 0。浏览器已验证 E1-L09 显示完整 22000022/300 张标签并成功“确认已归位”，DE1-P01 显示“确认已归位”，E1-R01 显示空闲绑定入口，22000022 候选可搜索和选择；右栏和绑定面板横向滚动宽度均为 0。
- 补充浏览器实测确认：从“全部区域”的全局平面图直接点击 E1-R01，右栏显示“当前位空闲 + 绑定货物”；表单标题为“绑定货物 · E1-R01”，可检索出天华 22000022 候选，表单父容器为全局详情栏且无横向溢出。本次补充验收没有点击保存、没有新增数据库写入。
- 自动验证：新增/相关 API 与前端场景 `7 passed`；全局空位绑定、共享面板关闭、标签换行与内联 JavaScript补充检查 `5 passed`；Python 编译及 `git diff --check` 通过。两文件全量定向回归为 `81 passed, 3 failed`；3 项失败均在未修改基线 `b7281f4` 缺失对应旧文案/排序字符串，和本轮改动无关。
- UAT 写入仅发生在上述副本：E1-L09 栈板 95 的 `needs_relocation` 从 1 改为 0、版本从 1 改为 2，并新增一条“现场确认三楼栈板已归位”操作日志；库存批次、栈板、栈板明细和库存流水数量没有增加。当前服务为 `127.0.0.1:18083`，测试账号 `codex_uat`，密码 `123456`。用户已确认区域归位、区域内空位绑定和全局 E1-R01 空位绑定全部验收通过，同意进入独立提交与推送阶段。

## 2026-07-23 N081-0 | 盘点准备只读导出与首次盘点模板

- 在最新正式远端基线 `origin/factory-current-baseline@030c10150735de1aff9c429200858ee4f4a1bab4` 建立独立 worktree `D:\tm-worktrees\erp-n081-phase0-readiness-export-20260723` 和分支 `codex/n081-phase0-readiness-export-20260723`，并纳入既有 N081 路线重排文档。
- 新增 `scripts/audit/n081_inventory_readiness.py`：必须显式指定 SQLite 和报告目录，使用 `mode=ro + PRAGMA query_only=ON`，没有 apply/import/delete 模式；输出库位、正式批次、栈板、快照和未定位问题的 JSON/CSV/Markdown，并核对 Alembic、quick_check、外键、扫描前后大小及 SHA-256。
- 新增空模板 `docs/warehouse_reports/templates/N081_INITIAL_STOCKTAKE_TEMPLATE.csv` 和脱敏样例 `N081_INITIAL_STOCKTAKE_SAMPLE_MASKED.csv`；模板只冻结字段口径，不具备正式导入或库存确认能力。
- 家庭隔离副本演练为 `cg63v8x9z52`、`quick_check=ok`、外键异常 0，扫描前后大小及 SHA-256 一致，数据库未写入；定向测试 `2 passed`，Python 编译和 `git diff --check` 通过。实施记录：`docs/warehouse_reports/N081_PHASE0_READINESS_EXPORT_IMPLEMENTATION_20260723.md`。
- 当前主机是家庭电脑 `PC-20230130ZRXT`，不是工厂主机 `PC-20250926DZYH`。工厂正式基线虽已远端合并到 `030c101`，但尚未在工厂执行备份、`cf62v8x9z51 -> cg63v8x9z52` 迁移、重启和复核；工厂 N081-0 只读导出也尚未执行。本阶段未连接、迁移或写入工厂正式数据库。

## 2026-07-22 P0-B | 上传路径、私有图纸与统一文件校验（家庭本地完成，待人工 UAT）

- 独立 worktree：`D:\tm-worktrees\erp-p0b-upload-security-20260722`；分支：`codex/release-p0b-upload-security`；基线：`c5cffa2123973b3f30b74be7f6dc198d1f070210`。本轮没有连接、迁移或写入工厂正式数据库，也没有新增 Alembic migration。
- 已移除订单创建对客户端 `temp_drawing_file` 本地路径的解释、`isfile/copy2` 和失败后原样落库逻辑。订单草稿图纸现在只返回 32 位随机、15 分钟、绑定上传用户的一次性 token；token 映射和临时文件仅位于固定私有临时目录，消费后不能重放，过期文件会清理。
- 新图纸存储在 `data/private_uploads`（可由 `ERP_FILE_STORAGE_DIR` 显式覆盖），不再写入公开 `static/uploads`。数据库只保存随机私有引用；原文件名、MIME、大小和 SHA-256 只保存在私有 metadata 与操作日志中。
- 产品图纸、订单图纸和草稿预览统一通过带登录、权限、客户范围和查看审计的 API 提供；通用静态挂载对 `/static/uploads` 一律返回 404。历史数据库中的 `/static/uploads/...` 引用仍可由鉴权接口在安全根目录内兼容读取，没有移动或删除历史文件。
- 新增统一上传校验：1MB 分块读取、单文件上限、30 秒分块超时、扩展名白名单、MIME 与文件签名三方一致校验、HTML/SVG/JS/XML 主动内容拒绝、批量 PDF 最多 20 个且请求总量最多 100MB、临时 token 清理。订单 PDF、PDF 训练样本、产品/订单图纸和天华预送货图片均已接入；Excel 校验策略要求真实 XLSX ZIP 结构。
- 依赖更新为 `python-multipart==0.0.27`、`pypdf==6.7.3`、`PyJWT==2.13.0`；隔离环境实测版本一致且 `pip check` 无冲突。生产配置现有至少 32 字符会话密钥门禁继续有效，本轮未发现文件泄露证据，因此没有轮换任何密钥。
- 家庭副本 `D:\纸箱厂erp软件搭建\static\uploads` 不存在；只读审计未发现异常文件，但该结果不能替代工厂主机复核。报告：`docs/security_reports/P0B_PUBLIC_UPLOADS_AUDIT_20260722.md`；只读审计脚本：`scripts/audit/public_uploads_audit.py`，没有删除/apply 模式。
- 自动验证：P0-B 核心与图纸权限 `17 passed`；新版依赖下上传、登录、PDF/OCR、图纸、客户隔离和权限组合 `150 passed, 1 skipped`；相关前端与内联 JavaScript `93 passed`；Python 编译和 `git diff --check` 通过。另有三个旧断言/用例已在未修改的 `c5cffa2` 基线复现（旧常用箱 DOM 标记、sales 账号预送货执行权限、PDF 训练路由清单漏列 correction），与 P0-B 无关。
- 人工验收：`docs/security_reports/P0B_UPLOAD_SECURITY_UAT_20260722.md`。今晚只保留家庭本地提交；按用户要求，明早到工厂后再共同决定推送、PR、合并和工厂发布。

## 2026-07-22 N081 Phase 0 | 库存盘点与首次数据入库路线重排（文档完成，未开发）

- 用户将近期开发优先级调整为 N081，先解决库存盘点和首次数据入库，再继续扩展此前 P0 包；本轮只做只读审计和路线修订，没有开发 API/页面/迁移。
- 权威候选基线仍为 `origin/factory-current-baseline@c5cffa2123973b3f30b74be7f6dc198d1f070210`；独立 worktree 为 `D:\tm-worktrees\erp-n081-inventory-count-roadmap-20260722`，分支为 `codex/n081-inventory-count-roadmap-20260722`。
- 当前正式数量权威已存在：`inventory_lots` + `inventory_movements`；客户专用/通用成品、栈板和三楼位置也已存在。N035 手机盘点只覆盖已有成品批次，不能登记账外库存或盘半成品。
- N081 改为“空间主数据 → 归属/来源/日期口径 → 账外库存导入草稿 → 3～5 个成品栈板与 2～3 个半成品栈板试盘 → 分区域入账 → 移动/拆板/复盘”。N080-B 的最小归属语义提前，完整备库生产/直接出库仍后置。
- N066 后移到 N081 全部盘点入账闭环之后，不再阻塞近期盘点；其位置仍必须复用 N081 的稳定 `location_id`。审计报告：`docs/warehouse_reports/N081_PHASE0_INVENTORY_COUNT_READINESS_AUDIT_20260722.md`。
- 开发优先级与工厂发布门禁分离：可先在家庭隔离环境开发 N081，但 P0-A/P0-B 未进入工厂基线前，禁止发布 N081 新迁移、文件导入或正式盘点确认。
- 本轮未找到 `天明包装ERP_新增需求长期记忆_20260630.md` 和 `天华模具明细(1).xls`；未连接、迁移或写入任何数据库，未 push。

## 2026-07-22 | 工厂 ERP 首页 Vue 模板空白页修复

- 独立 worktree：`D:\tm-worktrees\erp-factory-blank-page-fix-20260722`；分支：`codex/factory-blank-page-fix-20260722`；基线：`5f2fb671ed64eeda7dcf0f4d72240acf7d4f9e34`。正式目录、正式分支、数据库、备份、安全分支和 stash 均未修改。
- 根因是 `static/index.html` 的订单分组明细表在 `0dab789` 中多出一个 `</template>`，提前截断顶层 `activePage` 条件链，导致 Vue 3.5.40 抛出 compiler error 30（后续 `v-else-if` 失去相邻 `v-if`）并清空 `#app`。无缓存刷新仍可复现，排除旧缓存和静态资源加载故障。
- 最小修复只删除多余结束标签并恢复缩进；新增静态回归断言，确保订单分组明细内只有一个对应的 `</template>`，避免再次导致首页空白。
- 隔离验证使用更新前备份的字节级副本 `D:\tm-uat\factory_blank_fix_20260722\carton_erp_blank_fix_uat.sqlite3`，副本 SHA-256 与备份一致、`integrity_check=ok`，未迁移；修复分支运行在 `127.0.0.1:18072`，首页显示登录页、Console 无错误、所有脚本 200、`/api/health` 返回 200。
- 自动验证：定向前端与内联 JavaScript 语法 `34 passed`；更宽组合 `45 passed, 1 failed`，失败项为旧 Phase 10 销售菜单文案断言，已在未修改的正式基线独立复现。`git diff --check` 通过（仅 Git 提示现有 LF/CRLF 转换策略）。
- 本修复不含数据库迁移，不连接、迁移或写入工厂正式数据库。正式目录仍为 `factory-current-baseline@5f2fb67` 且工作区干净；待用户审阅修复提交后再决定是否重启并快进正式基线。

## 2026-07-21 | 撤销来料后订单删除提示修复

- 独立 worktree：`D:\tm-worktrees\erp-order-delete-incoming-audit-fix`；分支：`codex/order-delete-incoming-audit-fix`；基线：`fb0aacaba5463fa7b2444a5cef166df1fb79a461`。未修改主目录、N041 WIP、报料更新回退 WIP 或 `D:\ERP交接备份`。
- 已确认的业务原因是 `incoming_receipt_items.order_id/order_item_id` 使用不可置空的 `ON DELETE RESTRICT`，撤销来料仅撤销业务效果，`status=reversed` 的审计事实必须永久保留。订单删除前置依赖检查过去未查询该表，导致提交阶段才被外键拒绝并返回通用误导文案。
- `app/api/orders.py` 的订单删除依赖检查现在优先识别来料事实：任一 `posted` 返回“有效来料实收”专用 409；没有 `posted` 但存在 `reversed` 或其他历史事实时返回“已撤销来料审计”专用 409。检查继续位于预送货解绑、库存释放、旧报料删除和删除日志写入之前，单删与组删均原子阻断；未删除、改空或绕过任何来料审计外键。
- `static/index.html` 的订单危险操作区新增“标记作废”，复用现有状态接口写入 `cancelled`；状态中文映射补齐死单、已结档、已作废和已归档。单删与组删确认文字明确提示撤销来料后仍不能物理删除，后端专用 409 继续通过统一错误解析原样显示。
- 自动验证：新增核心场景 `6 passed`；订单、来料、N029 生产集成和相关前端推荐回归主运行 `145 passed, 2 failed`，两项仅因当前系统 Python 缺少未声明的 `cv2`；N029 其余用例复跑 `11 passed, 2 deselected`，缺依赖两项使用一次性临时目录中的 `opencv-python-headless` 复跑 `2 passed`。前端内联 JavaScript 语法 `2 passed`，Python 编译与 `git diff --check` 通过。
- 本修复不含数据库迁移；所有自动测试均显式使用临时 SQLite 路径，未连接、迁移或写入工厂正式数据库。

## 2026-07-19 N039/N040 | 复合产品生产闭环与客户材质候选追溯

- N039 已经人工验收并合并到主功能分支，合并提交 `ba34b7e`；正式数据库已在在线备份后由 `cc59v8x9z48` 线性升级到 `cd60v8x9z49`，迁移后 `integrity_check=ok`、外键异常 0。升级前备份为 `data/backups/carton_erp_before_n039_cd60_20260719_160006.sqlite3`，SHA-256 为 `6FC7024D828858BA056F16F7ABC0F36A3102AD03C429FD5D9A31DA9FB1A10B0F`。
- N040 独立 worktree：`D:\tm-worktrees\erp-material-candidate-trace-n040`；分支：`feature/material-candidate-trace-n040`。新增客户材质候选、不可变选择历史和订单原始材质快照；系统只给出可解释推荐，必须人工确认，默认不同步常用箱。
- N040 迁移 `ce61v8x9z50` 线性接在 N039 `cd60v8x9z49` 后。正式库尚未执行 N040 迁移；副本已完成 `cd60 -> ce61 -> cd60 -> ce61`，空事实可回退，产生候选或历史事实后 downgrade fail-closed，历史 UPDATE/DELETE 被数据库触发器拒绝。
- N040 业务契约与迁移专项 `7 passed`；候选维护入口位于“常用箱与材质”，待报料弹窗显示客户原始代码、候选供应商/材质/参考价/历史次数/推荐理由，并保留手工选择。本阶段未 commit、未 push，待跨模块回归与隔离 UAT。

## 2026-07-17 P2/P3 | PDF 预览客户范围隔离与成本脱敏（已集成，待统一隔离 UAT）

- P2 独立分支 `feature/pdf-preview-customer-scope-p2` 提交 `06d0489`，合并提交 `6652b77`；P3 独立分支 `feature/pdf-preview-cost-redaction-p3` 提交 `87e9d46`，合并提交 `a791da0`。本轮没有新增 migration，没有连接或写入正式数据库，也没有创建正式订单。
- 单份预览、批量预览和人工重匹配共用同一个客户范围门禁。受限账号只在 `customer_scope_ids` 内解析客户；空范围、跨客户模板和跨客户名称命中返回白名单构造的无业务数据草稿，并在产品、常用箱及其成本候选查询前终止。
- 旧版天华、高泰和思迈尔可能出现 `customer_route=locked` 但没有 `template_customer_id`；该路径不再被误拒绝，而是只在当前账号允许的客户集合内按现有名称归一化规则唯一匹配，无法唯一确认时 fail-closed。
- PDF 响应统一先附加可信安全 token，再按 `cost.view` 塑形。无成本权限时递归删除成本、毛利、供应商/材料/纸板采购价、报价构成和成本候选，并移除标准材质标签末尾报价；PDF 客户单价、常用箱销售价、产品、规格和匹配证据保持可见。源草稿不被修改。
- 自动验证：客户范围专项 `19 passed`；订单 PDF 导入、天华、高泰、思迈尔和 token 回归 `29 passed`；成本脱敏纯函数/API finalizer `4 passed`；客户范围与敏感业务权限组合 `21 passed`；相关 Python 编译与 `git diff --check` 通过。版本更新为 `v0.22.14 PDF 预览权限隔离与成本脱敏`。

## 2026-07-17 N016 P2 | 通用客户 PDF 模板执行层（本地完成，待统一隔离 UAT）

- 独立 worktree：`D:\tm-worktrees\erp-pdf-template-execution-n016-p2`；分支：`feature/pdf-template-execution-n016-p2`；基线 `96cee39`。本轮未新增 migration、未连接或写入正式数据库，也未创建正式订单。
- 只有生命周期为 `active` 的通用客户模板会执行订单号、日期、明细行和字段映射；`draft/retired` 不参与。字段映射优先使用 `field_mapping`，兼容 `item_field_map` 和旧版顶层数字捕获组；训练库重解析、激活 dry-run 与订单预览继续共用 `parse_pdf_bytes`。
- 天华超净、天华新能源、高泰和思迈尔继续优先使用专项解析器；宽泛通用模板不能抢占专项客户，天华两类客户按完整 `customer_type` 区分，不能互相绑定客户 ID 或常用箱。
- 用户模板正则先校验长度、捕获组和高风险结构，再在独立 Python 子进程中执行；单次 2 秒硬超时会终止子进程并转人工确认，历史 active 模板也不能通过灾难性回溯卡住 ERP worker。非法 JSON、捕获组、金额、数量和真实日历日期均 fail-closed。
- 通用模板只有在原单恰好存在一组独立整单合计，且数量、金额与解析明细在 `0.01` 误差内一致时才得到 `integrity_status=passed`。无合计、多个合计或分页小计保持 `unknown`，正式保存门禁不会放行；模板正则命中数不再冒充源明细数。
- 自动验证：N016/PDF 解析、生命周期、专项路由与既有导入回归 `111 passed`；训练库前端 `6 passed, 46 deselected`；版本更新脚本 `9 passed`；相关 Python 编译和 `git diff --check` 通过。独立最终审查确认无 P0/P1，可提交。版本更新为 `v0.22.13 PDF 客户模板执行与安全校验`。

## 2026-07-17 N022 Phase C.2 | 模具码 + 位置码双码移动确认（本地完成，待人工 UAT）

- 独立 worktree：`D:\tm-worktrees\erp-mold-location-movement-n022-c2`；分支：`feature/mold-location-movement-n022-c2`；基线 `5bf1540`。本轮未 commit、未 push，未连接、迁移或写入正式数据库。
- 新迁移 `bb55v8x9z45` 线性接在 `ba54v8x9z44` 后：`mold_tools` 新增非空 `location_version` 和最后位置确认时间/人员；新增唯一幂等、记录 CAS 前后版本且由数据库触发器保护 UPDATE/DELETE 的 `mold_location_movements`。一旦存在移动事实或确认版本，downgrade 会 fail-closed。
- 新增 `warehouse.view` 双码预览和 `warehouse.execute` 移动确认接口。目标仅接受合法平放/竖放 `3F-M` 位置码；旧自由文本、非法编码、停用模具、目标被其他启用模具占用、旧版本和异业务幂等键均拒绝。幂等键在去除首尾空白后必须至少 8 个字符；相同业务重放复用同一流水，换模具/目标/版本/来源/备注均返回 409。同位置确认没有状态变化，不生成移动业务事实，因而不占用幂等键；成功移动写 `OperationLog`。
- 原 `MoldTool.rack_location` 显示、模具查询、二维码标签和关联产品保持兼容；旧档案编辑接口不再允许绕过流水直接改位置。`static/mobile_mold_lookup.html` 新增模具码、位置码、备注、预览和确认流程，支持手输、粘贴以及 `mold/location` URL 参数，没有引入新前端库。
- 移动事务不查询或修改成品/半成品库存、订单、报料、库存流水或 `warehouse_locations`；专项非空保护基线验证这些表的行数和数量不变。`3F-M` 继续是独立模具位置体系，不映射三楼成品货位。
- 隔离迁移副本 `C:\tmp\n022_c2_rehearsal_20260717_125745\carton_erp_uat_copy.sqlite3` 在升级前备份 SHA-256 校验一致后完成 `ba54 -> bb55 -> ba54 -> bb55`；三阶段均 `integrity_check=ok`、外键异常 0，两条模具基线保留且升级后版本为 1。备份 SHA-256：`97F792619E4E3D4B76885F59A843DDAB1C4A3677DBC05BDA164EF2E3EE149B5D`。
- 自动验证：独立审计修正后的 N022 C.2 专项、迁移和原模具工作流定向运行 `19 passed`；N022/权限/仓库/订单/报料扩展回归此前为 `156 passed, 2 failed`。两项失败均来自本轮未修改文件中的旧前端断言（旧菜单连续字符串、禁止基线已有的 `time-utils.js`），未越界修改。人工清单：`docs/warehouse_reports/N022_PHASE_C2_UAT_CHECKLIST_20260717.md`。

## 2026-07-17 | 最终回归兼容：P5 批处理确认与前端时间工具

- `material_mapping` 仅在调用者明确 `preview_confirmed=True` 时捕获精确的 P5 confirmation-required 409，并使用返回 token 保持原 `expected_version` 重试一次；默认未确认调用仍原样抛错。
- 七层材质映射测试改为先走正式预览生成对象确认 tokens；半成品 Node harness 在首页内联脚本前执行真实 `static/assets/time-utils.js`，生产代码未增加 fallback。
- 后端原失败节点连同新增 P5 门禁定向测试 `6 passed`，前端原失败节点 `1 passed`；材质映射/P4 间接写入 `67 passed`；半成品前端 `25 passed`。`py_compile` 与 `git diff --check` 通过；未 commit、未 push、未写正式数据库。

## 2026-07-17 | Alembic 客户账期迁移回滚链修复

- 修复 `aq44v7w8x9m34` 在最新 head 回退时无法删除 `customers.statement_cycle_start_day` 的问题。真实根因是后续 SQLite batch recreate 将列级 CHECK 提升为表级约束；旧版原生 `DROP COLUMN` 会留下引用已删除列的约束。
- `aq44` downgrade 改为 batch recreate，同时删除命名 CHECK 和字段；升级路径、正式业务模型和现有数据口径均未改变。
- 新增 `head -> an41v7w8x9j31 -> head` 往返测试；客户账期、来料、三楼货位、成本快照、主数据版本、生产和 N031 会话迁移共 `39 passed`。
- 往返后的 `integrity_check=ok`、`foreign_key_check=0`，Alembic 仍只有 `ba54v8x9z44` 一个 head。本轮只使用临时测试数据库，未连接或修改正式数据库。

## 2026-07-17 N022 Phase C.1 | 模具位置现场试盘只读 dry-run（本地完成，待集成验收）

- 独立 worktree：`D:\tm-worktrees\erp-mold-location-pilot-n022-c1`；分支：`feature/mold-location-pilot-n022-c1`。本轮只新增审计脚本、专项测试和操作文档，未修改 API、模型、前端、迁移或数据库。
- `scripts/audit/mold_location_pilot.py` 要求显式 `--database`，以 SQLite `mode=ro + PRAGMA query_only=ON` 打开；没有 apply 模式，拒绝符号链接数据库及符号链接/正式 live 路径的报告写入。
- dry-run 仅查询 `mold_tools`，稳定输出 JSON/CSV 差异和汇总；核对空位置、旧自由文本、非法 `3F-M`、启用模具的位置重复、模具编号重复及停用状态，绝不映射到三楼成品 `warehouse_locations`。
- 定向测试验证数据库 SHA256、mtime、`mold_tools` 和 `warehouse_locations` 行数均不变，重复输出稳定，平放/竖放、非法/重复/旧文本均正确分类；`2 passed`、`py_compile`、`git diff --check` 已通过。

## 2026-07-17 N027 Phase C.1 | 只读库存经营指标补全（待统一 UAT）

- 指定 worktree：`D:\tm-worktrees\erp-inventory-insights-n027-c1`；分支：`feature/inventory-insights-n027-c1`。本轮只编辑库存洞察服务、仓库页面、相关测试和审计文档；未新增 migration，未修改库存写接口或任何数据库。
- 库龄保持 `stock_date` 口径，新增最后异动和停滞天数仅用于解释；成品需求覆盖是只读汇总，半成品已确认候选关系只展示、不自动抵扣。
- 成本覆盖拆为入库快照估算、当前材料报价估算、产品参考估算，明确不得将其称为实际现金成本覆盖；实际现金占用仍为待补。
- 隔离临时库定向回归 `15 passed`；库存、预占、七层和半成品广泛回归 `133 passed, 2 failed`，两项失败已在干净基线复现，分别是旧材质批处理测试未适配 P5 二次确认、旧 Node 前端测试未注入共享时间工具，与本轮 C.1 无关。同时把两条已落后于现状的仓库页面断言更新为当前生产菜单顺序和唯一共享时间脚本。Python 编译和 `git diff --check` 通过；正式库未写入，待统一 UAT 后再决定推送与集成。

## 2026-07-17 N016 P0 | PDF 草稿保存安全闸门 + 思迈尔重复 CPN fail-closed（未提交）

- PDF 单份/批量预览签发 5 分钟 HS256 `preview_safety_token`，绑定操作员、来源文件名/哈希、识别状态、客户路由状态、客户匹配状态、完整性状态和已匹配客户。客户重匹配必须验证旧 token 及来源一致性，再按可信 claims 换签；篡改、过期、缺失、跨文件或跨操作员 token 均拒绝。
- 保存链路只从签名 claims 读取安全状态，不信任客户端状态文字；签名完整性不是 `passed` 时 fail-closed。页面明确 boolean 确认后，后端仍重新校验客户状态、客户 token 绑定、每行既有常用箱归属、产品有效性和整数数量。`needs_confirmation/failed` 或客户路由未锁定的人工放行写 `OperationLog.action=PDF_SAFETY_OVERRIDE`，不要求无意义文字原因。
- 思迈尔同一 CPN 多变体匹配改为 fail-closed：名称和规格必须存在，并综合变体参考号、名称、规格和销售单价；关键证据冲突、最高分低于 170 或领先分差小于 25 时保持 `unmatched/needs_confirmation`。普通天华/高泰和唯一编码仍沿用原有唯一最高分路径。
- 预览候选返回变体编码、名称、规格、销售单价、总分、分项证据和领先分差；不返回成本。页面展示分数、分差和拒绝原因。客户单号/日期、产品、数量、单价、生产说明、删除、新产品切换、图纸及库存抵扣计划发生变化后统一将 `confirmed=false`；共享库存 helper 仅在 item 属于 `orderImportDrafts` 时失效确认，不影响普通新建订单。
- 回归覆盖：签名 claims 与来源绑定、篡改/过期/缺失拒绝且零订单、完整性客户端伪造无效、重匹配换签、重复 CPN 正确变体及低分/小分差/冲突 fail-closed、编辑与库存计划确认失效、人工思迈尔选择并新鲜确认后保存记日志、天华/高泰、普通非 PDF 订单和预览零写入。
- 验证结果：核心 PDF 导入与客户路由 `33 passed`；合并相关前端/库存回归共 `114 passed`；更宽订单创建与库存预占后端回归 `92 passed`；`py_compile` 与 `git diff --check` 通过。

## 2026-07-16 N016 | v0.22.6 思迈尔 PDF 安全识别收口（待人工验收，未提交）

- 本轮从最新 `at47v7w8x9p37` 主线建立独立 worktree 重做 N016；旧的脏 worktree 仅作只读参考，未直接合并，也未新增 Alembic migration。
- 订单 PDF 预览和训练库统一使用同一套文本质量判断与思迈尔混合解析：文本层负责精确客户单号、数量、单价、金额和日期，OCR 只补名称及规格。
- 思迈尔同一 CPN 可对应不同名称、规格和价格；草稿逐行保留，匹配同时参考供应商变体编码、名称、规格和单价，歧义项维持未匹配/待人工确认，不自动保存订单。
- 真实样本目录的 12 份 PDF 全部识别为思迈尔，共 61/61 行保留，12/12 金额完整性通过；`P-0028338-2` 两条 `CPN084557` 分别按 `CPN126831`、`CPN126830` 匹配不同常用箱，`P-0028533-1` 保留真实第 15 行。
- 修复 `P-0029254-1` 的 OCR 小字号合计误读：文本层明细合计和原文 `13211 CNY` 优先，不能让 OCR 的 `1321` 覆盖精确数值。
- 训练库标注只生成字段级纠错日志和评分；新增审计脚本以 SQLite `mode=ro + query_only` 打开数据库，只输出 dry-run 清单，没有 apply/delete 模式。
- 隔离 UAT 使用正式库副本和 18057 服务；四份关键 PDF 的真实 `/api/orders/pdf-preview` 返回 200，订单 31、订单明细 174、训练样本 472 在预览前后保持不变。
- 自动回归共 `290 passed`，另有 1 条既有 pytest 返回值告警；Python 编译和 `git diff --check` 均通过。正式 8000 与正式数据库未被本轮 N016 开发或验收修改。

## 2026-07-10 Phase 0-D | v0.22.1 稳定化基线（待复核，未提交）

- 当前目标分支为 `release/v0221-stabilization`，基准提交为 `ff6b007`；本轮只保留未提交 WIP，不 commit、不 push。
- SPA 已补齐 11 个业务深链，登录后保留目标页面，根地址仍进入首页仪表盘；`/incoming.html` 明确返回 `no-store/no-cache` 响应头。
- `static/index.html` 与 `static/warehouse.html` 统一使用兼容幂等键 helper：优先 `crypto.randomUUID()`，旧浏览器依次回退到 `crypto.getRandomValues()` 和 `Date.now()+Math.random()`，不再直接调用缺失的 `randomUUID()`。
- `create_app()` 重复初始化会先清空已构建的 middleware stack，再按当前配置重建 CORS，避免同一测试进程中重复创建报错。
- 普通待报料、合并建议、`merged_pending` 与供应商草稿预览/保存统一按当前 active 成品库存预占、每箱片数和开料系数重算；不再沿用旧 `requisition_qty`。供应商草稿不信任客户端填写的库存抵扣数量，也不会改订单数量、送货数量、库存批次数量或清零预占。
- 精确临时库验收已覆盖订单 10、双拼 2、active 预占 9：页面/API 均得到预占 9、需生产 1、需小片 2、采购 2；即使客户端携带旧值 20，保存快照仍为 `1/9/2/2`，lot 保持 `11/9/0`、reservation 保持 active，库存流水数不变。
- 层数与楞型校验集中复用到产品保存、产品字段同步、订单同步、待报料同步和报价转常用箱入口；材质字典继续只管理材质代码，保存时丢弃楞型字段。
- 测试使用显式临时 `ERP_DATABASE_PATH`、备份目录和密钥；安全全量基线为 `948 passed, 27 skipped, 7 deselected`。排除未证明隔离的 phase18、依赖 checkout 数据库的旧测试、两个 phase191 数据库迁移测试及 phase19 API 隔离缺陷用例。
- 隔离浏览器 smoke 已覆盖首页、`/requisition`、`/orders`、`/deliveries`、`/system`、`/incoming.html`；标题与目标页正常，刷新不丢失深链，最终服务日志无 500。
- 本轮无数据库迁移、无历史数据修改；所有任务测试和验收服务均以临时库为目标。正式库存在独立运行服务的外部写入，不能用整库哈希变化推断本任务写入归因。

## 2026-07-08 N-027 多箱型报料公式与天地盖盖/底拆分

- 常用箱箱型推荐已扩展：A1/0201 保持原有单拼/双拼逻辑；A3 天地盖按盖/底两套报料尺寸和压线保存；平卡、刀卡、隔板、围套、半开槽箱、全搭盖箱支持自动推荐，仍允许手工修改后保存。
- 常用箱前端规则已调整：只有需要舌头的箱型显示舌头字段；只有 A1/0201 显示拼箱方式；其他明确箱型默认 `splice_mode=single`、`pieces_per_box=1`、`flap_mm=NULL`。
- 后端产品、订单明细快照和 PDF 匹配流程已补齐底料报料尺寸、底料压线和底料备注字段；订单编辑勾选同步常用箱时会同步这些字段。
- 报料生成对 A3/天地盖自动拆成“盖 / 底”两条采购报料明细，订单明细主记录保存合计采购张数和 `盖:L×W；底:L×W` 规格；来料入库支持把实际入库数量按多条有效报料明细分摊。
- 新增正式迁移 revision：`ab29u7v8w9x18`，在 `products` 增加 7 个 `base_*` 字段，在 `sales_order_items` 增加 7 个 `snapshot_base_*` 字段。
- 正式库迁移前备份：`data/backups/carton_erp_20260708_123118_BEFORE_AB29_BOX_FORMULAS.sqlite3`，SHA-256 `E7FF4A8E588CBA476398198C135F450CBA24D7C94EEE38400071F88B44485C01`。
- 正式库已从 `aa18t6u7v8w17` 升级到 `ab29u7v8w9x18`；迁移前后关键表行数保持不变：客户 131、常用箱 3323、订单 21、订单明细 105、报料单 33、报料明细 117。
- 迁移后 `integrity_check=ok`、`foreign_key_check=0`，新增列齐全；后端已恢复到 8000，健康检查返回 `ok=true`，启动后复查版本仍为 `ab29u7v8w9x18`。
- 本轮未批量修改客户、常用箱、订单、报料、送货、对账或库存业务数据；只写入数据库结构和 `alembic_version`。
- 验证：相关回归 `69 passed`；扩展回归 `99 passed, 35 warnings`；天华专项 `48 passed, 1 skipped`；Python 语法检查、前端 inline script 语法检查、临时库 Alembic 升级验证均通过。

## 2026-07-02 v0.22.0-b 成品库存预占抵扣报料

- 新增成品库存候选、人工预占、查询和释放接口；候选只按订单明细 `product_id`、客户专用/通用归属、active 状态和可用数量硬匹配。
- 通用库存候选显示黄色提示，后端要求提交 `GENERAL_FINISHED_STOCK` 人工确认代码。
- 预占在同一事务内条件扣减 available、增加 reserved、写 `inventory_reservations` 和 `reserve` 流水；释放执行反向余额变更并写 `release_reserve`，均使用幂等键。
- 订单响应新增已预占成品库存、需生产数量和是否全额覆盖；订单删除、明细删除、取消/结档及流程撤回会在允许的业务边界内自动释放 active 预占。
- 报料需求改为 `max(订单数量 - active成品预占, 0) × 每箱片数`；旧自由填写库存抵扣前端已移除，后端拒绝非零旧字段。
- 全额成品预占显示“成品库存已全额抵扣”，不进入普通报料或供应商采购单生成。为兼容送货，只有全额有效预占可以绕过来料入库门禁；本阶段不转 consumed。
- 仓库成品列表显示可用、已预占、已消耗，支持查看关联订单和预占记录；流水支持筛选 reserve/release_reserve。
- 未新增数据库迁移，Alembic head 仍为 `c20u7v8w9x19`；未批量修改历史数据。
- 专项及订单/报料/采购单/送货/仓库回归 `125 passed`；报价、PDF、来料、财务和天华冻结回归 `65 passed`。

## 2026-07-02 v0.22.0 仓库库存页面入口热修复

- 根因：`/api/auth/me` 返回 `{ok, user}`，仓库页错误地把整个响应当作用户对象，导致已登录 admin 的 `role` 被读成空值并立即跳回首页。
- 仓库页改为读取 `authResponse.user`；admin 和 workshop 可以进入。未登录仍回登录页，权限不足或初始化失败时在当前页面显示中文提示，不再静默跳回首页。
- 首页侧边菜单正式加入“仓库库存管理”，点击明确跳转 `/warehouse.html`；原快捷入口继续保留。
- `/warehouse.html` 继续由 `app/main.py` 使用 `FileResponse` 返回 `static/warehouse.html`，本机和局域网地址使用相同路径。
- 本轮没有新增数据库迁移，没有修改库存表和业务数据，也没有接入订单或报料库存抵扣。
- 页面入口、权限、JavaScript 语法及首页/来料回归：`57 passed`。

## 2026-07-01 v0.22.0 仓库成品 / 半成品库存基础

- 新增库位、库存批次、成品快照、半成品快照、预占结构和不可变库存流水六张表；正式迁移 revision 为 `c20u7v8w9x19`。
- 成品和半成品均只支持人工确认入库。现有“来料入库”不会自动生成半成品库存，订单、报料和送货也尚未自动抵扣库存。
- 半成品按实际物理张/片和实际长宽记录；未分切大张的一开几只写入开料说明，本阶段不换算可抵扣小片数，也不允许长宽旋转匹配。
- 支持库存冻结、解冻、盘点增减、报损、报废和管理员将成品转为通用库存。所有数量操作校验版本号并写入前后余额流水，重复请求通过幂等键拦截。
- 通用成品库存仅允许管理员手工转换；本阶段不自动跨客户匹配。预占表只建立结构，未提供候选、预占、释放或消耗接口。
- 新增独立页面 `/warehouse.html`，管理员和车间人员可以维护库存；库位新增、编辑、启停及成品转通用仅限管理员。
- 库龄按入库日期提示：满1年重点关注、满18个月安排处理、满2年盘点清理；最后流水日期只用于最近操作排序和审计，不再重置库龄。
- 正式迁移前备份：`data/backups/carton_erp_20260701_161542_BEFORE_V0220_WAREHOUSE_FOUNDATION.sqlite3`，SHA-256 `73DBD3142CECCC0C3601624DA0A5689DF6EE983AF1110B84B6D5E51306BC5E49`。
- 迁移前后客户、产品、订单、报料、送货、对账和开票行数均未变化；新库存表初始均为0行。迁移后 `integrity_check=ok`、`foreign_key_check=0`。
- 定向库存测试 `12 passed`，现有前端/启动/报料回归 `24 passed`。全量测试为 `891 passed, 8 skipped, 12 failed`；12项失败均为本轮前已存在的旧规则/旧文案/现场样本断言（包括已确认保留的 LAN `0.0.0.0` 与旧 `127.0.0.1` 断言冲突），未修改这些超出 v0.22.0 范围的测试。项目 `.venv` 未安装 pytest，测试使用本机 Python 3.10 测试环境执行。

## 2026-07-01 P0 天华常用箱显示热修复

- 事故判定为读取接口500，不是数据删除：天华超净备份中可见常用箱925条，当前历史常用箱仍为925条，另有报价转换新增1条；天华新能源16条、无锡天华4条均未变化。
- 根因是产品列表读取复用了新增/编辑产品的严格层数/楞型校验。新转换产品 ID 3320 为三层 CCC + AB，导致整页序列化失败，前端表现为常用箱全部消失。
- 产品列表和详情改为直接序列化已存储值，不在读取时阻断历史旧组合；新增和编辑仍继续执行严格的层数/楞型校验。
- 报价转常用箱前端现在根据所选材质层数检查楞型；缺失或不匹配时要求重新选择，后端继续二次校验，只新增当前报价明细的一条产品。
- 未恢复、删除、覆盖或批量修改任何正式常用箱；异常的新产品仍保留并可在页面人工修正为三层允许的 A/B/E。
- 修复前备份：`data/backups/carton_erp_20260701_154648_BEFORE_P0_TIANHUA_COMMON_BOX_FIX.sqlite3`，SHA-256 `A17901E42088310EC45F8C05646F7CC80B24D3DA40523846533DC989F804CCAE`，完整性 `ok`、外键异常0。
- 本轮无数据库迁移，不改变报价、订单、报料、送货或对账数量口径。

## 2026-07-01 v0.21.1 报价到报料主流程与 PDF 导入补丁

- 报价明细转常用箱前由后端确认材质、供应商、层数、楞型和最终单价；A1/0201 固化采购报料推荐尺寸，其他箱型必须人工输入报料长宽。
- 常用箱继续保存实际楞型；订单创建时固化材质代码、楞型、供应商、报料尺寸和压线快照，后续修改常用箱不会静默覆盖历史订单。
- 合并报料键继续包含供应商、材质、层数、楞型、单片报料尺寸、拼箱方式和压线信息；相同材质代码的 AB、BE 等不同楞型不会误合并。
- 供应商采购报料单继续显示业务明细的“材质代码 / 楞型”和采购张数，不显示订单、送货、对账数量或内部成本。
- 嘉林亿采购长低于500mm或采购宽低于270mm时，采购单打印前弹出确认提醒；提醒不自动修改尺寸或开料方式。
- PDF 识别弹窗取消重复的全局核对勾选；底部“加入批量保存”按钮直接确认所有完整草稿，并显示已加入数量。
- 订单组删除改为单次二次确认和后端原子事务；纯 PDF 导入且未进入后续流程的订单组可以删除，已有报料、采购单、送货或预送货关联时返回中文409提示，不再暴露500。
- 本轮无数据库迁移、无历史数据批量修改，不改变订单、送货或对账数量口径。

## 2026-07-01 v0.21.0 材质组合验收补丁

- 材质字典彻底取消楞型维度：列表和编辑不显示楞型，新建材质保存 `flute_type=NULL`；常用箱、订单、报价和报料继续保存各自实际楞型。
- 材质重复判断统一为“供应商 + 层数 + 清洗后的3/5位材质代码”，历史 `A416D-AB/EB` 与新输入 `A416D` 会被识别为同一条并拒绝重复保存。
- 组合材质的供应商、层数或代码一旦修改，前端立即清空结构、双价格和保存资格；重新解析成功后才能保存。
- 保存接口重新按当前供应商、层数和代码执行推算；建议价来源始终使用后端当前推算值，旧代码的建议价不能写入新代码，手工价必须明确标记为 `manual`。
- 嘉林亿报料草稿的采购长、采购宽根据当前输入值实时检查；低于500mm/270mm时对应输入框显示黄底，改到合格值后自动消失。一开一宽度不足时只建议一开二，不自动修改。
- 本轮无数据库迁移、无历史材质批量修改，不改变订单、报料、送货或对账数量口径。

## 2026-07-01 v0.21.0 嘉林亿瓦楞材质计价规则修正

- 三层结构统一为“面纸/瓦楞纸/里纸”，第2位只使用瓦纸换纸规则；五层统一为“面纸/B楞瓦纸/芯纸/A楞瓦纸/里纸”，面芯纸换纸仅用于第3位。
- 新增供应商基准报价、换纸加价和规则备注三张隔离表；2026-04-14报价只作为规则基准，不覆盖 `materials.quote_price` 当前价格。
- 嘉林亿基础代码采用“进口AAA级俄卡”和“国产A级施胶高瓦”；导入后基础代码18条、基准报价86条、换纸规则19条、规则备注7条。
- 正式推算验证：`C6C`基准1.31、A楞使用基准1.35、当前建议1.38/1.42；`J616J`基准2.86、应用2026-06-26统一涨价5%后当前建议3.00。
- 新材质基础记录不再绑定固定楞型；规则基准价写入 `rule_base_price`，下游当前成本使用价写入 `quote_price`，具体楞型仍由常用箱、订单、报价和报料选择。
- 材质页面合并为“新增/组合材质”，双列显示报价表基准价和当前使用建议价；基础代码、楞型加价和供应商调价收进“材质规则维护”。
- 待报料和合并报料对嘉林亿采购长低于500mm、采购宽低于270mm给出提醒；一开一宽度不足时只建议一开二，不强制修改。
- 分纸加价保持 `pending_confirmation`，长度超过3000mm加价保持不启用，均不参与自动报价。
- 正式迁移 revision：`b19t6u7v8w18`；迁移前备份：`data/backups/carton_erp_20260701_102432_750314_CORRUGATED_RULES_BEFORE_MIGRATION.sqlite3`。
- 规则导入前备份：`data/backups/carton_erp_20260701_102501_545484_JIALINYI_RULES_BEFORE_IMPORT.sqlite3`；导入报告：`docs/material_reports/JIALINYI_MATERIAL_RULES_APPLY_20260701_102503.md`。
- 正式材质仍为382条，价格合计仍为1053.11，嘉林亿 `C4C` 当前价仍为1.27；迁移及导入后 `integrity_check=ok`、`foreign_key_check=0`。

## 2026-06-30 v0.21.0 供应商材质代码组合

- 材质管理新增“基础纸种代码”和“组合材质”区域；基础字符按供应商隔离，可新增、编辑、搜索、停用和启用。
- 新增 `supplier_paper_codes` 表，唯一约束为“供应商名称 + 单字符代码”；没有调整 `materials.code` 现有全局唯一约束。
- 3位材质按“面纸/芯纸/里纸”解析，5位按“面纸/芯纸/中纸/芯纸/里纸”解析；缺失字符会逐层标红并阻止保存。
- 组合模块只汇总逐层结构和总克重，不按克重推算平方价；同供应商完整材质已存在时带出现价，新组合必须人工填写平方价。
- 新组合继续保存到现有 `materials` 表，因此常用箱、订单、报料、供应商采购单和报价模块沿用原有材质选择及成本口径。
- 正式迁移 revision：`a18t5u6v7w17`；迁移前备份：`data/backups/carton_erp_20260630_225938_451390_SUPPLIER_MATERIAL_COMPOSER_BEFORE_MIGRATION.sqlite3`，SHA-256 `2a7b8151e6a739240d84b3d0576339b53bf841ef9dfdfbdc199aa1435bd34aaa`，完整性 `ok`。
- 迁移前后历史材质均为382条，基础代码表初始为0条；未写种子数据、未批量修改历史材质，迁移后 `integrity_check=ok`、`foreign_key_check=0`。

## 2026-06-30 v0.21.0 客户报价流程

- 客户管理每个客户增加“报价”入口，可创建多行报价、保存草稿、生成报价单、查看历史、标记客户接受、作废和转入常用箱。
- 新增 `quotation_orders`、`quotation_items` 和 `quotation_daily_sequences`，报价单号按 `QT-YYYYMMDD-001` 当日递增；迁移已接入现有单一 Alembic 链。
- A1/0201 报价复用现有纸板成本口径；建议单价为 `预估单个成本 ÷ (1 - 毛利率)`，默认毛利率 20%，最终单价可人工修改。
- 客户打印/PDF只显示产品、规格、材质、数量、最终单价、金额和公开公司信息，不返回内部成本、毛利率或建议单价。
- 客户接受后才允许转常用箱；正式存货编码必须人工填写，不自动生成，不自动覆盖同客户同编码产品，不自动生成订单。
- 本轮不迁移历史报价数据，不修改历史订单，也不改变订单、报料、入库、送货或对账数量口径。
- 报价专项与核心业务回归：`124 passed`；前端 JavaScript 语法检查通过。

## 2026-06-30 v0.21.0 PDF 客户短名清理与匹配修正

- 新增 `scripts/admin/merge_short_tianhua_customer.py`，支持 `--dry-run` 和 `--apply`，只合并名称精确等于“天华”的客户。
- 正式库短名客户 ID 3 已安全合并到“苏州天华超净科技股份有限公司”ID 5；完整天华超净、天华新能源和无锡天华客户均保留。
- 合并前保护备份：`data/backups/carton_erp_20260630_204521_982918_BEFORE_TIANHUA_CUSTOMER_MERGE.sqlite3`，完整性 `ok`。
- 共处理 1,554 个影响点：775 条外键引用、778 条客户名称快照及 1 条短名客户；未应用历史采购候选、未生成订单或常用箱。
- 合并后 `integrity_check=ok`、`foreign_key_check=0`，短名客户剩余 0 条，正式目标客户 1 条。
- PDF 客户匹配改为完整名称精确匹配优先、标准化名称其次、正式客户部分匹配最后，并且只使用启用中的真实客户资料。

## 2026-06-30 v0.21.0 待办、排序与录入效率优化

- 首页待报料、待入库、待送货卡片和待办只统计交期为空、已到期或已超期订单；未来交期仍可在业务页面查询。
- 订单按更新时间/创建时间倒序；报料按明细创建时间倒序；入库按报料或入库时间倒序；送货按打印、发货、创建时间倒序。
- 常用箱筛选默认只显示存货编码，名称、规格、材质和停用状态收进“更多筛选”；报料长宽输入框加宽。
- 历史常用箱已有报料尺寸继续按手工尺寸保护，页面明确提示；“重新推荐”仍按当前箱型、单拼/双拼和舌头覆盖。
- 仓库来料支持当前待入库列表勾选和批量确认；后端逐条条件更新，重复或非法状态不会入库，部分失败返回逐条原因。
- PDF 草稿明确显示识别客户原文、自动匹配结果和多候选；底部增加全局核对门禁，未核对不能批量保存。
- 本轮无数据库结构变更、无历史数据迁移，不改变订单、报料、入库、送货和对账数量口径。

## 2026-06-30 v0.20.9 报料换材质与采购单打印补丁

- 待报料和合并报料明细统一显示供应商、清洗后的材质代码与楞型，并可通过“更换供应商/材质”修改。
- 换成不同供应商后明细仍保持未报料，刷新时按新供应商重新分组；同供应商换材质只按新材质重新分组。
- 合并报料“每箱片数”压缩为“每箱”，内容显示 `1片` / `2片`；采购张数仍是突出显示的报料主数量。
- 采购报料单标题独立居中，TO、NO、日期下移到表格上方；六列改为纸板长宽、压线、材质/楞型等现场用语。
- 采购报料单右下角读取公司信息中的地址和电话；配置为空时保持空白。
- 本轮无数据库结构变更、无历史数据迁移，未修改订单、送货、对账数量口径。

## 2026-06-30 天华预送货图片导入

- 实际运行项目固定为 `D:\纸箱厂erp软件搭建`，不得再修改 `tm_desktop_erp`。
- 送货与回单工具栏已增加“导入天华预送货”，前端调用独立 FastAPI 接口完成图片识别、预处理、草稿生成和草稿修改。
- OCR 仅识别固定截图左列 8 位存货编码与右列整数数量；用户样图验证 30/30 行。
- 新增四张 `tianhua_pre_delivery_*` 隔离表；草稿不写 `sales_deliveries` / `sales_delivery_items`，不修改订单已送量，不出库、不生成回单或对账。
- 正式迁移 revision：`w95q2r3s4t83`。
- 迁移前备份：`data/backups/carton_erp_20260630_110820_345949_TIANHUA_PRE_DELIVERY_BEFORE_MIGRATION.sqlite3`，完整性 `ok`。
- 迁移后 `integrity_check=ok`、`foreign_key_check=0`，正式订单、送货、回单、对账表行数不变。
- 天华与现有送货定向测试：18 passed；全量：848 passed、8 skipped、4 个既有无关断言失败。

## 2026-06-30 v0.20.9 订单编辑与报料 UI 完整优化

- 订单明细新增单个/总预估成本、单个/总预估毛利和销售金额展示；列表主视觉改为明细总成本、总毛利。
- 订单编辑页按常用箱布局补齐箱型、长宽高、实际数量、材质、拼箱、报料尺寸、压线、工艺、印刷和备注；有关联常用箱时在同一次后端事务中同步。
- 待报料池新增供应商款数统计和供应商优先筛选；未生成报料单的明细可人工更换供应商、材质和楞型，并可同步常用箱。
- 合并报料不再显示 TM 单号和“需求小片”列，客户使用简称、产品限制两行，采购张数作为主数量突出显示。
- 供应商采购报料单改为老采购单结构：TO、工厂名称、采购单、NO、日期，以及“序号/纸板规格/压线/材质/数量/报料备注”六列。
- 本轮无数据库结构变更、无历史数据迁移；订单、送货、对账仍使用成品数量，采购张数只用于报料和来料入库。
- 专项与核心回归：`86 passed`；前端 JavaScript 语法检查通过。

## 2026-06-30 v0.20.9 供应商采购报料单材质显示修正

- 供应商采购报料单材质统一显示为“材质代码 / 楞型”。
- 五层材质代码取清洗后的前 5 位，三层取前 3 位；`CCC-B`、`A414B-AB/EB` 等历史组合尾巴不会进入打印显示。
- 楞型只接受 `AB`、`E`、`BE`、`B`、`C`、`A`；楞型为空时仅显示材质代码。
- 供应商报料单接口、列表和打印预览统一使用 `material_display`。
- 本轮未改数据库结构、数量计算、单拼/双拼、一开几、订单、送货或对账口径。

## 2026-06-28 v0.20.8 常用箱编辑页面优化

- 现场修正版把常用箱编辑弹窗压缩为五排：核心信息；尺寸/报料/压线；材质；工艺/印刷/图纸；单价/备注。
- 长宽高、报料长宽和压线尺寸在编辑与保存边界统一为整数毫米，单价继续保留原有小数精度。
- A1/0201 普通开槽箱恢复自动推荐：报料长 `2 × (L + W) + 30`，报料宽 `W + H + 5`，压线 `round(W/2) / H / round(W/2)`。
- 自动推荐要求箱型、长宽高和材质齐全；压线类型仍由人工选择。
- 已删除“A1 推荐计算”按钮，保留“重新推荐”作为用户主动覆盖手工值的入口。
- 页面通过前端状态区分自动推荐和人工修改；已有报料/压线值或人工修改后的值不会被自动覆盖。
- 箱型列表补充 A1/0201、A3 天地盖、平卡、刀卡、隔板、围套、半开槽箱、全搭盖箱、异形箱和其他；非 A1/0201 暂不编造公式。
- 生产工艺复用现有 `production_process` 字段，可多选粘贴、打钉、模切、双拼和其他；旧的非标准工艺文字会保留，不会因打开页面被覆盖。
- 印刷类型复用现有 `print_content` 字段；无印刷时隐藏图纸区域，有印刷时显示上传和版本历史。
- 图纸继续复用现有 `product_drawings` 版本表和上传接口，新版本不会覆盖旧版本。
- 本轮不改数据库结构、不执行历史迁移、不批量修改产品或历史订单；不改订单、送货、回单和对账流程。
- 现场修正版回归测试：`132 passed in 153.30s`；前端 JavaScript 语法检查通过。
- 定向验证记录见：
  `docs/go_live_checklists/COMMON_BOX_EDIT_OPTIMIZATION_V0208_20260628_221929.md`

## 2026-06-28 v0.20.7 首页待对账汇总与一键启动

- 首页的“待对账”提醒现在按客户 + 月份合并显示，不再重复刷同一个客户的多条记录。
- 新增一键启动脚本，双击后会自动检查 Python、升级数据库、启动 ERP、打开浏览器。
- 新增停止脚本、桌面快捷方式和开机自启脚本，方便工厂电脑直接使用。
- 这次改动不改数据库结构，不做历史迁移，不碰 `legacy_*`。

## 2026-06-28 v0.20.3 合并进基线

- 合并结果：`feature/v0203-company-info` 已合并到 `factory-current-baseline`
- 合并提交：`6913749`
- 关联修复提交：`4df6c50`
- 验证结果：
  - `tests/test_v0203_company_info.py`：16 passed
  - `tests/test_phase10_frontend.py`：7 passed
  - `tests/test_phase7_deliveries.py`：14 passed
- 主库未写入，正式数据库 SHA-256 未变化
- 已补充状态文档：
  - `docs/ERP_PROJECT_STATE.md`
  - `docs/BUSINESS_RULES.md`
  - `docs/UI_STYLE_GUIDE.md`

## 2026-06-28 订单历史清理（2026-03 以前）

- 已按授权删除 `sales_orders.order_date < '2026-03-01'` 的历史订单。
- 删除前主库：`sales_orders=15595`、`sales_order_items=15661`，目标订单 `15098`、目标明细 `15158`。
- 已连带删除对应的回单、对账、结清、送货、报料、迁移映射和操作日志。
- 删除后主库：`sales_orders=497`、`sales_order_items=503`。
- 完整性检查：`integrity_check=ok`，`foreign_key_check=0`。
- 备份：`data/backups/carton_erp_before_delete_orders_before_2026_03_20260628_131335.sqlite3`
- 当前主库 SHA-256：`9947423b4f820a1bf822a3cd4a7a9e78dbff13a8ab80e09b8e6b23d86416cc1c`
- 说明：这次是本地数据库数据清理，不是历史迁移，不改 `legacy_*`。

## 2026-06-22 全量测试收尾（P1，仅改测试，未动正式库/源码/RBAC）

- 目标：将全量 `python -X utf8 -m pytest -q` 从 `285 passed / 6 failed` 修到 `291 passed / 0 failed`。
- 本轮未修改正式库 `data/carton_erp.sqlite3`、未动 `legacy_ruida_*` / `migration_*` / `RUIDA-*`、未改 RBAC/权限/密码、未改业务源码。改动全部在 `tests/`。
- 失败 6 项分类与修法：
  - `tests/test_phase12_uat.py::test_material_normalizes_weight_and_rejects_unknown_flute`、`::test_product_drawing_upload_saves_compressed_files_not_base64`：过时。master 写接口（`app/api/materials.py`、`app/api/products.py` 的 `can_write`）现为 `RoleChecker(["admin"])`，测试仍以 `sales` 登录 → 403。修法：`_login(client,"sales")` → `_login(client,"admin")`（不放宽 RBAC）。
  - `tests/test_phase10_frontend.py::test_phase10_frontend_enforces_auth_and_workshop_finance_masking`：过时。`static/index.html` 的 workshop 菜单新增 `incoming`（来料）。修法：断言更新为 `workshop: ["dashboard", "orders", "incoming", "deliveries"]`。
  - `tests/test_database_path_unification.py::test_start_script_uses_complete_backend_entrypoint`、`tests/test_phase13_deployment.py::test_start_batch_uses_project_venv_one_worker_and_production_port`：过时。启动入口已由 `start_erp.bat` 委托 `scripts/admin/start_erp_background.ps1`，uvicorn/venv/port/worker 等参数现位于 ps1。修法：测试改为校验「bat 委托 ps1 + ps1 含 `app.main:app`/`0.0.0.0`/`8000`/`--workers 1`/`.venv\\Scripts\\python.exe`/`erp_server.log` + `.env` 含 `ERP_DATABASE_PATH`/`ERP_ENVIRONMENT`」。
  - `tests/test_database_path_unification.py::test_database_path_is_absolute_and_independent_of_working_directory`：环境相关（Windows）。子进程打印含中文路径，父进程按 cp936 解码 UTF-8 失败导致 `stdout=None`。修法：`subprocess.run` 子进程环境加 `PYTHONUTF8=1`/`PYTHONIOENCODING=utf-8` 并显式 `encoding="utf-8"`。
- 结果：`tests/test_database_path_unification.py tests/test_phase13_deployment.py tests/test_phase10_frontend.py tests/test_phase12_uat.py` → `26 passed`；全量 `python -X utf8 -m pytest -q` → `291 passed, 0 failed`。

## 2026-06-22 订单状态约束迁移 h48d9f6c1e32 应用（P0）

- 本轮未执行历史迁移，未修改 `legacy_ruida_*` / `migration_*` / `snapshot_*` / `RUIDA-*`，仅扩展订单状态 CHECK 约束。
- 主库 alembic：`g37c8e5b0d21` → `h48d9f6c1e32`；`sales_orders/sales_order_items` 保持 `9549/9615`，`integrity_check=ok`，`foreign_key_check=0`。
- `sales_orders.ck_sales_orders_status` 已由 6 状态扩展为 14 状态（含 `dead/closed/archived/completed/pending_confirmation/pending_reconciliation`），修复管理员"标记死单/已结档/已归档"会触发 CHECK 失败的问题。
- 迁移前备份：`data/backups/carton_erp_BEFORE_ORDER_STATUS_CLOSURE_20260622_144745.sqlite3`（SHA-256 `b1f597bf3172ade5cdee847fccf7f1ed9ead0642ed445a2430d3a3eb348a95e3`，与迁移前主库一致）。
- 功能验证：受控测试单 `TM20260621001`(id=15594) 标记 `dead` 成功且写入审计日志，随后恢复为 `cancelled`。
- 回归测试：`75 passed`（`tests/test_phase5_orders.py tests/test_phase14_frontend.py tests/test_phase8_finance.py tests/test_phase7_deliveries.py`）。
- 已重启 8000 服务，`/api/health` 返回 `9549/9615` 正常。
- 报告：`docs/go_live_checklists/ORDER_STATUS_CLOSURE_MIGRATION_APPLY_20260622_144745.md`

## 2026-06-22 老板端仓库来料、手机卡片与天华规格识别优化

- 老板/admin 业务中心新增桌面版“仓库来料入库”；workshop 电脑端也可进入，页面使用表格布局，不复用手机卡片 UI。
- 桌面端和手机端均展示纸板报料尺寸、客户名称、存货编码、产品名称、材质、报料日期和入库数量。
- 入库数量默认取报料数量，可在确认入库前修改；确认后同步更新订单明细和有效报料明细，并写入既有入库审计日志。
- 手机卡片顺序已调整：第一行纸板报料尺寸；第二行客户名称/存货编码/产品名称；第三行材质和可编辑数量；第四行报料日期。
- 天华 PDF 规格型号解析改为仅提取尺寸表达式；例如 `28.5*19.5*5.5cm`，不会混入数量、单价、总价和日期。
- 相关回归：`83 passed`；前端 JavaScript 语法检查通过。
- 本轮未执行历史迁移，未修改 `legacy_*`，未做批量历史订单修改。

## 2026-06-22 ERP 自动启动与桌面入口

- `start_erp.bat` 闪退原因：项目 `.venv` 指向已失效的 Codex 临时 Python 路径。
- 已新增 `scripts/admin/start_erp_background.ps1`，自动跳过损坏的虚拟环境，使用本机可用 Python 后台启动，并检查 8000 健康接口。
- 已把 `Tianming ERP Auto Start.lnk` 写入当前 Windows 用户启动目录；用户登录 Windows 后 ERP 自动后台运行。
- 已在桌面创建“天明ERP系统”网页快捷方式，员工只需双击该图标。
- `start_erp.bat` 也已改为后台启动成功后自动打开网页；失败时窗口不再闪退，会保留中文错误提示。
- 当前已验证 `http://127.0.0.1:8000/` 和 `/api/health` 正常。

## 2026-06-22 产品 PDF 图纸与手机扫码查看

- 本轮未执行历史迁移，未修改历史订单，未修改 `legacy_*`，未写正式主库业务数据。
- 产品图纸上传现支持 JPG、PNG、WEBP 和 PDF，单文件最大 20MB。
- PDF 会校验 `%PDF-` 文件头并作为图纸版本保存；图片图纸的压缩和缩略图逻辑保持不变。
- 产品编辑页的图纸版本历史可直接点击打开 PDF。
- 手机扫码来料/订单尺寸页面接口会返回产品最新图纸；存在 PDF 时卡片显示“打开 PDF 图纸”按钮。
- 图纸上传权限保持现有管理员权限；管理员和车间可在手机页面查看。
- 无数据库结构变更。

## 2026-06-22 OCR 草稿匹配、多文件识别、成本参考与订单状态闭环

- 本轮未执行历史迁移，未修改历史订单，未修改 `legacy_*`，未写正式主库业务数据。
- 已完成 OCR 独立草稿层、手动换客户保留识别内容、客户三态匹配、产品/材质/规格候选匹配。
- 已完成多文件识别、文件 hash 去重、订单明细签名去重、逐份确认和批量保存已确认草稿。
- 已在新建订单和编辑订单明细显示客户单价、预估成本和预估毛利；数据不足显示待计算；对账主表未增加成本列。
- 已完成订单删除双确认、有关联订单删除门禁、死单/已结档备注和日志、默认未完成筛选与统计口径。
- 新增结构版本 `h48d9f6c1e32`，仅扩展订单状态检查约束；正式主库尚未执行，隔离副本升级演练通过。
- 测试：OCR/订单/前端 `49 passed`；送货/财务/报料跨模块 `38 passed`；合计 `87 passed`。
- 报告：`docs/go_live_checklists/OCR_ORDER_DRAFT_MATCHING_AND_ORDER_STATUS_OPTIMIZATION_20260622_105522.md`
- 下一步：人工验收真实 PDF；计划停机窗口内备份后执行 Alembic 结构升级并重启 8000 服务。

## 2026-06-21 工厂电脑独立 Codex 接手文档

- 新增独立交接文档：
  `docs/go_live_checklists/FACTORY_CODEX_HANDOFF_20260621_222400.md`
- 用途：
  - 给工厂电脑上的另一套 Codex 独立接手使用
  - 不依赖当前聊天记录
  - 明确正式项目目录、正式数据库路径、Z 盘旧目录不要作为运行目录
  - 明确下一步只做 OCR 订单识别草稿 / 新建订单 / 订单状态与成本显示流程优化
- 重点门禁：
  - 不执行历史迁移
  - 不批量修改历史订单
  - 不修改 `legacy_*`
  - 不把 `Z:\sata1-18015598002\纸箱厂erp软件搭建` 当正式运行目录
  - 不把 `Z:\sata1-18015598002\BoxERP\erp.db` 当正式订单库

## 2026-06-21 存量订单归档与纸板尺寸显示调整摘要

- 本轮未执行历史迁移，未修改 `legacy_*`。
- 已新增脚本：
  `scripts/admin/archive_existing_orders.py`
- 已执行一次主库存量订单归档：
  - 备份：
    `data/backups/carton_erp_before_archive_existing_orders_20260621_170308.sqlite3`
  - 截止 `order_id <= 15594`
  - 当前已有订单统一标记为已结款口径
  - 当前已有订单明细统一标记为 `已结算`，仅用于历史查看，不再参与待报料/待收料
- 主库核对：
  - `sales_orders = 15594`
  - `sales_order_items = 15660`
  - `pending_pool = 0`
  - `incoming_pool = 0`
  - `integrity_check = ok`
- 前端已调整：
  - 报料管理“纸板规格”显示为整数 `mm`
  - 报料弹窗列名改为 `纸板长(mm)` / `纸板宽(mm)`
  - 来料页面“纸板报料尺寸”显示为整数 `mm`
  - 收款核销前端不再要求输入收款账户
- 后端已支持收款核销缺省 `account`，但 8000 端口当前运行中的 Python 进程无法由本轮会话直接结束，若页面仍沿用旧逻辑，需要现场手动重启服务后生效。
- 回归测试：
  `.\.venv\Scripts\python.exe -m pytest tests\test_phase11_requisition.py tests\test_phase8_finance.py tests\test_phase14_frontend.py tests\test_phase15_requisition_finance_adjustments.py tests\scripts\test_archive_existing_orders.py -q`
  结果：`48 passed`

## 2026-06-21 历史报料归档与收款核销交互调整摘要

- 本轮未执行历史迁移，未修改历史订单，未修改 `legacy_*`，未写主库业务数据。
- 已调整报料接口：
  - 历史订单不再进入 `/api/requisition/pending` 待报料池。
  - 历史订单即使原始 `requisition_status` 仍为 `未报料`，也会在 `/api/requisition/items` 中按归档记录返回。
  - 归档历史报料项接口展示状态映射为 `settled`，用于只读查看，不回写数据库。
- 已调整财务收款核销：
  - 前端 `收款核销` 不再弹出“收款账户”输入框。
  - 后端 `/api/finance/statements/{id}/settle` 允许缺省 `account`，落库为空字符串，不要求改表。
- 相关回归测试：
  `.\.venv\Scripts\python.exe -m pytest tests\test_phase11_requisition.py tests\test_phase8_finance.py tests\test_phase14_frontend.py tests\test_phase15_requisition_finance_adjustments.py -q`
  结果：`44 passed`

## 2026-06-21 订单管理前端 UI 重构摘要

- 本轮未执行历史迁移，未修改历史订单，未修改 `legacy_*`。
- 已重构订单管理顶部区域：标题说明、主按钮区、筛选卡片区。
- 默认订单组列表已调整为“客户名称优先”，主系统单号默认隐藏。
- 展开区已改为浅色明细卡片，显示主系统单号与明细系统单号。
- 新建订单表单已补齐 `customer_po` 与下单日期。
- 订单组编辑表单已补齐 `customer_po`，通过 `/api/orders/{order_id}` 支持订单头编辑。
- 订单列表/详情新增 `display_material`，显示层会把 `045 A113B` 清洗为 `A113B`，不批量改原始库值。
- 订单列表接口新增日期范围与状态筛选参数：`date_from` / `date_to` / `status`。
- 只读主库校验：
  - `sales_orders = 15594`
  - `sales_order_items = 15660`
  - `legacy_ruida_orders = 39922`
  - `legacy_ruida_order_items = 40449`
  - `integrity_check = ok`
- 测试命令：
  `.\.venv\Scripts\python.exe -m pytest tests\test_phase5_orders.py tests\test_phase14_frontend.py tests\test_order_number_structure_rehearsal.py tests\scripts\test_apply_order_number_structure.py -q`
  结果：`31 passed`
- 报告：
  `docs/go_live_checklists/ORDER_MANAGEMENT_UI_REDESIGN_AND_FORM_FIX_20260621_145404.md`
- 补充说明：本轮尝试做内置浏览器自动验收时，运行时返回 `codex/sandbox-state-meta: missing field sandboxPolicy`，因此页面层面仍建议再做一次人工点击验收。

## 2026-06-21 订单编号结构主库实施摘要

- 本轮未执行历史迁移，未修改历史订单，未修改 `legacy_*`。
- 已创建主库备份：
  `data/backups/carton_erp_before_order_number_structure_apply_20260621_093327.sqlite3`
- 主库结构迁移已完成：
  - `sales_order_items.item_order_number`
  - `sales_order_items.item_sequence`
  - `order_item_number_sequences`
  - `ix_sales_orders_customer_po`
  - `ix_sales_orders_customer_po_group`
  - `ux_sales_order_items_item_order_number`
  - `ix_sales_order_items_snapshot_product_code`
  - `ix_sales_order_items_snapshot_product_name`
- 后端已启用新编号：
  - 主单：`TMYYYYMMDDNNN`
  - 明细：`TMYYYYMMDDNNN-001`
- 订单管理默认已切换到 `customer_id + customer_po` 分组；系统单号默认隐藏，展开后显示主 / 明细系统单号。
- 已创建 1 张受控测试订单并按业务规则作废：
  - 客户单号：`TEST-ORDER-NUMBER-VERIFY-20260621`
  - 主系统单号：`TM20260621001`
  - 明细系统单号：`TM20260621001-001/002/003`
- 不复用验证通过：
  - 下一张主单预览：`TM20260621002`
  - 下一条明细序号预览：`4`
- 主库当前数量：
  - `sales_orders = 15594`
  - `sales_order_items = 15661`
  - 历史订单 = `15589`
  - 台账订单 / 明细 = `15589 / 15651`
- `integrity_check = ok`
- 应用实际连接 `foreign_keys = 1`
- 验证报告：
  `docs/go_live_checklists/ORDER_NUMBER_STRUCTURE_APPLY_AND_VERIFY_20260621_093846.md`
- 下一步建议：重新执行综合人工试用验收，重点复核新建订单预览、分组展开、搜索与测试订单作废显示。

## 2026-06-20 阻塞项修复摘要

- 本轮未执行历史迁移，未修改历史订单，未修改 `legacy_*`。
- 修复了综合人工试用报告中的 10 个阻塞项：
  1. 历史订单列表改为 TM 显示编号
  2. TM 搜索恢复可用
  3. `/api/customers` 未登录改为 401，普通链路不再暴露旧追溯字段
  4. 客户默认分页统一 25
  5. 常用箱页面恢复客户主从视图
  6. 产品资料筛选补齐
  7. 新建订单未选客户前禁用产品明细输入
  8. 保存失败改为中文友好提示
  9. 订单管理补齐客户单号 / 客户名称 / 日期分组
  10. 历史订单稳定显示“历史归档”
- 相关测试：
  `.\.venv\Scripts\python.exe -m pytest tests/test_phase2_auth.py tests/test_phase3_api.py tests/test_phase5_orders.py tests/test_phase8_finance.py tests/test_phase14_frontend.py -q`
  结果：`54 passed`
- 主库只读核对保持：
  `sales_orders=15593`、`sales_order_items=15658`、历史订单 `15589`、`integrity_check=ok`
- 修复报告：
  `docs/go_live_checklists/MANUAL_TRIAL_BLOCKERS_FIX_20260620_205211.md`
- 建议下一步重新执行综合人工试用验收；本轮未创建测试订单。

更新时间：2026-06-19

## 1. 项目路径

用户指定交接路径：

```text
C:/Users/Administrator/Documents/纸箱厂erp软件搭建/
```

本线程实际工作区：

```text
D:/纸箱厂erp软件搭建/
```

新线程应先确认实际工作区，禁止因路径差异复制、覆盖数据库。

## 2. 当前重要结论

### 2.0 2026-06-19 dry-run 差异核对已完成

只读报告：

```text
docs/migration_reports/DRY_RUN_DIFF_20260619_081040.md
```

本轮未修改任何数据库，未刷新 `legacy_ruida_*`，未写入正式业务表。

| 对比 | SQL Server | SQLite legacy | SQL Server 独有 | SQLite 独有 |
|---|---:|---:|---:|---:|
| 订单 | 39,922 | 39,766 | 156 | 0 |
| 订单明细 | 40,449 | 40,293 | 156 | 0 |
| 客户 | 132 | 132 | 0 | 0 |

缺失订单 ID 为 `42688–42843`，缺失明细 ID 为 `43370–43527`，集中于 2026-06-01 至 2026-06-16。三组源键均无重复，订单/客户关联无孤儿。正式表仍为 `sales_orders=4`、`sales_order_items=7`。

### 2.0.1 legacy 原始层刷新方案已设计，尚未执行

方案：`docs/migration_reports/LEGACY_REFRESH_PLAN_20260619_083121.md`

脚本：`scripts/migration/refresh_legacy_ruida_from_sqlserver.py`

- 默认 dry-run，写入必须显式 `--apply` 和确认短语。
- 写当前主沙盘还需要 `--allow-main-sandbox`。
- 幂等键为 `legacy_order_id`、`legacy_item_id`、`legacy_customer_id`。
- 只追加缺失源 ID，不自动更新已有 legacy 行。
- 客户 132 对 132，默认不刷新。
- dry-run 实测预计新增：订单 156、明细 156、客户 0。
- 最新脚本 dry-run 报告：`docs/migration_reports/LEGACY_REFRESH_DRY_RUN_20260619_083632.md`。
- 本轮未修改数据库，未运行 `--apply`。

### 2.0.2 隔离副本 Apply 验证已通过

报告：`docs/migration_reports/LEGACY_REFRESH_APPLY_TEST_20260619_085901.md`

副本：`data/sandboxes/carton_erp_legacy_refresh_apply_test_20260619_085901.sqlite3`

- 仅副本执行一次 apply，主库未修改。
- legacy 订单：39,766 → 39,922；明细：40,293 → 40,449。
- 客户保持 132；`sales_orders` / `sales_order_items` 保持 4 / 7。
- Apply 后 dry-run 为 0 / 0 / 0。
- 幂等键无重复，孤儿明细为 0，关键字段对照差异为 0。
- 主库 SHA-256、大小、修改时间和表数量均未变化。

### 2.0.3 主库 legacy 原始层增量刷新已完成

报告：`docs/migration_reports/LEGACY_REFRESH_MAIN_APPLY_20260619_090705.md`

备份：`data/backups/carton_erp_before_legacy_refresh_apply_20260619_090705.sqlite3`

- 仅主库三张 `legacy_ruida_*` 表追加数据。
- legacy 订单：39,766 → 39,922。
- legacy 明细：40,293 → 40,449。
- legacy 客户保持 132。
- `sales_orders` / `sales_order_items` 保持 4 / 7。
- Apply 后 dry-run 为 0 / 0 / 0。
- `integrity_check = ok`，幂等键无重复，孤儿明细为 0。
- 本轮未执行任何正式业务表迁移。

### 2.0.4 正式表映射与100条副本试迁移已完成

方案：`docs/migration_reports/FORMAL_SALES_MAPPING_PLAN_20260619_091639.md`

副本：`data/sandboxes/carton_erp_formal_mapping_test_20260619_091639.sqlite3`

- 主库未修改，正式表主库仍为4/7。
- 副本正式表由4/7变为104/107，新增100个订单和100条明细。
- 样本金额165,528.97，完整性、外键和金额规则校验通过。
- Apply后同样本dry-run为0/0，幂等验证通过。
- 产品按“客户+款号精确匹配产品编码/客户料号”；未匹配整单跳过。
- 全量前仍需解决约30,304条未匹配产品明细。

### 2.0.5 产品匹配缺口与多明细专项测试已完成

报告：`docs/migration_reports/PRODUCT_MATCH_GAP_AND_MULTI_ITEM_TEST_20260619_094449.md`

副本：`data/sandboxes/carton_erp_multi_item_formal_test_20260619_094449.sqlite3`

- 产品唯一匹配10,145，0匹配30,304，多匹配0。
- 未匹配金额48,944,776.46，涉及30,074个订单。
- 主要原因是`style_no`包含“编码 / 描述”；提取斜杠前编码有27,431条唯一候选，但尚未应用。
- 找到90张完整多明细候选，选20张共56条明细做副本测试。
- 副本正式表4/7 → 24/63，逐单明细和金额差异均为0，复跑为0/0。
- 主库未修改，产品和客户资料未修改。

### 2.1 `erp.db` 不是历史订单源

`Z:\sata1-18015598002\BoxERP\erp.db` 只读探查结果：

| 项目 | 结果 |
|---|---:|
| 文件大小 | 258,048 字节 |
| `PRAGMA integrity_check` | `ok` |
| `customers` | 128 |
| `materials` | 46 |
| `products` | 1 |
| `orders` | 0 |
| `order_items` | 0 |
| `delivery_records` | 0 |
| `users` | 4 |

结论：不得把 `erp.db` 当作 4 万历史订单来源。

### 2.2 瑞达 SQL Server 数据规模

4 万历史订单位于瑞达 SQL Server 备份体系。审计文件和已还原数据库的记录数为：

| 表 | 记录数 |
|---|---:|
| `Orders` | 约 39,918 至 39,922 |
| `OrderXLs` | 约 40,445 至 40,449 |
| `CaiGouDanDetails` | 约 31,961 |
| `OrderXLs_common` | 约 3,407 |
| `Customers` | 约 132 |

### 2.3 当前主沙盘状态

`data/carton_erp.sqlite3` 已有瑞达原始抽取层：

| 表 | 记录数 |
|---|---:|
| `legacy_ruida_orders` | 约 39,766 |
| `legacy_ruida_order_items` | 约 40,293 |
| `legacy_ruida_customers` | 约 132 |

正式业务表当前数据很少：

| 表 | 记录数 |
|---|---:|
| `sales_orders` | 约 4 |
| `sales_order_items` | 约 7 |

结论：历史数据已经部分抽取，但尚未完成：

```text
legacy_ruida_* 原始层
→ sales_orders / sales_order_items 正式 ERP 业务表
```

## 3. SQL Server / BAK 状态

1. 初始环境没有 LocalDB 和 `sqlcmd`。
2. `winget` 可用，已找到官方包 `Microsoft.SQLServer.2022.Express`。
3. 安装介质已下载并解压到 `C:\SQL2022\Express_ENU`。
4. winget 引导器规则检查通过，但曾以 `0x84C4000E` 退出。
5. 后续直接调用 `SETUP.EXE` 创建独立实例 `BOXERP`。
6. `MSSQL$BOXERP` 服务后来显示 `Running`。
7. BAK 已复制到 SQL Server 可访问目录。
8. 本地副本与 Z 盘源文件 SHA-256 一致：

```text
39570E96824B2E5E054FACAF5BBF75612C5DB531982BE11F06EE94783459521D
```

9. `RESTORE VERIFYONLY = OK`。
10. BAK 信息：
    - 数据库名：`BoxDB20`
    - 备份时间：2026-06-16 14:50:06 至 14:50:26
    - 来源版本：SQL Server 2005
    - 数据库版本：611
    - 数据文件约 558 MB
    - 日志约 285 MB
11. 隔离还原目标：`BoxDB20_REPRO`。
12. 本线程曾观察到 `BoxDB20_REPRO` 为 `ONLINE`，但新线程仍须只读复核。
13. 禁止覆盖任何生产库或原始备份。

## 4. 下一步推荐路线

1. 新线程先读取本文件和项目根目录 `AGENTS.md`。
2. 不依赖旧聊天记录。
3. 以 `BoxDB20_REPRO` 为权威源，以现有 `legacy_ruida_*` 作为字段映射参考。
4. dry-run 差异统计和原始层增量刷新方案评审已完成。
5. 主库 legacy 原始层增量刷新已完成并归零。
6. 正式表映射和首批100条副本试迁移已完成。
7. 产品缺口分析和多明细专项样本已完成。
8. 斜杠前编码候选映射清单已生成，下一步只做人工复核结果文件设计与校验。
9. 未经新授权，不修改产品，不写入主库正式表。
10. 人工映射机制确认后，才评审扩大副本迁移范围。

## 5. 风险

- 不能把 `erp.db` 当作历史订单源。
- 不能直接覆盖 `data/carton_erp.sqlite3`。
- 不能直接把 4 万订单灌入正式业务表。
- SQL Server 2005 数据需处理字段类型、编码、日期和金额精度。
- 现有 `legacy_ruida_*` 比最新 BAK 少约 156 条订单，必须差异校验。
- 正式表已有少量数据，导入前必须备份并确认去重策略。
- 长日志写入 `docs/migration_reports/`，不要放入聊天上下文。

## 6. 斜杠前编码候选清单（2026-06-19）

- 只读脚本：`scripts/migration/analyze_product_prefix_candidates.py`
- 报告：`docs/migration_reports/PRODUCT_PREFIX_CANDIDATES_REPORT_20260619_150159.md`
- 全量候选：`docs/migration_reports/PRODUCT_PREFIX_CANDIDATES_20260619_150159.csv`
- 天华 Top 200：`docs/migration_reports/PRODUCT_PREFIX_TIANHUA_TOP_REVIEW_20260619_150159.csv`
- 严格斜杠记录口径：`prefix_unique=27,430`、`prefix_multi_match=0`、`prefix_no_match=707`。
- 疑似费用项 85 条、金额 173,704.40，禁止自动映射为普通产品。
- 模拟批准非费用 `prefix_unique` 后，完整可迁移订单预计由 9,823 增至 37,027，可迁移明细预计为 37,543。
- 主库 SHA-256 前后均为 `6ED950245BC91E18A8D4C8F55CB55B6630396B16117DF4145922C725E28E7F77`，未写数据库。

## 7. 人工复核与审批校验（2026-06-19）

- Worksheet：`docs/migration_reports/PRODUCT_PREFIX_TIANHUA_REVIEW_WORKSHEET_20260619_150757.csv`
- 填写说明：`docs/migration_reports/PRODUCT_PREFIX_REVIEW_GUIDE_20260619_150757.md`
- 校验脚本：`scripts/migration/validate_product_prefix_review.py`
- 流程报告：`docs/migration_reports/PRODUCT_PREFIX_REVIEW_WORKFLOW_20260619_150757.md`
- 首次校验报告：`docs/migration_reports/PRODUCT_PREFIX_REVIEW_VALIDATION_20260619_150845.md`
- 当前 200 行全部为 `pending`，已批准映射为 0，校验错误为 0。
- 脚本只读访问 SQLite，不提供 `--apply`，运行前后校验主库 SHA-256。
- 下一步由人工填写审批字段；完成小批审批并校验通过后，才设计隔离副本试迁移。

## 8. 天华已审核 worksheet 校验（2026-06-19）

- 审核文件：`docs/migration_reports/PRODUCT_PREFIX_TIANHUA_REVIEW_WORKSHEET_APPROVED_EXCEPT_32_157.csv`
- 校验报告：`docs/migration_reports/PRODUCT_PREFIX_REVIEW_VALIDATION_20260619_172131.md`
- 状态统计：approved 198、rejected 0、pending 0、needs_check 2。
- 字段、决定值、产品 ID、候选 ID、重复键、空白批准和费用项检查均无异常。
- CSV 第 32、157 物理行保持未批准，但使用 `needs_check + create_product_later`；现行规则要求 `create_product_later + rejected`，因此审批错误为 2，校验不通过。
- 主库 SHA-256 未变化，`integrity_check=ok`，正式表和产品表未写入。
- 当前门禁：禁止进入副本试迁移。下一步由用户决定修改 worksheet 状态或另行授权调整校验规则。

## 9. 天华审核状态修正与复验（2026-06-19）

- 修正文件：`docs/migration_reports/PRODUCT_PREFIX_TIANHUA_REVIEW_WORKSHEET_APPROVED_EXCEPT_32_157_FIXED.csv`
- 校验报告：`docs/migration_reports/PRODUCT_PREFIX_REVIEW_VALIDATION_FIXED_20260619_172509.md`
- 仅第 32、157 物理行由 `needs_check` 改为 `rejected`，决定保持 `create_product_later`，产品 ID 为空；其余 198 行未变化。
- 状态统计：approved 198、rejected 2、pending 0、needs_check 0。
- 审批错误 0，无费用项误批准，校验通过。
- 主库 SHA-256 未变化，`integrity_check=ok`，正式表和产品表未写入。
- 下一步只可设计“已批准映射驱动的隔离副本小批量试迁移”，仍不得写主库正式表。

## 10. 天华 approved prefix 副本试迁移（2026-06-19）

- 副本：`data/sandboxes/carton_erp_tianhua_prefix_review_apply_test_20260619_173046.sqlite3`
- 报告：`docs/migration_reports/TIANHUA_PREFIX_REVIEW_SANDBOX_APPLY_20260619_173202.md`
- Dry-run：100张订单、100条明细，prefix 80、精确20，金额689,972.39。
- 副本 Apply：正式表 4/7 → 104/107；产品表保持3,316。
- 两个 rejected 款号迁移数均为0；关联、金额、台账和完整性校验无差异。
- 复跑 dry-run 为0/0，幂等通过。
- 主库 SHA-256、大小、修改时间及4/7/3,316数量均未变化。
- 本结果仅证明隔离副本小批量验证通过，不授权主库正式表迁移。

## 11. 天华 approved prefix 全范围副本试迁移（2026-06-19）

- 副本：`data/sandboxes/carton_erp_tianhua_prefix_full_scope_test_20260619_174557.sqlite3`
- 报告：`docs/migration_reports/TIANHUA_PREFIX_FULL_SCOPE_SANDBOX_APPLY_20260619_174719.md`
- Dry-run/Apply：15,589张订单、15,651条明细，金额31,761,489.94。
- Prefix映射14,175条、精确匹配1,476条；多明细订单50张。
- 跳过8,598张：未匹配8,527、rejected 70、费用项1。
- Rejected款号、费用项和未匹配明细进入迁移数均为0。
- 逐单明细、金额、关联、产品外键和台账校验通过；复跑0/0。
- 主库哈希、元数据及4/7/3,316数量未变化。
- 本结果仍不授权主库正式表迁移。

## 12. 天华主库正式迁移就绪性评审（2026-06-19）

- 报告：`docs/migration_reports/TIANHUA_MAIN_APPLY_READINESS_REVIEW_20260619_175201.md`
- 本轮仅运行主库dry-run，未运行apply；基线仍为15,589/15,651，金额31,761,489.94。
- 当前结论：尚不具备主库迁移条件。
- 阻断1：脚本明确禁止规范主库路径apply。
- 阻断2：当前limit不能连续推进分批，需不可变批次清单。
- 阻断3：运行中API订单总数为6，而拟迁移主库正式订单为4，后端实际数据库未锁定。
- 建议5批：100、1,000、5,000、5,000、余量。
- 备份、停机、整库回滚、逐批验收方案已完成；必须完成脚本改造、数据库URL确认和副本分批/回滚演练后再申请授权。

## 13. 主库保护改造与5批副本演练（2026-06-19）

- 报告：`docs/migration_reports/TIANHUA_MAIN_APPLY_GUARD_AND_5BATCH_REHEARSAL_20260619_180153.md`
- 后端真实数据库确认：`data/tm_phase3_dev.sqlite3`，使用`orders/order_items`；不是拟迁移的`data/carton_erp.sqlite3`。
- API订单6来自`tm_phase3_dev.sqlite3.orders`；目标库正式订单4来自`carton_erp.sqlite3.sales_orders`。
- 原因：数据库模块不读取`.env`中的`ERP_DATABASE_PATH`，回退到相对默认路径。
- 迁移脚本已加入主库双重授权、预期哈希、不可变manifest和batch-id门禁。
- Manifest：`TIANHUA_MAIN_BATCH_MANIFEST_20260619_175953.csv`，5批固定100/1,000/5,000/5,000/4,489。
- 新副本5批连续演练通过，合计15,589/15,651，全部批次及全范围复跑归零。
- 主库未变化。剩余阻塞是后端数据库/表模型与迁移目标不统一，禁止主库apply。

## 14. 后端数据库路径统一（2026-06-19）

- 报告：`docs/migration_reports/DATABASE_PATH_UNIFICATION_APPLY_20260619_200919.md`
- 正式启动入口已由 `phase1_postgres.main:app` 改为 `app.main:app`。
- 后端现读取 `D:\纸箱厂erp软件搭建\data\carton_erp.sqlite3`，健康接口报告 `sales_orders/sales_order_items=4/7`。
- 前端首页由同一 8000 端口提供；RBAC 未登录请求保持 401。
- `tm_phase3_dev.sqlite3` 保留为废弃测试库，6 张测试订单不迁移。
- 主库哈希、大小、修改时间未变化，`integrity_check=ok`；相关测试 49 项通过。
- 活跃后端已使用 `sales_orders/sales_order_items`；旧 `phase1_postgres` 模型不再作为正式入口。
- 下一步仍须先做正式迁移前只读 dry-run、停机和备份复核，不得直接主库 apply。

## 15. 天华主库正式迁移前最终 Dry-run（2026-06-19）

- 报告：`docs/migration_reports/TIANHUA_MAIN_APPLY_FINAL_DRY_RUN_20260619_202709.md`
- 本轮未执行主库 apply，未写入正式订单。
- 正式字节级备份：`data/backups/carton_erp_before_tianhua_sales_apply_20260619_202543.sqlite3`
- 主库与备份 SHA-256 均为 `6ED950245BC91E18A8D4C8F55CB55B6630396B16117DF4145922C725E28E7F77`，完整性均为 `ok`。
- 五批主库 dry-run 合计 15,589 张订单、15,651 条明细、金额 31,761,489.94；Prefix 14,175、精确 1,476。
- 结果与 Manifest 和五批副本演练完全一致；rejected 款号、费用项和未匹配明细进入计划均为 0。
- 正式表使用 `INTEGER PRIMARY KEY`、非 AUTOINCREMENT，无对应 sqlite_sequence，风险低。
- 表外键已定义，现有 `foreign_key_check=0`；脚本 apply 会在事务前开启外键，正式执行仍须现场断言 `foreign_keys=1`。
- 当前可申请仅 `batch_1`（100 张）的主库 apply 授权；执行前必须停机、确认无占用并重做时点备份。

## 16. 天华主库 Batch 1 正式迁移（2026-06-19）

- 报告：`docs/migration_reports/TIANHUA_MAIN_BATCH_1_APPLY_20260619_203511.md`
- 仅执行 `batch_1`；未执行 `batch_2` 或其他批次。
- 时点备份：`data/backups/carton_erp_before_tianhua_batch_1_apply_20260619_203207.sqlite3`
- Apply 前主库/备份 SHA-256 均为 `6ED950245BC91E18A8D4C8F55CB55B6630396B16117DF4145922C725E28E7F77`。
- Dry-run 与 Apply 均为 100 张订单、100 条明细，金额 689,972.39；Prefix 80、精确 20。
- 正式表由 4/7 变为 104/107；产品、客户、legacy 和用户数量未变化。
- `integrity_check=ok`、`foreign_keys=1`、`foreign_key_check=0`；无孤儿、无效产品、重复台账、明细数或金额差异。
- Rejected 款号及费用项迁移数为 0；Batch 2 迁移数为 0。
- Batch 1 复跑 dry-run 为 0/0。
- 后端已恢复并连接正式库，健康接口报告 104/107。
- 下一步只读验收 Batch 1 页面显示，不得自动执行 Batch 2。

## 17. 天华 Batch 1 只读 API / 页面验收（2026-06-19）

- 报告：`docs/migration_reports/TIANHUA_BATCH_1_READONLY_ACCEPTANCE_20260619_205822.md`
- 本轮未执行 Batch 2、未执行 apply、未修改数据库，仅发送 GET。
- API 分页 3 页成功返回正式订单总数 104，累计读取到 100 张 `RUIDA-` 历史订单。
- Batch 1 数据库交叉校验：100 张订单、100 条台账明细；无孤儿、无效产品、空金额、非法数量、重复单号、rejected 或费用项。
- 已抽查 5 张订单的嵌套明细，客户、日期、金额、产品名称、规格和材质合理。
- 前端登录页加载正常且无控制台错误；因本轮禁止登录 POST，未进入认证后的订单页面。
- 当前 API 不支持订单号 keyword 搜索，`keyword=RUIDA-` 被忽略；订单详情为列表内展开，没有独立详情 GET。
- 当前不建议申请 Batch 2；先单独授权只读业务页面登录验收，或补齐搜索/详情能力后再评审。

## 18. Batch 1 搜索、详情与登录后页面验收（2026-06-19）

- 报告：`docs/migration_reports/TIANHUA_BATCH_1_SEARCH_DETAIL_ACCEPTANCE_20260619_211909.md`
- 本轮未执行 Batch 2、未迁移订单、未修改主库数据。
- `GET /api/orders` 已支持 `keyword`、`order_number`、`customer_name` 和分页。
- 新增 `GET /api/orders/{order_id}`，返回订单、客户和嵌套明细，不存在返回404。
- 主库搜索 `RUIDA-` 返回100，天华客户名称返回103（原有3+Batch 1的100）。
- 前端订单页新增订单号搜索和独立只读详情弹窗。
- 登录后页面验收使用主库副本和8004临时服务；正式主库只执行GET。
- 浏览器验收搜索100条、客户筛选、2页分页和5张详情均通过，无应用控制台错误。
- 回归测试34项通过；主库保持104/107、哈希不变、完整性为ok。
- 当前可申请 Batch 2 授权，但仍须单独授权并重新执行全部迁移门禁。

## 19. 天华主库 Batch 2 正式迁移（2026-06-20）

- 报告：`docs/migration_reports/TIANHUA_MAIN_BATCH_2_APPLY_20260620_074806.md`
- 仅执行 `batch_2`；未执行 Batch 3、4、5。
- 时点备份：`data/backups/carton_erp_before_tianhua_batch_2_apply_20260620_074551.sqlite3`
- Apply 前主库/备份 SHA-256 均为 `2EBFB2F9A0BE2B7F50A08E114B44532C25B7B497505C3CDAB73ACFE66A67EFD3`。
- Dry-run 与 Apply 均为 1,000 张订单、1,006 条明细，金额 1,230,739.83；Prefix 822、精确 184。
- 正式表由 104/107 变为 1,104/1,113；Batch 1 的100张保持不变。
- `integrity_check=ok`、`foreign_keys=1`、`foreign_key_check=0`；无孤儿、无效产品、重复台账或逐单金额差异。
- Rejected 款号及费用项迁移数为0；Batch 3～5迁移数为0。
- Batch 2 复跑 dry-run 为0/0。
- 后端已恢复并连接正式库，健康接口报告1,104/1,113。
- 下一步只读验收 Batch 2，不得自动执行 Batch 3。

## 20. 天华 Batch 2 只读验收（2026-06-20）

- 报告：`docs/migration_reports/TIANHUA_BATCH_2_READONLY_ACCEPTANCE_20260620_082805.md`
- 本轮未执行 Batch 3、未修改数据库，仅发送 GET。
- API 正式订单/明细为1,104/1,113，`RUIDA-`历史订单为1,100。
- 天华客户筛选为1,103（原有3+迁移1,100），与`RUIDA-`组合筛选为1,100。
- 第1、11、22页分页正常；15次列表查询中位10.98ms、最大31.44ms。
- 抽查Batch 1三张、Batch 2七张详情，明细、金额、日期和产品快照均与数据库一致。
- 前端搜索、客户筛选、组合筛选、分页、10张详情及对账页面均正常，控制台无应用错误。
- 主库哈希、大小、修改时间、当前1,104/1,113数量及操作日志计数前后不变，完整性为ok。
- 当前可申请Batch 3授权，但未经单独授权不得执行。

## 21. 天华主库 Batch 3 正式迁移（2026-06-20）

- 报告：`docs/migration_reports/TIANHUA_MAIN_BATCH_3_APPLY_20260620_085943.md`
- 仅执行`batch_3`，未执行Batch 4、5或全量迁移。
- 时点备份：`data/backups/carton_erp_before_tianhua_batch_3_apply_20260620_085704.sqlite3`
- Dry-run与Apply均为5,000张订单、5,054条明细，金额8,590,604.37。
- Prefix 4,638、精确416，多明细订单43张。
- 正式表由1,104/1,113变为6,104/6,167。
- `integrity_check=ok`、`foreign_keys=1`、`foreign_key_check=0`。
- 明细数、金额、关联、台账、排除项和sqlite sequence检查均无异常。
- Batch 3复跑为0/0；Batch 4/5迁移数仍为0。
- 后端已恢复并读取正式库，健康接口为6,104/6,167，`RUIDA-`为6,100。
- 下一步只读验收Batch 3，不得自动执行Batch 4。

## 22. 天华 Batch 3 只读验收（2026-06-20）

- 报告：`docs/migration_reports/TIANHUA_BATCH_3_READONLY_ACCEPTANCE_20260620_093126.md`
- 本轮未执行Batch 4/5、未修改数据库，仅执行GET和只读查询。
- API正式订单/明细为6,104/6,167，`RUIDA-`和天华迁移订单均为6,100。
- 指定第1、10、50、80、122页、客户筛选和组合筛选全部正常。
- 抽查Batch 1三张、Batch 2四张、Batch 3八张详情，数据与主库一致。
- 列表API 39次中位9.87ms、最大32.09ms；详情60次中位4.40ms、最大28.44ms。
- 前端15张详情、分页和对账开票收款页面正常，控制台无应用错误。
- `order_number`、`customer_id`、`order_id`关键索引均已存在，不建议重复加索引。
- 主库哈希、大小、修改时间、正式表和操作日志计数前后不变，完整性为ok。
- 当前可申请Batch 4授权，但未经单独授权不得执行。
## 23. 天华主库 Batch 4 正式迁移（2026-06-20）

- 报告：`docs/migration_reports/TIANHUA_MAIN_BATCH_4_APPLY_20260620_130527.md`
- 仅执行`batch_4`，未执行`batch_5`或全量迁移。
- 时点备份：`data/backups/carton_erp_before_tianhua_batch_4_apply_20260620_130056.sqlite3`
- Apply前主库/备份SHA-256一致：
  `1BE0238B6C5B9AD8BAA48488AB2DD7B3283B1D56D40E5460CF18BD4C408EC1FF`
- Dry-run与Apply均为5,000张订单、5,002条明细，金额16,331,793.23。
- Prefix 4,533、精确469，多明细订单2张。
- 正式表由6,104/6,167变为11,104/11,169；`RUIDA-`订单变为11,100。
- `integrity_check=ok`、`foreign_keys=1`、`foreign_key_check=0`。
- 无孤儿、无效产品、重复台账、重复单号、非法数量或逐单金额差异。
- Rejected款号、费用项和Batch 5迁移数均为0。
- Batch 4复跑dry-run为0/0。
- 后端已恢复并读取正式库，健康接口为11,104/11,169。
- 下一步只做Batch 4只读验收，不得自动执行Batch 5。
## 24. 天华 Batch 4 只读验收（2026-06-20）
- 报告：`docs/migration_reports/TIANHUA_BATCH_4_READONLY_ACCEPTANCE_20260620_133200.md`
- 本轮未执行 Batch 5，未修改数据库，仅执行 GET/API/页面只读验收。
- 健康检查继续指向 `D:\纸箱厂erp软件搭建\data\carton_erp.sqlite3`，返回 `sales_orders/sales_order_items = 11104/11169`。
- `RUIDA-` 历史订单数 = 11100；天华客户 + `RUIDA-` 组合筛选数 = 11100。
- 数据库交叉校验通过：Batch 1/2/3/4/5 订单数 = `100/1000/5000/5000/0`，明细数 = `100/1006/5054/5002/0`。
- `integrity_check=ok`、`foreign_key_check=0`、孤儿明细 0、无效产品 0、重复单号 0、费用项命中 0。
- API 深分页校验通过：第 1 / 10 / 50 / 100 / 150 / 200 / 最后一页均正常；列表接口中位 20.34 ms，详情接口中位 8.02 ms。
- 前端订单页、搜索 `RUIDA-`、客户筛选、组合筛选、详情抽屉和第 2 页翻页正常，浏览器 console 未见 error/warn。
- 当前允许申请 Batch 5 授权，但仍需新的单独授权、停机、锁检查、时点备份和 Batch 5 dry-run。
## 25. 天华主库 Batch 5 正式迁移与最终总对账（2026-06-20）
- 报告：`docs/migration_reports/TIANHUA_MAIN_BATCH_5_APPLY_AND_FINAL_RECON_20260620_135253.md`
- 仅执行 `batch_5`，未重新执行 batch_1/2/3/4，未发生全量滑批。
- 时点备份：`data/backups/carton_erp_before_tianhua_batch_5_apply_20260620_134925.sqlite3`
- Apply 前主库/备份 SHA-256 一致：`9C7E6BF7CAE37E02DD573F328250A53D075FD8E578C28BC6AB204EB029C13BD1`
- Batch 5 dry-run 与 apply 均为 `4489` 张订单、`4489` 条明细，金额 `4,918,380.12`。
- Prefix 明细 `4102`，精确匹配 `387`；rejected 实际选入 `0`，费用项选入 `0`，未匹配选入 `0`。
- 正式表由 `11104/11169` 变为 `15593/15658`；`RUIDA-` 订单变为 `15589`。
- 订单/明细台账分别为 `15589 / 15651`；五批订单数 `100/1000/5000/5000/4489`，五批明细数 `100/1006/5054/5002/4489`。
- `integrity_check=ok`、`foreign_keys=1`、`foreign_key_check=0`；无孤儿、无无效产品、无重复单号、无非正数量。
- Batch 5 逐单明细数差异 `0`、逐单金额差异 `0`；batch_5 legacy / sales 金额合计均为 `4,918,380.12`。
- 第 32、157 行 rejected raw 款号进入正式明细均为 `0`；费用项进入正式明细为 `0`。
- Batch 5 dry-run 复跑 `0/0`，全量剩余 dry-run `0/0`。
- 后端已恢复并读取 `D:\纸箱厂erp软件搭建\data\carton_erp.sqlite3`，健康检查为 `15593/15658`，`GET /api/orders?keyword=RUIDA-` 为 `15589`。
- 当前建议进入“最终只读验收”，不要宣布项目结束；之后还需做最终备份归档。

## 26. 最终只读验收与最终备份归档（2026-06-20）

- 最终只读验收报告：`docs/migration_reports/TIANHUA_FINAL_READONLY_ACCEPTANCE_20260620_142359.md`
- 最终备份归档报告：`docs/migration_reports/TIANHUA_FINAL_BACKUP_ARCHIVE_20260620_145234.md`
- 正式库：`data/carton_erp.sqlite3`
- 最终归档备份：`data/backups/carton_erp_FINAL_AFTER_TIANHUA_MIGRATION_20260620_145234.sqlite3`
- 当前正式表：`sales_orders=15593`、`sales_order_items=15658`
- `RUIDA-` 订单：`15589`
- 迁移台账：订单 `15589` / 明细 `15651`
- `integrity_check=ok`、`foreign_key_check=0`
- 第 32、157 行 rejected raw 款号未进入正式明细，费用项未进入正式明细
- 本轮未执行任何追加迁移写入

## 27. 日常使用前收口检查（2026-06-20）

- 文档目录：`docs/go_live_checklists/`
- 已生成：
  - `PASSWORD_AND_ACCOUNT_SECURITY.md`
  - `RBAC_PERMISSION_CHECK.md`
  - `BACKUP_AND_RESTORE_GUIDE.md`
  - `MANUAL_ACCEPTANCE_CHECKLIST.md`
  - `DAILY_OPERATION_GUIDE.md`
  - `GO_LIVE_READINESS_SUMMARY.md`
- 只读检查结果：
  - 正式库当前仅有 `admin`、`workshop` 两个账号
  - 未发现 `finance`、`sales` 正式账号
  - `workshop` 命中旧式 `123456` SHA256 痕迹，正式使用前必须重置
  - 未登录返回 `401`，伪造 Cookie 返回 `401`，越权访问返回 `403`
  - 手机来料页 HTML 可直接打开，但数据接口仍受登录保护
- 上线前仍需人工确认：
  - 财务 / 销售正式账号创建
  - 财务菜单是否过宽
  - 车间菜单是否过宽
  - 实际使用人员按清单完成一轮人工试用

## 28. 账号、密码与 RBAC 权限收口执行（2026-06-20）

- 报告：`docs/go_live_checklists/ACCOUNT_RBAC_HARDENING_APPLY_20260620_152716.md`
- 本轮未执行任何历史迁移 apply，未修改历史订单、legacy、客户、产品或迁移台账。
- 显式备份：`data/backups/carton_erp_before_account_rbac_hardening_20260620_150305.sqlite3`
- 正式账号现为：`admin / finance / sales / workshop`
- 四个账号均为现代密码哈希，`must_change_password=1`
- `workshop` 旧式弱口令哈希风险已移除；`admin` 已完成密码重置
- 后端 RBAC 已收紧：
  - 财务不可读产品/材质/来料，不可操作送货
  - 销售可读客户，但不可写客户；可操作订单/送货/报料
  - 车间仅保留订单/送货只读与来料访问
- 前端菜单已按 admin / finance / sales / workshop 分离
- 四角色登录验收通过；错误密码、未登录、伪造 Cookie 和越权门禁分别返回 `401 / 401 / 401 / 403`
- 手机来料页面 HTML 壳仍可直接打开，但数据接口未登录返回 `401`
- 回归测试：`80 passed`
- 剩余人工确认项：
  1. 现场管理员需重新设置四个账号的正式交接密码
  2. 财务菜单范围是否还需继续收紧
  3. 车间账号菜单范围是否还需继续收紧
  4. 手机来料页面壳公开是否接受
- 在“现场可交接密码”未设置前，不建议直接进入实际人员试用

## 29. 正式交接密码与人工试用准备（2026-06-20）

- 报告：`docs/go_live_checklists/FINAL_PASSWORD_HANDOFF_AND_TRIAL_READY_20260620_160320.md`
- 本轮未执行历史迁移，未修改历史订单、legacy、客户、产品或迁移台账。
- 已新增本地交互脚本：`scripts/admin/final_password_handoff.py`
  - `getpass` 隐藏输入四个账号正式交接密码
  - 不从命令行参数接收密码
  - 不把密码写入文档、报告、`.env` 或日志
  - 改密后会自动做登录 / 错误密码 / 登出 / 权限复核
- 新单测：`tests/scripts/test_final_password_handoff.py`
- 回归测试已通过：`83 passed`
- 已创建本轮正式改密前备份：
  `data/backups/carton_erp_before_final_password_handoff_20260620_154200.sqlite3`
- 主库 / 备份均为 `integrity_check=ok`、`foreign_key_check=0`，正式订单与历史订单数量未变化：
  - `sales_orders=15593`
  - `sales_order_items=15658`
  - `RUIDA-=15589`
- 本轮阻塞：正式交接密码必须由现场管理员在本机交互窗口输入，当前会话无法代替完成隐藏输入，因此未完成最终正式改密与登录验收。
- 下一步：由现场管理员本机执行交互脚本，完成后再做一次四角色试用验收。

## 30. 现场正式密码交接执行门禁（2026-06-20）

- 报告：`docs/go_live_checklists/ONSITE_PASSWORD_HANDOFF_AND_TRIAL_APPROVAL_20260620_165210.md`
- 本轮未执行历史迁移，未修改历史订单、legacy、客户、产品或迁移台账。
- 已创建新的改密前备份：
  `data/backups/carton_erp_before_onsite_password_handoff_20260620_165210.sqlite3`
- 主库与备份均为 `integrity_check=ok`、`foreign_key_check=0`
- 当前正式数量未变：
  - `sales_orders=15593`
  - `sales_order_items=15658`
  - `RUIDA-=15589`
  - 订单台账 `15589`
  - 明细台账 `15651`
- 回归测试通过：`83 passed`
- 按安全规则，本轮没有改密，因为正式密码必须由现场管理员本人在本机隐藏输入，不能由 Codex 代输，也不能写入参数、文件或日志。
- 已提供现场管理员手动执行命令：
  `.\.venv\Scripts\python.exe .\scripts\admin\final_password_handoff.py --sqlite-path .\data\carton_erp.sqlite3 --api-base-url http://127.0.0.1:8000 --output-json .\docs\go_live_checklists\FINAL_PASSWORD_HANDOFF_RESULT_LOCAL.json`
- 下一步：由现场管理员本机完成交互式改密，再读取本地 JSON 结果生成最终“可进入人工试用”批准报告。

## 31. 现场密码设置后最终复验（2026-06-20）

- 报告：`docs/go_live_checklists/FINAL_ONSITE_PASSWORD_VERIFICATION_AND_TRIAL_READY_20260620_165210.md`
- 本轮未执行历史迁移，未修改历史订单、legacy、客户、产品或迁移台账。
- 正式库继续为：`data/carton_erp.sqlite3`
- 数量保持不变：
  - `sales_orders=15593`
  - `sales_order_items=15658`
  - `RUIDA-=15589`
  - 台账订单/明细 `15589 / 15651`
- 新备份：`data/backups/carton_erp_before_onsite_password_handoff_20260620_165210.sqlite3`
- 主库 / 备份完整性均正常，`foreign_key_check=0`
- 已自动验证：
  - `admin/admin` 失效（401）
  - `workshop/123456` 失效（401）
  - 错误密码失效（401）
  - 未登录 / 伪造 Cookie / 未登录来料 API 门禁正常
- 四账号密码哈希均为 bcrypt (`$2b$`)
- 本轮无法在“不知道新密码明文”的前提下独立完成四个新密码登录复验；需把首次人工登录作为现场验收的一部分。
- 回归测试：`83 passed`
- 当前可进入“实际人员人工试用”，但建议以四角色首次真实登录作为首个试用动作。

## 32. 正式使用层历史订单显示优化与旧系统名称隐藏（2026-06-20）

- 实施报告：`docs/migration_reports/ERP_USABILITY_AND_HISTORY_DISPLAY_OPTIMIZATION_20260620_181607.md`
- 副本 TM 改号演练报告：`docs/migration_reports/HISTORY_ORDER_TM_RENUMBER_REHEARSAL_20260620_181354.md`
- 副本映射清单：`docs/migration_reports/HISTORY_ORDER_TM_RENUMBER_MAPPING_20260620_181354.csv`
- 本轮未执行任何历史迁移 apply，未修改主库历史订单号，未修改 `legacy_ruida_*`。
- 普通业务 API 与前端页面已统一隐藏旧系统名称；历史订单对外统一显示 `TMYYYYMMDD-####`。
- 已改造接口：`orders`、`incoming`、`requisition`、`finance`、`deliveries`。
- 已新增共享服务：`app/services/history_orders.py`。
- 已清理日常用户文档与页面中的旧系统名称；内部技术对象名和历史迁移审计材料暂保留。
- 已完成副本真实改号演练：15,589 条历史订单全部转为 TM 编号，重复 0，`integrity_check=ok`，主库未变化。
- 定向测试通过：`54 passed`。
- 后续若需主库真实批量改号，必须单独授权；当前仅完成显示层隐藏和副本演练。

## 33. 备份保留策略与常规备份清理（2026-06-20）

- 报告：`docs/go_live_checklists/BACKUP_RETENTION_CLEANUP_20260620_194430.md`
- 新增共享模块：`app/core/backup_retention.py`
- 新增管理脚本：`scripts/admin/manage_backups.py`
- `backup_to_nas()` 已接入“最近 5 个常规备份”自动保留策略
- `final_password_handoff.py` 的本地备份也已接入同一策略
- 保护备份关键词：
  - `FINAL`
  - `ARCHIVE`
  - `MIGRATION`
  - `tianhua_batch`
  - `tianhua_sales_apply`
  - `legacy_refresh`
- 本轮未执行历史迁移，未修改历史订单，未修改主库
- 清理前：`.sqlite3` 备份 44 个（常规 33 / 保护 11）
- Dry-run 计划删除常规备份 28 个，预计释放约 5718.50 MB
- Apply 已执行：实际删除 28 个常规备份，保护备份全部保留
- 清理后：`.sqlite3` 备份 16 个（常规 5 / 保护 11）
- 主库仍存在且 `integrity_check=ok`
- 相关测试通过：`15 passed`

## 34. 收款核销兼容修复与 PDF 订单识别草稿（2026-06-21）

- 本轮未执行历史迁移，未修改历史订单，未修改 `legacy_*`，未写主库数据。
- 收款核销兼容修复：
  - 前端 `收款核销` 请求已改为显式提交 `account: ""`。
  - 这样旧版后端若仍要求 `body.account` 字段，也不会再因缺字段报右下角红字。
  - 后端源码仍保持“收款账户可缺省”逻辑。
- 已新增 PDF 订单识别草稿能力：
  - 新服务：`app/services/order_pdf_import.py`
  - 新接口：`POST /api/orders/pdf-preview`
  - 现阶段流程为：上传 PDF → 识别草稿 → 人工确认 → 带入新建订单表单
  - 不会直接写正式订单。
- 前端订单页已新增：
  - `识别PDF订单` 按钮
  - `识别采购订单 PDF` 弹窗
  - `识别预览草稿` / `带入新建订单` 流程
- 实测 3 份样本 PDF 可识别出：
  - 客户名
  - 客户单号（采购单号）
  - 下单日期
  - 交货日期
  - 明细行
- 当前真实样本匹配情况：
  - 客户已可自动归一化匹配（如“有限公司”对“股份有限公司”）
  - 产品主数据尚未按样本存货编码建档，因此当前为“识别成功、产品待人工确认”状态
- 依赖补充：`requirements.txt` 已加入 `pypdf==6.0.0`
- 回归测试：
  `.\.venv\Scripts\python.exe -m pytest tests\test_phase8_finance.py tests\test_phase5_orders.py tests\test_phase14_frontend.py tests\test_phase16_pdf_order_import.py -q`
  结果：`55 passed`
- 运行态说明：
  - 当前 8000 端口旧 Python 进程 PID `25776` 无法由本会话结束（Access denied）
  - 新代码已可在 `127.0.0.1:8002` 正常启动并返回健康检查
  - 若要让 8000 正式页面立即获得 PDF 识别后端能力，需现场手动重启 8000 服务

## 35. 订单未送货统计、流程撤回、历史清理与手机入口（2026-06-22）

- 报告：`docs/go_live_checklists/ORDER_WORKFLOW_ROLLBACK_CLEANUP_AND_MOBILE_ENTRY_20260622_133828.md`
- 订单菜单角标已改为独立使用 `unfinished_total`，只统计真正未送货的日常订单，不再显示全部历史订单数。
- 订单管理默认进入“日常订单”，已送货完成的日常订单仍可查看；“未送货”和“历史订单”可单独筛选。
- 当前真实未送货角标应为 `1`，对应 `TM20260622001`（剩余 100）；不是人为清零。
- 送货单打印“款号”仅显示产品款号，已清除拼接在款号后的总数量、单价、总价、订单日期等污染内容。
- 新建 PDF 订单在报料、入库、送货、收款后仍保留在“日常订单”列表，不再因默认未完成筛选而消失。
- 已新增管理员“撤回到未报料”操作：
  - 必须填写撤回原因。
  - 会清理该订单对应的报料、入库、送货、回单、对账、开票、收款核销链并恢复未报料、未送货、未收款状态。
  - 若送货单或对账单混有其他订单，会拒绝自动撤回，避免误删其他订单业务数据。
  - 操作写入审计日志。
- 已新增手机扫码入口：电脑端业务中心可显示局域网访问地址和二维码，手机登录后可进入车间来料入库。
- “已报料/已入库”默认列表隐藏历史迁移数据；需要审计时可显式使用 `include_history=true` 查询。
- 用户明确授权的数据清理已执行：
  - 删除 2020 年以前正式订单 `6048` 张及明细 `6048` 条。
  - 清理后正式库订单 `9548` 张、明细 `9614` 条。
  - 2020 年以前订单剩余 `0`。
  - 当前“已报料/供应商已排单”明细剩余 `0`。
  - 未修改任何 `legacy_*` 表。
- 清理前保护备份：
  `data/backups/carton_erp_ARCHIVE_before_cleanup_pre2020_20260622_132429.sqlite3`
- 数据库复核：`integrity_check=ok`，`foreign_key_check=0`。
- 新增受控清理脚本：`scripts/admin/cleanup_pre2020_orders.py`，默认 dry-run，正式执行需同时提供 `--apply --confirm DELETE_PRE2020_ORDERS`。
- 定向回归测试：`108 passed`。
- ERP 已重新启动，`http://127.0.0.1:8000/api/health` 返回正常。

## 36. 正式 UI 审查与 Figma 重设计准备（2026-06-27）

- 本轮目标：审查 `http://127.0.0.1:8000/` 当前正式前端，为标准模式、大字模式和手机收料端 Figma 重设计建立证据。
- 审查记录：`docs/ui_audit/2026-06-27-figma-redesign/audit-notes.md`
- 已保存并人工核对运行态截图：
  - `docs/ui_audit/2026-06-27-figma-redesign/01-login.png`
  - `docs/ui_audit/2026-06-27-figma-redesign/02-mobile-incoming-logged-out.png`
- 已确认正式首页存在登录门禁；旧默认密码已经失效，本轮未猜测、读取或代填现场密码。
- 手机来料页在 390×844 视口下出现标题、提示和按钮逐字纵排的严重响应式问题。
- 手机来料页 viewport 当前禁止用户缩放，与老花眼和长辈友好要求冲突。
- 本轮未写入数据库，未执行订单、报料、来料、送货、回单、对账、开票、收款或系统设置操作。
- 下一步：现场用户在已打开的浏览器中手动登录后，继续逐页截图审查；完成证据板后再创建 Figma 设计系统和关键页面方案。

## 37. 正式 UI 完整审查与 Figma 部分交付（2026-06-28）

- 用户已在浏览器中完成管理员登录。
- 已基于真实运行态检查 18 个界面 / 状态：
  - 登录、首页、订单、报料、桌面来料、手机来料
  - 送货回单、财务、客户、产品、材质、系统设置
  - 新建订单、OCR 导入、新增送货单、生成对账单、新增客户
- 截图与审查记录：
  `docs/ui_audit/2026-06-27-figma-redesign/`
- 完整设计说明：
  `docs/product_planning/FIGMA_UI_REDESIGN_20260628.md`
- Figma 文件：
  `https://www.figma.com/design/iyA1wrl2IK7G0zI2OGgnKy`
- Figma 已完成：
  - “00 现状审查”页面
  - 18 张截图上传和逐页问题说明
  - “01 设计系统”页面骨架
  - 标题、设计目标和第一组颜色语义
- 关键发现：
  - 手机来料页 390px 下严重错位，登录态和真实数据下均复现
  - 手机页禁止缩放，与老花眼目标冲突
  - 桌面来料、送货、新建订单仍依赖横向滚动
  - 缺少标准 / 大字模式
  - 状态色不统一，危险操作过度暴露
- Figma Starter 方案已触发 MCP 调用上限，服务端拒绝继续写入；现有节点已保留。
- 本轮未写入数据库，未执行任何业务状态操作。
- 下一步：Figma 调用额度恢复或方案升级后，继续完成标准 / 大字组件对照和首页、订单、送货、财务、手机收料高保真画板。

## 38. Figma 额度阻塞确认（2026-06-28）
- 当前 Figma 文件 `iyA1wrl2IK7G0zI2OGgnKy` 仍可保留，但 MCP 写入 / 读取调用已经触发 Starter 方案上限。
- 明确报错：`You've reached the Figma MCP tool call limit on the Starter plan.`
- 受影响的后续动作：
  - `get_metadata`
  - `use_figma`
  - 页面补全
  - 组件对照
  - 截图验证
- 现在可继续做的事情：
  - 维护本地设计说明 `docs/product_planning/FIGMA_UI_REDESIGN_20260628.md`
  - 维护审查记录 `docs/ui_audit/2026-06-27-figma-redesign/`
  - 等额度恢复后，回到同一个 Figma 文件继续补完未完成页面

## 39. 天华预送货手机扫码拿货（2026-06-30）
- 新增独立手机页面：`/mobile/tianhua-pick?token=...`，不加载 ERP 后台菜单。
- 电脑端天华草稿支持生成 24 小时签名二维码，并刷新手机拿货状态。
- 手机端支持已拿货、没货、部分拿货、实际数量和备注；库存不足行仍允许现场确认。
- 手机实际数量同步到天华导入明细和独立草稿明细，不写正式送货、库存、回单或对账表。
- 新增角色代码 `delivery_picker`（送货拿货员），现有业务 API 的角色白名单均不包含该角色。
- 生产迁移版本：`x06r3s4t5u94`。
- 迁移前备份：`data/backups/carton_erp_20260630_163846_069028_TIANHUA_MOBILE_PICK_BEFORE_MIGRATION.sqlite3`。

## 40. RUIDA 清理与天华订单匹配（2026-06-30）
- 删除 9535 张 `RUIDA` 迁移订单和 9597 条订单明细，保留 5 张非 RUIDA 订单。
- 迁移映射同步删除；无正式送货、回单、对账、库存或报料关联记录。
- 168 条天华导入记录保留，仅解除已删除 RUIDA 订单绑定并标记为未匹配；天华草稿表未删除记录。
- 清理前备份：`data/backups/carton_erp_20260630_204709_728363_BEFORE_DELETE_RUIDA_ORDERS.sqlite3`。
- 清理报告和 Excel 清单位于 `docs/cleanup_reports/`；删除后完整性为 `ok`，外键违规为 0。
- 天华批次新增预送货日期，识别明细新增图片订单号、订单主表绑定、客户订单号、匹配说明、分数和候选数。
- 匹配顺序：图片订单号、数量完全匹配、预送货日期距离、未送数量差；排除 RUIDA 和非有效/已完成订单。
- 天华匹配迁移版本：`y17s4t5u6v05`，前置报价迁移：`z28t5u6v7w16`。

## 41. N-027 库存现金占用与积压洞察只读审计（2026-07-12）

- 独立 worktree：`D:\tm-worktrees\erp-inventory-insights-n027`；分支：`feature/inventory-insights-n027`。
- 正式库仅以 SQLite `mode=ro` + `PRAGMA query_only` 审计，未迁移、未写数据；`integrity_check=ok`。
- 正式库当前仅 1 个成品库存批次，已报废 100、可用 0；半成品批次和库存预占均为 0，现实三楼库存尚未形成事实账。
- 3295 个有效产品 `cost_unit_price` 覆盖率为 0%；382 个有效材质当前平方报价覆盖 100%，但当前报价不能冒充历史实际成本。
- 正式库迁移仍为 `ab29u7v8w9x18`，代码 head 为 `af33v7w8x9b23`；正式库未执行后续共享半成品和补库迁移。
- 结论：先做无迁移的数量/库龄/需求覆盖/成本待补确定性看板；成本事实账需后续独立 migration，并区分实际成本与估算成本。
- 审计报告：`docs/warehouse_reports/N027_INVENTORY_INSIGHTS_AUDIT_20260712.md`。
- AI 建议继续后置到 3～6 个月可信流水后，且永不自动修改采购、库存、预占或报损。
- Phase A 已完成无迁移编码：`GET /api/warehouse/insights` + 仓库页面“库存经营看板”，显示数量、六档库龄、需求覆盖、行动依据和成本待补。
- 库存洞察、库存基础、预占、共享半成品、配送消耗和补库相关回归共 `130 passed`，Python 编译和 `git diff --check` 通过。
- 隔离副本升级到 `af33v7w8x9b23` 后完整性 `ok`、外键异常 0；验收服务为 `http://127.0.0.1:18040/`，正式库未迁移、未写入。
- 页面烟雾验收确认：3 个批次、成品可用 20、半成品可用 30、2 条行动项、实际现金占用“成本待补”，浏览器控制台无错误。
- 页面明确提示“只统计已录入 ERP 的库存”“建议仅供人工判断”“不会修改库存、订单、预占或报料”；未出现“自动抵扣 / 自动少报 / 自动清理 / 自动报损 / 一键清理”操作。
- 当前闸门：等待用户按 `docs/warehouse_reports/N027_PHASE_A_UAT_CHECKLIST_20260712.md` 人工验收；通过前不提交、不 push。
- Phase A 已由用户验收通过，提交 `264aac3 feat: add read-only inventory insights dashboard` 已推送到 `origin/feature/inventory-insights-n027`；尚未合并主功能分支。
- Phase B 独立 worktree：`D:\tm-worktrees\erp-inventory-cost-snapshot-n027`；分支：`feature/inventory-cost-snapshot-n027`，当前未提交、未 push。
- Phase B 只新增“当前材料估算”快照：材料平方报价 × 报料面积，复用现有楞型加价；成品考虑每箱片数，A3 天地盖按盖与底面积相加。实际现金占用仍保持“成本待补”。
- 新迁移 `ai36v7w8x9e26` 仅在安全副本完成 upgrade / downgrade / re-upgrade；每阶段完整性 `ok`、外键异常 0，正式库未迁移。最终副本为 `D:\tm-worktrees\uat-data\carton_erp_inventory_cost_snapshot_phase_b_safe_20260713_125615.sqlite3`。
- 回填 dry-run 已改为 SQLite 真正只读连接；apply 需要 `COPY_ONLY + copy-root`，并拒绝正式库命名、目录越界、符号链接、多硬链接和已知 live database。
- 成本匹配要求材质和供应商一致，不再把 A 供应商基础价与 B 供应商楞型加价混用；UAT 副本中的损坏合成供应商值在备份后仅针对该副本规范化。
- 回填后当前材料估算 `¥127.84`（成品 `¥62.18`、半成品 `¥65.66`），可用批次成本覆盖 100%；重复 dry-run 候选 0。
- Phase B 库存、材质报价相关回归 `151 passed`，专项门禁 `32 passed`；未知计价单位和 A3 底片尺寸不完整均保持“成本待补”。完整演练报告：`docs/warehouse_reports/N027_PHASE_B_COST_SNAPSHOT_REHEARSAL_20260713.md`。
- Phase B 验收服务：`http://127.0.0.1:18043/`；用户于 2026-07-13 人工验收通过并批准分组提交。该批准不包含正式库迁移、回填或 push 主功能分支；验收清单：`docs/warehouse_reports/N027_PHASE_B_UAT_CHECKLIST_20260713.md`。

## 42. N-022 模具手机查询与二维码标签（2026-07-12）

- 独立 worktree：`D:\tm-worktrees\erp-mold-mobile-n022`；分支：`feature/mold-mobile-n022`。
- 未新增数据库字段或 migration；复用现有 `mold_tools.rack_location` 和常用箱 `mold_tool_id` 绑定。
- 新增手机只读页 `/mobile/mold-lookup`，可按模具编号、位置、客户、存货编码或产品名称查询。
- 新增标签页 `/mold-label.html` 和 `GET /api/warehouse/molds/{mold_id}/label`，二维码指向局域网手机查询页。
- 位置编码统一沿用盘点模板既定规则：平放 `3F-M-R02-L2-D03-P08`，重型竖放 `3F-M-R01-L1-V-P12`；旧自由文本仍可显示。
- 手机页显示人眼路线、产品名称、存货编码、规格和纸板方向提醒；重型竖放自动提示两人搬运。
- 自动回归共 `118 passed`；Python 编译和 `git diff --check` 通过。
- 隔离副本升级至 `af33v7w8x9b23` 后 `integrity=ok`、外键异常 0；验收端口 `http://127.0.0.1:18041/`，正式库未迁移、未写入。
- 浏览器已验证桌面模具列表、平放/竖放手机提示和标签二维码，控制台无错误。
- 用户已按 `docs/warehouse_reports/N022_MOLD_MOBILE_UAT_20260712.md` 完成人工验收，确认 `3F-M` 唯一位置编码并批准分组提交子分支。
- 三楼地图点位、模具移位扫码流水和现场照片仍属于后续 Phase C，本阶段不是无线实时定位。

## 43. 主 ERP 已验收功能交付（2026-07-14）

- 主功能分支已更新并推送到 `47b3cc7 merge: align incoming status and improve order search`；包含此前已合并的 N-022、N-024、N-026、N-027、N-028，以及本轮来料状态一致性、订单搜索和命中高亮修复。
- 旧的 `d274ea7` SPA 深链补丁未重复合并；主功能分支已有更完整的桌面模块深链、刷新保留页面、权限回退实现。
- 正式库迁移前在线备份：`data/backups/carton_erp_before_main_delivery_20260714_171811.sqlite3`；SHA256：`1938BD38E194752C7569AC423994BB8562B4D2150DA64E809DA195DDB99E1149`。
- 最新备份副本已完成 `ab29 → aj37 → ab29 → aj37` 往返演练；各阶段完整性 `ok`、外键异常 0，用户身份/角色/密码哈希及客户、产品、订单、库存等基线数量保持不变。
- 正式库已从 `ab29u7v8w9x18` 迁移到 `aj37v7w8x9f27`；迁移后完整性 `ok`、外键异常 0、11 张新基础表存在，原 4 个账号身份和密码哈希未改变。
- 主 ERP 已从 `D:\纸箱厂erp软件搭建` 重启在 `http://0.0.0.0:8000/`；`/api/health`、首页、`/warehouse.html` 和 `/requisition` 均返回 200。
- 核心订单/来料/送货/报料/前端/供应商单回归 `196 passed`；库存、权限、模具等相关测试 `156 passed`。另有 1 个天华手机预拿货测试在干净的 `78caa24` 基线也同样失败，确认不是本轮集成引入，后续单独修复测试权限夹具。
- 本次明确未交付：N-005 数量差异未闭环部分、N-029 轻量生产/短收结单、PDF 多客户识别、三楼货位 Stage A、旧 N-018 分支；这些仍保留在独立 worktree，禁止直接混入正式主线。

## 44. 三楼库存货位 Phase A 安全移植（2026-07-15）

- 目标 worktree：`D:\tm-worktrees\erp-floor3-locations-phase-a-v2`；分支：`feature/floor3-warehouse-locations-phase-a-v2`；基线：`f86b4ad` / `an41v7w8x9j31`。旧 worktree 仅作只读来源，未覆盖主线共享文件。
- 三楼货位基础迁移为 `ar45v7w8x9n35_floor3_warehouse_locations.py`，`revision=ar45v7w8x9n35`、`down_revision=aq44v7w8x9m34`。对既有 `warehouse_locations` 只逐列 `op.add_column`，未使用 `batch_alter_table(recreate='always')`，保留既有表、索引与外键；`storage_type` 由轻量触发器及应用层共同校验。
- upgrade 在任何结构或 seed 写入前检查 396 个编码是否与既有货位碰撞，碰撞时列出编码并中止；downgrade 在任何 DROP 前检查三张 Phase A 业务表为空、V11 不被正式库存表引用、396 条 seed 结构未漂移，否则拒绝回退。异常中文文案保持 UTF-8，并有乱码回归测试。
- V11 seed 固定 396 个货位、22 个区域、11 个临放位；排除 `LA1`、`LA2`、`LCD1`，`F12` / `F34` 为临放，`CD1` 共 14 个且备注容量待现场复核。迁移不创建栈板、明细或真实库存。
- 三楼列表、详情及写操作统一限制 `warehouse_floor=3 AND source_version='V11'`；正式库存入口统一排除 V11。权限继续复用 N028 的查看、执行及客户范围控制，未增加绕过路径。
- 追加、移位、清空及重定位均要求 `expected_version` 并使用 CAS，版本冲突返回 409；`access_restricted` 栈板前端不显示移位、清空等写按钮。
- 自动验证：三楼专项 `28 passed`；正式库存与 N028 相关回归 `79 passed`；来料迁移往返测试 `1 passed`；Python 编译、内联 JavaScript 语法、`alembic heads` 和 `git diff --check` 均通过。
- 本轮未连接、迁移或写入正式数据库，未 commit、未 push；正式部署前仍须在隔离副本复演 ao42，并由人工确认现场货位容量。

## 45. 三楼货位互动平面图与真实栈板移位（2026-07-15）

- 用户确认以 V11 PNG 作为三楼货位主视图：100% 缩放时整图单屏显示且页面不产生纵向滚动；放大到 120% 以上时才允许地图区域滚动。
- 交互分为两个层次：点击区域边界进入该区域聚焦视图；点击全图中的具体占用栈板不隐藏全图，而是在原图层上高亮并弹出栈板内容、数量和“进入货位详情”。
- `WarehouseLocation` 表示一个真实物理栈板位，`InventoryPallet` 表示一块真实栈板，栈板内容独立存储；日常移位和管理员布局编辑完全分离，布局变化不修改库存数量。
- 日常模式允许把真实栈板拖到空货位，也允许临时放到 F12 / F34 通道位，再二次移到正式货位；移动前必须确认，后端使用 `expected_version`、幂等键和 CAS 防止重复或并发覆盖。
- 管理员区域模式可新增、停用、恢复和移动物理栈板位；占用货位不能停用，越界布局和过期版本均被后端拒绝。
- 迁移链现为 `aq44v7w8x9m34 -> ar45v7w8x9n35 -> as46v7w8x9o36`，唯一 head 为 `as46v7w8x9o36`。`as46` 为 396 个 V11 货位写入可编辑布局；货架型区域按真实层数分布，不再把多层货位叠在同一坐标。
- 隔离副本 `D:\tm-worktrees\uat-data\carton_erp_floor3_app_uat_20260715_144243.sqlite3` 已完成 `an41 -> ao42 -> ap43 -> ao42 -> ap43` 演练；最终 `integrity_check=ok`、外键异常 0、布局 396 条。正式数据库未迁移、未写入。
- 隔离 UAT 中真实栈板 `UAT-REAL-PALLET-001` 已完成 `A1-L01 -> F12-P01 -> A1-L01`，移动前后数量均为 37；重复幂等请求不增加版本，布局编辑前后库存总量均为 37，过期布局版本返回 409。
- 自动验证：三楼专项 `51 passed`；库存、权限与来料相邻回归 `126 passed`；订单、来料、送货、报料、前端与供应商单核心回归 `197 passed`；Python 编译、唯一 Alembic head 与 `git diff --check` 均通过。
- 当前隔离验收地址：`http://127.0.0.1:18053/warehouse.html`。正式库、主系统、Git 提交与远端分支均未改动。

## 46. 库位管理与三楼平面图入口合并（2026-07-15）

- 仓库页只保留一个顶层“库位管理”入口，内部使用“三楼平面图 / 全部库位台账”两个子视图；默认进入三楼平面图，不再出现重复的“三楼货位”顶层入口。
- 三楼平面图继续允许具备 `warehouse.view` 的用户查看；“全部库位台账”维护入口仅管理员可进入，既有后端写权限边界未放宽。
- 新增物理货位成功后先刷新全局货位集合并立即重绘平面图，再刷新当前区域明细；即使区域明细加载失败，全局新增货位也不会被旧画面隐藏，且不会错误显示成功后仍不同步。
- 隔离 UAT 新增 `A1-UAT-11` 后，A1 货位从 10 增至 11，全局 V11 货位从 396 增至 397；全局图立即出现该货位，台账与平面图读取同一 `warehouse_locations` 主数据。
- UAT 中栈板内容总量仍为 37；新增货位、切换台账和返回平面图均未修改库存数量。`A1-UAT-11` 只存在于隔离副本，不在正式数据库。
- 当前验证：三楼迁移/API/前端 `52 passed`；合并入口前端/访问 `27 passed`；来料与补库相邻回归 `47 passed`；Python 编译、唯一 Alembic head `as46v7w8x9o36`、`git diff --check` 均通过。
- 正式数据库只读复核仍为 `an41v7w8x9j31`、`integrity_check=ok`、外键异常 0，且不存在三楼栈板表；正式库、主系统、Git 提交与远端分支均未改动。

## 47. 三楼仓库实尺互动平面图收口（2026-07-15）

- 本阶段交付边界是“三楼货位与真实栈板定位/盘点快照”：客户与产品候选读取正式主数据，但栈板内容不作为正式库存账，不自动参与报料抵扣、来料入库、送货出库或数量结算；这些联动必须在后续独立 Phase B 中实现。
- 用户确认使用资料包中的实际比例模型；平面图按 `37m × 47.4m` 世界坐标绘制，不再依赖 V11 PNG 作为交互底图。页面展示层旋转为横向满版，但保存的真实坐标与尺寸不变；100% 缩放时地图区域无横向或纵向滚动，放大后仍可查看细节。
- 三楼地图模式使用整页工作区，隐藏无关页头与重复导航；右侧固定显示库存统计与区域统计，地图保留 16 个非 F 区入口和 1 个 F 区总入口。非 F 区入口与区域轮廓彻底分离并统一放在相邻通道，轮廓不接收点击，入口不再覆盖底部栈板或货架格。
- 继续保留 22 个区域、396 个既有预设货位及其历史关联；区域同时显示“实际启用容量”和“实尺模型理论容量”，管理员可新增、停用或恢复实际货位。
- F2、F3、F4 明确为纯货架区域，共 90 个货架登记格；没有底部栈板位。货架格可登记、清空和查看物料，但不能作为真实栈板拖动的起点或目标。
- 真实栈板只允许移动到启用且空闲的地面位或 F12/F34 临放位；主通道不可作为货位，临放后保留待归位提示，所有移动继续使用确认、幂等键与 CAS 版本校验。
- 只有点击区域标识才进入区域缩略视图；点击具体货位不会离开全图，而是在固定右栏显示客户、存货编码、产品名称、数量和款式数。区域和货位图层均由同一套 `warehouse_locations` 主数据生成。
- F1/F2/F3/F4/F12/F34 在全图合并为单独的 F 区入口；进入后显示立体货架：F1 分为底部栈板货物和二层货架物料，F2/F3/F4 各按三层显示，F12/F34 作为临放位显示。点击任一层位会在右侧高亮显示具体内容。
- F2、F3、F4 三个三层货架采用等宽等高布局；F12、F34 固定排在 F1 下方，F 区工作区和页面均不产生横向滚动。
- D1 按现场口径显示为“二层一排 8 个货架格 + 底部两排各 10 个栈板位”；E4 显示为左侧一列 4 个货架格、右侧两列栈板位（L/R 各 6 位），三列均从上到下排列；DE1 的 5 个栈板位也从上到下排列。
- D1、E4、DE1 使用结构化默认布局，但管理员一旦保存人工布局即以保存值为准；区域内每个货架格或栈板位仍可单独点击查看详情，不改变库存数量或既有货位编码。
- 页面、全图、区域工作区与 F 区立体货架均禁止横向滚动，右侧详情不会被移出视口；F 区内部仅在内容高度超出时允许纵向滚动。
- 迁移链保持线性：`aq44v7w8x9m34 -> ar45v7w8x9n35 -> as46v7w8x9o36`，唯一 head 为 `as46v7w8x9o36`。
- 安全副本 `D:\tm-worktrees\uat-data\carton_erp_floor3_scale_uat_20260715_194617.sqlite3` 已完成 `aq44 -> as46 -> aq44 -> as46` 往返演练；最终完整性 `ok`、外键异常 0、布局 396 条，正式数据库未迁移或写入。
- 自动验证：三楼前端、API、迁移、来料、补库与更新脚本组合回归 `119 passed`；Python 编译、唯一 Alembic head `as46v7w8x9o36` 与 `git diff --check` 通过。
- 隔离验收地址：`http://127.0.0.1:18055/warehouse.html`。浏览器在 `1280 × 720` 下实测全图约 `937 × 537`、右侧固定栏约 `291 × 606`，页面、全图及 F 区货架横向溢出均为 0；F1/F2/F3/F4 共 4 个立体货架视图，层数依次为 `2/3/3/3`，层位详情可打开，控制台无页面错误。
- 区域入口避让复核：全图 16 个非 F 区入口与 283 个实际货位的矩形交叠数为 0；A1 区入口可正常进入缩略图，最底部 `A2-L01` 可直接点击并在右栏显示详情，页面和地图横纵溢出均为 0。

## 48. 仓库可视化库存交互升级第一阶段（2026-07-16）

- 继续复用 `static/warehouse.html` 的 DOM/CSS 绝对定位画布，没有另建演示页面、引入重量级绘图库或改变 `FLOOR3_ZONES`、货位布局坐标及既有 396 个预设货位。
- 三楼总览改为三栏工作区：左侧可折叠区域树显示启用、占用、空闲、待归位及区域使用率；中间保留真实比例地图；右侧固定显示选中货位、栈板和多条物料明细。
- 顶部工具栏提供区域、客户、占用状态和关键词组合筛选，以及清除定位、操作记录、适应画布、恢复默认、缩放、全屏和图例入口。
- `GET /api/warehouse/floor3/locations` 新增可选 `customer_id` 参数，继续复用 N028 客户范围校验；客户筛选可与关键词、区域和占用状态组合，不会泄露无权客户内容。
- 搜索输入采用 260ms 防抖并忽略过期响应；搜索结果可把非 F 区货位定位到全图 140% 并闪烁高亮，F 区货位进入 F 区真实货架视图后打开详情。
- 浏览模式与库位调整模式分离。退出调整模式或离开三楼视图后会清除待移动状态和旧操作控件；后端移动仍复用 `warehouse.execute`、确认、幂等键、CAS 版本及 409 刷新保护。
- 100% 视图在 `1294 × 920` 浏览器实测页面宽高均无溢出；区域导航折叠/展开、120% 缩放/恢复、A1 聚焦/返回、搜索定位及模式切换均完成浏览器验证，控制台无错误。
- 本阶段没有新增第三个迁移，没有连接或写入正式数据库，也没有实现正式盘点单、撤销移动、批量拖拽或三楼快照与正式订单/报料/来料/库存/送货数量联动；这些属于后续独立阶段。
## 49. 三楼区域布局拖拽与 E4 现场位置修补（2026-07-16）

- 修复管理员布局编辑中货位拖走后无法准确拖回原位的问题：拖拽记录鼠标在货位内的抓取偏移，落点按货位自身宽高限制在区域画布内，旋转显示与真实保存坐标可往返还原。
- 新增“撤销本区未保存调整”，可以在保存前恢复本次编辑前的位置，不写数据库、不改变库存数量。
- E4 放大视图继续保持左侧一列 4 格货架、右侧两列各 6 个栈板；全局平面图中货架保持左移后的现场位置，仅将两列栈板向右展开到接近 AB2 的显示宽度，允许覆盖不影响通行的死角。
- 本轮没有新增 migration、没有写正式数据库、没有提交或推送。

## 50. 成品仓与三楼货位一键绑定（2026-07-16）

- 日常操作收口为两条路径：成品做好后马上发货时不办理入仓；需要暂存时只办理一次“入成品仓”，选择客户、常用箱/产品、实际数量和三楼具体货位即可。
- 入成品仓不再一次加载并翻找该客户的全部常用箱：先选择客户，再手动输入存货编码或产品名称，260ms 防抖后显示匹配候选；候选同时显示编码、产品名称和规格，必须人工点击确认，修改关键词会立即清除旧选择。
- 成品仓库存表使用固定列宽在页面内完整显示，不再依赖横向滚动；批次固定为批次号和状态两行，客户最多两行，材质与楞型固定各一行。成品仓操作只保留“编辑”和“转通用”并右对齐，冻结、解冻、报损、报废不再占用日常列表；半成品仓原有操作保持不变。规格、材质/楞型和库位默认获得更多宽度；每个表头分隔线可像 Excel 一样拖动并同步反向调整相邻列，保持总宽不变，列宽保存在当前浏览器，双击分隔线恢复默认。
- 成品库存批次点击“编辑”后，才允许修改归属、存货编码、可用数量、三楼货位和入库日期。客户专用库存必须从该客户候选中重新点选产品；通用库存也可重新绑定客户和产品。库龄始终由入库日期计算，不提供直接篡改库龄数字的入口。已有预占或消耗时，数量、货位和日期仍可编辑，但产品、客户和通用归属会被 409 拦截，避免原订单预占或历史出库指向错误产品。保存使用幂等键、版本校验和单一事务，并以库存调整流水记录数量变化；货位变化同步真实栈板绑定。
- 正式 `inventory_lots` 继续作为唯一数量账；新增 `inventory_pallet_items.inventory_lot_id` 一对一关联，只把正式成品库存投影到三楼真实栈板，不再维护第二份可独立修改的数量。
- 入成品仓在同一事务内生成正式成品库存、`manual_in` 流水、当前货位真实栈板及关联物料；任一步失败会整体回滚。重复幂等请求不会重复入库或重复创建栈板内容。
- 三楼平面图对关联物料实时读取正式库存的可用量与预占量；正式库存调整后地图数量同步变化。移动栈板时同步更新正式库存批次的 `warehouse_location_id`，数量不变。
- 有正数正式库存的栈板禁止直接清空，必须先完成出库或库存调整；现场快速绑定产生的历史盘点快照继续明确标识，不会自动成为正式库存。
- 本阶段只接入成品仓。半成品与库存补库继续使用既有专用库位，未修改订单、报料、送货或对账数量口径。
- 新迁移 `at47v7w8x9p37` 线性接在 `as46v7w8x9o36` 后；降级前若仍有正式库存绑定会拒绝删除关联字段，避免产生无位置追溯的业务数据。
- 正式库仅只读复核：`aq44v7w8x9m34`、`integrity_check=ok`、外键异常 0，未迁移、未写入。隔离副本 `D:\tm-worktrees\uat-data\carton_erp_floor3_binding_uat_20260716_112558.sqlite3` 已完成 `aq44 -> at47 -> as46 -> at47` 往返演练，最终完整性正常。
- 副本模拟把天华产品 `22000107` 的 9 个成品一次入仓到 `A1-L01`，自动生成正式批次 `FG-20260716-24B8C08ACA` 和真实栈板；移到 `A1-L02` 再回移 `A1-L01` 后数量始终为 9，幂等重放仍只有 1 个正式批次、1 个关联物料。
- 隔离验收地址为 `http://127.0.0.1:18056/warehouse.html`，仅连接上述副本；测试账号 `admin` 的副本密码为 `123456`，正式账号密码未修改。
- 成品仓、三楼 API/前端/迁移、库存预占、来料、补库和更新脚本组合回归 `188 passed`；Python 编译、唯一 Alembic head `at47v7w8x9p37` 与 `git diff --check` 均通过。

## 51. N-029 轻量生产完工与库存流转（2026-07-17）

- 独立 worktree：`D:\tm-worktrees\erp-production-n029-v2`；分支：`feature/production-n029-v2`；基线已包含半成品批次款号硬绑定 `721b280` 与七层材质支持 `54f6db4`。本阶段未推送、未合并主功能分支。
- 在来料与送货之间新增轻量生产任务：新订单按明细建立任务，材料满足后进入待完工；同一客户可批量确认，必须逐行选择“直接送货”或“转临时成品库”，系统不替员工猜测。
- 转库存只允许三楼 `F12` / `F34` 临放位，复用正式成品库存、真实栈板、成本快照和库存预占；直接送货完工可在真实发货前转库存，发生任何有效 `dispatched` 发货后即使回单把累计数量调回 0 也禁止整批转库。
- 生产完工会消费对应半成品预占；生产管理明细发货时不再重复消费半成品。若订单已有半成品预占，新增成品抵扣会返回 409，避免两类库存同时被占用。
- 生产完工、转库存、发货、订单终止/删除/回退及来料撤销统一使用 `Order -> Delivery -> OrderItem` 锁序，并在持锁后重读状态；终态、强制关闭、已送货、过期版本和已有生产事实均由后端再次拦截。所有批量完工与转库存请求保留幂等键和请求内容哈希。
- 半成品路径完工后禁止再叠加普通成品抵扣；存在活动生产完工成品预占时禁止强制结案。已经产生库存出库分配的送货单不能被 workflow rollback 直接删除，接口明确返回 409 并要求先撤销发货恢复库存，避免外键 500 和库存事实丢失。
- 旧订单若没有 `production_tasks`，继续保持原来在发货阶段消费半成品的兼容路径；新订单必须完成生产任务后才能送货。N005 超送仍只显示黄色提醒，没有新增强制原因。
- 新迁移 `ax51v8x9z41` 线性接在 `aw50v8x9y0s40` 后，仅新增 `production_tasks`、`production_completion_batches`、`production_completions`、`production_stock_transfers` 四张表；存在任何生产任务或完工事实时 downgrade 均会拒绝，避免新订单降级成可绕过生产的旧兼容订单。
- 正式库副本 `D:\tm-uat\production_n029_20260717_034833\carton_erp_uat.sqlite3` 已在最终迁移代码下再次完成 `ax51 -> aw50 -> ax51` 演练，最终 `integrity_check=ok`、外键异常 0、唯一 head 为 `ax51v8x9z41`。正式数据库仍只读保持 `au48v8x9y0q38`，未迁移、未写入。
- 自动验证：N029 迁移/服务/API/前端 `84 passed`；订单、来料、报料、权限及旧兼容分组 `145 passed`；订单与送货 `84 passed`；成品/半成品库存与送货分组 `78 passed`；系统版本接口 `16 passed`。Python 编译、内联 JavaScript 语法、唯一 Alembic head 和 `git diff --check` 均通过；独立最终审查结论为无 P0/P1。
- 版本日志更新为 `v0.22.7 轻量生产与库存约束版`。本阶段尚未启动统一 UAT，需等后续 P4、N030、N031/PDF C、三楼 Phase B/P5/N033 全部收口后一次性人工验收。

## 52. P4 主数据版本追溯与并发保护（2026-07-17）

- 独立 worktree：`D:\tm-worktrees\erp-master-data-versioning-p4`；分支：`feature/master-data-versioning-p4`。提交链按计划依次包含半成品批次绑定、七层材质、N029 轻量生产，再接 P4；没有回滚其他阶段代码。
- 迁移 `ay52v8x9z42`（ay52）线性接在 `ax51v8x9z41`（ax51）后，覆盖客户、常用箱/产品和材质主数据的版本历史、字段快照、修改原因与操作者追溯；并发冲突返回 409，异常变更要求二次确认，管理员恢复历史会作为新版本写入，敏感价格按权限脱敏。
- 最终副本演练路径：`D:\tm-uat\master_data_p4_final_20260717_072226\carton_erp_uat.sqlite3`。已完成 `ay52 -> ax51 -> ay52` 往返演练；最终 head 为 `ay52v8x9z42`，`integrity_check=ok`，外键异常 0，不可变账本 UPDATE/DELETE 触发器均存在。现有客户 131 条、产品 3323 条、材质 387 条均初始化为 `version=1`。
- 旧离线写脚本默认 fail-closed；只有显式 dry-run 可以读取并输出预览。系统批量更新采用预览、逐对象确认令牌和过期版本 409，避免绕过版本账本。
- 本轮用完整业务依赖环境并追加纯测试工具路径实跑：P4 核心/写入/间接写入/迁移/前端/旧脚本守卫 `53 passed`；订单、报料、客户范围、产品生命周期 `119 passed`；材质映射、楞型映射、调价、系统更新、七层写入守卫 `165 passed`，合计 `337 passed`。独立审查发现的旧客户合并脚本绕过与已停用产品移入垃圾站不记版本问题均已补回归测试并通过。
- 正式库未迁移、未写入；P4 仍需与后续阶段一起进入统一 UAT，统一验收通过后再 push、合并和单独批准正式迁移。

## 53. P5 老板大字版与重点看板（2026-07-17）

- 独立 worktree：`D:\tm-worktrees\erp-boss-large-ui-p5`；分支：`feature/boss-large-ui-p5`，线性接在 P4 的 `ay52v8x9z42` 之后，没有回滚前序阶段。
- 新迁移 `az53v8x9z43` 为用户增加 `ui_mode=standard/large`；历史老板账号迁移为大字，其他账号保持标准，新建或晋升为老板时默认大字。所有用户均可在顶部切换，偏好持久化到账号并记录操作日志。
- 大字模式正文 18px、标题 28px、标签 16px、按钮和输入框最小 48px，并为 1366×768/125% 与 1920×1080/150% 等常见显示组合提供响应式布局；普通角色的标准模式不受影响。
- 老板首页固定显示待报料、待入库、待送货、应收、库存风险和经营异常六张业务卡；库存卡只返回待处理数量，不暴露单位成本或金额。老板页面隐藏系统维护和权限技术入口，仍受后端 `warehouse.view` 等权限控制。
- 自动验证：P5/权限核心 27 项通过，完整 N028 权限与认证/P5 回归 64 项通过，独立认证回归 13 项通过；Python 编译、JavaScript 语法、Alembic 单一 head、迁移升降级和数据库完整性还需在最终副本演练中再次确认。
- 本阶段只在隔离数据库副本验收，不迁移正式数据库，不 push；待后续 N033、N031 等计划阶段全部收口后统一人工验收。

## 54. N033 北京时间一致性与时间边界保护（2026-07-17）

- 独立 worktree：`D:\tm-worktrees\erp-beijing-time-n033`；分支：`feature/beijing-time-n033`，线性接在 P5 的 `az53v8x9z43` 之后。N033 不新增数据库表、字段或 Alembic migration，也不批量改写历史时间。
- 新增统一时间契约：业务日期固定按北京时间（UTC+8）计算；数据库 `CURRENT_TIMESTAMP` 和新审计时间按 UTC-naive 保存；API 时间必须明确返回 `Z` 或 `+08:00`，业务日期继续使用 `YYYY-MM-DD`。
- 北京业务日期筛选统一转换为半开 UTC 区间 `[前一日 16:00, 当日 16:00)`；修复凌晨 00:00 至 07:59 的记录被归入前一天、日期查询边界重复或漏查的问题。
- 历史兼容不采用全表统一减 8 小时。正式库只读核对确认：产品手工修改/删除/清理时间、公司配置更新时间、三楼货位移动时间和供应商报料作废时间过去由 `datetime.now()` 写入，是北京时间 naive；这些字段继续按北京时间语义写入并返回 `+08:00`。数据库默认时间、库存账本、操作日志等 UTC 字段继续返回 `Z`。
- 静态前端和 Vue 前端统一使用共享时间工具；UTC 时间先转换为北京时间再显示或提取日期，不再直接截取 UTC 字符串。订单默认交期继续按自然日增加，没有误改为工作日。
- 最终自动验证：N033 时间契约、API、服务、前端、历史兼容和系统版本测试 `60 passed, 1 skipped`；订单、来料（排除已知旧迁移降级用例）、报料、送货相邻回归合计 `156 passed`；Python 编译和共享 JavaScript 语法通过。此前订单、来料、送货、报料、N029、P4、P5、N028 组合回归 `267 passed`，另有 1 个早于 N033 已存在的 SQLite 旧迁移降级测试失败（`aq44` 删除 `statement_cycle_start_day`），与 N033 无迁移改动无关，留待最终迁移集成阶段单独修复。
- 隔离 UAT `http://127.0.0.1:18064/` 连接副本 `D:\tm-uat\beijing_n033_final_20260717_101256\carton_erp_uat.sqlite3`：来料订单创建时间和实收时间返回 `Z`，供应商报料单作废时间及三楼栈板移动时间返回 `+08:00`，仪表盘排序日期保持 `YYYY-MM-DD`；未连接或写入正式数据库。
- 本阶段暂不 push、不迁移正式数据库；须先完成隔离副本 UAT 和最终独立审查，再进入 N031 安全远程访问底座。

## 55. N031 安全访问底座与会话撤销保护（2026-07-17）

- 独立 worktree：`D:\tm-worktrees\erp-security-remote-n031`；分支：`feature/security-remote-access-n031`；线性接在 N033 提交 `0951c13` 后。本阶段只建设安全访问底座，不开放公网端口、不修改路由器、防火墙、域名或证书。
- 公开 `GET /api/health` 只返回最小可用状态，数据库异常返回 503，不再暴露数据库路径、记录数或内部错误；公开 `GET /api/system/version` 仅返回版本摘要，完整 `GET /api/system/version/changelog` 必须登录后读取。
- 生产环境启用安全 Cookie、HTTPS 跳转、HSTS、可信主机、显式 HTTPS 跨域来源和明确的受信代理配置；生产默认只监听 `127.0.0.1`，必须提供至少 32 字符的会话密钥或预先配置密钥文件。开发与隔离 UAT 继续允许本机 HTTP 调试。
- 会话令牌新增 `auth_version`。退出登录、修改密码、停用账号、调整角色、功能权限、客户范围或访问范围时递增认证版本，使旧会话立即失效；天华手机拿货等专用短期令牌保持独立用途，不混入后台登录会话。
- 登录接口按“用户名 + 来源 IP”统计最近 15 分钟失败次数，连续 5 次失败后返回 429；失败与限流写入 `OperationLog`，不记录密码，不会因为共享办公网络锁住其他用户名。登录成功后该用户名与来源重新开始计数。
- 新迁移 `ba54v8x9z44` 线性接在 `az53v8x9z43` 后，仅给用户表增加认证版本字段。隔离副本 `D:\tm-uat\security_n031_20260717_110044\carton_erp_uat.sqlite3` 已升级到 `ba54v8x9z44`，`integrity_check=ok`、外键异常 0；降级保护副本验证在已有账号时会 fail-closed，避免丢失会话撤销状态。
- 最终自动验证：N031 安全、认证、迁移、启动脚本、系统页面与相邻回归 `125 passed`；订单、送货、报料与 N028 权限矩阵 `157 passed`；来料回归 `37 passed`，另有 1 个 N031 之前已存在的 `aq44` SQLite 降级触发器用例失败，留待迁移链收口阶段单独修复。
- 隔离 UAT `http://127.0.0.1:18065/` 只连接 `D:\tm-uat\security_n031_20260717_110044\carton_erp_uat.sqlite3`：公开健康检查 200、匿名精确版本 401、登录后版本/日志 200、退出后旧 Cookie 401、重新登录成功、连续错误登录返回 `401 × 5 + 429`；服务日志确认未连接正式数据库。
- 正式数据库尚未执行本迁移，分支尚未 push；后续正式迁移、反向代理或远程访问启用必须单独审批。

## 56. N031 反向代理安全契约（2026-07-17）

- 生产后端只监听 loopback；ERP_TRUSTED_PROXY_IPS 只能配置实际连接后端的 loopback TCP peer（127.0.0.1 或 ::1），不能填写 LAN 地址、公网地址或通配符。
- 反向代理转发到 ERP 时必须覆盖（overwrite）而不是追加客户端传入的 Host、X-Forwarded-Proto、X-Forwarded-For。其中 Host 必须写为批准的外部主机，X-Forwarded-Proto 必须由 TLS 终止状态写为 https，X-Forwarded-For 必须由代理重建为实际客户端地址。
- 禁止把客户端原有的上述头部拼接进转发值；否则 loopback 代理会把攻击者伪造内容当成可信来源。代理上线前必须验证不可信 TCP peer 的伪造头无效、loopback 代理覆盖后的头才生效。
- 启动脚本只用 http://127.0.0.1:<port>/api/health 判断本机进程是否就绪；本机就绪后才单独检查外部 HTTPS。外部证书或代理异常只告警，不得把外部重定向当作本机就绪，也不得终止已经本机就绪的 ERP 进程。
- 登录限流使用进程内按“规范化用户名 + 来源 IP”划分的细粒度锁；同一键的计数、门禁、验密和短审计写入有序，不同键可并行验密，且不再持有 SQLite 全库写锁跨越 bcrypt。该契约依赖单 worker：`ERP_WORKERS` 只能为 `1`，部署生成器与 Windows 启动器均强制该值；任何绕过项目启动器的部署同样必须只启动一个应用 worker。

## 57. N034 Phase A 复合产品 BOM 交接（2026-07-18）

- 独立 worktree：`D:\tm-worktrees\erp-composite-bom-n034`；分支：`feature/composite-bom-n034`。本轮仅追加项目文档交接，未 commit、未 push。
- 迁移 `bd57v8x9z47` 线性接在 `bc56v8x9z46` 后；新增 `product_bom_components`、`sales_order_item_bom_components`、`requisition_item_bom_sources` 三张表，并在 `products` 增加 `is_composite`、`is_internal_component` 两个标记。
- 订单 BOM 历史快照是不可变事实：快照字段更新被拒绝；模板来源 `product_bom_component_id` 仅允许因模板删除而 `SET NULL`，不得反向改写其他快照字段。
- downgrade 采用 fail-closed：三张 N034 表任一存在事实行，或 `products` 中任一 `is_composite` / `is_internal_component` 为真，均拒绝降级；仅在三表全空且两个 Product 标记均无真值时允许降级。
- 隔离副本 `D:\tm-uat\composite_bom_n034_20260718_130112\carton_erp_uat.sqlite3` 已完成 `head -> base -> head` 往返演练，最终 `integrity_check=ok`、`foreign_key_check=0`；自动测试 `144 passed`。UAT 端口：`18068`。
- 本轮未写正式库；正式库未迁移、未写入。业务代码和已有工作区改动未在本轮处理。

## 71. N038 PDF 识别小白纠错闭环（2026-07-18）

- 独立 worktree：`D:\tm-worktrees\erp-pdf-correction-n038`；分支：`feature/pdf-correction-assistant-n038`；基线为 `f2df300 merge: optimize paginated delivery note printing`。本阶段尚未提交、推送或合并。
- 订单管理 PDF 识别草稿新增“识别有误？提交改进”。管理员可在当前草稿内用普通中文表单修正客户、客户单号、日期及明细编码、名称、规格、数量、单位、单价和金额；提交后持续显示样本 ID 与“待管理员复核”。该操作不会确认草稿、加入批量保存或创建正式订单。
- 新增 `POST /api/pdf-training/samples/submit-correction`：接收原 PDF、确认客户、人工正确结果和说明；按 SHA-256 复用或创建训练样本，保存可复核的原始 PDF，写入人工标注、评分和字段差异记录。重复提交相同内容不会重复创建样本或差异记录；同一 PDF 已绑定其他客户时返回 409。
- 重新解析训练样本前统一校验原 PDF 的 SHA-256；文件缺失、内容被替换或摘要不一致时拒绝重新解析，不再直接读取不可信文件。
- 客户模板生命周期能力仍保留，但默认折叠为“管理员高级设置（一般操作无需使用）”。提交纠错不会创建、激活或退休客户模板；模板生效仍必须由管理员复核金样本并完成原有 dry-run/激活流程。
- 本阶段没有新增数据库表、字段或 Alembic migration；没有连接或写入正式数据库。隔离测试明确断言提交纠错前后正式订单表记录数为 0。
- 自动验证：N038 后端/前端 `8 passed`；PDF 训练库前端 `6 passed`；N016 模板生命周期/执行 `27 passed`；订单 PDF 导入 `29 passed`；思迈尔/天华/高泰/PDF 修复回归 `22 passed`；Python 编译和 `git diff --check` 通过。按安全边界未运行会写现有数据库的 `tests/test_phase18_pdf_training.py` 集成测试。
- 隔离 UAT 已启动于 `http://127.0.0.1:18067/`，数据库副本为 `D:\tm-uat\pdf_correction_n038_20260718_105028\carton_erp_uat.sqlite3`。启动日志明确打印该副本路径；副本 `integrity_check=ok`、外键异常 0、head=`bc56v8x9z46`，基线正式订单 31 条。测试账号 `admin` 的副本密码为 `123456`，正式账号密码未修改。
- 下一步人工验证“订单管理 -> PDF 识别 -> 提交改进 -> 获得样本 ID -> 训练库待复核”，并再次确认正式订单数量不变；人工通过后才允许提交和推送子分支。

## 72. 订单录入提示、生产回退与库位联动（2026-07-21）

- 独立 worktree：`D:\tm-worktrees\erp-ux-step-back`；分支：`codex/erp-ux-step-back`；基线为 `fb0aaca`。本轮没有修改主目录、N041 worktree、报料更新回退 worktree或永久只读交接备份。
- PDF 识别草稿和新建订单均在“已匹配常用箱”旁显示“常用箱已修改 / 常用箱未修改”。未修改时可直接打开完整常用箱编辑弹窗，保存后返回原 PDF 草稿或新建订单并刷新匹配，不再丢失已识别内容。PDF 默认只展示行数、存货编码、产品名称、数量和库存操作；材质、规格、价格、图纸、匹配证据与候选等内容收进“高级匹配详情”。
- 生产历史新增管理员专用“撤销生产确认”。只有未送货、未移动、未消耗、未调整且库存数量未变化的完工事实才可原子回退；临时成品库存、成品预占和本次半成品消耗会一并撤回，生产任务和订单回到待生产确认。已撤销完工和转库存记录保留原因、人员、时间并由数据库触发器禁止修改或删除；之后允许重新确认生产。
- 来料历史按钮改为“撤销收料，回到已报料”，操作前明确确认并要求填写原因；继续复用已有来料撤销接口和不可删除审计记录。
- 全部库位台账新增楼层和区域。新建三楼成品/共用库位时同时创建 `floor3_location_layouts` 布局并立即出现在三楼平面图；一楼等其他楼层保存为统一库位台账，供后续平面图扩展。三楼货位仍从平面图定位和维护，避免台账与地图两套独立数据。
- 成品仓、半成品仓、入仓和批次编辑统一改为“先选区域，再选具体货位”。三楼快速绑定默认只显示客户、存货编码/产品和数量，多余的栈板备注及待匹配快照折叠为高级选项。生产历史“定位库存”携带批次与货位参数进入仓库页面并自动筛选。
- 新迁移 `cf62v8x9z51` 线性接在 `ce61v8x9z50` 后，只给生产完工和转库存事实增加撤销审计字段，并把每任务唯一完工改为仅约束 `status=posted` 的活动唯一索引；不修改订单、来料或正式库存历史数据。
- 隔离验证：新迁移从空库完整升级到 head，并完成 `cf62 -> ce61 -> cf62` 往返；撤销审计触发器的不可更新、不可删除和存在事实时禁止降级均通过。生产服务专项 `15 passed`；三楼/来料/PDF/前端组合 `102 passed`；本轮新增静态、匹配和迁移专项 `10 passed`；既有 N029 组合另有 `24 passed`，2 项仅因家庭环境缺少 `cv2` 未执行成功。Python 编译、两份前端内联 JavaScript 语法和 `git diff --check` 均通过。
- 全程只使用 pytest 临时 SQLite 和单独临时迁移库；未连接、迁移或写入工厂正式数据库。验收前不得更新 `origin/factory-current-baseline`。人工验收通过后，把本提交及已独立完成的订单删除审计修复提交 `d8dec72` 合入交付分支，再由发布负责人把验收后的交付 SHA fast-forward 到 `factory-current-baseline`；工厂夜间更新脚本才会备份数据库、执行 `git merge --ff-only`、迁移并重启。

## 73. 2026-07-21 工厂夜间更新发布集成

- 用户已明确验收并授权集成订单删除审计修复 `d8dec72` 与订单录入/生产回退/库位联动 `8ab664f`，发布 worktree 为 `D:\tm-worktrees\erp-factory-release-20260721`，分支为 `codex/factory-release-20260721`。
- 发布基线固定为远端 `origin/feature/v0208-common-box-edit` 的 `fb0aaca`。先集成订单删除修复，再集成工作流与 UI 修复；唯一内容冲突位于 `_flow_delete_message()`，最终同时保留有效来料、撤销来料审计、有效生产完工和撤销生产审计四类专用 409 文案。
- 最终发布差异相对 `fb0aaca` 共 18 个文件：不删除 reversed 来料或生产审计，不绕过外键；新增迁移仍只有 `cf62v8x9z51`，Alembic 保持单一 head。
- 发布前组合回归：订单及删除原子性、来料、三楼库位、PDF 导入、生产工作流、前端静态断言、迁移保护和工厂更新脚本共 `228 passed`。Python 全量编译、`static/index.html` 与 `static/warehouse.html` 内联 JavaScript 语法、`git diff --check` 均通过。
- 验证只使用 pytest 隔离 SQLite 和测试迁移库，没有连接或写入工厂数据库。发布动作仅允许用带远端旧 SHA 租约的 fast-forward 推送更新 `origin/factory-current-baseline`；工厂电脑仍由 `scripts/admin/update_erp.ps1` 在夜间自行获取、备份、完整性检查、快进、迁移和重启。

## 74. 2026-07-22 PDF 订单常用箱楞型修正

- 故障单据为 `PO2026070628.pdf`。第 2 条历史报料文本含 `BC14C/A`；`A` 对五层纸板确属非法，但管理员把常用箱改为 `5 层 / AB` 并成功保存后，PDF 草稿重新匹配仍保留旧 `A`，导致批量加入订单继续失败。
- 根因在 `order_pdf_import._apply_standard_product()`：代码声明标准字段以常用箱为准，实际却只读取关联材质的楞型；当材质字典的 `flute_type` 为空时回退 PDF 历史值，漏掉常用箱自身的 `flute_type` 和 `layer_count`。正式库只读证据确认产品 `21302001` 已为 `5 / AB`，关联材质 `BC14C` 为 5 层且楞型为空。
- 独立 worktree 为 `D:\tm-worktrees\erp-pdf-flute-correction-fix-20260722`，分支为 `codex/pdf-flute-correction-fix-20260722`，基线为正式提交 `ff9b4ebdc12be0e6d9d8995e74b3ac47fcaf56a3`。最小修复让常用箱层数/楞型优先，材质和 PDF 仅作为缺省回退，并新增 `BC14C/A -> 常用箱 5/AB` 回归断言。
- 验证结果：`tests/test_phase192_hotfix3.py` 为 `23 passed, 18 skipped`；PDF 导入与楞型相关组合为 `152 passed, 8 skipped, 2 failed`。2 个失败是基线已存在的旧材质更新用例未携带 P4 后新增的 `expected_version/change_reason`，在未修改的正式基线同样失败。Python 编译和 `git diff --check` 通过。
- 本轮没有迁移、没有手工修改或写入正式数据库，也没有改动正式运行目录。修复尚未发布；发布前须单独报告提交 SHA、测试结果和是否需要重启，并等待用户授权。

## 75. 2026-07-22 P0-A 启动、发布迁移与 UAT 隔离收口

- 用户已明确确认唯一候选基线为 `origin/factory-current-baseline@c5cffa2123973b3f30b74be7f6dc198d1f070210`，并授权开始 P0-A；对应 GitHub 整改项为 #24（普通启动/发布迁移）与 #26（生产配置/UAT 隔离）。独立 worktree 为 `D:\tm-worktrees\erp-p0a-startup-migration-safety-20260722`，分支为 `codex/release-p0a-startup-migration-safety`。
- `scripts/windows/start_erp.ps1` 已移除普通启动中的 `alembic upgrade head`。正式启动现在强制 `ERP_ENVIRONMENT=production`、正式数据库绝对路径、loopback/HTTPS/单 worker 配置，并通过 `scripts/admin/release_erp.py check-startup` 只读检查数据库可访问性、`integrity_check`、外键、核心业务表和 `current == code head`；任何不一致都拒绝启动，不会自动迁移。
- 旧 `scripts/admin/update_erp.ps1` 已 fail-closed 停用。新 `scripts/admin/release_erp.ps1` 只允许干净的 `factory-current-baseline` 和已批准 40 位 SHA，采用 Prepare / Apply 两阶段：Prepare 停服后创建 SQLite Backup API 备份、记录 source/backup SHA-256、核验完整性/外键/revision/核心计数、从备份建立隔离演练副本并精确迁移到指定 revision；随后生成与本次证据绑定的 `APPLY-...` 口令并保持停服。Apply 再次核对代码、正式库主文件及 WAL 指纹、revision、计数、服务停机和人工口令，才允许精确迁移；失败保持停服，迁移及普通启动健康检查成功后才把报告标为 completed。
- `scripts/windows/start_erp_uat.ps1` 强制使用独立数据库副本、`18000-19999` 端口、`127.0.0.1`、`ERP_ENVIRONMENT=test` 和只读 revision 门禁；同时拒绝当前工作树默认库及已确认工厂绝对正式库 `D:\纸箱厂erp软件搭建\data\carton_erp.sqlite3`。详细操作与失败恢复边界记录在 `docs/P0A_STARTUP_RELEASE_RUNBOOK.md`。
- 自动验证：P0-A、启动、既有发布、数据库路径及生产安全组合 `34 passed`；Python 编译、4 个 PowerShell 脚本语法解析、`git diff --check` 均通过。系统 Python 缺少 `cv2`，最终测试改用已有隔离 UAT venv `D:\tm-uat\home-n041\.venv`，没有改动工厂正式 venv。
- 隔离迁移验证位于 `D:\tm-uat\p0a_release_gate_20260722_203300`：新空库迁移前先创建并验证备份（SHA-256 一致、`integrity_check=ok`），随后完整升级到唯一 head `cf62v8x9z51`。P0-A Prepare 又从该隔离库创建备份和演练副本，结果为 source hash 不变、backup/rehearsal `integrity_check=ok`、外键异常 0、核心业务表齐全、演练 revision=`cf62v8x9z51`；明确未执行 Apply。
- 隔离 UAT 已运行在 `http://127.0.0.1:18080/`，数据库为上述隔离目录的 `carton_erp_uat.sqlite3`；首页与 `/api/health` 均为 200，监听地址仅为 `127.0.0.1`，启动入口明确未迁移数据库，供人工验收。
- 本轮未修改工厂正式 `.env`、正式目录、正式数据库、正式服务、`origin/main` 或任何 Alembic revision，未启动 N081。当前正式部署仍被 `development + LAN HTTP + 未确认 HTTPS 反向代理` 阻断；不得直接把本分支替换进工厂启动目录。
- 只读复核还发现外部运行状态已较用户最初快照变化：本轮检查时端口 8000 的监听命令行指向 `D:\tm-worktrees\erp-factory-latest-uat-20260722`，不是已确认的工厂正式目录。本轮没有停止或修改该进程；任何正式发布前必须重新确认 8000 进程归属、正式启动目录和维护窗口。

## 76. 2026-07-22 P0-A 人工 UAT 验收通过

- 用户已在本机打开 `http://127.0.0.1:18080/`，确认 UAT 页面正常，并明确回复“UAT 页面正常，验收通过”。P0-A 的人工 UAT 门禁记录为通过。
- 验收对应实现提交为 `9eb2ebd755d4732a625c63d2bf4de2e354558cb4`；验收记录提交为 `189c09a46cc1ecbe0d085adbc2f5fa885e1ac26b`。用户随后明确授权推送并创建 Draft PR，分支 `codex/release-p0a-startup-migration-safety` 已推送，Draft PR 为 `https://github.com/slj19890902/tianming-erp/pull/35`，目标分支仅为 `factory-current-baseline`，未触碰 `origin/main`，未部署。
- 验收完成后已精确核对端口 18080 进程命令行为当前 P0-A worktree、`app.main:app`、loopback 和端口 18080，并只停止该隔离 UAT 进程；端口 18080 已不再监听。隔离数据库、备份、演练副本和报告继续保留在 `D:\tm-uat\p0a_release_gate_20260722_203300`，未删除。
- 本轮未停止或修改端口 8000 的进程，未修改工厂正式目录、正式 `.env` 或正式数据库。PR #35 当前保持 Draft，GitHub 复核为 CLEAN / MERGEABLE；合并与任何正式部署仍需独立人工批准，并继续要求确认 8000 进程归属、生产 HTTPS 配置和维护窗口。

## 77. 2026-07-22 P0-A 合并与正式发布预检阻断

- 用户已明确授权“审查后直接合并并正式发布”。代码审查复跑 P0-A、启动、既有发布、数据库路径及生产安全组合，结果仍为 `34 passed`；相对 `origin/factory-current-baseline` 没有 Alembic 文件变化，普通启动和旧更新入口均不存在 `alembic upgrade head`。P0-A 代码本身未发现新的 P0/P1 阻断缺陷。
- 正式发布只读预检与此前口径不一致：`D:\纸箱厂erp软件搭建` 当前为 `feature/v0208-common-box-edit@fb0aacaba5463fa7b2444a5cef166df1fb79a461`，不是 `factory-current-baseline@c5cffa2`；正式目录 `.env` 中生产运行变量均未配置。
- `D:\纸箱厂erp软件搭建\data\carton_erp.sqlite3` 实测 revision 为 `t68n0r1s7u50`，不是此前确认的 `cf62v8x9z51`；`integrity_check=ok`、外键异常 0，但缺少后续迁移才创建的 `incoming_receipts`。若直接部署 P0-A，普通启动会按设计拒绝 `current != code head`；若直接 Apply，则会跨越大量历史 revision，已超出本轮“无正式迁移”的既定验收前提。
- 当前 8000 监听仍为 PID 38552，命令行 `--app-dir D:\tm-worktrees\erp-factory-latest-uat-20260722`；该工作树为 `c5cffa2` 且 `docs/CODEX_HANDOFF.md` 有未提交修改，没有 `.env`，默认工作树数据库文件也不存在，因此进程实际继承的数据库路径无法仅从命令行证明。端口 80/443 均无监听，未发现可承接 production HTTPS 的本机代理。
- 因正式代码、数据库 revision、进程归属和网络配置四项门禁同时不满足，本轮在合并前安全停止：PR #35 继续保持 Draft，未合并、未部署、未停止 8000、未创建正式备份、未写正式数据库。下一步必须先重新确认唯一正式运行实例和权威数据库；若权威库确为 `t68`，需另开“t68 → cf62 隔离副本全链迁移与正式发布”闭环，完成备份、哈希、往返/失败恢复和人工批准；同时确定生产 HTTPS/反向代理方案后，才能重新申请合并与正式发布。

## 78. 2026-07-23 工厂只读复核与 P0-A 局域网生产模式

- 用户已在工厂主机 `PC-20250926DZYH` 只读确认：正式目录 `D:\纸箱厂erp软件搭建` 为干净的 `factory-current-baseline@c5cffa2123973b3f30b74be7f6dc198d1f070210`；实际 8000 服务命令行的 `--app-dir` 指向该正式目录；正式库为 `data\carton_erp.sqlite3`，revision=`cf62v8x9z51`、`quick_check=ok`、外键异常 0，`/api/health` 返回 200。此前家庭电脑对另一目录/副本得到的 `t68` 结果不再作为工厂正式事实。
- 工厂网络为 `192.168.3.80/24`，服务监听 `0.0.0.0:8000`；核验时没有活动客户端连接，也未发现 80/443 监听或 HTTPS 代理证据。项目 `.venv` 存在，关键运行依赖版本可导入且 `pip check` 无损坏；会话密钥文件长度 64，未显示密钥内容。
- `static/uploads` 共 21 个历史文件（PDF 9、WebP 6、JPG 4、PNG 2），扩展名与文件签名全部一致，无签名不匹配文件；没有活动上传扩展名，也没有超过 25 MB 的文件。以上核验均未修改代码、配置、Git、服务或数据库。
- P0-A 新增显式 `ERP_PRODUCTION_TRANSPORT`：默认 `https_proxy` 继续要求 loopback、HTTPS、受信 loopback 代理、Secure Cookie、HTTPS 跳转与 HSTS；仅显式 `lan_http` 才允许工厂私网 HTTP，并强制私网 Origin/URL、明确端口、可信 Host、禁止代理信任、单 worker 和至少 32 字符密钥。局域网模式不启用跳转/HSTS且 Cookie 不带 Secure，但仍启用精确 CORS、带 Cookie 写请求的 Origin/CSRF、HttpOnly、SameSite=Lax、Host 门禁和其余安全响应头。
- Windows 启动器和两阶段发布脚本均读取同一传输模式并按模式 fail-closed；普通启动仍只读检查 revision，未恢复任何自动迁移。工厂 `lan_http` 必须额外确认 Windows 防火墙把 TCP 8000 限定到 Private 配置文件和 `192.168.3.0/24`，且不存在更宽的旧入站规则；发现公网/Any 规则时停止发布。
- 当前配置、认证、系统、部署、数据库路径、发布安全及 P0-A 相邻扩大回归为 `111 passed`；随后补充未知传输模式和 Host 子域通配符 fail-closed 用例，传输安全专项为 `30 passed`。覆盖 HTTPS 默认不降级、LAN HTTP 私网/Host/Origin/CSRF/安全头、LAN Cookie 登录、启动/发布脚本和备份发布门禁。Python 编译、4 个 PowerShell 脚本语法、工厂实际 LAN 参数的纯配置加载和 `git diff --check` 通过。测试使用 P0-B 隔离工作树已有 venv；没有向工厂或家庭正式 venv 安装测试包。正式 `.env`、正式服务和正式数据库仍未修改，PR #35 尚未合并，本轮也没有新增 Alembic revision。

## 79. 2026-07-23 P0-A + P0-B 工厂发布集成候选

- 独立候选工作树为 `D:\tm-worktrees\erp-p0ab-factory-candidate-20260723`，分支为 `codex/release-p0ab-factory-candidate-20260723`。它以已推送的 P0-A `2addb5bb4fee5632507346a07431943f9ac2f9e9` 为起点，线性集成 P0-B 原提交 `804a90cbb0e4cc12a87392ed4878cb3f37bcf8fe`（候选中的等价提交为 `5315e14`）；P0-B 独立分支也已推送备审。没有更新 `origin/factory-current-baseline` 或 `origin/main`。
- 自动集成同时保留 P0-A 的 `https_proxy/lan_http` 生产传输、Host/Origin/CSRF/启动发布门禁，以及 P0-B 的 `/static/uploads` 强制 404、私有图纸鉴权 API、统一文件签名/大小/超时校验和一次性草稿 token。相对 `c5cffa2` 没有 Alembic 文件变化。
- 联合后端、上传、图纸权限、客户范围、PDF、天华、认证、传输、启动、发布与数据库路径回归为 `163 passed, 1 skipped`；前端、权限显示和内联 JavaScript 扩大回归为 `185 passed`。最初出现的 2 个 Phase 12 客户测试和 3 个旧前端断言均在未修改的 `c5cffa2` 复现；候选仅把测试请求补齐现有 `expected_version/change_reason`，并同步当前销售/车间菜单及常用箱材质区 DOM，修正后全部通过，业务代码未为旧断言降级。
- 工厂历史公开上传目录 21 个文件的扩展名/签名只读核验已全部一致，无活动类型、无大于 25MB 文件；未移动或删除历史文件。正式发布仍需先只读确认 Windows 防火墙没有公网/Any 宽规则，并将 TCP 8000 限定到 Private 配置文件和 `192.168.3.0/24`。
- 本轮没有停止工厂 8000 服务，没有修改工厂正式 `.env`，没有安装正式依赖，没有创建或写入正式备份/数据库。发布时 P0-B 需要把正式 venv 依赖核验到 `python-multipart==0.0.27`、`pypdf==6.7.3`、`PyJWT==2.13.0`；该依赖更新必须与代码、备份、配置切换和人工 UAT 放在同一受控维护窗口。

## 80. 2026-07-23 工厂防火墙精确预检阻断

- 工厂以太网连接类别为 `Private`，但 `Get-NetFirewallProfile` 显示 Windows Firewall 的 `Private=False`、`Public=False`，只有 `Domain=True`；当前主机未加入本轮确认的域网络。因此现有 Private/Any 入站规则不能作为 TCP 8000 的有效保护证据，正式发布继续阻断。
- 8000 监听仍为 PID 10248，命令行确认是正式目录 `D:\纸箱厂erp软件搭建`、`app.main:app`、`0.0.0.0:8000`、单 worker。发现三条已启用的本地 Allow 规则：`{7c358cec-bfac-4f88-8887-30bb351389f8}` 允许 Any Profile/LocalSubnet；`{8E8A6B0B-8918-4E86-8207-C651FEFF4F7A}` 与 `{8dab25df-2ad2-42d8-ac31-ca3758c4b1bf}` 均允许 Any Profile/RemoteAddress=Any。后两条范围过宽，第一条也未限定到 Private 与 `192.168.3.0/24`。
- `EdgeTraversal=Block` 只限制边缘穿越，不等于阻止普通 TCP 入站。不能仅新增窄规则后保留宽规则，也不能在未核对第三方防火墙和其他局域网监听前直接开启 Windows Private Firewall，以免误伤工厂共享或管理服务。
- 本轮仍为只读预检：没有启用/禁用/删除防火墙规则，没有停止 ERP，没有修改 `.env` 或正式数据库。下一步先核实 Security Center 是否由第三方防火墙接管、Windows Firewall 服务状态及所有非 loopback 监听，再决定精确的防火墙切换和验证闭环。

## 81. 2026-07-23 工厂防火墙产品与监听服务复核

- `root/SecurityCenter2/FirewallProduct` 返回产品数量 0，未发现已向 Windows Security Center 注册的第三方防火墙；Windows Defender Firewall 服务 `mpssvc` 为 `Running/Auto`，但活动以太网为 `Private` 且该配置文件仍关闭。因此目前没有证据表明第三方产品正在替代 Windows Firewall 提供主机入站保护。
- 非 loopback TCP 监听除 ERP `0.0.0.0:8000` 外，还包括 SMB/RPC（135、139、445 及动态端口）、打印后台、`BSZnetSignServer:6026` 和 `wpscloudsvr`；UDP 监听还包括网络发现、IPsec、`AweSun`、极空间及 WPS 服务。直接开启 Private Firewall 可能中断共享、打印、税控签名、NAS/极空间或当前远程会话，必须先只读映射已有入站规则并设计现场可回退窗口。
- 本轮没有修改防火墙配置文件或规则，没有停止任何进程，没有修改正式 `.env`、代码或数据库。正式发布继续阻断；不得在仅有向日葵远程通道时直接启用 Private Firewall。

## 82. 2026-07-23 工厂现有入站规则映射

- 38 条相关已启用 Allow 规则中，`AweSun.exe` 与 `agent\AweSun.exe` 均已有 Private TCP/UDP 且按程序路径限定的规则；网络发现的 Private 规则均限定 `LocalSubnet`。因此启用 Private Firewall 后向日葵和本地网络发现具备既有放行依据，但仍须使用自动回退窗口实测，不能把规则存在等同于远程一定不会中断。
- 未在相关规则中看到 SMB 139/445 的文件共享放行；当前快照没有活动入站 TCP 连接，但这不能证明工厂日常无人使用共享。税控现有规则限定 `BSZXInput.exe`，没有覆盖当前监听 TCP 6026 的 `BSZnetSignServer` 证据；极空间监听也没有匹配的入站规则。WPS 则存在 Any Profile/Any Protocol/Any Port/Any Remote 的程序规则，范围较宽，但不属于本次 ERP 发布必须改动的规则。
- ERP 仍是三条规则：一条 Any Profile/LocalSubnet，两条 Any Profile/RemoteAddress=Any。启用 Private Firewall 前必须先确认共享、共享打印机、税控签名与极空间是否需要被其他局域网主机主动连接；随后以可自动回退的维护窗口启用 Private，并把 ERP 收敛到单一 Private + `192.168.3.0/24` 规则。
- 本轮继续只读：未更改任何工厂规则、配置文件、进程或数据库，正式发布继续阻断。

## 83. 2026-07-23 P0-A/P0-B 工厂本机受控发布完成

- 工厂正式目录 `D:\纸箱厂erp软件搭建` 已从 `factory-current-baseline@c5cffa2123973b3f30b74be7f6dc198d1f070210` fast-forward 至经验证候选 `00263ec12de63a8fd67f410b58da7d8b7bbc8843`。本次未修改或推送 `origin/main`；候选功能代码来自已验证的远端候选，离线 bundle 仅额外包含本交接文档前序记录。
- 发布前已在 `D:\tm-release-backups\p0ab-factory-20260723\formal-sqlite-backup\carton_erp_before_p0ab_release.sqlite3` 创建 SQLite Backup API 一致性备份：大小 `219,447,296` 字节、SHA-256 `DF56C603722D7672BD1606970B9DB70D524C8EB9EEAF77F9E5007EC62C6A34A2`、revision=`cf62v8x9z51`、`integrity_check=ok`、外键异常 0。代码基线与原 `.env` 也已备份到同一外部发布备份根目录。
- 正式 `.env` 仅切换为明确的 `ERP_ENVIRONMENT=production` 与 `ERP_PRODUCTION_TRANSPORT=lan_http`，保留 `0.0.0.0:8000` 局域网入口并限制为明确的 `192.168.3.80:8000` Origin/URL、可信 Host、单 worker 和已有会话密钥文件；未改动防火墙、路由器、HTTPS、反向代理或正式数据库 revision。
- 隔离 UAT 使用外部 SQLite 副本和 `127.0.0.1:18081` 单 worker，未迁移数据库。登录、订单、报料、来料、送货只读接口均为 200，浏览器 Console 无 error/warn。为隔离测试创建的临时 admin 密码及其审计仅写入 UAT 副本，未写入正式数据库。
- 正式启动器执行只读 `current == code head`、完整性、外键和核心表门禁后成功启动。最终局域网 `/api/health` 为 200 `{"ok":true}`，登录页为 200，监听 PID 使用正式目录、`0.0.0.0:8000` 与单 worker。停服前备份与上线后正式 SQLite 的文件哈希不同，但逐表行数与逻辑内容哈希完全一致，确认本次没有业务数据写入。

## 84. 2026-07-23 库存正式转换与常用箱默认开料方式紧急修复

- 独立 worktree：`D:\tm-worktrees\erp-urgent-inventory-cutting-20260723`；分支：`codex/urgent-inventory-cutting-default-20260723`；唯一开发与隔离 UAT 基线：`origin/factory-current-baseline@c5cffa2123973b3f30b74be7f6dc198d1f070210`。紧急修复提交后，以普通合并方式纳入已由工厂验收并推送的正式基线 `28d90525da514d2a88bf36baedeca0f0c4eefee7`，仅为解决 Draft PR 基线前移，不改写原提交历史、不强推、不修改 `origin/main`。
- 工厂副本原件、只读核验副本和迁移前备份的 SHA-256 均逐字符等于 `A9B0732506453BB86C7267395866B0ADE6EC07600D7F3BE0A06EBBCCA8D871FC`；只有工作副本 `D:\tm-uat\urgent_inventory_cutting_20260723\working\carton_erp_urgent_inventory_cutting_uat.sqlite3` 被迁移和写入 UAT 数据。
- 22000022 在 E1-L09 的 300 张记录是已匹配但未绑定 `inventory_lot_id` 的 `semi_finished/sheets` 现场快照。订单库存候选只读取正式 `InventoryLot`，因此初始不可抵扣；修复没有增加快照直扣后门，而是让页面经权限、版本、幂等和两次确认调用既有正式入库流程，生成客户专用成品批次并保留库位、栈板、流水和操作日志。
- 常用箱新增独立字段 `products.default_cutting_mode`，合法值为一开一至一开五，默认一开一；新迁移 `cg63v8x9z52` 线性接在 `cf62v8x9z51` 后且为唯一 head。新订单把默认值冻结到明细 `special_process`；修改常用箱不回写旧订单。当前报料草稿允许人工改开料方式并立即重算采购张数，不复用 `pieces_per_box`。
- 定向及相邻自动测试：订单组合 `67 passed`；报料、主数据版本和前端组合 `45 passed`；三楼库存组合 `79 passed, 3 failed`。3 个失败均为 `c5cffa2` 基线中未修改位置的旧前端文本/排序断言，与本轮改动无关；本轮新增的库存转换、客户隔离、幂等、默认开料和采购张数用例均通过。纳入正式基线 `28d9052` 后再次运行 6 个本任务定向用例，结果 `6 passed`。Python 编译、两份内联 JavaScript 语法、`git diff --check` 均通过。
- 隔离页面 UAT 已复现快照初始不可抵扣；转换后创建正式批次并可供同客户订单发现和预占，其他客户候选为空且强行预占被拒绝，重复转换返回幂等结果。常用箱一开二保存重读成功，新订单 100 个冻结一开二并带出 50 张，报料页手改一开一立即变为 100 张；随后把常用箱改为一开三，旧订单仍保持一开二。
- 工作副本最终为 `cg63v8x9z52`、`integrity_check=ok`、外键异常 0；UAT 服务已停止。工厂正式数据库未连接、未迁移、未写入。将来正式发布需要先备份并验证、执行线性迁移、再重启服务，且必须另行授权。

## 85. 2026-07-23 常用箱开料字段按箱型显示优化（人工验收通过）

- 在同一紧急修复分支继续最小修改：“平卡”选择项改名为“模切内盒”，旧 `box_style=平卡` 在编辑和保存时兼容归一为“模切内盒”；“模切内盒、隔板”仅允许压线类型 `净料/毛片/其他`，页面不提供“压线”选项。
- `default_cutting_mode` 从常用箱首行移到压线类型右侧，只在“模切内盒、隔板”显示；页面标签缩短为“开料方式”，并在这两类箱型下隐藏无关的“压线尺寸”、加宽压线类型与开料方式区域，避免字体遮挡。A1、A3、异形箱等其他箱型不显示开料方式且后端保存时强制为“一开一”，新订单也只对“模切内盒、隔板”冻结该默认值。
- 本轮不新增 Alembic revision，继续使用 `cg63v8x9z52`。定向 API、订单冻结和前端显示用例 `7 passed`；Python 编译、内联 JavaScript 语法和 `git diff --check` 通过。扩大运行旧 `test_v0208_common_box_edit.py` 时另有 3 个 `28d9052` 基线既有布局断言失败，均在未修改断言位置查找已不存在的 `product-material-row`。
- 人工 UAT 地址为 `http://127.0.0.1:18082/`，数据库为 `D:\tm-uat\cutting_mode_visibility_20260723_113848\carton_erp_uat.sqlite3`，由指定 SHA-256 为 `A9B0732506453BB86C7267395866B0ADE6EC07600D7F3BE0A06EBBCCA8D871FC` 的工厂副本重新复制并升级；revision=`cg63v8x9z52`、`integrity_check=ok`、外键异常 0。临时账号 `codex_uat`，密码 `123456`。
- 页面自动 UAT 已确认：模切内盒和隔板显示净/毛/其他及右侧“开料方式”，不再显示压线尺寸，标签与选择框无重叠遮挡；A1、A3 不显示开料方式；浏览器 Console 无 warn/error。用户已于 2026-07-23 明确确认人工验收通过并授权推送给工厂 ERP；正式发布仍须由工厂主机按“备份验证 → 线性迁移 → 重启 → 完整性与页面复核”执行。本轮家庭侧正式数据库未连接、未迁移、未写入。
