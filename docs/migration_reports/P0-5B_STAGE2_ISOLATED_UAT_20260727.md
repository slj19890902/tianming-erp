# P0-5B 阶段2隔离 UAT 记录（候选）

执行日期：2026-07-27

状态：老板人工 UAT 已通过，已授权提交并推送独立候选分支
执行范围：仅家庭 UAT 沙箱；未连接、复制、迁移或写入工厂正式数据库与服务。

## 沙箱边界

- 代码副本：`D:\tm-uat\p0-5b-stage2-runtime-20260727-202348\formal`
- 隔离当前库：`...\data\carton_erp.sqlite3`
- 隔离候选库：`...\data\candidate.sqlite3`
- 当前模拟监听：`127.0.0.1:18161`
- 目标 loopback 预热：`127.0.0.1:18162`
- 未使用 8000、`0.0.0.0`、工厂目录或工厂 SQLite。

沙箱建立时排除了源目录中的 `data`、`.env`、日志、备份和 Git 工作目录；随后创建了新的空白隔离 SQLite，并仅在该库执行 Alembic upgrade。为兼容性夹具而建立的两条本地 Git 空提交只位于沙箱，未推送。

## 运行证据

1. 初次以 production 环境启动时，缺少显式 `ERP_ALLOWED_ORIGINS` 和 `ERP_TRUSTED_HOSTS` 被配置门禁阻断；这证明生产配置不会静默使用不安全默认值。
2. 补齐仅限本机的 production 配置后：
   - 当前模拟服务 `127.0.0.1:18161`：HTTP 200，`{"ok":true}`；PID 32940；单 worker；命令行绑定到本沙箱。
   - 目标预热服务 `127.0.0.1:18162`：HTTP 200，`{"ok":true}`；PID 40108；单 worker；命令行绑定到本沙箱。
   - 目标预热停止后，18161 仍返回精确健康响应，证明预热未中断当前模拟服务。
3. 两个监听均按端口、PID、Python 命令行、`--app-dir`、host 和 workers 核对后停止。结束检查确认 18161、18162 均无监听。

## 数据库与私有文件

两库最终均为：

| 库 | Alembic | integrity_check | foreign_key_check |
| --- | --- | --- | --- |
| 隔离当前库 | `cr74v8x9z63` | `ok` | 0 |
| 隔离候选库 | `cr74v8x9z63` | `ok` | 0 |

- 当前库与候选库最终 SHA-256 相同：
  `F1C5E669E1EF06F0A7C1A0117EEA2598329DA00A921AB812D4C6911502607030`。
- `data/private_uploads/uat-marker.txt` 的 SHA-256 在 18162 预热前后均为 `8E322E2CDFC75BBEE74C7BAB3CA9224841E0DAC659973787C542B1E34CB62FBD`。
- `data/session_secret.key` 的 SHA-256 在预热前后均为 `ED12E7866EA2CDADB27F79F1F8F26FB8D5F6895F188DA7313568A3F2D21F1163`。

## 自动化门禁结果

在沙箱中运行：

```text
python -m pytest tests/test_release_rollback_execution.py tests/test_release_rollback_compatibility.py -q
  -k "not real_previous_commit_starts_only_on_fresh_isolated_database_copy"
```

结果：`50 passed, 1 deselected`。

覆盖并通过：

- 签名计划、一次授权错误/过期/跨计划/重放阻断；
- 成功活动指针切换、loopback 验证、双签名正式开放意向/承诺；
- 开放前目标失败仅一次恢复当前运行；
- 自动恢复许可已消费、但 intent 尚未写入时，restore/revoke 均 fail-closed 并保留目标；
- intent-only、exposure-only、损坏 intent/exposure 时均禁止自动恢复；
- 开放后的候选库业务写入保留且可完成；`manual_target_retained` 保留指针和候选库；
- 未知监听 PID 生成签名人工恢复终态，不误杀未知进程；
- 活动数据库同内容替换、路径逃逸、reparse/link、完整性或外键异常均阻断；
- 无活动指针但存在未撤销 claim 时阻断隐式启动；有效撤销记录才可通过至正式发布证据门禁；
- 常规发布门禁拒绝活动指针、损坏指针和孤立 exposure。

未运行的 1 项为 `test_real_previous_commit_starts_only_on_fresh_isolated_database_copy`：该历史夹具固定使用 `127.0.0.1:18139`，与本次 UAT 明确限定的 18161/18162 端口冲突，故有意排除。其“真实 loopback 启动”目的已由本报告中的 18162 独立候选 SQLite 预热健康检查覆盖；但这不替代未来工厂端的正式发布授权与门禁。

## 结论与限制

本轮只验证候选的隔离闭环。未执行工厂切换、未执行真实回退、未写入工厂业务数据、未新增 Alembic revision，也未 commit、push、合并或更新远端基线。

工厂端如要实际使用，仍必须重新执行本机 Prepare、重新验签、重新制作并验证备份、检查正式服务身份与正式数据库，并取得老板单独发布/回退授权。
