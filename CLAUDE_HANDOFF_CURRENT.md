# ERP 系统交接文档 — v0.18.0

生成时间：2026-06-24  
负责人：Claude Sonnet 4.6

---

## 1. Git 状态

| 项目 | 值 |
|---|---|
| 分支 | `factory-current-baseline` |
| 远端 | `origin https://github.com/slj19890902/tianming-erp.git` |
| 推送状态 | 已推送（`origin/factory-current-baseline` 与本地同步） |

### 最新提交

| Hash | 说明 |
|---|---|
| `3dee265` | fix: v0.18.0 补丁 — OperationLog 字段修正 + 测试截断修复 |
| `157f3b3` | feat: v0.18.0 — PDF 订单识别训练样本库基础设施 |
| `3439e5c` | fix: v0.17.1 — 楞型一致性修复 + 歧义检测 + 后端校验 + 前端防呆 |
| `92d287f` | feat: Phase 17 — v0.17.0 订单状态精简与楞型识别版 |

工作区状态：干净（仅有一个与项目无关的临时文件 `C：Temphome_ui_apple_diff.txt` 未追踪，无需处理）

---

## 2. 数据库状态

| 项目 | 值 |
|---|---|
| 路径 | `D:\纸箱厂erp软件搭建\data\carton_erp.sqlite3` |
| Alembic 版本 | `k71e2b3c6f58`（Phase 18，当前 head） |
| PRAGMA integrity_check | `ok` |
| 迁移链 | `j60d1a9b5e47`（Phase 17）→ `k71e2b3c6f58`（Phase 18） |

---

## 3. v0.17.1 修复内容

### 问题根因
原代码误将 `/A` 后缀设为 `layer_count=5`。物理规则：
- 单楞（A / B / E）= 三层纸板（3层）
- 双楞（AB / BE）= 五层纸板（5层）

### 代码修复

| 文件 | 修改内容 |
|---|---|
| `app/services/flute_mapping.py` | 所有单楞后缀返回 `layer_count=3`；新增歧义正则 `_AMBIGUOUS_RE`；新增 `validate_flute_consistency()` 和 `apply_flute_consistency_fix()` |
| `app/api/products.py` | `ProductPayload` 新增 `model_validator`，非法组合返回 422 |
| `app/api/system.py` | 新增 `POST /api/system/flute-mapping/fix-consistency` |
| `static/index.html` | 楞型下拉动态过滤（3层仅 A/B/E，5层仅 AB/BE）；层数改 select；实时错误提示 |
| `tests/test_phase17_flute_mapping.py` | 全面改写，44 个用例 |

### 生产库修复结果

| 指标 | 数量 |
|---|---|
| 修复前非法组合（5层+单楞） | 67 条 |
| 修复前非法组合（3层+双楞） | 0 条 |
| 修复后非法组合 | **0 条** |
| 当前楞型分布 | AB/5=1389，B/3=205，A/3=170，E/3=45 |

---

## 4. v0.18.0 新增内容：PDF 识别训练样本库

### 4.1 数据库新增 4 张表（Alembic `k71e2b3c6f58`）

| 表名 | 用途 |
|---|---|
| `pdf_order_training_batches` | 批次管理，每次上传一组 PDF 为一批 |
| `pdf_order_training_samples` | 单 PDF 样本：文件信息、解析结果 JSON、人工标注 JSON、评分、状态 |
| `pdf_order_customer_templates` | 客户级正则模板（为定制解析规则预留） |
| `pdf_order_correction_logs` | 字段级纠错历史（统计高频出错字段） |

#### pdf_order_training_samples 关键字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `file_sha256` | String(64) | 防重复上传 |
| `parser_result_json` | Text | 解析器输出（JSON）|
| `ground_truth_json` | Text | 人工标注正确值（JSON）|
| `score` | Float | 评分 0.0–1.0 |
| `parse_status` | String | `pending` / `labeled` / `reviewed` |
| `parse_method` | String | `text` / `ocr` / `failed` / `unknown` |
| `extracted_text` | Text | PDF 提取原文（可选存储）|

#### ground_truth_json / parser_result_json 标准格式

