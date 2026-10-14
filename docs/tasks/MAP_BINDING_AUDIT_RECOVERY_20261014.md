# 2026-10-14 有限地图绑定恢复合同

本候选从 `68b1e676` 建立于 `codex/map-binding-audit-recovery-20261014`，仅新增服务、专用测试和本文。根代理明确授权该独立子任务。没有增加 HTTP/UI、DDL、通用修复开关，也没有修改既有 exact-source 预览或业务服务。本代理没有访问受保护实例、认证、停服、复制或写入真实业务数据库。

## 事实与授权边界

本案的目标是恢复一楼 23 项已发布区域策略与当前地图的绑定，保留全部真实库位、布局、地堆计划、库存、模具及其历史。不能把地图恢复当成实物移位，不能发布现有含恢复货架的草稿。

- 真实几何锚：`pre-publication-6c6875.json`，完整 SHA `6c6875b3af262a5a24ee5992adbf7d30ac1bd4db5dd5cb652558a285e0ae7e27`，一楼 revision `063898adb53eb21e`。
- 原策略绑定：`12069ae4137257d6`；原发布 SHA `e79feaba1ab0443a1ef7a337a10e7d3615a8536d21298a0af20161a390da521b`。该文件没有找到。本服务始终记录 `exact_binding_source_available=false`，不生成或重标这种源图。
- 当前 runtime SHA `cef9dfd43e8ef2b4d94abdd43c50e631d548e5f02a447041c996709832e1ab1b`，一楼 revision `e919e36e2f3ec786`。
- 当前 draft SHA `08c87da8d4a6797c48e4874f9248416f59b930df906a9f3a9510729a23ece38f`；草稿保持原字节。
- 原审计区间 41374–41428，共 55 行。固定字段 `id/action/resource/entity_type/entity_id/module_code/action_code/result/user_id/schema_version/details`；details 解析后统一使用 UTF-8、ensure_ascii=False、sort_keys=True、紧凑分隔、禁止非有限数值做 SHA。摘要 `b6e753909d0af4cdee1c64fa518415124c0d8449e1f267329cd648df1db59243`。

以上 pins 编译在该有限服务中，调用者不能传入替代 pins。测试中使用独立合成图/合成 SQLite，仅在 pytest monkeypatch 内替换这些信任锚，不存在产品绕过参数。

完整 owned 证据目录为 `/Users/kimiyf/ERP-Migration/evidence/erp-plan-v3-20261014/map-preflight-20261014-034856/`。真实6c原字节来自此前已有导出按官方 writer 格式复原，完整 SHA 匹配；不是猜造 e79。旧源缺失的代码原因是旧 `delete_published_warehouse_twin_rack` 只在内存保存发布状态后原子替换 runtime，未写 layout_backups；正常 publish 则在覆盖前备份，所以保留了发布前6c，没有留下两次删除前e79。

## 独立证明与真实业务门禁

服务不把几何锚的 revision 改成原绑定 revision。它分别核对：

1. 两份真实地图的字节 SHA、各自楼层内容摘要、楼层集合和其他楼层；一楼 features/bounds/source/layout 身份等全部受保护字段相同，其他 active rack 和既有 tombstone 完全相同。
2. 当前只比6c少指定两架。第一架完整 tombstone 等于6c对象；第二架的 version 6→7 具有41376完整 afterimage，除 version 外等于6c。当前 tombstone 完整等于该真实 afterimage。全部格 UUID/别名与历史格保留。
3. 当前 layout_edit_receipts 是原完整历史追加两条且仅两条删除回执；与41406/41428身份、key、fingerprint和不改库存事实完全相符。最后一次删除指向真实当前 revision。
4. 41374证明6c发布；41400/41401将真实6c备份、e79/12069发布和单独EDIT-006应用连接起来。22份独立 area receipts 为其余22项策略给出原版本、feature和承接事实。所有23项策略ID/area/version固定，逐项核用途、名称、feature、区域状态和无草稿。
5. 两架无启用正式库位引用；全部现有与历史 MCELL 无模具本体位置引用。现有实际模具搬位事实保留。
6. 待入库4区采用独立几何合同：真实6c与current feature及邻近边界一致，区域均已有原正式 functional 位置，CURRENT_MAP、placed、layout版本和有效范围正确。合同明确区分几何来源063898和审计绑定12069；它不调用需要相同source revision的原staging preview去伪造对象。策略CAS后逐位调用原 `operational_location_issue(require_published=True, require_map_geometry=True)`，实际可用才接受。
7. FIN-001唯一 published plan及全部active location signatures与原41399精确相同。策略CAS后调用原 `record_map_applications(previous_floor_layout=真实6c, floor_layout=真实current)`，写且仅写一个新适用回执，随后原 `published_ground_plan` 验证该回执。原plan revision/version、slots和locations全部保持。

