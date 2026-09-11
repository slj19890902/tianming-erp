# 货架正视图点选货位自动收起

任务 ID：RACK-CELL-AUTO-COLLAPSE-20260911。老板要求：打开货架正视图后，点击货位自动收起正视图，直接展示该货位信息，不必另点返回。

基线：origin/factory-current-baseline d44ba00e / v0.22.349；发布前再次核对。单代理，独立 codex/rack-cell-auto-collapse-20260911 分支及 worktree。

验收：点击格数或格内非操作内容，按唯一正式 location_id 选中货位、关闭正视图并显示右侧货位信息；查货/移货/盘点保持当前模式和草稿。新增货物仍进入该格盘点；编码查看产品标签、批次展开、打印不触发意外关闭。缺失或冲突的正式身份不可选；沿用既有移货目标判断，不提交任何库存操作。

文件 allowlist：WarehouseTwinApp.tsx、rackEmptyCellClick.test.mjs、本任务文档、版本文件、对应前端构建资产/入口、发布回执及必要正式基线索引。无后端/数据库/地图结构/库存/数量/成本/权限改动。先失败用例，再最小实现、相邻回归、构建及差异检查，按既有发布授权和备份健康门禁交付，写 NAS 独立任务回执。

## 开发验证

- 根因：有货格的格数按钮只改变正视图内部 selectedItem，不选中正式货位；rack-focused 样式持续隐藏右侧货位信息。
- 新增独立查看货位回调，格数和非操作内容选中唯一 location_id 后收起正视图，复用地图选中逻辑，聚焦原货位信息栏。不清除移货来源/盘点草稿，不调用提交接口；空格无添加权限时仍可只读查看。
- 编码标签、展开批次、打印、添加按钮不触发格内点击冒泡；身份缺失/冲突不开放选择；新增库存权限和规则未放宽。
- 先执行新增失败用例：3 项失败、7 项既有测试通过。修复后 rackEmptyCellClick + rackFront 共 13 项通过；TypeScript、Vite、git diff --check 通过。未运行无关后端/全量测试。
- 构建入口 warehouse-twin.html 引用 warehouseTwin-B9odmguF.js，SHA-256 7D09F78C7D6182381273DCE2EC29BEBDBA23A9301F1F9C0AE700A286F6F58E37。
- 唯一 head：rz10v8x9z74，沿用 v349 已有邮件关联迁移，本任务无新迁移。
- Chrome 工具可发现浏览器，但打开专用仓库验收页 30 秒超时并重置会话，未使用 IAB；开发验证通过，正式 ERP 人工验收待完成。