```json
{
  "order_no": "THPO-2024-001",
  "customer_name": "某某公司",
  "order_date": "2024-01-15",
  "delivery_date": "2024-01-30",
  "items": [
    {
      "line_no": 1,
      "product_code": "P001",
      "product_name": "产品甲",
      "spec": "500*300*200",
      "quantity": 100,
      "unit": "件",
      "unit_price": "12.50",
      "amount": "1250.00",
      "delivery_date": "2024-01-30"
    }
  ]
}
```

### 4.2 评分服务 `app/services/pdf_scoring.py`

字段权重：

| 字段 | 权重 |
|---|---|
| `order_no` | 20% |
| `customer_name` | 10% |
| `order_date` | 10% |
| `delivery_date` | 5% |
| `items`（整体）| 55% |

行级字段权重：`product_code` 25% / `quantity` 20% / `unit_price` 15% / `product_name` 15% / `spec` 15% / `amount` 10%

比对规则：去空白、大小写归一化、数值误差容忍 ±0.01

### 4.3 REST API（前缀 `/api/pdf-training/`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/batches` | 批次列表 |
| POST | `/batches` | 新建批次（需管理员）|
| GET | `/samples` | 样本列表（分页 + 过滤）|
| POST | `/samples/upload` | 上传 PDF（运行解析器）|
| GET | `/samples/{id}` | 样本详情 |
| PUT | `/samples/{id}/ground-truth` | 写入人工标注（自动评分）|
| POST | `/samples/{id}/score` | 重新评分 |
| DELETE | `/samples/{id}` | 删除样本（需管理员）|
| POST | `/samples/{id}/corrections` | 新增字段纠错记录 |
| GET | `/samples/{id}/corrections` | 查看纠错记录 |
| GET | `/templates` | 客户模板列表 |
| POST | `/templates` | 创建模板（需管理员）|
| PUT | `/templates/{id}` | 更新模板 |
| DELETE | `/templates/{id}` | 删除模板 |
| GET | `/stats` | 全局统计 |

### 4.4 前端（`static/index.html`）

系统设置页面新增「PDF 订单识别训练库」面板，包含：
- 统计看板：总样本数、已标注数、平均分、高/中/低分布、高频出错字段 top-5
- 样本列表（分页，含解析方式/状态/评分/上传时间）
- 上传弹窗：选文件 + 选批次 + 是否落盘 + 备注
- 详情/标注弹窗：查看解析结果、编辑 ground truth JSON、一键保存并评分
- 管理员可删除样本

手机端不显示此功能（系统设置仅 PC 可访问）。

---

## 5. 数据库备份清单

| 文件名 | 大小 | SHA-256 前16位 | 用途 |
|---|---|---|---|
| `carton_erp_20260624_111735_..._AFTER_PHASE18_PDF_TRAINING_MIGRATION.sqlite3` | 209.28 MB | `ecf0cca594a658a7` | Phase 18 迁移后恢复点（当前推荐）|
| `carton_erp_BEFORE_FLUTE_CONSISTENCY_FIX_20260624_105027.sqlite3` | 209.28 MB | `29320f1d51dcf8bf` | v0.17.1 楞型修复前备份 |
| `carton_erp_BEFORE_MATERIAL_MAPPING_AND_CUSTOMER_CODE_20260623_153310.sqlite3` | 201.59 MB | `7b61375879311d1e` | Phase 16 前备份 |

**注意**：v0.17.1 修复前已备份，v0.18.0 迁移完成后补做了 `AFTER_PHASE18` 备份。

---

## 6. PDF 样本文件安全

| 项目 | 状态 |
|---|---|
| PDF 样本存储目录 | `data/pdf_training_samples/`（本地，不进入 Git）|
| `.gitignore` 排除 | `data/` 已整体排除，`*.pdf` 也已排除 |
| 云端上传 | 无，系统不调用任何外部服务 |
| OCR | 系统当前无 OCR 能力（仅 pypdf 文本提取）|
| 客户 PDF 写入代码文件 | 不会 |

---

## 7. 敏感文件检查

通过 Python 过滤 `git ls-files` 输出，确认以下内容均未进入 Git：
- 正式数据库（`.sqlite3`）
- 备份文件（`backups/`）
- PDF 样本（`*.pdf`）
- Excel/CSV（`*.xlsx`, `*.xls`）
- 环境变量（`.env`）
- 密钥（`.key`）
- 日志（`logs/`）
- 真实业务资料（`customer_file_summary`, `pdf_training_samples`）

结果：**CLEAN — 无敏感文件进入 Git**

---

