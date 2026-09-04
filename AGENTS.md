# 天明 ERP 文档与任务执行规则（精简版）

## 0. 核心边界

- 不在未备份和无复核前写正式数据。
- 不直接覆盖生产库、主沙盘、原始 BAK。
- `erp.db` 不是历史订单正式来源。
- `BoxDB20_REPRO`、`legacy_ruida_*` 只作隔离核对源，先只读。
- 每次任务只做一个最小可验证闭环。
- 优先文档梳理，除非有明确业务修复目标再修改代码。

## 1. 任务前检查（固定最少）

1. `docs/CODEX_START.md`
2. NAS `AI_START.md`（当前会话已加载）
3. 当前任务卡
4. 相关主模块上下文（`docs/context/*`）
5. 按需执行：`git branch --show-current` / `git status --short` / `git log --oneline -10`

## 2. 迁移与数据库纪律

- 涉及迁移时同时读取：
  - `docs/MIGRATION_PLAN.md`
  - `docs/MIGRATION_RUNBOOK.md`
  - `docs/MIGRATION_CHECKLIST.md`
- 第一步只能做 SQL Server -> `legacy_ruida_*` 的 dry-run 差异统计。
- 未经授权不得刷新 `legacy_ruida_*`。
- 未经 100 条验收不做正式历史批量写入。
- 长日志与大规模差异清单写入 `docs/migration_reports/`。

## 3. 文档层级（推荐默认读取顺序）

- `AGENTS.md`
- `docs/index.md`
- 当前模块文档（`docs/modules/*`）
- 与问题直接相关代码

不默认读取：
- 全量 `docs/CODEX_HANDOFF.md`
- 全量长期进度/旧 BUG 归档
- 与任务无关的历史模块文档
- `CURRENT.md`（仅当用户要求当前状态或任务涉及专项时再读）

## 4. FAST_FIX

适用：按钮失效、显示错误、单字段/单接口/单页面问题、CSS、单页面交互。

默认行为：
- 只读 `AGENTS.md` + `docs/index.md` + 当前模块文档 + 相关最小代码。
- 第一轮默认最多打开 8 个左右相关源码文件。
- 禁止默认读取：
  - 全量总需求
  - 全部历史回执与迁移档案
  - 非命中的模块文档
 - 无关模块文档

验证：
- 目标用例
- 1~2 个相关冒烟点

禁止默认：
- subagent
- 多 worktree
- 全仓测试
- 顺手代码重构

FAST_FIX 默认流程：
- 定向调查 → 确定根因 → 最小修改 → 定向验证

## 5. 任务等级

### L1 FAST_FIX
- 最小上下文、最小修改、定向验证

### L2 MODULE_CHANGE
- 单模块内功能修改
- 仅加载当前模块上下文与关键代码

### L3 CROSS_MODULE
- 两个以上业务链路联动
- 允许扩展到两个模块文档，需给出跨模块证据

### L4 HIGH_RISK
- schema、迁移、批量历史改写、财务金额、对账开票收款、权限、作废历史
- 默认要求：备份、影响分析、迁移演练、回滚方案、定向回归

## 6. 文档治理动作

- 维护：
  - `docs/index.md`
  - `docs/CURRENT.md`
  - `docs/modules/*`
  - `docs/archive/*`
- 同一职责只保留一个主文档（禁止并行版本文件）
- 历史内容默认归档，不删除；确认后再移入 `docs/archive/`

## 7. 交付默认格式

- 先说明：
  - 改了哪些文件
  - 验证了什么
  - 是否写入数据（否）
  - 知识库回写位置
  - 下一步建议
