# 第三版方案：送货原请求地图数值兼容修复

状态：开发验证通过，尚未正式发布。执行模型 GPT-6 Astra；未派生其他代理。

工作区 `/Users/kimiyf/ERP-Migration/dispatch-v3-20261014`，分支 `codex/dispatch-v3-20261014`。基线是总任务已核实正式源码 `61e5fafba43ec9fe27e0381d0dbab59d76eb96a5`，本子任务没有刷新远端，不声称远端已同步。

## 问题与最小修复

旧地图投影的四个百分比是 Python float；例如 10.0 经 JSON/JavaScript 本地存储后成为 10。旧 digest 保留 Python 的小数点表示，导致发货、resolve 和 close 在账本查询前拒绝相同原请求。本任务先用隔离库真实 HTTP→实际 JS 恢复 helper→HTTP 复现三项 409，再改代码。

新快照保留 schema_version=1，增加 `map_coordinate_encoding: decimal-string-v1`。仅 `pick_task.items[*].location_lines[*].map_point` 的 left_pct/top_pct/width_pct/height_pct 编成规范十进制字符串，例如 `10`、`2.5`；数值范围 0..100，最多四位小数，与原 `_map_number` 投影精度一致。业务数量、ID、版本、客户、位置身份和实际来源保持原类型和校验。

没有替换全局 canonical/digest、request_hash、账本动作或回执结构。对无编码字段的旧请求，先校验原摘要；只在失败时将上述四个地图字段还原为旧 float 表示，且必须再次与原 snapshot_hash 完全吻合。还原只存在于服务器本次处理内，原 key、原 hash、浏览器记录均不改。数量 100.0→100、额外字段、不同来源等不会被这条兼容路径归一化。

execute 在原编码下重新构造实际快照并精确比较；现有订单→送货锁、版本、权限、客户范围、原来源、数量守恒、审计与事务不变。close 与 execute 仍争同一锁。已完成/已结束优先返回原凭据，不再扣库。

未知编码、损坏坐标、标识/版本或摘要不符返回具体类别与 `X-Delivery-Dispatch-Original-Invalid: 1`。页面保留原记录与 closing 状态，清掉过期“可按原内容继续”资格，提示通过查看原单核账；确定的损坏请求不再提示靠重复结束修复。没有清 localStorage、换 key 或自动重发。

## 协议、缓存与回退

`tests/fixtures/delivery-dispatch-map-v1/` 保存从上述已核实基线直接提取的原 service/helper，provenance.json 保存来源 SHA 与逐文件 SHA-256，测试执行原 reader 而非模拟其判断。

- v625 原 HTTP 实现生成的旧 completed/closed 账本，经真实 JS 保存后可由新实现恢复，原 request_hash 不变；resolve 为零 DML。
- 新 completed/closed 凭据可由 v625 原 reader 实际查询、重放、关闭并经原 helper 验证。
- 新请求尚未完成时，回退 v625 原 writer 安全返回 409（不扣库），仍可按原请求 close，之后新旧 reader 都能读取结束凭据。
- 旧 helper 容忍新字段和字符串坐标，实际保存及终态验证通过。因此不需要修改 `delivery_dispatch_v1` 助手能力标记，无 DDL。
- 集成时根代理需将 static/index.html 第26行 helper 的旧 cache-bust `cabbe6fcb1a5` 更新为最终 helper SHA。该引用由根串行负责；本子任务只改获授权的送货恢复方法。

## 验证证据

运行时：`/Users/kimiyf/ERP-Migration/venv/bin/python`，`/Users/kimiyf/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node`。全部 pytest 使用专属 `--basetemp=/Users/kimiyf/ERP-Migration/evidence/dispatch-v3-pytest`。

1. 修复前新增 HTTP→JS→HTTP 用例：3 failed，execute/resolve/close 分别返回原 409 错误；未替换 activation、ledger、库存、认证或事务。合成的只是隔离库业务/几何数据与当前发布地图文件身份。
2. 修复后首轮新链路及已有送货命令：19 passed。
3. 新旧协议、真实旧 reader、并发和异常专项首轮：32 passed。
4. 完整定向后端组合：75 passed（101.57s，131 warnings）。命令：

   `/Users/kimiyf/ERP-Migration/venv/bin/python -m pytest tests/test_delivery_dispatch_map_protocol.py tests/test_delivery_dispatch_commands.py tests/test_delivery_dispatch_revision_compat.py tests/desktop_assistant/test_delivery_dispatch_reader.py -q --basetemp=/Users/kimiyf/ERP-Migration/evidence/dispatch-v3-pytest --disable-warnings`

5. 对新编码补充规范小数长度及超大指数拒绝后，最终专项复验：39 passed（52.42s，79 warnings）。命令为上述 pytest，仅运行 `tests/test_delivery_dispatch_map_protocol.py`。
6. UI 与草稿打印相邻回归：68 passed；最终 helper 校验收紧后 UI 本体复验：44 passed。命令：

   `NODE_PATH=/Users/kimiyf/ERP-Migration/tools/inventory-ui-test-runtime/node_modules /Users/kimiyf/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node --test tests/ui/delivery_dispatch_recovery.test.cjs tests/ui/delivery_draft_print_recovery.test.cjs`

   最终复验仅运行第一个文件。复用既有 linkedom 运行时，未安装或复制依赖。
7. `py_compile`、`git diff --check` 通过；Alembic 唯一 head 为 `ep1010md`，没有迁移改动。

专项验证包含：新旧地图快照的 execute/resolve/close；无地图新旧对照；原完成/结束重放；真实坐标变化阻止发货而允许结束原请求；改数量/客户/来源/原账号/标识/版本/确认标志拒绝；未知/null/bool 编码、非有限/超范围/布尔/字符串坐标、额外字段及业务数量浮点变化拒绝；篡改 payload 后即使重算快照 hash 仍不能冒用终态 key；真实写入全部 flush 后故障的数量、拿货任务、账本和审计回滚；旧完成与新完成的完整消耗证明验证。

并发不是顺序重放：两个真实 HTTP 请求，首个请求在 commit 前持锁，第二个请求到达共享写锁，之后放行。新编码/旧 float × execute/close 两种获胜顺序，共四项；两端返回同一终态，每个 key 只增加一条账本。execute 获胜只扣 130 只（实际拿货80+50），close 获胜扣0且送货/拿货任务不变。

UI 首轮另发现既有14个 JSON 归档的 manifest SHA 按 Windows CRLF 计算，Git 当前保存 LF；逐个恢复 CRLF 后14/14匹配原 hash。只将专用校验改为原字节或 CRLF 原归档字节双模式；没有修改任何旧 JSON、manifest 或其历史哈希。

## 数据与剩余边界

仅写隔离 pytest 合成数据库；没有正式数据库读写、系统认证、业务服务启动、服务控制、备份、发布、Git push 或正式页面点击。没有处理 C架、库存治理、000120/000204或其他库存修复。

用户现场那条原请求尚未取得，不能宣称已核账、已发货、已结束或该条最终根因已确认。根任务仍需按原账号/key/送货单ID只读对应幂等回执、发货/撤销历史和库存流水。无法由四个已知字段精确恢复原摘要的记录（包括其他数值表示损坏）保持拒绝，不猜测或重签。

正式页面与实体打印未人工验收。集成后最短人工核验：保留原记录→查原结果；核对凭据及原单/流水；按实际结果决定是否结束原确认。不要把合成用例当作现场发货核账结果。

真实 token 用量不可获取，未估算。
