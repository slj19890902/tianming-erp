> 最终修订：UI候选为 **57d267de718854afdcea17348205a1c3cc6bbc76**，承接但替代旧候选d75bd05f。旧候选正文/27组及原失败证据保留于candidate-d75-before；下方追加的后续修复及29组是最终交付事实。独立发现的两处边界已修，不再沿用旧段落“疑点待确认”为当前源码状态。现场验收仍pending。

# MOBILE-PRODUCT-WORKBENCH-UI-FIX-20261010

本轮完成首页/手机共用产品工作台的5类已复现可靠性修复，以及根批准的实际加工子件再次反查状态丢失相邻修复。仅6个UI白名单文件，无后端、数量/匹配/库存/版本/迁移改变；未发布。正式起点 `4b66fc4c104cf49bc48b065c20b5aeed17f9403a` / v606，新分支 `codex/mobile-product-workbench-ui-20261010`，旧分支保留。候选SHA见末尾。

修前只读 `REPORT.md`、`probe.cjs/json/xml`、`source-fingerprints.json` 保留原11观察；本报告是实施回执，不覆盖旧错误证据。

## 最终行为

1. 手机结果与生产资料都通过 `mountMobile → mount(lazyDrawings:true)` 复用既有 TmProductDrawings.append。未展开没有预览src/fetch；展开后按原图片/PDF元信息、权限及安全URL加载。每次重绘/销毁仅 disposeWithin 当前工作台宿主；收起/切产品/页签/账号停止旧预览并释放对象URL。桌面原直接缩略图、原图入口、图纸错误重试保持。手机结果的收起控制占整行，避免压在原窄图片列，未增加新的业务流程或常驻帮助。
2. show 返回有效轮次/产品身份，历史勾选与初始恢复的 continuation 都检查同轮次和当前产品，A晚回不改B页签。普通历史勾选仍留订单页签。
3. 已提交 params 与未提交 draft 分离。查询响应、分页、重试、页签/地图重绘保留正在输入的条件；分页/重试只读上次明确提交的条件，不偷用新草稿。只有明确模式切换/返回清理或实际批次“查用途”重设相应草稿。手机图纸重试沿旧组件局部load，不重建整份表单。
4. 产品读取15秒有限等待；超时退忙并显示原查询/产品重试，不自动重发。close/destroy/新请求取消本轮等待和计时器；即使请求忽略abort，旧响应或continuation也不能回填。只影响等待读取，未引入业务写请求。
5. 查询拒绝null/非对象、缺items/total/分页或行ID的坏2xx；详情校验匹配产品身份、稳定顶层对象、库存/订单items、生产molds/bom/process_steps数组及正常授权库存summary/groups。错误显示可重试，不冒零库存/无匹配、不抛render异常。真实hidden_by_permission包例外、停用、零库存及文本/尺寸/cutting合法null保持。门禁只覆盖这些稳定核心结构，不声称捕获任意字段损坏。
6. 相邻独立根因：output_piece实际来源首次反查正确，但现状select原无对应选项，重新提交丢参数、后端按raw默认处理。保留“已加工子件”选项，原lot_id和真实状态不变；不改变反查或数量算法。

index/mobile HTML仅更新产品工作台JS/CSS内容hash引用。旧 TmProductDrawings、其CSS/测试、home-workbench及其测试与正式4b66逐字LF相同。旧product11测试只补真实正常分页/库存汇总夹具字段，所有原断言逐项未变。

## 修前失败与最终验证

- `fix-before-red-direct.tap`：最初6条真正修复期望全部失败。第一条手机收起/PDF生命周期，2条旧continuation，草稿、超时、null/{}；随后初修6绿。
- `processed-form-before-red.tap`：独立output_piece重提期望红（6绿1红），补选项后绿，不与原5根因合计凑数。
- `partial-contract-before-red.tap`：缺稳定分页及生产数组/库存汇总2条真实期望红（25绿2红），补最小结构门禁后绿。
- 最终新回归 **27/27**：`final-new.tap/xml`，含14份最终真实HTTP响应消费及实际mobile HTML mount片段、旧图纸生命周期、账号换轮、草稿/分页/失败恢复。
- 原产品工作台 **11/11**：`final-product.tap/xml`；原首页 **16/16**：`final-home.tap/xml`。
- 原图纸组件脚本通过：`final-drawings.tap`，原脚本无数字统计，不虚报用例数。
- `fix-http-equivalence.json` **14/14**：embedded HTTP去掉archivePath/archiveSha256后与实际文件逐字段完全一致，原始SHA一致，API来源全部 `37361f51e5d5fa11809547bfe230f166b14a4e19`。合法normal/hidden/inactive/zero-nullable、search/free/actual reverse都通过。processed库存位置行按原实际片单位显示，raw仍原张；不改后端持久数量/单位事实。
- `git diff --check`通过；`fix-source-fingerprints.json`记录6文件、引用内容hash和未变文件指纹。最初源自对象断言格式化的node --test前台会话较久后自行退出，日志 `fix-before-red.tap`保留且不计为有效6条红；改布尔断言后直接node运行可靠，无任何PID停止操作。