## API 与事务

模块：`app/services/map_binding_audit_recovery_20261014.py`。

```python
plan = preview(db, actor=admin, source_bytes=verified_6c_raw_bytes)
receipt = apply(
    db, actor=admin, source_bytes=verified_6c_raw_bytes,
    expected_preview=plan, operation_key=reviewed_operation_key,
)
```

`preview` 只读，要求清洁 Session 和数据库中仍有效、全客户范围的 admin/boss。返回完整可审核合同与 `preview_sha256`，包括全部策略行和指定业务表的完整逻辑摘要。库存/仓库/模具相关表、位置布局、staging、偏好均进入摘要。该摘要不等同只比较可用数量；每行所有字段参加校验。

`apply` 默认持有既有地图事务锁直到提交，进入原 `atomic_bom`，持有持久一楼投影 claim 后重新读取完整预览。原始文件在预览读前后、CAS前及写入末尾再次校验。任一变更拒绝旧预览。按原 `_advance_policy_version` 执行23次真实CAS，任何一项失败回滚所有已做变化。

只允许23项策略 `published_map_revision/version/updated_by/updated_at` 变化。完整前后摘要核对禁止area/location/layout/plan/slot/occupancy/库存/模具等变化；末尾审计后再次检查，55条原始审计也必须保持。失败抛出并回滚；不吞掉审计和commit失败。

完整回执写入 `FinanceIdempotencyRecord`，普通恢复审计只保存 receipt ID、receipt/preview摘要及诚实的两个来源版本。回执自带完整性摘要。同key、同actor、同完整预览重放返回原回执，不增加策略版本、日志或新回执；不同载荷/actor/损坏回执拒绝。默认重放亦结束自身事务，不遗留写锁。

`commit=False` 只为已有外层事务/隔离回滚演练提供；调用者若最终提交，必须把外层地图锁持有到提交，并在同一受控事务中完成。正式独立调用使用默认 `commit=True`。服务没有批量修其他地图、重新选择source或自动放宽pins的后备路径。

## 合成验证与实际演练分界

初始缺失服务失败用例已记录；随后23个合成业务/回滚用例通过，再补20项边界后，新专项43项及原49项相邻回归共 **92 passed in 13.40s**：

```sh
PYTHONDONTWRITEBYTECODE=1 /Users/kimiyf/ERP-Migration/venv/bin/python -m pytest \
  tests/test_map_binding_audit_recovery_20261014.py \
  tests/test_map_binding_retirement_preview_v3.py \
  tests/test_inventory_map_publication_closure.py -q --tb=short \
  --basetemp /Users/kimiyf/ERP-Migration/evidence/erp-plan-v3-20261014/map-binding-audit-recovery-regression
```

覆盖全部23项成功、一个合成FIN计划、四个合成staging、全库每表逻辑hash不变量、同key零增量重放、不同key拒绝重复修复、策略/用途/位置/库存/审计/文件/actor版本预览过期、第二和第23项CAS失败、ground/staging/audit/commit失败、claim后换位、晚审计污染库存/历史、真实旧格模具引用、完整afterimage/回执篡改、外层事务成功后rollback与失败恢复，以及严格JSON。

92项不代表真实23项已正式恢复。根代理另用既有run_task在owned child检查真实数据、回滚/提交/重放及实际耗时；该独立实际结果由根记录，不能以合成样本代替。正式ERP仍由根统一授权和安排，本代理未做发布或正式更正。

知识库回执：本文件作为本轮独立候选回执交根统一回写NAS，避免并行操作当前已报告阻塞的NAS。状态：开发验证通过，尚未正式发布；无管理员人工验收声明。
