# 合并新正式 v608 后的补充独立核验

结论 **pass**。根合并候选 `90cdddf02f0bb575a2ca9cbc1189e5683d2e6482` 保留外部正式 `f751729378a667c15b5d2a3293299e53458f7981`；截至本回执，本任务尚未发布。人工页面/手机验收 **pending**。

原 API `8c79504e0df337fa6ece5487927bd926ee00414e` / UI `90b0d4c47faf824cc9db04f728f51038130a3f15` 的候选独立 5/5 证据仍保留在 FINAL.md / verification.json，没有覆盖或换标签。

对合并 commit 与实际工作文件做 LF 字节比较：API 4 文件和 UI 除 index 外 4 文件均与原受检候选相同。index 相对 UI 候选严格只有 home-workbench.js/css 两个资源查询值从 home2 改为 stock-alert3，其实际 go、错误拦截器和报料动作方法没有变化。合并后的 home-workbench.js、home-workbench.css、stock_replenishment.py 与新的正式 f751 原样相同，未回退其库存预警首页更新。精确文件 SHA 在 merged-verification.json。

合并树另执行最短 3 条，全部通过，233.467 毫秒（merged-ui.xml / merged-ui.log）：

- 独立实际 go/loadPage 卸载原组件的正常报料与失败提示两种分支。
- 新正式 home 实际模块对待审批、可安排、来料及缺资料入口继续区分。
- 新正式 home 实际模块的连续点击仍只启动一个进行中的导航。

后两条选取现有 home16 的入口合同节点；没有重复跑全部 home16 或原 API/UI 大矩阵。实际 HTML + linkedom/VM 技术验证不冒充浏览器视觉、Cookie 或管理员现场验收。根另执行完整目标回归及发布门禁，本回执只补源交集及上述三个节点。