## 8. 版本号确认

```python
APP_VERSION = "v0.18.0"
APP_VERSION_NAME = "楞型一致性修复 + PDF 识别训练库版"
APP_BUILD_DATE = "2026-06-24"
```

更新日志包含：
- 修复三层/五层楞型一致性校验
- 新增歧义楞型检测
- 新增 PDF 识别训练样本库（4 张表）
- 支持人工标准答案标注
- 支持识别结果字段级评分
- 支持客户模板规则雏形
- 支持人工纠错沉淀
- PDF 样本文件本地保存，不进入 Git

---

## 9. 测试结果

| 命令 | 结果 |
|---|---|
| `pytest tests/test_phase17_flute_mapping.py -q` | **44 passed** |
| `pytest tests/test_phase18_pdf_training.py -q` | **30 passed** |
| `pytest tests/ -q`（全套） | **446 passed, 1 warning** |

总计 **446/446**，零失败。

---

## 10. 手工验收步骤

### 10.1 楞型防呆验证
1. 进入「常用箱与材质」→ 编辑任意产品
2. 选择「层数」= 5层 → 楞型下拉只显示 AB / BE
3. 选择「层数」= 3层 → 楞型下拉只显示 A / B / E
4. 手动选择不匹配组合（如层数=3，楞型=AB）→ 保存按钮前应显示红色提示
5. 直接 POST `{"flute_type":"AB","layer_count":3}` 到产品编辑 API → 应返回 422

### 10.2 系统设置 PDF 训练库验证
1. 进入「系统备份（系统设置）」→ 下滑到「PDF 订单识别训练库」面板
2. 点击「刷新统计」→ 看板显示数据（初始全为 0）
3. 点击「上传 PDF 样本」→ 弹窗出现，可选文件和批次
4. （选一个测试 PDF）上传后刷新样本列表，看到新条目
5. 点击「详情/标注」→ 弹窗显示解析结果 JSON
6. 在 ground truth 框填入标准 JSON → 点「保存标注 + 自动评分」→ 评分显示在面板

### 10.3 备份还原验证
如需回滚到 Phase 18 前：
```
data/backups/carton_erp_BEFORE_FLUTE_CONSISTENCY_FIX_20260624_105027.sqlite3
```
如需回滚到当前（Phase 18 后）：
```
data/backups/carton_erp_20260624_111735_..._AFTER_PHASE18_PDF_TRAINING_MIGRATION.sqlite3
```

---

## 11. 下一步：第一批 PDF 样本导入策略

### 推荐流程（50 样本首批）

**第一步：建立批次**
在系统设置 → PDF 训练库 → 「上传 PDF 样本」前，先通过 API 建一个批次：
```
POST /api/pdf-training/batches
{"batch_name": "第一批-2026Q3", "description": "首批50个采购订单PDF"}
```

**第二步：逐个上传**
- 每个客户选 5-10 个有代表性的采购订单 PDF
- 上传时选择对应批次，`store_pdf=false`（初期不落盘节省空间）
- 系统自动运行解析器，结果存入 `parser_result_json`

**第三步：人工标注**
- 打开「详情/标注」弹窗，对照 PDF 原件填入正确的 ground truth JSON
- 重点字段：`order_no`、`items[].product_code`、`items[].quantity`、`items[].unit_price`
- 保存后自动评分；评分 ≥ 0.9 为高质量，< 0.7 需分析出错字段

**第四步：分析报告**
- 统计看板中「高频出错字段 top-5」会自动汇总
- 出错频繁的字段（如 `items[0].spec`）对应 `order_pdf_import.py` 中的正则需优化

**第五步：建立客户模板（Phase 19 工作）**
- 对于识别率 < 80% 的客户，通过 `/api/pdf-training/templates` 为其单独配置正则规则
- 目前模板表已建，下游解析器使用模板规则的功能为 Phase 19

### 文件安全提醒
- 实际客户 PDF 只能上传到正式生产服务器（本地 `data/pdf_training_samples/`）
- 不要将真实 PDF 放入 Git 仓库
- 不要将 PDF 发送到任何云端 AI 服务
- 标注时只填必要字段，不要在 ground truth JSON 里写客户敏感信息

---

*本文档由 Claude Sonnet 4.6 自动生成，仅供内部参考。不含任何真实客户明细、PDF 内容、订单金额或数据库内容。*
