# 单任务加工保存恢复：最终独立复核

日期：2026-10-10。结论：在本次限定的 single_job complete(actual_input_quantity) 范围内，未发现阻止交付的剩余代码/API恢复问题。可交根继续发布安全门禁；本子任务没有发布、正式业务写入或页面操作。group dispose 等未纳入本卡的问题仍保留。

## 精确候选与一致性

- API：c02693f8a678b1a72ed3de285b32633842b7c22c，production-save-recovery-api-20261010，4 个白名单文件。独立 LF 哈希与候选及根 ef6c4c15 同等，见 candidate-api-verification.json。
- UI：b9ac296b9aabd1007461b53f5286edf76af35746，production-save-recovery-ui-20261010，5 个白名单文件。最终独立核工作区、提交文件、作者 final-source-fingerprints.json 一致；树干净。
- 独立再次核对 UI CJS 内嵌 8 份 HTTP 档案：originalBody、write.status/body、resolve.status/body 与最终 API 导出或本线真实 nullable 导出完全一致。没有用手写成功包替代真实服务端结果。final-independent-verification.json 保存文件哈希和档案哈希。

## 最后有效验证结果

| 验证 | 最后有效证据 | 结论 |
| --- | --- | --- |
| API 独立两个组合边界 | independent-candidate-api.xml | 2 passed，5.20 秒 |
| 实际页面方法最短跨合同 | independent-ui-cross-contract.json | 3/3：组合 nullable 首写、已提交丢 ack 后刷新恢复、legacy trace |
| 真实 Vue 编译及内存渲染 | independent-vue-runtime.json | Vue 3.5.40；5 实际模板编译、6 类断言通过，0 warning/0 error |
| 作者 API 覆盖复用 | fix/api/final.xml | 29 项（25 专用 + 4 邻近）通过，不重复宽跑 |
| 作者 UI 覆盖复用 | fix/ui/final-tests.json/xml、final-pytest.xml、REPORT.md | 49/49、包装 1、相邻 harness 通过；本线只重跑以上 3 项 |

“通过”限于相应技术层，不代表浏览器或员工现场验收。根随后整合后的测试、备份、恢复、健康、版本及发布核对由根执行，不能以本报告替代。

## 两个关键后端门禁

1. 原 Command 不可改写。独立合成库启用正式 sprep0912 同款 BEFORE UPDATE/DELETE trigger；新 proof 在两个父 Command 首 INSERT 前装配。真实部分 8/20 首次保存和原键整体 JSON 重放相等，尝试原父 Command 无值变化 UPDATE 仍被 trigger 拒绝。父请求 compact 编码、旧子命令带空格编码保留；expected_actor 不混进旧签名，已有键不补 proof。
2. readonly 不依赖当前加工资格。首次保存后，仅在合成库改变原 job 快照、收料状态、源库存版本、目标位置名称和当前输出库存（原产出 8，后为可用 5/消耗 3），并阻止调用 prep.source、Session.commit。只读查询仍准确返回原冻结 8 和原位置，捕获 SQL 零业务 DML，所有业务事实前后相同。旧 body 省略 output_kind 时取持久 request_json，有效用途不再取后来 job 快照。

对应真实证据 independent-source-changed.json；详细记录 API-CANDIDATE-REVIEW.md。权限、当前/冻结客户、actor、异 body、损坏证明、commit 前回滚、commit 后丢 ack、在途未见、旧无 proof trace 等复用作者真实定向测试，不放宽原门禁。

## 前端跨合同结果

独立在精确 UI SHA 执行实际 index inline 方法、production-workspace mixin 和恢复模块的最短 3 项。

