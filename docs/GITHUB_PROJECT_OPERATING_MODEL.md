# 天明 ERP GitHub Project 与 Codex 分工模型

## 1. 当前约束

- 唯一候选基线：`origin/factory-current-baseline@ff9b4ebdc12be0e6d9d8995e74b3ac47fcaf56a3`。
- 旧 `main@9be70622683535f476a353860cf1f2cb32b399b0` 暂不替换；替换前必须归档旧引用、通过 CI、回归、人工 UAT 和受审计 PR 流程。
- 正式数据库只有 `D:\纸箱厂erp软件搭建\data\carton_erp.sqlite3`。任何 `erp.db`、旧 worktree、备份或 UAT 副本都不得被描述为正式库。
- 当前稳定期继续使用 SQLite；PostgreSQL 只在明确触发条件满足后进入评估。
- 首轮安全工作只允许只读审计和威胁建模。正式数据库、部署、网络、`main` 和迁移均不在默认授权范围内。

## 2. GitHub 层级

```text
Tianming ERP 总 Project
└── Program Epic：总体规划、安全与稳定化
    ├── 模块 Epic：稳定业务域和长期负责人
    │   └── Work Item：一个最小可验证闭环
    └── 安全 Epic：一个攻击面或治理域
        ├── Read-only Audit：证据与威胁模型
        └── Remediation：经 PM 门禁批准后的独立修复
```

每个 Work Item 只绑定一个父 Epic、一个主要负责 Codex 任务和一个验收结果。跨模块变更必须列出依赖 Epic，并由 PM/架构任务协调；不得让多个负责人同时在同一巨型 Issue 内追加无边界需求。

## 3. Project 字段和视图

建议 Project 名称：`Tianming ERP — Stabilization & Modular Ownership`。

字段：

| 字段 | 取值 |
|---|---|
| Status | Inbox / Discovery / Ready / In progress / Review / UAT / Blocked / Done |
| Priority | P0 / P1 / P2 / P3 |
| Area | 与 `area:*` 标签一致 |
| Work type | Program / Epic / Audit / Remediation / Work item / Bug |
| Risk gate | None / Human approval / Backup required / Migration / Network / Production data |
| Target | Stabilization / Later / PostgreSQL gate |
| Codex Task | 专属任务标题和任务 ID |

视图：

1. `PM 总览`：按 Status 分组，显示全部 Epic 和 P0/P1。
2. `每日核心链路`：订单、报料、来料、送货、回单。
3. `月底财务链路`：对账、开票、收款。
4. `安全与稳定`：安全、数据库、QE/发布、前端平台。
5. `待人工门禁`：所有 `gate:*` 或 `risk:*` 项。
6. `PostgreSQL 评估`：只显示门槛与证据，不提前创建迁移实施任务。

GitHub Project API 当前没有创建或修改视图的 mutation。上述视图需要在 GitHub Web UI 手工建立；字段、选项和 28 个项目项已经配置完成，可直接按这里的筛选条件保存视图。

## 4. 长期模块与专属 Codex 任务

| 模块 | 主要职责 | 不拥有的职责 |
|---|---|---|
| PM / 架构与发布集成 | 总路线图、接口合同、跨模块决策、发布准入 | 不长期承包具体页面 |
| 工作流状态与审计 | 合法状态迁移、业务事实投影、回退和审计 | 不直接实现各模块 UI |
| 客户、产品与报价 | 主数据、版本、客户范围、报价转订单契约 | 不拥有订单履约状态 |
| 订单与 PDF 录入 | 订单聚合、编号、快照、录入、详情和订单打印 | 不直接写财务完成状态 |
| 报料与来料 | 报料、供应商单、到料、短收/超收和异常 | 不做复杂采购付款 |
| 生产与 BOM | 轻生产、完工、回退、BOM 消耗 | 不做复杂排程 |
| 仓库与库位 | 轻库存、预占、批次、盘点、模具和库位 | 不扩展为多仓大型 WMS |
| 送货与回单 | 送货草稿、拣货、部分送货、签收差异和回单 | 不决定开票或收款 |
| 对账、开票与收款 | 以回单确认数量对账、发票和收款事实 | 不做总账或会计科目 |
| 前端平台 | 本地依赖、路由、API 客户端、表格、弹窗、打印导出 | 不定义业务状态机 |
| 安全与权限 | 登录、会话、RBAC、客户数据范围、上传、网络边界 | 不代替业务负责人验收规则 |
| QE / 发布可靠性 | CI、测试分层、迁移演练、备份恢复、日志告警 | 不代替模块负责人写领域测试 |
| 瑞达历史数据迁移 | `BoxDB20_REPRO`、`legacy_ruida_*`、dry-run、100 条试迁移与隔离验收 | 不把 `erp.db` 当历史源，不绕过迁移门禁 |

路由规则示例：修改订单录入先进入“订单与 PDF 录入”任务；若触及状态跳转，同时关联“工作流状态与审计”；若增加迁移，再关联“QE / 发布可靠性”并加备份和人工门禁。PM 只协调依赖和验收顺序，不把实现重新收回总任务。

已创建的专属 Codex 任务：

