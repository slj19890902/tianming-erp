# GROUP-ASSEMBLY-SAVE-RECOVERY-AUDIT-20261010：UI只读审查

日期：2026-10-10。正式v604源码75129569ed5ff0bf454d075471c9b4fd902b070f；只读UI树固定e578e90506fbcd654a6b2341fed75d447dcc8003，树干净。index.html、production-workspace.js及stock-preparation-group-recovery.js逐字LF与正式源码一致。没有源码、仓库测试、分支、版本、数据、服务或PID修改。

## 实际员工入口和原载荷

实际生产备库group_stock“子件存放 / 组套”→openStockDialog→选“已组好整套入库”→HTML4916保存入库→saveStockDialog('assemble')→共享axios POST /api/production/stock-preparation/group-actions。这是已加工子件另行组套；不属于刚发布的group_job/dispose。没有人工添加普通保存确认。

openStockDialog的sets取remaining_sets或原计划group.sets，全部子件semi时入库方式初始semi，员工选finished才调用assemble。原body包含action=assemble、disposition=finished、parent_id、sets、basis_hash、group_key、顶层位置/layout_version、confirm_overproduction=false、原jobs数组顺序的job_id/job_version/lot_version/actual_output（原expected_output）/output_version/位置/layout_version及operation_key。没有expected_actor_id。具体原文保存在probe.json的observations和API normal-2.json.originalBody。

key仅存在弹窗d.attempt；signature为未附key的完整payload JSON。任何相关输入变化即生成新key；关闭或刷新没有恢复该attempt。首POST前没有保存完整原body到业务恢复storage。

## 三个已复现问题

1. **空/部分2xx误报成功。** production-workspace.js164只await axios.post，不读取data；null和{}都关闭弹窗并提示“成套成品已入库”。实际HTML保存处理器及实际方法复现。两种坏回执同一根因，不分别计Bug。不能凭前端误报证明后台已经执行。
2. **未知结果只有弹窗内存，改数/关闭重开/刷新丢原请求。** 网络未知后2→3套生成不同key，storage业务恢复记录为0且同组assemble不锁。使用实际HTML关闭处理器后openStockDialog重开回默认5套，新页面也无原key。真实提交后丢ack的同库GET则显示已经产出的2套和余片，但页面重开默认余下3套、没有原2套请求恢复/锁。已有key精确重放仍正确；不能称原key重复二扣，也不能仅从页面推断员工又做了一次实物组套。风险是未知事实未先核清，员工可在新上下文继续新请求。
3. **旧账号迟到401在epoch判断之前触发共享Axios全局登录副作用。** 实际HTML注册的全局response interceptor先调用erpAuthRequired，saveStockDialog自己的authGeneration判断随后才执行。探针换到账号B并打开新窗口后，A请求迟到401仍触发全局回调1次；旧业务窗口数据没有覆盖B。这是实际函数/拦截器顺序验证，未冒充真实浏览器Cookie/完整登录DOM验收。

## 已有效保护，不计新增Bug

- 真正在途loading使关闭/保存按钮disabled，save方法重复调用零第二POST；请求结束退出loading。
- 旧账号迟到空2xx受已有authGeneration门禁保护，不关闭B窗口、不toast成功。
- 已发布dispose unknown记录确实会锁同组group_stock assemble（也包括后续子件存放入口）；新assemble保存没有被dispose组件持久接管。不能把dispose已修恢复能力算成assemble已有。
- 真实assemble200后列表GET失败：通用成功提示仍保留，stockPrepError显示列表失败；未误称业务保存失败。当前没有保存原结果/可读原笔结果卡，这随请求证明恢复方案补齐，不另凑一个根因。
- 实际group选位GET已有局部认证通道；本轮迟到401发现针对真实共享业务POST，不误指已保护的选位GET。

## 真实HTTP对应与观察结果

只读探针调用完整实际HTML的methods、production-workspace mixin、single/group恢复模块；不是函数切片或重写业务实现。probe.cjs只位于artifact/ui，运行需cwd为固定UI树。

**10/10观察事实复现通过**（probe.json、probe.xml），含7个故障/有效门禁观察和3个真实HTTP入口观察。这不是10个修复通过：本轮没有修复源码。

