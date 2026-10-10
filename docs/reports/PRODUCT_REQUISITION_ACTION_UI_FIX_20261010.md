# 产品工作台动作续步可靠性 — UI实施回执

候选：`90b0d4c47faf824cc9db04f728f51038130a3f15`；分支 `codex/product-requisition-action-ui-20261010`；起点 `2b18eef03e885b187eeb98e678ea405447d806df`（完整v607基线）。工作树干净，文件已释放给根整合。本轮只修改授权5文件，未push、发布、改版本、后端或正式数据；没有浏览器、服务或PID操作。

## 最终行为

产品动作仍只导航、读取并打开/填写草稿，未引入业务POST，也未新增普通保存确认。根持有actionContext，允许真实go/loadPage正常卸载原工作台；同一轮账号、authGeneration、导航轮次、activePage及确切form/modal持续匹配才允许续步。更换产品、账号、页面、表单或新动作后，旧成功/失败均不覆盖当前页面。拒绝导航或当前报料权限缺失时不重置/填写旧草稿。

动作有15秒总等待上限；局部Axios读取也设15秒超时。超时结束忙态、使迟到结果失效，并提示关闭窗口后从产品重新打开。实际重复打开回归可再次成功。正常手动/策略报料、只读订单三条路径均调用实际HTML的go，由loadPage卸载原组件，再调用实际bootstrap/products/policy/order/charges方法；并非用go替身代替导航。

局部读取逐次axios.create继承当时全局配置并携带cookie，不继承全局响应副作用。当前401/403仍走原门禁。共享全局Axios仅增加同步捕获actor/authGeneration的request身份，以及同会话才触发erpAuthRequired/erpForbidden/offerRepair；旧请求仍reject给调用者。初始化无getter/匿名请求也有明确捕获语义，未暴露凭据。原loadPage及requestIsCurrent保持；本轮不宣称所有子页loader赋值均已全面保护。

正常handoff卸载原组件后若发生真实失败（如当前产品未找到），由仍有效根上下文显示一次错误及重新打开的可操作提示；旧上下文静默，不重复局部已显示的错误。手机onOrder完成后也只在原账号、当前lookup页面和原动作有效时滚动结果。

## 红绿与验证

- 原9条真正期望回归在2b18基线为1通过/8失败，保留 `action-before-red.tap`。
- 单独运行基线实际共享Axios响应拦截器，A请求晚401/403作用于B：logout=1/forbidden=1/repair=2；`interceptor-before.json`与红日志保留。
- 最终新增18条动作回归全通过，`final-action.tap` / `final-action.xml`。覆盖真实go卸载后三种正常入口、双击、A/B产品、同页手动导航、warehouse许可等待、账号变化、忽略abort、替换form/modal、冷启动客户/材质分页读取、当前与旧401、同步全局拦截器、有限等待及实际重试、handoff当前失败一次提示、当前报料权限拒绝。
- 原产品11+首页16+手机29共56条通过，`final-adjacent.tap` / `final-adjacent.xml`。原手机29仅actual-entry提取正则适配onOrder的新签名，行为断言与数量未改。
- 原图纸脚本通过，`final-drawings.tap`；保持默认收起/展开预览、PDF首页、重试、权限、弱网及dispose行为。
- git diff --check通过。saveStockReplenishmentDraft实际方法与起点LF逐字相同；business-approvals.js、home-workbench.js及旧图纸JS/CSS未改。

最终5文件LF SHA见 `source-fingerprints.json`。两入口资源引用一致：`product-workbench.js?v=c880d8dc7f20`（SHA前12位），JS完整LF SHA `c880d8dc7f20f1ce53066edd351011adef1c27b60429c29812ee1016195b2e14`。

## 最短重跑

在候选树设置NODE_PATH到 `D:/.codex/worktrees/home-product-workbench-20261010/纸箱厂erp软件搭建/tests/ui/node_modules`；运行：

```text
node --test tests/ui/product-requisition-action-reliability.test.cjs
node --test tests/ui/product_workbench.test.cjs tests/ui/home_workbench.test.cjs tests/ui/mobile_product_workbench_reliability.test.cjs
node tests/mobile_product_drawings_ui.cjs
```

前端证据来自真实HTML方法/共享组件/linkedom/VM，读响应和时钟为离线替身。真实go内的loadPage按原方法调用，但测试用卸载回调替代全后台列表HTTP；认证/cookie、手机现场与浏览器可见性仍待管理员。本轮未做真实保存HTTP，不把离线动作证据称后台写事务验收。

## 保留边界

空/部分保存ack报成功、未知后重新打开丢原请求的已证实保存恢复问题未在本卡实施，readonly REPORT及11观察保留；需要另立完整body持久化/精确结果核对合同。本轮没有修改saveStockReplenishmentDraft或未知恢复，不声称这两类已解决。API同伴独立保存门禁工作不改变本轮动作端点/body，也没有新增resolver/预期actor/Rejected合同供UI依赖。
