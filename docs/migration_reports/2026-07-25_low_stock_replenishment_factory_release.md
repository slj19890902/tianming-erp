# 2026-07-25 低库存智能报料与送货备注隔离工厂发布记录

## 发布范围

- 工厂分支：`factory-current-baseline`
- 发布前代码：`1b318efed68c77fa39ff50935e79d09dd0ba8961`
- 发布后代码：`12d7e94a1a2d9779816658baa1a9d515fce9403c`
- 更新方式：`git merge --ff-only`
- 数据库版本：`cn70v8x9z59 → cn70v8x9z59`
- 老板授权：2026-07-25 明确确认已验收并允许同步

本次包含送货客户备注与天华内部说明隔离、首页常用箱低库存按客户分组、
建议报料可编辑草稿、资料缺失拦截、兼容款人工加入和客户专用纸板备料边界。
没有新增 Alembic migration。

## 现场改动保护

发布前工厂目录存在 `AGENTS.md`、`docs/CODEX_HANDOFF.md` 和两份历史发布记录
的未提交改动。已保存为：

`stash@{0}: factory-pre-12d7e94-local-docs-20260725`

发布后已恢复这些文件并人工合并交接文档冲突。stash 保留，未删除。

## 备份与演练

- 运行时报告：
  `docs/migration_reports/release_runtime_20260725_130047.json`
- 正式库发布前 SHA-256：
  `e03c57b6ce96c02d8b00549e33adf449371250c87e7ed03f18fcbed949a08d71`
- 验证备份：
  `data/backups/carton_erp_before_release_20260725_130048.sqlite3`
- 备份 SHA-256：
  `0c725922a1469778af246acacc9a80d747544b307892a93d1f41c70e31a1d51b`
- 演练副本：
  `data/release_rehearsals/carton_erp_release_rehearsal_20260725_130048.sqlite3`

正式库、备份和演练副本均为 `cn70v8x9z59`，`integrity_check=ok`，
外键异常 0，必需表齐全，14 个核心表计数逐项一致。由于没有新增 migration，
隔离演练为 `cn70v8x9z59 → cn70v8x9z59`。

## Apply 与重启结果

- 发布报告状态：`completed`
- Apply 后数据库 SHA-256 与发布前一致
- Apply 后 revision：`cn70v8x9z59`
- Apply 后 `integrity_check=ok`
- Apply 后外键异常：0
- Apply 后核心表计数与发布前一致
- ERP 监听：`0.0.0.0:8000`
- `GET /api/health`：HTTP 200，`{"ok":true}`

## 验证结果

- `git rev-parse HEAD` 精确等于目标提交
  `12d7e94a1a2d9779816658baa1a9d515fce9403c`
- `alembic current` 与 `alembic heads` 均为 `cn70v8x9z59`
- `python -m compileall -q app`：通过
- 未登录访问首页概览、库存补货产品和送货列表接口：均返回 HTTP 401
- 工厂虚拟环境与当前系统 Python 均未安装 pytest，因此未在正式目录补装依赖；
  家庭端候选任务回执记录 110 项自动测试通过

## 数据写入

没有新增或修改正式业务记录，没有批量回写历史表。没有执行数据库结构迁移；
Apply 后正式库文件 SHA-256 和核心表计数均未变化。发布动作只更新代码并重启 ERP。

## 待现场回归

1. 登录首页确认低库存预警按客户分组，展开款号信息正确。
2. 确认建议报料只生成可编辑草稿；资料缺失时明确拦截。
3. 确认兼容款必须人工加入，多余未加工纸板只进入客户专用纸板备料。
4. 确认送货客户备注只显示和打印客户可见内容，天华内部来源不打印。
5. 后续单独回归“纸板备料预占 → 生产消耗 → 具体款号完工转成品”完整链路。