| 专属任务 | Epic | Codex 任务 ID |
|---|---:|---|
| PM 总控与范围门禁 | #3 | `019f87c3-8d6d-7891-a3bc-6fe129e595b7` |
| 跨模块工作流与审计 | #4 | `019f87c3-a57e-7150-ab34-741fb333b6b2` |
| 主数据、产品与报价 | #5 | `019f87e4-476a-7993-b5b3-368ddb15a89e` |
| 订单与 PDF 录入 | #6 | `019f87c3-be12-78d2-acb6-142d3014444f` |
| 报料与来料 | #7 | `019f87c3-d6d4-7e81-8742-8b718f8184b4` |
| 生产与 BOM | #8 | `019f87e4-5fd5-7e43-b3c2-a6ae10f6bd56` |
| 仓库与轻库存 | #9 | `019f87e4-884a-7a03-8915-4eb70aa7d6cf` |
| 送货与回单 | #10 | `019f87c3-e2f1-7c31-9910-44a98bda112e` |
| 对账、开票与收款 | #11 | `019f87e4-9b90-7992-b7df-98f404b08aab` |
| 前端平台与文员体验 | #12 | `019f87e4-b409-7250-81de-8c305da407fa` |
| 安全、登录与 RBAC | #13 | `019f87c3-f94f-7c02-811c-d6680ff8d084` |
| QE、发布与恢复 | #14 | `019f87c4-1174-7cf1-8820-a7b02d07c941` |
| 瑞达历史数据迁移 | #15 | `019f87e4-c4cf-7010-b831-1081d411443c` |

PM 任务已固定在 Codex 侧边栏。每个 Epic 的 `Codex Task` Project 字段也保存了对应标题与任务 ID；后续需求先按 Epic 路由到专属任务，跨域时再由 PM 增加依赖，不在 Program #3 内直接堆实现细节。

## 5. 首批安全 Epic 顺序

1. 正式基线与发布供应链。
2. 局域网 HTTP、HTTPS 与 Windows 主机边界。
3. 登录、会话撤销与密码安全。
4. RBAC、客户范围与敏感数据隔离。
5. SQLite 备份恢复、锁、迁移和完整性。
6. 日志、审计与安全告警。
7. 订单、报料、来料、送货、回单五个每日核心模块安全回归。
8. PostgreSQL 评估门禁。

首轮对应 Codex 任务只做只读审计。每项发现必须包含文件/行号或配置证据、可利用条件、业务影响、最小修复范围、回归场景和需要人工批准的动作。修复不得直接在审计任务里顺手实施。

## 6. Ready 与 Done

工作项进入 `Ready` 前必须具备：父 Epic、负责人、业务价值、明确边界、验收场景、数据库影响、权限影响、依赖和回滚思路。

工作项进入 `Done` 前必须具备：

- 最小范围实现和代码审阅；
- 相关自动测试，且测试使用明确的临时/UAT 数据库；
- 权限拒绝、非法状态、重复请求、失败恢复和审计验证；
- 如有迁移：备份、SHA-256、完整性、隔离迁移演练、回退和人工批准；
- 如影响每日核心链路：订单到回单的人工 UAT；
- `docs/CODEX_HANDOFF.md` 已更新；
- 未把其他 SQLite 或历史来源误当正式数据库。

## 7. PostgreSQL 进入条件

只有出现真实 SQLite 写锁/性能问题、多应用服务器或多地点访问、高可用/集中审计需求、SQLite 无法满足 RPO/RTO，或完成完整稳定期和迁移演练时，才允许把 PostgreSQL Epic 从门禁项转为实施评估。评估与当前安全整改不得并行改造生产数据层。

## 8. 2026-07-22 启动状态

- Program Epic：[#3](https://github.com/slj19890902/tianming-erp/issues/3)。
- 模块 Epic：[#4](https://github.com/slj19890902/tianming-erp/issues/4) 至 [#15](https://github.com/slj19890902/tianming-erp/issues/15)。
- 首批只读安全 Epic：[#16](https://github.com/slj19890902/tianming-erp/issues/16) 至 [#23](https://github.com/slj19890902/tianming-erp/issues/23)。
- 已登记但禁止自动实施的整改项：[#24](https://github.com/slj19890902/tianming-erp/issues/24) 至 [#30](https://github.com/slj19890902/tianming-erp/issues/30)。
- 已同步 38 个自定义标签。Issue Forms、PR 模板和 CODEOWNERS 位于本治理分支，合并前不会在仓库默认分支生效。
- 总 Project 已创建并链接仓库：[`Tianming ERP — Stabilization & Modular Ownership`](https://github.com/users/slj19890902/projects/1)。Token 已验证具备 `project`、`repo`、`read:org`、`read:discussion` scope。
- #3-#30 共 28 个 Issue 已全部进入 Project；#4-#23 是 Program #3 的子 Issue，#24-#30 已分别挂到主责安全/工作流 Epic。
- Project 已配置 Status、Priority、Area、Work Type、Risk Gate、Target 和 Codex Task 字段；Program 为 In progress，只读安全审计为 Discovery，未经授权的整改项为 Blocked。
- 13 个职能专属 Codex 任务已建立并回填到 Project；首轮只允许形成职责章程和只读审计，不授权直接改代码或数据。
- 自定义视图受 GitHub Project API 能力限制，需要按第 3 节在 Web UI 手工保存；这不影响字段、Issue 层级和任务路由使用。
- 治理变更位于 Draft PR [#31](https://github.com/slj19890902/tianming-erp/pull/31)，目标为 `factory-current-baseline`，尚未合并、部署或替换 `main`。
