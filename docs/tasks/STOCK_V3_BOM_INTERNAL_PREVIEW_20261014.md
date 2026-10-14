# 第三版内部 BOM 只读开料预览回执（Sol）

负责人 GPT-6.1 Sol，工作区 stock-v3-20261014。根在 UI f8d50f2d 完成后授权独占 app/api/requisition.py，修复原任何有效报料均阻止只读 preview 的门禁。此提交只包含该 API、专用合成测试和本回执；没有修改 warehouse.py、static/index.html 或送货方法。

## 行为与边界

`POST /api/requisition/bom-sheet-cutting/preview` 对有效内部报料使用根的 `_manual_sheet_procurement_state` 判定用途阶段。原内部明细未采购、未收料且未冻结用途，所有有效行均在可修订集合时，允许使用原冻结开料只读重算。冻结订单 BOM、原内部明细的开料方式/供应商尺寸/开料合同和来源实出数共同核验；输入可以省略原合同，但不能换份数、模数、实出数、理论尺寸或供应商修边尺寸。旧内部明细尺寸与冻结 BOM 不一致、多行原合同不一致、历史 whole 与盖/底来源不明确时仍拒绝，避免显示被静默替换的合同。

供应商采购、内部或供应商冻结用途、收料/收货事实继续 409。根新累计用途校验的 WarehouseInventoryError 在本预览调用边界转换为有明确信息的 HTTP 响应，冲突用途不产生 500。权限与客户校验沿用原入口。

预览不写明细、版本、用途或库存。内部需求本身不计作已采购 carried material；schema5 多级 BOM 同样核验此点。手工采用后返回重新计算的剩余片数、张数、产品版本与用途指纹，并附内部明细 id/version/requisition_qty，重复读取结果稳定。供应商修边 v3 合同保留原 1000×720，理论 1000×700 不覆盖供应商尺寸。

例：已有内部 BOM 需求 40 片，采用 7 张现有一出一片料后，原内部需求修订为 33；重新 preview 显示剩余 33、carried=0、新用途指纹。冻结一出二的内部合同 40 片显示 20 张。只读例外仅在此 preview 内，`create_batch`、全局 `_bom_snapshot_has_active_requisition` 与重复正式报料门禁未改；测试再次提交同组件正式报料仍 409。

## 验证

先复现两个原行为失败：已有内部行原开料预览返回 409、采用后的原开料刷新返回 409。随后真实 API 覆盖原合同和 legacy 模式、库存采用与新指纹、重复正式报料拒绝、开料/尺寸/分切/模数/修边改变拒绝、采购/冻结/收料拒绝、v3 修边保留、schema5 冻结图库存刷新不误计采购。追加用途合计 41 大于供应商原明细 40 的真实数据链，先复现未捕获异常，后验证明确 409。

最终 55 通过（41.83 秒）：新内部预览 16 + BOM 开料流程 2 + 供应商开料流程 7 + 原手工片料 20 + 共享用途/组件 8 + UI/API 实际请求链 2。

```text
/Users/kimiyf/ERP-Migration/venv/bin/python -m pytest -q \
  tests/test_stock_v3_bom_internal_preview_20261014.py \
  tests/test_sheet_cutting_bom_flow.py tests/test_sheet_cutting_supplier_flow.py \
  tests/test_manual_sheet_deduction_20261013.py \
  tests/test_stock_v3_shared_purpose_20261014.py \
  tests/test_stock_v3_manual_sheet_ui_api_20261014.py \
  --basetemp=/tmp/stock-v3-bom-preview-delivery-pytest
```

所有 DB 均为上述独立 basetemp 内测试 fixture 新建的合成 SQLite；没有读取正式副本。`git diff --check` 通过。已接根 ff6edfd4（本工作区 967eaa3b）与 a50cd5c4（本工作区 7d3ccb7c）作为依赖，根合并本提交时不重复 cherry-pick 这些依赖或 dispatch/UI 提交。

补记已完成 UI 相邻验证：`test_delivery_dispatch_map_protocol.py` 中真实 HTTP→JS→HTTP 映射和原 v625 fallback 选定 9 个测试通过；PDF axios stub 基线失败仍按根要求不扩修。此前 UI 回执指出的内部 BOM 原开料刷新阻断由本提交解决，错误草稿阻止保存的 UI 防护继续保留。

本阶段未正式操作、未发布、未 push、未新增应急更正写库入口，未重复生产 50 只。000204 指定 YL0204 的 100 张历史使用、000120 上下盖各 50 套登记库存的实际更正仍由根按已确认事实串行准备；本提交仅完成可重复核验的开发代码。
