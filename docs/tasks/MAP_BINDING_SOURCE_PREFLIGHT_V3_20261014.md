# 地图绑定修复：有限源图取证与事务预览

承接 `266e7cf8` 的只读定位。根授权本阶段设计发布前置同批的有限读取，并编写纯函数预览/合成测试；未授权本代理访问正式实例。本阶段不新增业务功能、不修改 app/desktop_assistant/UI，不申请管理员认证，不建正式副本、不执行正式写入。

## 当前停点与 exact source

现存 88 份导出中仍无所需原图。唯一接受的原发布文档必须同时满足：

- 原文件 bytes 的 SHA256 为 `e79feaba1ab0443a1ef7a337a10e7d3615a8536d21298a0af20161a390da521b`；依据 operation_logs 41400/41401。
- 一楼 revision 为 `12069ae4137257d6`，并能用原楼层全部字段（除 revision）反算到同一 revision。
- 不接受从当前图补回两架后猜时间、重排对象或直接赋旧 revision 的“重建图”。

当前目标文档 SHA `cef9dfd43e8ef2b4d94abdd43c50e631d548e5f02a447041c996709832e1ab1b`，一楼 `e919e36e2f3ec786`；当前草稿 SHA `08c87da8d4a6797c48e4874f9248416f59b930df906a9f3a9510729a23ece38f`。这些只是 10:49 保存证据的 pins；若发布前只读核验变化，须重做预览，不能使用旧结果写入。

## 与发布前置同批的只读采集方案

由根在已计划的同一认证/发布前置窗口执行，复用现有受控只读执行能力，不新增 root API 或绕过签名授权。不因当前缺图另开认证。

1. 先取受控 runtime/draft 当前文件元信息和 SHA、所涉 23 个 policy 的 ID/version/revision、区域和库位/布局/ground/staging/mold 版本摘要，与已有 pins 比对。任何预期外变动先停止修复分支，保留 ERP 原状态；不得顺手发布现有草稿。
2. 只枚举 `/Library/TianmingERP/instance/shared/data/layout_backups` 这一已知目录的一层文件名和元信息。最多 4096 个目录项、元信息阶段最多 3 秒；达到任一上限必须标记 `enumeration_incomplete=true`，不能得出“不存在”。不递归、不枚举 DB/凭据/其他备份根、不触及 NAS。
3. 只保留符合既有 `twin_layout_v1.before_YYYYMMDD_HHMMSS_microseconds_hash12.json` 命名且日期为 **20261013 或 20261014** 的常规文件；不跟随符号链接。先按后缀 `e79feaba1ab0` 定位，再按 10/13 03:02:37 UTC 发布时点附近及较新候选排序。文件名后缀只用于优先级，不能充当完整 SHA 验证。
4. 常规文件逐一按只读 FD 读取，核对前后 device/inode/size/mtime 未变；拒绝 symlink、超限文件及路径越界。**每文件最多 2 MiB、最多读取 8 个文件、累计最多 16 MiB、全流程硬截止 20 秒**，任一上限先到即退出并返回小型候选/未读原因清单。每个块/文件边界检查 deadline。不能把本次失败扩大成重新从最旧文件顺序读 80 MB。
5. 只有完整 SHA 与 `e79fea…` 匹配后才导出原始 bytes，落到本任务有所有权的证据目录。只导出 **1 份、≤2 MiB** 原图及小型 manifest；保留 LF/CRLF，不只返回经重新序列化的 JSON。多个同 SHA 拷贝只取一份、记录其余标签。输出工具只展示小型 manifest，不把整图塞进对话导致截断。
6. 若无匹配，返回有界的 `not_found_within_scope` 或 `limit_reached`。可顺带返回已知 `twin_layout_v1.before_20261013_030237_891851_6c6875b3af26.json` 的存在/大小/SHA；其整 SHA 为 `6c6875b3af262a5a24ee5992adbf7d30ac1bd4db5dd5cb652558a285e0ae7e27`，但它是**前一版本 063898adb53eb21e**，不能冒充原绑定图。若只有此文件，不自动改走另一条恢复路径。

这只是待根纳入受控前置的操作规格，本提交不包含也未运行正式读取命令。若元信息枚举或限定范围不足，先保留缺口，由根决定是否定向扩展范围，不自动放宽上限。

## 纯函数预览的门禁

`scripts/audit/map_binding_retirement_preview.py::preview` 不打开路径、不导入业务模块、不执行 SQL、不写文件。调用者传入 source/current 原 bytes、独立记录的 SHA/revision pins、当前区域/策略/库位/模具快照 SHA，以及两次删除审计的 rack ID、operation_key、request_fingerprint。

成功条件包括：

- 原字节 SHA、revision 内容摘要、schema 和楼层一致，JSON 无重复键、NaN、Infinity；其他楼层和文档元数据未变。
- 原楼层 features、边界、source/layout 身份、pallets 及其他受保护字段完全不变。当前只少两个指定 active rack，retired 集合只新增这两个完整原对象；每个旧 cell UUID/格号/别名都原样保留。
- 两个原架无当前活跃 warehouse_location 引用；其现有和历史 MCELL 无当前 mold_tool 精确引用。原有 tombstone 和剩余 rack 完整不变。
- 两次原操作的 receipt 与独立审计匹配、inventory_changed=false、inactive_location_count=0，无额外或被截断的 receipt 历史。
- 23 个区域/策略与 feature 的唯一关系和业务字段一致，无草稿策略，无错误原 revision；两个退役架各对应一个绑定区域。

