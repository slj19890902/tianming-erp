# 第三版手工片料UI执行回执（Sol）

本阶段负责人：GPT-6.1 Sol，stock-v3-20261014。根授权在dispatch ba918c1b完成后接管static/index.html；本工作区先cherry-pick同一dispatch为0dfdad4d，仅作依赖，交付UI提交时根只cherry-pickUI，不重复该dispatch。

已有后端17e4aeb5/da3582bd不改。app/api/warehouse.py已释放给C架任务，本阶段未修改任何后端、数据库或送货方法。

## 实际行为

- 原5个普通/BOM报料及草稿位置接同一手工组件；新增展开订单行、订单详情、内部已报料明细抽屉，共8模板位置。入口不凭“已报料”或can_requisition隐藏；实际权限、客户、阶段和用途由原API核验。
- 订单按真实明细ID，BOM按真实快照ID/whole-cover-base，合并草稿展开每个子件。编码相同不代替物理产品/子件身份。内部明细从原reported-document相同document_id/source_type/line_id/order_item_id读取typed source，BOM严格解析component:snapshot:component；普通历史精确盖/底后缀沿用后端原编码约定。
- 展示已承诺片数、未合并剩余片数、已安排库存片数、实际可用张数、每张产出/分切依据；用户填写实际张数，不自动填全库存。
- 成功后普通详情重新GET订单事实、内部原单据刷新；普通采购草稿调用原权威刷新服务；BOM重新preview版本/fingerprint，更新本子件及合并父行总量，保留合法备注、独立额外备库和其他子件指纹。
- 库存已全覆盖且无额外备库时移除采购行/空供应商组，不造0张采购。合并BOM已覆盖子件从最终payload过滤，其余子件仍以自己的指纹提交；明确额外备库可保留。
- 原请求超时复用同key及原数量。父表单保存和关闭受阻，原组件的核对按钮可用；父原控件单独禁用，避免整体fieldset挡住子组件恢复。重挂载时从pending恢复uncertain可核对。切换账号/用途后迟到读写响应不填入新会话；原待核对按actor identity隔离。
- 刷新失败保留已成功预占事实，标旧草稿失效并阻止保存，不假造新fingerprint、不再写一遍。普通明细不必删单重录。

## 验证

Node22通过：原组件10（新增账号/用途迟到及父禁用后重挂载恢复），工作流12（真实ID与独立底别名、原单据组件、逐BOM刷新、父量与备库/备注、0张处理、过期草稿拒绝、订单晚响应、内部明细、父保存门禁、全部inline JS语法、订单/已报料/三个报料fieldset Vue编译、cache及零子件payload）。

Python35通过：新UI/API 2 + 内部报料13 + 原手工20。新2是实际GET已有“已报料”订单和原内部单据→实际JS组件产生请求→真实API采用60/100→内部40/0→同key回执重放→重新读取余额，真实片料仍204-adopt可用/adopt预占/0消耗，不增加成品。

最终git diff --check通过。缓存：manual-sheet-deduction 12c4e210d462414c2d4669ad52fd242ef83d2de3b4438dc69c0d48d2c4d7b672；dispatch已批准helper ca08d87efb56d0cb009ea68fe147e0add030d69a211e7b8aeb34e35977a79536；按根指令引用product-workbench 902a32bc5f41，已直接核对根工作区实际文件902a32bc5f41a99ac738743318ccbf0e66664a9efd2ea7c24a41bc19576713db，本分支未复制搜索整包。

## 根需串行解决的后端相邻限制

app/api/requisition.py preview_bom_sheet_cutting第12987仍以任何active RequisitionItem禁止preview；现有内部BOM采用后从采购草稿刷新可能409。已有订单/内部报料入口只读订单/原单据，已验证可采用，不使用该preview。根须在实际未采购用途边界下允许原冻结开料不变的只读重算，并核验_bom_snapshot_requirements对内部行是否误计已采购的carried_material。UI对此不会放宽或自行伪造权威fingerprint，而是明确旧草稿失效、阻止保存。此限制已发根，不作为“全部BOM内部采购草稿已完整闭环”的通过证明。

正式仍未发布，未点击正式页面、未控制服务、未备份、未认证、未Git push、未正式数据写入；未新增更正命令、未重复生产50只。未运行真实屏幕/打印/手机现场，根最终整合及管理员验收与本开发验证分开记载。此UI阶段按根指令不等待NAS。
