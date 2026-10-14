# 一楼 23 个地图绑定失配：只读证据与修复预览

任务来自根代理 2026-10-14 的独立委派；基线 `decf8a02`，工作分支 `codex/map-binding-audit-v3-20261014`。本轮只读已有导出和已登记隔离副本，不访问正式路径、服务、认证、NAS，不执行修复。未申请新副本。

## 结论与证据边界

23 项全部为一楼已启用、策略已发布的区域：策略仍绑定 `12069ae4137257d6`，当前 runtime 地图为 `e919e36e2f3ec786`。每项当前 feature ID、区域码、正式名称、楼层/区域 ID、存放类型、库存类型及实测边界检查均通过；这证明当前对象可唯一定位，**不能代替原发布源图的完整对比**。

88 份已导出地图中，没有一份一楼 revision 为 `12069ae4137257d6`。85 份 backup 截止 2026-09-07；另有当前 runtime、draft 和程序包地图。导出明确标记 `layout_backup_documents` 截断，不能据此认定原图已丢失或不存在。88 份导出的 JSON 均能按既有 writer 还原到各自原 SHA：86 份 LF、2 份 CRLF；不重写任何原文件。

现有证据可将 21 项列入“待证明区域及所属资产未变”，2 项列入“须明确核验历史空架退役”。**可执行修复数为 0，拟写业务变更列表为空。** 不清空 map binding，不只覆盖 revision，不启用旧位置，不发布现存草稿。

独立脚本：[map_binding_v3_20261014.py](../../scripts/audit/map_binding_v3_20261014.py)。完整有限预览：[preview-v2.json](/Users/kimiyf/ERP-Migration/evidence/erp-plan-v3-20261014/map-binding-preview/preview-v2.json)，200744 bytes，SHA256 `aa79f8b03e8d16ebb9a38b98e65312c41e0dfcf6c7d66e86f846233efe189cf2`。预览保存每项完整 feature、几何、policy version、location IDs/布局摘要、资产身份和必要审计。

## 23 个具体区域

下表的“活跃/全部位置”来自当前库位主数据；模具 MCELL 不等于 warehouse_locations，不能以 0 库位推断无模具或无历史。

| area ID | 区域码 | 名称 | policy ID | 活跃/全部位置 | 后续证明类型 |
|---|---|---|---|---|---|
|29|RAW-002|待入库区 002|4|8/8|区域及资产未变|
|30|RAW-003|待入库区 003|5|12/12|区域及资产未变|
|31|FIN-001|成品待送堆放区|6|10/10|区域及资产未变；ground application|
|32|TEMP-001|临时周转堆放区|7|0/10|区域及资产未变|
|33|FIN-002|成品待送堆放区|8|0/4|区域及资产未变|
|34|FIN-003|成品待送堆放区|9|0/12|区域及资产未变|
|42|RAW-005|待入库区 005|17|8/8|区域及资产未变|
|43|SEMI-002|半成品堆放区|18|0/6|区域及资产未变|
|44|RAW-006|待入库区 006|19|2/2|区域及资产未变|
|45|SEMI-001|半成品堆放区|20|0/8|区域及资产未变|
|46|SEMI-003|半成品堆放区|21|0/4|区域及资产未变|
|47|TEMP-002|临时周转堆放区|22|0/2|区域及资产未变|
|49|MOLD-001|模具堆放区|25|0/0|区域及资产未变|
|50|MOLD-002|模具堆放区|26|0/0|区域及资产未变|
|51|PLATE-001|印刷版堆放区|27|0/0|区域及资产未变|
|52|PLATE-002|印刷版堆放区|28|0/0|区域及资产未变|
|53|RAW-004|原料堆放区|29|0/0|区域及资产未变|
|54|RAW-007|原料堆放区|30|0/0|区域及资产未变|
|55|TEMP-003|临时周转堆放区|31|0/0|区域及资产未变|
|144|EDIT-003|B模具|141|0/0|区域及资产未变|
|145|EDIT-004|A模具|142|0/0|区域及资产未变|
|146|EDIT-005|C模具|143|0/0|83b533e9 架退役|
|147|EDIT-006|模具 E|144|0/0|d00e4e04 架退役|

FIN 的三个独立稳定 feature 分别为 `25437caa-aef0-4979-8c66-7168a652489d`、`a5312753-a309-4196-a85c-af99f33f0243`、`8d5f69c9-c9f6-44b8-b2a0-4ada26b18c14`。FIN-001 保留活跃 location 423–432；FIN-002 保留已停用 443–446；FIN-003 保留已停用 447–458。它们不合并、不重建、不自动启用。FIN-001 ground plan ID 1/version 2 的原发布基准为 `d27ae0261368c38c`；审计 41399 曾记录对 `12069ae4137257d6` 的适用，不应直接覆盖 ground plan 的历史基准。

## 原发布与两次删除的审计链

以下时间按原日志 UTC 表达，地图 ISO 时间带 `+00:00`。这是一份 2026-10-14 10:49 +08:00 的已保存证据，不宣称执行时实时状态。

