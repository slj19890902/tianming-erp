# 单项加工保存结果恢复：根整合复核

本报告记录 v603 候选技术验证，正式发布事实由文末发布回执补充。范围严格为 single_job 保存入库，不混入 group dispose、组套、撤销或手机永久结束未知请求。

正式起点 v0.22.602 / 6640d5369e2d48ecba36be9aefae1afa9ed449db / en1009hp。API候选 c02693f8a678b1a72ed3de285b32633842b7c22c；UI候选 b9ac296b9aabd1007461b53f5286edf76af35746。根分别整合为 ef6c4c15、5c1abfeb；相关文件逐一与候选相同。本次无迁移，不自动修改正式业务历史，不push。

## 原问题与修复

真实入口是单项加工保存→actions(action=complete且actual_input_quantity存在)。原页面把空/坏2xx当成功并关框；原请求仅在可变对象内，未知后编辑、关闭再开或刷新会丢原key/body；局部请求迟到401可经公共拦截器影响后来登录账号。原同key幂等与事务回滚一直有效，刷新后continuation的新key是另一合法加工，不能误称同key二扣。

完整原请求在发送前按账号和key独立保存并读回；按receipt保护原材料的后续任务，未知时不能重新填写为另一完成请求。查询原结果只读，not_recorded仅表示查询当时未见；明确按原内容继续才沿原key/body发送。完整确认才显示成功数量/位置和可读加工记录，列表刷新或缓存清理失败分别提示。原位置/客户/单位缺失按旧合法合同显示待核对，不造数量单位或新业务门禁。

后端把本次完成证明随父Command首次INSERT写入，保持原不可UPDATE/DELETE触发器。process仅新增默认None的可选builder和两处装配调用，不改变加工、预占、冻结工艺、BOM、成本或审计算法。真实物理产出与本次台账movement单位分列；原body、actor、requested/completed映射、原来源和位置都冻结。只读恢复不受后来的当前加工资格限制，但保留当前客户与原冻结客户权限。

原process compact签名及旧mutate普通JSON字节保持。相邻测试发现新增write.replayed false/true破坏首/重放整JSON相同合同，已删除这项非必要字段，未改旧测试。局部single保存/恢复及首次和地图货位读取使用有限超时的独立请求实例，当前401仍按原规则失效，旧账号迟到响应不影响新窗口。

## 技术验证与证据

- API最终29项通过（25专用＋4原邻近）；覆盖部分/全量、同key零新增、异body/actor、权限、真实commit丢ack、提交前整体回滚、正式同款append-only触发器、旧无proof追溯、来源后来变化、nullable、持久用途核对和不足一件余料。
- 最终API真实HTTP导出6节点形成7组JSON，另1节点补真实workspace GET与history。UI还消费独立组合nullable原文。根逐份比对8份档案的原body、write/resolve/replay、实际列表和历史响应，与CJS嵌入内容相同；未手写成功回执。原来源行仅投影未用字段，不把投影当服务端新合同。
- 根整合实际HTML/mixin/恢复模块49场景通过，记录五文件源指纹；API/UI均与精确候选相同。提交前存储失败零POST、实际关闭/重开、页面状态重建、未知后冲突、不同账号、迟到响应、成功后列表/清理失败均有断言。
- 独立API最终2项通过：触发器拒绝UPDATE、旧签名字节、真实状态/快照/库存后来改变后只读仍返冻结8，捕获业务零DML/commit；组合空客户/空单位/空名称真实首写/查询/重放合法。
- 独立UI3项通过：真实组合nullable、实际commit丢ack→新页面真实GET continuation→原8恢复及receipt锁→readonly completed→加工历史，业务actions总数保持1；legacy_trace保留原body/cache不续写。
- 使用项目自带Vue3.5.40真实编译器编译5份实际模板，非DOM自定义renderer6项注册/状态/事件/禁用检查通过，0warning/0error，另有非法模板确实被拒的反向对照。空列表或错误状态仍保留恢复卡，不把离线渲染称为浏览器截图。

重复覆盖不相加作独立缺陷总数，未运行全仓。证据目录：D:/.codex/visualizations/2026/10/10/production-completion-save-recovery-fix、production-completion-result-review、production-completion-recovery-release-v603。

故障记录保留：作者新facts适配器误写OperationLog.details_json造成初20失败，改为真实details后通过；独立探针先把physical_basis字符串当对象、非DOM host缺旧toolbar所需事件钩子，均只修artifact后再通过，不当ERP缺陷计数。安全红和真实兼容回归失败单独记录，不隐藏。

## 剩余边界与发布要求

自动审批拒绝了新隔离Chrome启动，仅给blocked by policy；CUA连接也失败。未换工具绕过，无新Chrome PID。截图审查停止，技术验证收窄为实际控制器/真实HTTP和离线Vue；浏览器DOM可见性、Cookie/现场存储、窄屏及管理员操作仍pending。此前旧测试进程停止亦被拒，旧18161/18162及中间18163合成服务保留，正式ERP未受影响。

localStorage不是跨设备原子锁，不宣称全球唯一。旧记录缺完整body或版本已失效而查无结果时，继续保留并供管理员核对；不提供尚未有回退/恢复兼容硬门禁的永久结束按钮。group dispose已另立只读审查，不混入本包，也不宣称整个ERP无Bug。

正式发布仍须精确候选/基线、签名包、唯一迁移头、NAS新冷备与恢复验证、逐表业务及附件保持、完整性/FK、健康与实际静态资源比对。正式页面不自动点击，人工验收不代填。
