# MOBILE-PRODUCT-WORKBENCH-RELIABILITY-UI-READONLY-20261010

本轮只读实际首页/手机共用产品工作台，确认 **5个可靠性根因**，不实施。`probe.cjs` 用实际 product-workbench.js、现有 linkedom/VM 模式和明确合成数据做交互；11项观察全部可靠复现（5缺陷、2同根因变体、4正常保护），不是修复后通过或浏览器验收。

启动沿 CODEX_START → NAS AI_START → 本卡 → PLATFORM → 总需求2/3/5/14/15/18/19、章程3～9完成。只读根树 HEAD `eb809c30c543b89ed64f786d12c085557b69c2f1`，分支 `codex/mobile-product-workbench-reliability-20261010`，前后clean。所读5个运行文件与正式源 `4b66fc4c104cf49bc48b065c20b5aeed17f9403a` 逐字LF一致，版本文件为v0.22.606；不将源码一致性冒作本轮正式健康检查。

## 已复现问题及最小方案

|优先级/证据|员工触发及实际结果|定位|最小修复建议|
|---|---|---|---|
|P2 UI-01|搜索产品或打开生产资料，未点击展开就出现实际preview src；没有展开开关。合成PDF首页也走该src。|product-workbench.js:54 image，结果/生产调用；CSS末尾600px规则又显示第四列|手机沿既有 TmProductDrawings.append/disposeWithin 生命周期，默认收起、展开才加载，保留缺档/重试/原图。桌面现有展示保留；重绘/销毁清理本容器旧图纸，不清其他业务。|
|P2 UI-02、UI-02b|A订单勾历史触发慢请求；切B，B生产页签正常。A晚回虽然body被get丢弃，旧change continuation仍把B改成orders。initial savedId.then同样把B改成A原inventory。|js:41 get、50 show、124 history handler、128 initial then|show返回本次token/product身份/是否有效；所有后续tab赋值都要求同token、产品、存活实例。正常历史切换仍留orders；旧操作不修改新产品页签。|
|P2 UI-03、UI-03b|发条件A后输入未提交B；A响应重建form将B改回A。搜索结果图纸失败后点击本地重试也整容器render，将未提交B清空。|js:84 render、109 submit、125 image retry|已发params与未提交draft分离；input/change同步draft，查询参数仅由明确submit冻结；局部图片重试不重建整个form。模式切换/返回清理只沿明确员工动作。|
|P2 UI-04|查询一直无响应：保持loading，没有有限等待/忙态重试；返回工作台可手动解除。|js:41 get、131 read|为读取设置有限超时，超时结束忙态并显示可重试；使原token失效，忽略即使未响应abort的晚成功。页面destroy/关闭清理定时器。不要自动重发。|
|P2 UI-05|实际read收到200 null时，无失败/重试提示；200 {}搜索显示“没有匹配产品”，详情render抛 Cannot read properties of undefined (reading 'customer_name')，同样无重试。|js:45 search、50 show、84 render、131 read|按search/detail边界做最小结构校验，空/缺字段回执进入可重试读取失败，保留已发条件/原productId，不能冒充零库存或无匹配。合法权限隐藏/空items与坏结构区分。|

UI-01是总需求15明确业务合同：“默认收起、展开才加载”，不是风格意见。loading=lazy只按视口延迟，不提供员工点击展开门槛。linkedom不发图片网络，因此本轮证明的是真实DOM已有src、无展开门槛和CSS可见规则，未声称测过真实下载量或手机屏幕。

UI-04没有真实等待15/60秒或模拟超时已通过；证据是实际mount在pending时没有调度任何timeout，以及源码get/read无时间边界。明确close可解除并忽略晚body，是既有正常门禁，不把请求永不停止写成无法离开。

## 正常保护与边界

- CONTROL-01：普通历史勾选读取完成后正确返回orders；不登记为Bug。
- CONTROL-02：正常库存位置→地图→返回，保留原产品17与inventory页签，固定只读同源location22链接；没有新业务请求。地图关闭只是原detail重绘，没有重新GET；本轮无后台变更场景，不凭此断言库存错误。
- CONTROL-03：destroy后旧请求不再渲染旧账号内容；不声称真实Cookie/手机账号切换已人工验收。
- CONTROL-04：真正不可解析的JSON已被get捕获并提供重试。问题是可解析null/{}缺少结构门禁，不重复登记JSON语法异常。

未扩展数量/库存/匹配/BOM/价格算法，无业务POST。API同伴发现的processed单位投影冲突及大整数500由其独立报告负责，本UI报告不重复计数，也不将合成UI字段当实际后台事务证据。

## 文件、证据与建议后续门禁

本目录 `probe.cjs` / `probe.json` / `probe.xml` / `source-fingerprints.json` 可复核。最短运行：`node D:/.codex/visualizations/2026/10/10/mobile-product-workbench-reliability/ui/probe.cjs`。依赖只读使用 `home-product-workbench-20261010/纸箱厂erp软件搭建/tests/ui/node_modules/linkedom`。运行源码路径和5个LF hash均在fingerprints中。

后续最小实施应将上述5根因分别建立修复红绿，并保护正常历史勾选、正常地图返回、跨产品/账号旧body不落地、所有权限隐藏空态、desktop图纸原呈现、mobile冻结生产任务图纸入口及旧图纸重试。有有限超时后需测试忽略abort的晚成功/晚错误不改变新状态；图纸组件需验证每次展开只加载对应产品、关闭/重绘释放对象URL且不串图。这些是拟方案，不是本轮已实现。

本轮没有仓库源码/测试/版本/分支写入，没有Chrome、服务或PID操作，没有正式页面/接口/数据库写入或Git push。没有本轮截图。浏览器DOM布局、窄屏/长编码/触控/底部导航、Cookie、真实图纸加载和管理员现场验收均pending。等待根修订实施卡及独立工作树授权，再写候选。
