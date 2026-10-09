# 独立组套保存恢复合同 v1

基线75129569/v604；仅原group_stock顶层action=assemble。single、dispose及其内嵌assemble不变。所有字段以下固定，不增加replayed；首次与精确重放完整写JSON相同。

## 请求与身份

原POST /api/production/stock-preparation/group-actions，仍为原GroupAction；新页面总带expected_actor_id（严格正整数，API可选以兼容旧调用）。认证actor必须一致，此字段不进入旧签名。

新只读POST /api/production/stock-preparation/group-assembly-result：{operation_key:string, original_request:完整原GroupAction, expected_actor_id:当前原owner正整数}。外层/内层key必须相同；两个expected_actor若有均核当前认证身份，不接管其他owner。

规范request=GroupAction.model_dump()，删除expected_actor_id（模型exclude=True），仅删除jobs[].lot_id为null的字段。其余全部默认字段保留：action,disposition,operation_key,parent_id,sets,basis_hash,group_key,jobs,location_id,layout_version,confirm_overproduction,assembly_key,output_version,sources,confirm_unused。jobs保留原数组顺序，每行job_id/job_version/lot_version/actual_output/output_version/location_id/layout_version，lot_id仅非null保留。默认disposition=finished,sets=1,basis_hash/group_key/assembly_key='',jobs/sources=[],location_id/layout_version=null,confirm_overproduction/confirm_unused=false,output_version=0；jobs actual_output/output_version=0,location_id/layout_version/lot_id=null。encode=JSON sort_keys=true,ensure_ascii=false,separators=(',',':')；digest=SHA256(UTF8(encode(value)))。digest字符串包含JSON引号。无默认字段宽泛忽略。

assemble旧忽略的disposition（semi也合法）、actual_output（0合法）、原料lot_version、行location/layout等仍回显/签名，不把它们误当本次成套事实。算法输入仍实际sets、top位置、job/output版本。

## 写与只读包络

写200保持旧字段action='assemble',group_key,sets,recipe,inputs,output_lot_id,placed_at_utc；附current_actor_id正整数,proof_status='complete'|'legacy_trace',assembly_completion_receipt=proof|null。旧inputs字段lot_id/quantity/product_id/movement_key/version保持；旧recipe允许原辅助字段，不改变。

只读200：{status:'completed'|'legacy_trace'|'not_recorded',operation_key,current_actor_id,assembly_completion_receipt:proof|null,result:旧原result去proof|null,trace_url:string|null}。not_recorded时result/proof/trace_url均null，只是查询时点未见父Command。legacy_trace仅精确actor/原签名相符且旧结果有效，无新增历史证明，不解未知锁。

专用业务成功/错误no-store。标准共享_group_user认证/角色错误沿既有Group-Preserve/no-store；FastAPI模型422可能无专属头，均不代表可清理原键。专用业务错误默认X-Production-Assembly-Preserve:1；actor不符409再加X-Production-Assembly-Actor-Mismatch:1。仅同锁fresh父key不存在、commit未开始、业务错误完整rollback成功时X-Production-Assembly-Rejected:1且绝无Preserve。此前unknown收到任何后续错误仍保留原请求。标准认证/角色门禁不放宽（admin/boss），当前及冻结客户范围同时核验。

## proof精确字段

所有字段必须存在，未知字段拒绝。未注明nullable均非null。正整数ID/版本；数量非负整数（inputs数量/sets/输出数量为正）；字符串允许空的字段明确注明。

proof={schema:1,operation_key:string,group_key:string,actor_id:positive,parent_id:positive,request:规范完整原请求,recipe:Recipe,sets:positive,sets_unit:'套',evidence_key:string,placed_at_utc:string,members:Member[],inputs:Input[],output_lot_id:positive,output_movement_id:positive,output_stock_quantity:positive,output_stock_unit:非空string,output_unit:string|null,location_id:positive,layout_version:integer|null,location_name:string(允许空),warehouse_floor:integer|null,trace_url:string}。

Recipe={parent_id,customer_id:positive,code/name/unit:string|null,version:positive,children:[{product_id,code/name/unit:string|null,per_set:positive,version:positive}]}。来自冻结recipe而非当前BOM。每product配比唯一，members允许同product多job。

Member={job_id,product_id,receipt_item_id,source_lot_id,source_customer_id:positive|null,output_lot_id,requested_job_version,requested_output_version:nonnegative,location_id:positive|null,location_name:string|null(允许空),warehouse_floor:integer|null,stock_unit:非空string,output_unit:string|null,before_available:nonnegative,consumed_quantity:nonnegative,remaining_stock_quantity:nonnegative,balance_basis:'consume_movement'|'locked_end_balance'}。

members为原jobs完整集合，按job_id定位，不要求数组同序；output_lot_id是job的已加工子件lot，不是原料source_lot_id。每job receipt/source/current身份与冻结客户都核。完整成员身份锚点是原job/receipt/product/请求版本；sourceRow的output_locations为过滤后的非完整位置投影，仅在有投影时对该lot做正向附加核对，不能要求每个member均在此集合。已耗尽且take0的成员合法output_locations=[]，以服务端持久proof核原output身份，不从页面猜lot或批次号。requested_job/output_version逐行对应原body，不把ignored原料lot_version当组套版本。冻结来源位置可以null，不能用目标位置替代。余额关系before_available-consumed_quantity=remaining_stock_quantity。

Input={job_id,product_id,output_lot_id,movement_id:positive,movement_key:string,quantity:positive,unit:非空string,before_available:nonnegative,after_available:nonnegative}。仅take>0成员，一个job一条本次consume；before/after来自本次流水，映射同Member。take0 Member从同写锁下lot的本次结束余额冻结，consumed_quantity=0、before_available=remaining_stock_quantity、balance_basis=locked_end_balance，不造零流水。其他members balance_basis=consume_movement。

按product汇总Input.quantity=sets*recipe.children.per_set。本次output_movement为manual_in，key='prep-kit:'+operation_key，数量=实际sets；inputs movement_key='prep-assembly:'+digest([operation_key,job_id])[:50]；evidence_key='assembly-inputs:'+digest(operation_key)[:45]。旧result.inputs逐条按lotID与proof inputs数量/product/key对应。output_lot_id/sets/recipe/placed_at_utc与旧result完全对应；proof.output位置/layout与原top request相同。

单位：stock_unit/inputs.unit/输出台账单位来自实际lot/流水；output_unit来自冻结physical_basis，可null或合法空字符串，UI明确显示“单位待核对”，不可猜“只”。本次余片是历史余额，不声称实时库存；不从初加工入库量倒算，不承诺可选共享登记状态。

trace_url='/api/production/stock-preparation/history/'+URL编码原group_key；历史读取失败不撤销已确认proof。readonly不跑当前来源posted/active/剩余数量/位置/当前BOM写资格，不从当前任务重造历史proof。

父proof由专用builder在最终父Command首次INSERT前装配，原输入证据→输出/成本/栈板→父Command/审计/flush→enroll→commit顺序保持；任何失败整体回滚。旧Command禁止UPDATE/DELETE触发器保留。
