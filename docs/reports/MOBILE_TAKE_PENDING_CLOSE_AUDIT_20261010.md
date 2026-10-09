# MOBILE-TAKE-PENDING-CLOSE-AUDIT-20261010 只读回执

结论：未确认新增业务Bug。已验证正常同键恢复与防重复扣库；“安全结束未知原请求”是既有功能待办，当前没有服务器持久终结事实。不得把删除浏览器缓存、409或一次not_recorded宣称取消。

## 源与隔离

固定chain-order-audit HEAD 9a3a9f33dfc9e5194017729898f5c4e963c7e904，未checkout、改分支、改源码/仓库测试/版本或提交。审查的API、实际手机controller、入口、库存/幂等模型及两组夹具共7文件，与真正正式v601源108de0681c73fc213f94dd123d077eec395a8186逐字LF规范化一致，source-fingerprints.json记录双方SHA256；订单恢复候选不当正式版本。本轮只在本artifact写探针与证据，全部真实HTTP只写安全conftest生成的独立合成库。未接正式DB/服务、未迁移、未push，未操作/清理任何进程或18161服务。独立UI服务继续引用原树。

## 三个确认边界

1. 原键完成能安全恢复。真实take首200、精确同键重放200同movement_id、resolve completed，无额外扣库/流水/业务审计。注入真实commit成功后丢ack，take返回409但无Rejected；resolve查实完成，精确原键再发仍replayed。两笔各3的正常+丢ack取用把合成批次可用10→4、预占始终2、已用1→7、version3→5；共2流水和2业务审计，再查/重放零新增。no-store属于现resolve合同；丢ack409现状没有新终结保证。

2. 查询未见不证明永不执行。实际take在写锁中CAS后、创建流水前暂停，另一真实HTTP resolve读到not_recorded且can_continue=true；解除暂停原POST仍200，随后completed，同一数量只扣一次。另反例仅模拟本地遗忘记录后再送原body，原POST仍200；真实UI没有“清缓存即取消”动作，这不是现有UI取消Bug。证据inflight.json、no-persistent-close.json说明未来不能仅删记录解锁。

3. 旧未知可安全保留但没有结束出口。真实另key取用1使version3→4后，原key resolve始终not_recorded/can_continue=false（库存版本原因）；原旧POST409 Rejected且库存/流水/审计不变。实际controller在此前已unknown时收到后来Rejected仍保留原body/key，取消只关取用框，禁止另批次取用，直到查实完成或资格恢复。本轮不证明所有写路径版本永远单调，不能把version>原值当永久失效证书。此为既有安全终结功能缺口，不新增计Bug。

## 当前手机恢复合同与限制

实际文件static/ui/mobile-dimension-stock.js使用sessionStorage而非localStorage：每账号tm-dimension-take-pending-v2:{userId}只有一槽，保存version/ownerId/lotId/outcome/sent和完整quantity/purpose/expected_version/location_id/address_version/idempotency_key/expected_actor_id。同标签页刷新可重读，关闭标签页、新浏览器或其他设备不共享；没有服务器待处理请求登记，不能保证跨设备发现未知键。不能声称sessionStorage跨页原子锁。

v1记录包含lotId+完整业务body但没有可信原owner；只能只读核对，完成显示真实历史操作者后显式确认；未见且资格允许才明确用当前账号继续原key原body。缺失/损坏body当前直接阻止取用，不以当前库存或新草稿补造原载荷。v2 owner与当前挂载身份、body.expected_actor_id一致；换账号重挂不会清旧owner。write服务器认证expected_actor；resolve回current_actor_id与历史actor_id分开，UI先核当前身份；完整movement_id/quantity/replayed才成功清准确缓存。确认成功后的库存刷新失败只查库存；confirmed缓存清理失败保留防重发状态。

新增实际controller5探针（frontend.json）证明：丢ack完成后只读核对清准确原key零再次take；未知后409 Rejected仍保留且不能继续；Cancel只关框；换账号不清旧owner；缺body不造请求。使用真实controller及实际API parser，但DOM/sessionStorage/ledger为继承合成适配，不是Chrome验收；真实HTTP另有api.xml及4JSON证据。

## 建议下一最小实现合同（尚未实施）

