# 独立组套保存恢复：后端候选独立复核

日期 2026-10-10。候选 **f767ebf874ed672ebb99b51cfd9b39529414dfac**，基线正式源 **75129569ed5ff0bf454d075471c9b4fd902b070f**。工作树 group-save-recovery-api-20261010；候选测试前后 HEAD 固定、工作树干净，仅批准的 4 个运行文件和 1 个新测试文件。

结论：本次后端最短独立复核通过，未发现后端发布阻断。UI 候选与跨合同/真实 Vue 技术检查尚待，不据此单独发布或宣布本闭环完成。

## 独立执行的两项边界

`independent-api.xml`：**2 passed，7.25s**；独立 runner 退出 0。仅复用已批准合成 fixture 的建库/备料方法，新断言与控制流程位于本 review artifact，不改候选源码。沿安全 conftest、--noconftest、测试环境、独立路径、启用 Command 拒 UPDATE/DELETE 触发器；没有复制或写正式库。

1. **多来源连续三次 2+1+2 套，原成员耗尽后继续组套。** 三 job、两产品。第一笔后剩 21 子件；第二笔采用旧合法 disposition=semi、actual_output=0、原料 lot_version=999、逐行目标空、lot_id=null、反转原 jobs 顺序，仍保存 1 套成功，剩 14；第三笔按真实 GET 原 body 完成 2 套后剩 0，实际 workspace 不再有 group_stock，成套合计 5。第二/第三笔均包含原批次耗尽的 take0 job，其 GET output_locations=[]，proof 完整成员仍包含该 job、before=consumed=remaining=0、locked_end_balance。真实本次 consume 的数据库 before/quantity/after 与 proof 相等。
2. **最终父 Command 已写入后，后置 enroll 抛业务异常。** 注入点位于真实父 Command/proof、输入证据、入库流水已在事务内可读的位置。业务 409 只带 Assembly-Rejected/no-store，无 Preserve；库存、Job、Command、Movement、Reservation 及业务审计逐字段完全回到提交前。随后只读 not_recorded，事实不变；解除故障后同原 key/body 成功，同键再重放完整 JSON 相同、零新增。这个注入证明后置业务失败的原子回滚，不声称成功的共享策略登记已测试。

三笔原请求在全部子件耗尽后逐笔只读恢复，返回各自历史 proof（21/14/0 余片），不被当前零余额替代。监听 SQL 没有 INSERT/UPDATE/DELETE，并将 Session.commit 替换为禁止调用；全部恢复仍成功。每笔精确 POST 重放整个 JSON 相同，core facts 零变化。对第二笔改变 ignored actual_output 或改变原 jobs 顺序，分别 POST/resolve 409 且保留请求。持久 request_json 无 expected_actor_id、null lot_id 沿旧规则删除、原数组顺序和完整规范请求与 proof.request 相同。

真实独立 HTTP 档案：

- `independent-second-ignored-zero-member.json`：第二笔旧合法 ignored 字段，用于验证器兼容；不声称普通 UI 会生成这种原 body。
- `independent-third-full-zero-member.json`：第三笔正常 UI 形状的实际 body/sourceRow；完成后整组从库存列表消失，原结果仍 completed，适合最终 UI 空列表恢复联验。
- `independent-enroll-after-parent-business-rollback.json`：后置异常真实拒绝头、事务内观察、not_recorded 和原请求重试结果。

## 源码与作者证据复核

实际读取 helper 全文及四运行文件 diff。新 builder 只在 assemble 最终父 Command 首次 INSERT 前调用；mutate_group 独立转发，dispose 内嵌 assemble 不传入。enroll 顺序未动；fresh 在 atomic 内读取身份/来源资格；已保存重放与 readonly 仅核当前/冻结归属、原精确签名及持久 consume/manual_in/输入证据。旧 Command 不补 proof，不改历史流水，默认 builder=None 保持历史 legacy_trace。

proof 成员全集/非零 inputs 分开，按 job/output lot 精确关联，本次余额来自 consume；take0 为同锁余额。首写前严格序列化；错误头的 Rejected/Preserve 分支互斥。角色门禁未放宽，expected_actor 排除旧签名但仍与当前认证及持久 Command actor 核对。只读成功不调用会重检当前生产资格的 source。

作者最终 `api-final.xml` 24 项、`adjacent.xml` 5 项是作者证据，独立只解析 XML/指纹，不重跑全部；13 份真实 HTTP 档案核 JSON/原始 SHA，5 个提交文件核 raw/LF/精确 Git blob。详情记录在 api-verification.json。作者中间 nullable 夹具误删必需 physical_basis 的失败属于探针准备错误；所谓 lock-drift-red 实为通过，不能计已复现新 Bug。

## 先前合同 P2 的状态

旧 CONTRACT-v1-reviewed 快照确有不完整位置投影可被当全集的风险；现 CONTRACT 第 33 行已明确 job/receipt/product/请求版本是全集身份锚点，output_locations 仅正向附加校验，耗尽 take0 允许空列表。第 39 行明确空/缺单位显示“单位待核对”。该设计门槛已关闭；最终 UI 实际是否遵守，仍须用上述独立真实档案验证。旧 CONTRACT-REVIEW/NAS 作为发现时快照保留，本文件为其后续状态。

## 交付边界

本轮独立两节点首次执行通过，只有既有合成 JWT 短 key/Pydantic exclude 元数据警告；不是正式配置变化。没有新业务探针错误、源码修改、服务或浏览器操作、正式业务 POST、迁移或发布。正式版本引用根记录仍 v604；最终 UI/整合/发布备份门禁及管理员验收未由本文替代。浏览器 DOM/Cookie/localStorage、共享策略成功、实际现场操作不冒称通过。
