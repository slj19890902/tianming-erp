# Business Rules

## Company Info

- Company info is an admin-maintained system setting.
- Only admins can view or edit it.
- Blank company names are invalid.

## Delivery Print

- Delivery print uses company sender information from the backend.
- If sender info is missing, the UI should fall back to the existing display values.

## Common Box Board Recommendation

- ERP 常用箱的长、宽、高、报料尺寸和压线尺寸统一使用整数毫米。
- A1/0201 普通开槽箱采购报料推荐：报料长 = `2 × (L + W) + 30`，报料宽 = `W + H + 5`。
- 其中 30mm 是整体舌头/搭口，5mm 是宽度方向经验放量；不得替换为报价面积公式。
- 人工选择“压线”后，推荐上摇盖、高、下摇盖为 `round(W/2)、H、round(W/2)`。
- 推荐值允许人工修改；人工修改后不得自动覆盖，只有点击“重新推荐”才强制刷新。
- 只有 A1/0201 使用上述公式。其他箱型没有确认公式时必须提示人工填写。
- 历史非标准箱型值必须保留，不做批量整理。

## Data Safety

- This release does not alter historical orders.
- This release does not modify `legacy_*` tables.
- This release does not run any migration against the formal database.

## Material Dictionary Flute Boundary

- 材质字典只保存供应商、层数、材质代码、纸张/克重结构和价格，不绑定楞型。
- 楞型只保存在常用箱、订单明细、报价明细和报料明细等具体使用场景。
- 材质重复判断使用“供应商 + 层数 + 清洗后的材质代码”，不得因 A/B/E 或 AB/BE 不同重复建立材质。
- 供应商报料单仍显示“材质代码 / 楞型”，其中楞型必须来自订单或常用箱明细，不能从材质字典回填。

## Product Read And Write Validation Boundary

- 新增或编辑常用箱时必须严格校验层数和楞型：三层只允许 A/B/E，五层只允许 AB/BE。
- 查询和显示历史常用箱时不得重新运行写入校验；历史缺失字段或旧组合必须先显示，供操作员检查和修正。
- 单条历史数据不符合当前规则时，不得导致同客户整个常用箱列表返回500或显示为空。
- 报价转常用箱只能新增当前报价明细对应的一条产品；同客户同存货编码必须拦截，不得覆盖、停用或改绑历史产品。

## Quotation To Requisition Snapshot Boundary

- 报价转常用箱必须先确认正式存货编码、材质、供应商、层数、楞型、最终单价和报料尺寸；转换本身不得自动生成订单。
- 常用箱保存具体产品使用的材质代码和楞型；订单选择常用箱时固化当时的材质、楞型、供应商、报料尺寸及压线快照。
- 后续修改常用箱不得自动覆盖已保存订单；只有用户明确重新选择或编辑订单明细时才能更新。
- 合并报料必须把楞型纳入分组条件。同材质代码但不同楞型的明细不得合并。
- 采购报料单数量始终是采购张数；不得替换为订单成品数量、送货数量或对账数量。
- 供应商最小切长、切宽只做醒目提醒。保存和打印不得自动修改采购尺寸或开料方式。

## Order Group Deletion

- 删除订单组必须二次确认，并由后端在一个事务中先校验整组、再统一删除，禁止逐张部分成功。
- 仅尚未进入报料、供应商采购单、送货、预送货等后续流程的订单组允许硬删除。
- 存在后续关联时返回明确中文业务提示；不得绕过外键或把数据库异常直接作为500显示给用户。
- 删除订单必须写操作日志，订单编号不得复用。
# 嘉林亿材质计价规则（2026-07-01）

- 2026-04-14报价表为规则基准价；2026-06-26统一涨价5%为当前调价，不得用旧基准价覆盖当前材质价格。
- 三层代码结构：面纸/瓦楞纸/里纸。第2位只使用B/E瓦纸换纸加价；A楞在具体使用场景增加0.04元/㎡。
- 五层代码结构：面纸/B楞瓦纸/芯纸/A楞瓦纸/里纸。第2、4位使用对应瓦纸规则，第3位才使用面芯纸规则。
- `C6C`规则基准价1.31，A楞使用基准1.35；`J616J`规则基准价2.86。应用当前5%调价后分别为1.38/1.42和3.00。
- X、R为“进口AAA级俄卡”；4、6、9、7为“国产A级施胶高瓦”。
- 最小切宽270mm、最小切长500mm只在待报料/合并报料提醒，不自动修改开料方式或正式报料单。
- 长度低于500mm分纸加价金额待确认，不参与自动报价；长度超过3000mm加价本轮不参与自动成本。