成功只输出 `ready_for_owned_copy_rehearsal=true` 和 23 项拟更新策略（old version→version+1、old revision→target revision）；`eligible_for_formal_write` **始终 false**。任何不足都清空拟变更。它是严格审计预筛，不代替真实 ground/staging、权限、并发和事务服务。当前由于 source 缺失，没有运行真实 23 项成功预览。

## 现有服务能做什么

`complete_floor_publication` 仅接受一个 `retired_rack_id`；本案有两个已删架，不能仅传一个 ID 就强行更新全部 23 项。合成失败用例已证明：第二项仍旧 revision 时最终全楼层断言会拒绝，事务回滚。

在独立副本获得全部证据后，现有服务可按以下顺序组合，**无须修改业务服务**：

1. 在现有 floor/file 锁和事务内重新核对 source/current/draft SHA、23 策略版本、快照与精确退役审计。先读取/验证 `preview_staging_transition`，此时策略尚未前移，避免其“已当前版本则跳过”分支掩盖所需预检。当前隔离副本 `receipt_staging_areas` 为 29、30、42、44。
2. 对 `83b533e9-e643-43ee-a68c-b151e951808e` 与 `d00e4e04-8043-4a98-bc4d-f018406d5aff` 分别调用 `carry_retired_rack_area`，使用同一原 source、当前 target、同一授权 actor 和业务操作键；各期望恰好更新一个策略并写明确的 retirement 审计。
3. 调用 `complete_floor_publication`，不传 retired_rack_id，不传 coordinate_adjustments，不发布任何地图，不变更草稿。期望其余 21 项区域通过 unchanged_area；随后实际 ground/staging 适用性验证和最终 23 项绑定断言全部通过。不能把审计计数替代业务后置校验。
4. 只在 owned child 中先验证 commit/replay/rollback；正式执行另由根沿既有授权和发布门禁串行安排。若幂等重放，应保持 policy versions、审计与实物事实不再变化。

**ground 的历史 plan/version 不应被“刷新”。** `record_map_applications` 保留原 plan/slot/location 数据，在已有审计表记录本次 target revision、当前 policy_version、原 plan/version、原始 revision 和精确 location signatures。真实证据中只有 FIN-001 的 1 个 published ground plan（ID 1/version 2）需要本次适用回执；FIN-002/003 的已停用位置不恢复。

补充只读核验：副本的 868 行 `warehouse_ground_layout_slots` 与导出逐行一致；现存 FIN-001 423–432 的全部 10 个 location signatures 与原审计 41399 完全一致（plan 1/version 2、policy version 34、map revision 12069ae4137257d6）。签名按现有 `warehouse_ground_map_application.location_signature` 字段顺序、布尔类型和四位 Decimal 规则重算。此证据有利于保留排位，仍不能替代缺失的源图几何和现场证据。

## 写入前后必须保持/验证

- 仅 23 个策略的 published_map_revision、version、既有更新审计字段及明确的新 map_application 日志改变；每个 version 恰好 +1。
- FIN 三个区域 ID、全部 location ID/active 状态、地址、布局坐标/版本、ground plans/slots/occupancy 原样；不重建、不挪位、不合区、不启用。
- 全部 inventory lots/flows 的数量、单位、来源、版本，模具本体位置/版本、历史 movement/二维码身份原样；不由新地图推导任何实物搬动。
- runtime 和 draft 原始 bytes/SHA 完全不变，其他楼层/草稿/恢复 pending 架不变；C 的 d00e4e04 恢复仍只在草稿。
- 所有 23 个 published policy 与 runtime feature/revision 一致；FIN-001 的 published_ground_plan/slot 读取链使用新适用回执，原 plan revision d27ae0261368c38c/version 2 保留。
- staging 4 区的既有功能位和正式可用性验证通过，未创建新位置。第二架身份错误、任何新占用/版本变化、审计失败、commit 失败均全部回滚，不部分成功。

## 已完成合成验证与剩余限制

`tests/test_map_binding_retirement_preview_v3.py` 新增 31 项：纯函数正向；错误/缺失原 SHA、策略快照变化、边界/source/其他楼层变化、重复对象、wrong cell、现存位置/模具引用、receipt 缺失/变化、非法 JSON；真实服务的“两架→21 区域→ground→最终断言”、重放、第二架失败、ground 审计失败、commit 失败。

合成真实服务样本含 23 区域和 **3 个合成 FIN ground plans**，成功得到两次明确退役 + 21 项未变审计 + 3 个 ground 回执；全部 policy 1→2。原 plan/slot/layout/location/库存、runtime/draft bytes 完全相同；重放没有版本或审计增量。不能把此处 3 个 ground 样本冒认为真实副本也是 3 个 published ground plan。

两 suite 合计 **49 passed in 3.52s**；命令：

```sh
/Users/kimiyf/ERP-Migration/venv/bin/python -m pytest tests/test_map_binding_retirement_preview_v3.py tests/test_inventory_map_publication_closure.py -q --basetemp /Users/kimiyf/ERP-Migration/evidence/erp-plan-v3-20261014/map-binding-preview/pytest-synthetic-regression
```

未建新的 run_task owned child，未用真实副本做任何写事务。必须先取得 exact source、通过有限预览，再向根提出 owned child 命令/后置断言清单；此处成功只证明既有事务原语可组合，尚未证明本现场 23 项已满足全部修复门禁。