- **真实已 commit 的 500 → 刷新 → 续任务 → 查原结果：** 原投入 8 的请求持久保留；用同 storage 创建新 controller，实际 loadStockPreparation 读取真实 workspace GET，显示新的 continuation job3。打开该任务仍恢复原投入 8，同 receipt 锁阻止另建动作。只读 resolve 回到原 completed job2/原产出 8，原 key/body/actor 不变；再走实际加工历史入口。业务 `/actions` POST 总数始终 1，不把续任务当原请求重试。
- **组合 nullable 真回包：** customer_id=null、output_unit=null、location_name='' 的独立合法 API 首写被实际保存函数接受为完整证明，能关原弹窗、保留 confirmed 成功卡并清自己的 pending。没有补造“只”或楼层；原数量/位置 ID、actor/key/body 仍严格匹配。楼层实际为 3，没有声称无楼层货位通过既有业务资格。
- **历史无完整 proof：** legacy_trace 保留 unknown、原 body/cache 和可读记录入口，不冒完整成功、不清原请求、不发业务续写。

作者 49 项中的错数量/货位/账号/映射、截断证明、存储失败零 POST、unknown 后 409/Rejected、同 receipt 锁/另一 receipt 可用、成功后 GET 或清缓存失败、迟到 401/成功/位置 GET等已读证据复用。首写安全拒绝仅限从未未知且服务明确回滚/无旧父键，后来未知不据状态码取消。

## 真实 Vue 离线编译和注册检查

使用仓库自身 static/vendor/vue-3.5.40.global.prod.js 的真实 Vue.compile，未安装依赖、未启动浏览器。捕获实际模块 install 注册，再运行 Vue 自定义内存 renderer。

编译实际新增组件 template、index 内弹窗入口、工作台入口、包含该卡片的完整工作台 section、原 data-panel template。另以非法 v-if 模板做负控制，真实编译器确实拒绝语法错误。实际模板没有待解码 HTML 实体，编译选项的实体解码仅保留原文且有断言，不仿造模板编译器。

工作台真实 section 中恢复卡位于 data-panel 之前。空列表、列表错误与恢复记录读取错误同时存在时，内存 VNode 树中仍有待核对卡和操作；notRecorded 未成立则没有“按原内容继续”。confirmed 使用真实 nullable proof，显示“单位待核对”“货位 #7（名称未填）”；busy 禁用核对按钮。实际组件五个 emit 与入口 reload/resolve/continue/view/refresh 绑定都到达对应回调。这里的回调计数用于验证组件事件接线；业务行为由上面实际控制器跨合同测试验证。

这是编译/注册/事件/VNode 验证，**不是 DOM、可见截图、真实浏览器 localStorage/Cookie 或视觉验收**。

## 探针纠正与未通过的外部验收

如实保留两次探针适配纠正：API 初次 nullable 夹具误将 physical_basis 字符串当对象，修独立探针解码后最终两项通过，初轮 XML 另存 independent-candidate-api-initial.xml。Vue 首轮自定义 host 没有为旧 toolbar v-model 提供 addEventListener 钩子，加入内存 listener/activeElement 最小接口后通过；未更改 Vue 或产品源码，也未把这个 host 缺项计为 ERP Bug。最终有效结果均取上表文件。

隔离 Chrome 启动曾被自动审批以 **blocked by policy** 拒绝，没有细分原因、未执行、无新 PID；Cua 获取失败，未绕过。根已批准将此次开发验证收窄为实际页面方法 + 真实 HTTP 档案 + 真实 Vue 离线检查。浏览器显示、真实登录接线、窄屏、员工与物理操作验收仍 pending，不能标 Chrome 通过。

## 交付边界

本线只写独立 artifact/NAS，没有修改源码、测试仓库、迁移、数据库模型、正式数据、运行服务或其他代理文件，未 push/发布。没有复用18163旧内存服务冒充最终源码。localStorage 不是跨设备全局原子锁；旧资料缺 body/后续版本变化仍可能需要管理员核对，系统不能声称所有未知请求都能安全清除。本卡不启用永久结束 marker，不扩修 group dispose。

最终报告和 NAS 同内容校验后交根。管理员操作验收及正式发布状态以根后续回执为准。
