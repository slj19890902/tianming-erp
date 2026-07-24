# 天明 ERP 当前唯一任务

更新时间：2026-07-24
当前任务：`REQ-20260723-001` 来料超收、生产余货与超量送货闭环
当前状态：工厂待发布，尚未授权工厂执行

## 当前目标

只处理发布槽位 1 的受控工厂交付，不开始新的业务开发，不把槽位 2、槽位 3 或新需求叠加进本次发布。

## 当前允许做什么

- 家庭电脑可以只读核对候选分支、完整 SHA、迁移 revision、历史测试和 UAT 证据。
- 工厂 Codex 可以先做只读现场预检。
- 只有用户在工厂任务中明确授权后，工厂 Codex才能执行备份、停服、更新、迁移和重启。

## 当前禁止做什么

- 不在家庭电脑连接、迁移、写入或替换工厂正式数据库。
- 不把 `99b223a6` 或 `f36112e8` 与槽位 1 一次发布。
- 不因新想法修改当前候选；新想法只登记到需求收件箱。
- 不修改 `origin/main`，不强推，不覆盖工厂正式目录。

## 当前候选

- 需求：`REQ-20260723-001`
- 分支：`origin/codex/p0-production-surplus-rebase-20260723`
- 完整 SHA：`f11d616f419b0bbf9ea47bfa68b41cfd9aedb53e`
- 迁移：`ch64v8x9z53 → ci65v8x9z54`
- 验收单：[REQ-20260723-001](acceptance/REQ-20260723-001.md)
- 发布单：[FACTORY-20260724-PENDING](releases/FACTORY-20260724-PENDING.md)

## 已完成的治理闭环

- `REQ-20260724-001` 已于 2026-07-24 获得用户家庭人工验收通过。
- 用户已授权提交并推送 `codex/local-governance-control-20260724`。
- 治理分支只包含 `AGENTS.md` 和 `docs/*`，不包含 ERP 业务代码或数据库变更。
- 治理分支的精确远端 tip 使用以下只读命令确认：

  `git rev-parse origin/codex/local-governance-control-20260724`

## 工厂事实边界

- 正式发布记录基线：`aebf9b23d3200840395baf29202491bfbfa97414`
- 发布记录中的工厂运行功能代码：`a3001568c56b941a3c0c0935f2fa51ddc6c44f85`
- 发布记录中的工厂正式 revision：`ch64v8x9z53`
- 当前家庭任务没有连接工厂主机；发布前必须由工厂 Codex 现场重新只读核对以上事实。

## 中断恢复点

如果现在闪退：

1. 进入治理工作树 `D:\tm-worktrees\erp-local-governance-20260724`。
2. 运行 `git status --short --branch`。
3. 阅读本文件。
4. 阅读 `docs/acceptance/REQ-20260723-001.md`。
5. 阅读 `docs/releases/FACTORY-20260724-PENDING.md`。
6. 未得到工厂发布授权时，只能停在现场预检准备，不得执行发布。

## 新想法处理

用户在家里电脑的任意本项目 Codex 任务中发送：

`登记到需求收件箱：你的想法`

Codex 只为它分配 `REQ-YYYYMMDD-序号`、整理范围和待澄清项并更新 `REQUIREMENT_INBOX.md`，不得修改当前候选。

## 下一步

等待用户决定何时让工厂 Codex 对发布槽位 1 做现场只读预检；现场预检通过后仍需再次明确授权，才能正式发布。