最初故障输入使用已正式等价的API4d6808e7 `group-preparation-save-recovery-fix/api/http/semi-sets-5.json.workspaceAfter`真实group_stock投影；只改变传输返回/账号/员工输入，不伪造成功proof。正常及真正丢ack消费本任务API `normal-2.json`、`committed-ack-loss.json`原文：

- normal-2首200实际body与UI发送逐字段完全相同；2套消费6+8，剩9+12。重复原key/完整body200完全同JSON且零新增事实；异常签名/旧版本后端门禁保留。
- committed-ack-loss真实Session.commit成功后OperationalError返回500，Command/result/actor已持久；exactReplay200没有新流水/库存调整。页面首POST收到这份真实500，刷新读取同库workspaceAfter同时有assembled_stock和group_stock；原operation_key对应history404，当前dispose resolver拒assemble409，不可当原请求not_recorded。groupHistory200可追溯，并不是精确原key/完整body证明。
- 此页面丢ack链总业务POST仅原首次1次；未调用新保存去证明第二笔真实执行。成套/余片库存事实由API合成真实库证据支持，页面网络适配验证仅证明反馈和生命周期行为。

source-fingerprints.json记录三个正式等价运行文件LF SHA、原投影来源、两真实档案原始SHA及探针SHA。实际probe采用可控传输适配，非浏览器DOM渲染、非现场实物操作。没有Chrome/服务/进程动作，不用IAB/finalize/Playwright。

## 建议的下一最小方案（待根评审及新卡，不实施）

建立独立assemble恢复组件，局部接group_stock/action=assemble；groupStockLocked合并dispose与assemble两类未知锁，恢复卡保持在空列表/错误列表之外可用；保留当前已发布dispose及single组件行为、数量算法和完整回归。原实际body首写前可靠保存actor+独立key+group/父产品+原完整body及可读套数/逐子件来源和目标位置摘要，按账号+原group锁此未知及同组后续写，异组可用。存储失败零POST，独立原key记录不互相覆盖；不要声称localStorage原子锁或跨设备互斥。

API另给真正assemble持久完成证明与只读原key/fullbody解析入口；服务端在现有事务/同写锁首次Command插入前冻结完整结果，不从当前workspace/历史摘要拼成功。证明需绑定原认证actor、完整规范原request/原数组顺序、冻结父子配比与每个真实输入来源lot/job映射、实际消耗与成套输出lot/流水、原位置和数量/单位；多job同产品、部分组套余片及nullable标签须沿当前合法业务，不重算算法。proof中的余片是本次组套完成后的冻结事实，不冒充实时库存；actual assemble原响应不能强要求dispose派生assembly key。精确字段由API和根另行固定，本报告不自行约定新端点/字段。

UI仅完整有效回执确认，{}和部分回执保持unknown；query not_recorded只是时点观察，原内容冻结，员工明确继续才同key/body发送。未知卡直接给“查原组套结果”、原套数/来源/目标位置、可读加工/组套记录及具体管理员核对依据；不能只提示有请求。旧无证明只追溯，不清原请求，不把history404解释成未执行。

新的本局部开窗/选位/地图/列表/保存/核对/刷新通道都按当前actor与epoch隔离迟到401，核当前账号/epoch；有限超时退忙但保持unknown，不声称撤销服务端执行。首次有阶段证明的明确拒绝可更正，任何此前unknown后续错误均保留。成功后列表失败只提供只读刷新，保留原套数、目标位置及查看入口；读取追溯失败不撤销confirmed。

未知无法安全结束/原版本已变化的记录保留管理员核对出口，本轮不发明注销。普通保存不加确认；实际超产等原有确认规则不改。管理员正式/现场验收待后续候选，不代填通过。

## 过程与交接

一次只读段落输出因Python默认GBK遇特殊字符中断；改本次终端PYTHONIOENCODING=utf-8后补读必要章节，不改代码/全局配置。其余10观察均正常完成。未扩大参数矩阵；共三个同根因问题及上述有效门禁即止。

UI树仍clean/e578固定。正式v604和旧服务/PID保持原状。本报告与NAS独立回执交根，等待先方案评审再实施。运行：在固定UI树执行 `node D:/.codex/visualizations/2026/10/10/group-assembly-save-recovery-audit/ui/probe.cjs`。