## 真实HTTP与离线边界

真实API原文在 `../api/fix-http/`，14份均包含请求、状态、headers、body、候选及源指纹；测试消费同一原文而非手造正常业务回包。整数边界两个直接ID大于JS安全整数，UI本来不接受此ID；这两份仅将不可变原路径的真实错误喂actual read，再检验安全产品入口的同类错误显示，不能称UI发了非安全整数。大offset包也只验证真实200包体的读取/呈现，不冒称浏览器精确发送64位整数。跨客户404沿真实read错误保持可重试，无假详情。

手机预览网络在linkedom内明确合成，只有manager协议、PDF首页标签、权限/缺档提示、原图链接、abort/释放/重试得到验证，没有真实PDF像素或图片视觉验收。实际mobile_erp的mount片段在VM执行；页面其余流程没有自动化或更改。无Chrome、新服务、正式页面、正式API/数据写入或Git push，没有操作早先锁定源/旧服务/PID。浏览器Cookie、真实DOM布局、320/360/390窄屏、触控/底部导航、真实图纸像素及管理员现场验收仍 **pending**。

最短复核（NODE_PATH指只读现有home-product-workbench树的tests/ui/node_modules）：直接node运行 `tests/ui/mobile_product_workbench_reliability.test.cjs`、`tests/ui/product_workbench.test.cjs`、`tests/ui/home_workbench.test.cjs`；旧图纸用 `tests/mobile_product_drawings_ui.cjs`。全量仓库或Chrome不在本轮验收范围。

建议管理员发布后最短3步：手机查产品，确认图纸收起并展开PDF/原图→切换产品/返回后不串图；慢请求期间输入新条件或切产品，确认条件及页签保持；断网/恢复后按可见重试，检查原条件、权限隐藏/停用提示与当前真实库存位置。未收到反馈前不写现场通过。

## 候选交付

最终 UI 候选 d75bd05f77d5679af6f6062f8e8eecae38a8a511，分支 codex/mobile-product-workbench-ui-20261010，提交后工作树干净，6 个白名单文件已释放。postcommit 指纹及14原文等价核验已更新；无后端/版本/迁移/正式数据/发布/push。根整合0e999a8d相邻54组与旧图纸通过，独立相邻重试身份疑点待确认；此报告不提前宣称该疑点通过。

## 独立复核后的最小后续修复

根授权2e176950/TASK追加，两条实际实现期望已在d75候选复现：`followup-before-red.tap`为0通过/2失败。① show A→切反查→空长宽提交→重试错误重开A，为第9类独立根因；②授权inventory `{summary:{},groups:{},items:[]}`被接受且显示假空库存，是既有坏2xx边界深化，不另计Bug。

最终补丁57d267de沿原白名单仅5文件：组件JS、两个入口JS内容hash、新回归和旧产品夹具。切模式/关闭清旧产品重试ID；本地校验失败改用当前表单重试，仍空尺寸不发网络，补正后仅发当前reverse。网络失败继续按已提交params重试，保留新draft，不误换成草稿。提交本地校验前结束旧请求轮次，旧结果不会覆盖该错误。

授权库存最低结构按真实API三类finished/semi_finished/processed_component：各summary必须有actual/available/reserved有限非负number及unit string，groups各对象有positions数组。不重算总量、不判跨字段相等、不收紧合法生产文本/null；hidden_by_permission仍不要求这些组。四种空/缺组/缺actual/缺positions均保持明确资料不完整+重试，不显示假无库存。原product11及成功fixture仅补真实三类结构，原断言逐项完全不变。

最终一次收口结果：新29/29（含新增两场景及原14真实HTTP消费）、原product11/11、home16/16、旧图纸脚本通过。最终final-new/product/home.tap+xml及final-drawings.tap，14实际包逐字段/rawSHA仍完全等价；API37361源未改，不重跑后端。`git diff --check`通过，提交后clean。CSS/原TmProductDrawings/home源码字节未变，未碰其他任务/正式数据/版本/服务/PID/浏览器。

最终LF SHA：product-workbench.js `4f07fc902f70c8e3d3fb55cefccd98e09fe5f4b31c0474f59115a0ba155759e4`；CSS原`1bbc57da356b23bb49b68741f7e8a8baa372c82ca732a3a6fd7b2d9660a2550d`；index `b09057510d38e8723c73e6343f8b5f1b89e9228bcc7d8f5c75a7ebd759d7df43`；mobile_erp `a85632b2a8177843811d2a18aa90045996a39fa8841216752f4fe3b6ac6965cb`。postcommit fix-source-fingerprints记录精确57d HEAD；两入口JS引用?v=4f07fc902f70。

源码已释放，交根整合和独立仅复核两红边界。没有宣称浏览器像素、Cookie、窄屏触控或管理员现场通过；这些边界保持pending。