新增明确“结束这笔原请求”写动作，可拟POST /api/mobile/erp/warehouse/dimension-stock/{lot_id}/close。它不替代readonly resolve，不发零数量consume，不用盘亏。不更改库存数量/版本/预占/栈板；只写持久key终结记录与同事务业务审计。请求保留原完整TakeRequest，expected_actor_id必须严格等于认证账号；原key/lot/数量/用途/库存和位置版本全部纳入原签名。请求的owner声明只保护当前账号一致，不当作未见历史的认证证据。

关闭权限warehouse.execute＋当前客户范围；终结标记冻结的客户与当前批次范围均复核；已有完成流水沿其真实权限合同核对，不凭请求自报客户补造历史归属，不泄漏另一客户。仍warehouse.view的只读核对可给事实，但禁止当执行授权。v2必须原owner账号操作；历史已完成操作者与当前认证分开，其他actor/异body冲突保留，不给Rejected或误清许可。v1完整body无owner：先只读核对；若未见，必须显式确认由当前账号接管原key再结束，不自动推断历史owner。真正缺body/键的旧记录不在自动关闭范围，保留可读管理员核对入口。

与take使用相同SQLite BEGIN IMMEDIATE写锁后再查原key。原POST先拿锁：关闭等待其commit，查精确consume则回completed，不产生tombstone。关闭先拿锁：查无完成后原子保存tombstone＋审计；迟到原POST在同锁中先检查该持久标记，永久拒绝该key（不同actor或异body也不能绕过占用）。take/close/read resolve都须查标记；close同key精确重放回同closed事实，异载荷/损坏标记409保留。若已完成与终结两种事实异常同时存在，不猜状态，冲突人工核对。若以后支持非SQLite，需两动作共享同批次行锁或键锁；不能照搬SQLite保证到无同锁路径。

过期版本、货位停用或可用数不足仅作原请求内容/当前不能执行提示，不阻止零数量变化的关闭，但客户权限仍必查。成功closed回执至少status、原key/lot/actor、current_actor、原body或精确签名、closed_id/时间；没有伪movement_id。UI严格验证“当前库未见原键完成流水，原键已停止接收后续取用”的事实后才允许显式结束并查询最新库存；不得说历史从未取用/实体从未拿货。关闭结果未知仍保存原body/key，可readonly核对completed或closed，不自动新键。

无需新迁移的最小候选：已有WarehouseGoodsMutation表（rv10v8x9z70，字段unique idempotency_key、request_hash、lot_id、response_json）可用专属mobile-take-close:<rawkey>命名空间保留tombstone，长度满足100，response_json冻结原owner/body/lot/customer/status/time。需专用helper直接写此表及审计，不调用warehouse_goods.record，因为它会改货物主档资料；不把关闭写作库存流水。FinanceIdempotencyRecord也有actor/action/resource/response字段，但偏财务语义，本方案优先仓库已有表，待根新卡明确；任何复用先对同键全局占用、既有profile命名空间、唯一约束与持久记录校验做定向测试。现schema能力支持无迁移方案，不表示本轮已写标记或验证正式表。

允许下一轮最小文件建议：app/api/mobile_stock_use.py、必要新app/services/mobile_stock_take_close.py、static/ui/mobile-dimension-stock.js、专用新API/前端恢复测试。入口只在引用版本必须变时由根串行处理；不改共享库存算法、主档/权限服务、模型或迁移。

## 下一轮最短验收

正常take/重放；已完成关闭只回原consume；关闭先锁→迟到take被永久拒绝；take先锁→关闭回完成；同close key重放及异body/actor冲突；权限/跨客户和原/当前范围；版本/停用仍可终结但库存/预占/版本零改变；marker/审计任一步故障全回滚；close提交后丢ack能只读恢复；缺body不误清；页面切账号/迟到结果/缓存失败保留原记录；closed后才允许显式新key取用。至少一条数量守恒及持久marker并发真实HTTP。

## 验证交付

api.xml：4 passed，9.610s；frontend.json：5 actual controller边界探针passed，约瞬时。源/节点及四真实响应JSON在本artifact。没有重新跑全仓或改仓库tests，未制造新缺陷计数。当前仅只读方案，候选/API终结/UI新出口均未实现；正式v602订单恢复发布由根独立进行，持续Goal保持active。