- 41376：10/13 03:02:33 更新 d00e4e04 架至 version 7，完整对象保留在副本日志中。
- 41377–41398：22 项区域 map_application 从 `063898adb53eb21e` 前移至 `12069ae4137257d6`，记录区域和资产未变；所选 EDIT-006 由 rack apply 发布。
- 41400/41401：10/13 03:02:37 发布 `12069ae4137257d6`，整图 SHA `e79feaba1ab0443a1ef7a337a10e7d3615a8536d21298a0af20161a390da521b`。记录的 backup `twin_layout_v1.before_20261013_030237_891851_6c6875b3af26.json` 是**发布前** `063898adb53eb21e`，不是所缺的原绑定图。
- 41406：10/13 03:03:21 删除 `83b533e9-e643-43ee-a68c-b151e951808e`，revision 变为 `8710a5f6f4228d20`，inactive_location_count=0。
- 41428：10/13 03:12:14 删除 `d00e4e04-8043-4a98-bc4d-f018406d5aff`，revision 变为 `e919e36e2f3ec786`，inactive_location_count=0。
- 41588：10/14 01:42:18 在草稿恢复 d00e4e04 架。草稿未发布，不能用其代替正式图。

当前 runtime 整图 SHA `cef9dfd43e8ef2b4d94abdd43c50e631d548e5f02a447041c996709832e1ab1b`；一楼 layout ID `756bcfa7-74d8-48f2-ba64-a4a23c9984ae`、source `###TM_FACTORY_1F_ERP_V4_CONFIRMED_OUTDOOR.dxf`、source SHA `68b36f47104814b1d7c7f303e5b8ba90da1c743dbaec4e2ef5a71eccc629c5`。这些是**当前**原始字段；未取得原 revision 完整图前，不冒认原图的全部 source/layout 元数据完全相同。

当前代码的正式空架删除路径只持内存回滚快照，不写一份 `layout_backups` 原图。删除完整 tombstone 保留了架体字段，但原楼层 `layout_edited_at`、原列表顺序等不足以由现有数据唯一还原。不得猜时间、逆造原 revision 或把推导对象标为已匹配原整图 SHA。

## C／E 实物身份对应

| 稳定 rack UUID | runtime 退役对象 | 正式图中的旧位置 | 当前草稿 |
|---|---|---|---|
|83b533e9-e643-43ee-a68c-b151e951808e|名称“模具C架”，短号 D，version 4|EDIT-005；(-18083,-5551)，2000×1000×3000mm|仍退役|
|d00e4e04-8043-4a98-bc4d-f018406d5aff|名称“模具E架”，短号 C，version 7|EDIT-006；(-22186,-1942)，2000×1000×3000mm|同一 UUID 恢复为“模具C架”，移到 EDIT-005、(-18083,-5551)，2000×800×2500mm，version 8|

草稿 d00e4e04 保留 C11/C20/C30 的 3 个当前 cell UUID，以及 C2/C3/C5/C6/C8/C9 的 6 个退役 UUID。两架合计 18 个现有或历史 cell UUID，当前均无 mold_tool.rack_location 精确引用；但分别存在 9、29 条相关移位记录，不能删除历史或把旧二维码解析到新格。

这 9/29 条历史涉及的模具分别为 4/19 个，当前账面位置均指向 `MCELL-787D36D9-C990-4C96-8EF6-0B03CFF312E1`：正式 A 架 `5ca09dba-440c-473c-8f6d-e64099dafd23`、EDIT-004、二层三格、短号 A6，架坐标 (-20556,-131)。最后一笔移出 d00e4e04 为 movement 155，10/13 03:12:02，早于其删除。这里只报告账面和地图一致的当前位置，没有现场盘点或实物迁移确认。

## 输入核验与脚本验证

- 导出 SHA `c7aaed2f7bf015930d02a2ed121f5857213334bea563366c69539667d54b686a`。
- 已登记副本 `task-2a4eba8403ba40fba6eff95f2961e553/copies/carton_erp.sqlite3` SHA `ab607153accc3950c5c0f14e4584b559226e813e8243e5140cdb32e5c753b4b4`。
- 两输入前后 SHA 完全一致；副本用 SQLite `mode=ro&immutable=1` 和 `PRAGMA query_only=ON` 读取并关闭。
- warehouse_floors/areas/policies/locations、floor3_location_layouts、ground plans、mold_tools/movements、inventory_lots 共 9 张表，在导出与副本中按 ID 全字段一致。
- 23 项当前唯一映射检查通过；88 份原字节 SHA 核验通过；拒绝非指定副本路径；重复输出因 create-only 拒绝且原输出 SHA 未变；`git diff --check` 通过。
- 输出 200744 bytes，脚本设 250000 bytes 上限。导出中的 `/Library/...` 路径仅是证据标签，脚本从不读取这些现场路径。

复跑需指定一个新的输出文件名：

```sh
/Users/kimiyf/ERP-Migration/venv/bin/python scripts/audit/map_binding_v3_20261014.py --output /Users/kimiyf/ERP-Migration/evidence/erp-plan-v3-20261014/map-binding-preview/preview-next.json
```

下一步仅设计发布前置同批的有限源图读取/比对；若未取得唯一匹配 `e79fea…` 的原整图及所需位置证据，停在本预览。任何写副本演练须另开 run_task owned child，任何正式写入由根按既有授权及发布门禁串行处理。
