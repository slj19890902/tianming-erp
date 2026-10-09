# 手机盘点保存结果核对：修复方案与根复核

任务MOBILE-STOCKTAKE-RECOVERY-20261010。开发定向验证通过，正式发布另记。持续Goal继续；不代表全ERP已无Bug。

## 证据与改动

旧手机盘点将空/坏2xx转为空对象后仍显示已生成，并清除原键和填写数量；原confirm在commit后读取完整回执，SQLite忙可返回409但实际已经保存。合成真实HTTP故障注入已证实此路径，不代表正式历史一定发生过。执行者原红测试与截图保留在mobile-stocktake-recovery-api及mobile-flow-audit。

本轮API只改app/api/stocktake.py：提交和确认在flush后、commit前构造完整plain回执，所有生成异常在回滚边界；新增只读resolve，以原动作、原key和完整原body核持久盘点/审核/调整事实。返回原键、当前认证和真实历史操作者；当前账号保护可选严格正整数expected_actor_id不改变原持久业务签名。原submit权限、confirm额外review+admin及全部客户范围保持；安全拒绝审计保留，resolve不autoflush或提交业务写。无差额允许没有调整流水，后来的库存/地址变化不抹掉原成功结果。

UI仅手机盘点页和专用恢复组件：发送前保存原账号/动作/key/全body/输入，空坏部分回执、弱网和其他不确定响应保留原记录，完整核对id/单号/状态/身份/命令key/货位与批次原快照才报成功。真实审核id/身份/时间/状态也须一致。按账号列多笔，每笔独立存储键；不把localStorage读写称为原子锁。只限制原货位，其余货位可读取最新事实后继续；成功但清缓存失败仍记confirmed并阻止同位置重复。未知先只读核对，明确继续只发相同原请求；not_found不能证明原请求已注销。新提交前校验实际schema及安全整数，资料缺失不生成新的待确认记录。LAN使用纯UTF8 SHA256，不依赖crypto.subtle。

根复核补充并闭环：
- 首次confirm201原本缺review集合，跨真实API→UI的3组确认失败拦截。原因是approve为sequence读取过reviews，flush新审核行后旧关系集合未刷新。补丁只过期reviews/reviewer两关联并于commit前重读；服务算法不改，未用expire_all。3个真实HTTP先红后绿，首次完整order与resolve字典一致。
- 清记录失败改为原record错误，避免全账号卡住；真正整体storage不可读或账号变化才全局阻止发送。
- 真正冷启动从空selectedLocation恢复后再次应用controller状态，确保原数量立即不可改。
- 两货位、不同账号和多个页面互不覆盖；旧A回执不能改当前B输入。成功与库存刷新结果分开，刷新失败仅GET。
- 隔离Chrome实际长表单发现恢复提示在屏外，新增同账号同货位结果的可见定位；未知时滚到恢复入口，成功时展示成功消息，不能用旧A结果抢占当前B位置。保留06截图为修前证据，不混入最终验收。
- 实际跨货位流程发现B成功信息可能落在A的待核对区，补上明确货位归属；无选中笔时明确剩余请求仍未确认，不把另一笔成功当作全部成功。
- 根整合后补查桌面嵌入合同：401切登录销毁controller后，旧请求finally不会再发busy=false，父ActualStocktakeDialog会永久禁止关闭。新增embedded真实页面消息测试先81通过/1失败，再在destroy退出保存状态时发送false；修后82通过，原请求仍保留。此为发送给同源父窗口的状态修复，不改父组件或保存权限。

## 验证

根后端43项通过（D:\erp-audit-temp\mobile-stocktake-recovery-integrated.xml，SHA256 3167358232b3ad4b041df68f4822fa2a25ce35b392c0b343176cbc31771af042），32新API恢复用例与11条旧相邻合同。真实盘盈、无差额、有效预占盘亏、回滚、同key并发、异载荷拒绝、只读零业务写、历史状态、权限客户/actor、故障提交结果未知与原key重放均覆盖。

根实际HTML+专用组件82场景通过（ui-integrated.json），包含坏2xx、输入恢复、存储读写/清理失败、401/403/409/422后原请求保持、双击、切账号晚成功/晚401、跨页面共享存储及可见换位入口、冷init禁改。Node DOM夹具不代称真实浏览器或后端。

旧Python前端组22通过/5失败的初证保留。其中两条由本次模块抽取产生的源码写法断言已维护：入口检查真实引用的module；busy/禁改改跑完整行为验证，不把旧赋值字符串伪加回产品代码。两节点复验通过，mobile-stocktake-maintained-contracts.xml；其中行为验证重用上述CJS，不累加为新81组。另三个已有基线失败分别是compact layout旧字段提取、旧submit单函数夹具缺依赖及规格渲染单函数夹具缺依赖，基线4eca原样复现；不宣称全仓测试全绿。

根7组跨合同通过（cross-contract.json）：6组真实HTTP导出响应直接交实际JS控制器消费，正常首次回执与网络未知后只读恢复均核对；包括管理员盘盈/无差额/盘亏、员工submitted/approved/rejected。另1组在无crypto.subtle上下文中交叉校验UTF8摘要。fixture候选1a38eda49af492e8d497e3bdff9ee5314ac4fc31的API源码与整合源码比对相同。修前cross-contract-before-api-repair.json保留3组失败，不伪报修前全绿。

隔离Chrome实际候选页面操作/截图见D:\.codex\visualizations\2026\10\10\mobile-stocktake-recovery-ui\REPORT.md；合成API与真实API证据分开，截图已查看。无正式页面自动点击、无IAB，无真实扫码硬件或现场手机通过声明。

执行者API d4dc1c2a＋1a38eda4，根96b9344f＋11f84478；当前整合HEAD 83b1edf58c2d42f71c6a0d3c4529c7ce4bc69c50。UI候选与最终发布源以Git/发布回执记录。仅4个运行文件允许变更；无迁移、无正式库存/订单/成本/历史补写，无Git push。唯一head、签名构建、备份恢复、323表/附件保持、健康及静态资源需发布门禁另验，不把这些技术结果冒充管理员验收。

## 已知后续

无法找到且原版本已变化的未知请求暂保留给管理员核对，持久安全终止另卡；不能宣传再试一定成功或让用户删除本机记录。跨页返回查货条件、触控/长编码布局、桌面approve/reject的旧commit后读取行为属于后续，本卡未扩改。正式技术发布后只需在正常盘点时核对单号/状态，出现待核对点原记录，成功刷新失败点重新读取库存；不为验收额外调整实物账。
