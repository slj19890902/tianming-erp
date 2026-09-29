# 常用箱默认货架位置记忆

方案：`docs/plans/PRODUCT_DEFAULT_LOCATION_20260929.md`。Sol 实现，Astra 审核和交付。普通代码发布沿用老板长期授权；不代用户添加任何正式库存或修改历史默认配置。

## 实现

- 首次货架新增成品不再要求所有旧库存均位于当前格；使用本次成功保存的实际层格，同批按提交顺序首次生效。
- 现有偏好、区域默认、明确取消和旧固定货架配置不被自动覆盖。通用库存、半成品和原料不设置客户常用箱默认。
- 手机同一添加入口的待归位库存首次归位也记忆；仅新成功事务调用，重放不写，普通移货保持原规则。
- 常用箱编辑直接读取并显示已保存默认位置；保存态与选择草稿分离，读取失败禁写、可重试。说明使用折叠提示。
- 产品 ID、客户范围、管理员权限、位置有效性、CAS、幂等、审计和事务保持。无迁移，无历史回填，不改变既有库存数量、成本或实际位置。

## 验证

1. 先建立目标用例时被旧夹具缺少成本证据阻断。将成本冻结在该服务测试中隔离后，再用测试插件加载 `08af5dc9` 的原 `remember_stocktake`，在真实目标断言复现 `None != 2`；使用修复实现同一用例通过。插件及原实现副本仅在 `D:/ERP-UAT/product-default-location-20260929`，不入发布包。
2. `tests/test_receipt_putaway.py` 9项通过，覆盖首次记忆、多现存位置、后续不覆盖、手动修改后默认路由、取消、固定货架避让、通用货物排除、地图版本和API权限/客户范围/幂等。
3. P1-47D 两个目标测试通过：同批同产品两格只记首格；失败后库存、偏好及审计全部回滚。
4. P1-47C pending 归位目标测试通过：原数量与预占守恒、重复提交、清空后重放不恢复、异常回滚。
5. 收料用途相邻流程、手机保存返回与缓存入口3项通过；Python合计15个不同目标用例通过，不以重复运行叠加计数。
6. `node tests/receipt_storage_frontend.cjs` 通过：已保存标签不随未保存草稿变化、保存与取消回显、读取失败禁写、产品切换忽略旧响应。
7. `node tests/check_index_vue_template.cjs` 完整模板编译通过；Python编译、差异检查及唯一Alembic头 `ed0928ml` 通过。

旧UI静态组合另有4项失败，已在未修改基线08af5dc9精确复跑，结果相同：P1-43C 的 `test_label_drawing_and_remarks_are_reduced_without_deleting_saved_fields`、`test_internal_bom_component_is_one_compact_business_row`、`test_hidden_bom_facts_survive_round_trip_and_delivery_mode_is_canonical`；P1-46 的 `test_selected_layout_keeps_one_source_of_truth_for_visible_fields`。分别为中文文本读取断言、缺少BOM测试辅助方法及旧页脚字符串假设，不属于本轮引入。没有运行全量测试。

## 现场核对

管理员在正常业务操作中：选尚未配置默认位置的常用箱，在货架添加或首次归位后，打开常用箱编辑核对层格；手动修改或取消后，核对后续添加不会覆盖或恢复。不要为验收重复录入已有库存。

## 正式发布结果

状态：技术发布完成，待管理员人工验收。环境没有提供本轮真实token统计，不估算。

- 版本 `v0.22.537`；运行源 `da88398e948c68cf3593ea99aa92cb820f7ebcc0`，正式分支已快进整合。
- 签名包 SHA256 `e9a96cce42c6ca5c9c0323cf2fdc173ea0f97368c77d7ba72b11365416ac3138`；前版/回滚程序包 `b0e5353587edb4db460c09382f9fe3fdbb7edb2e8f3524baaeb97ff261575e39`（v536）。
- 受管更新器停服时点备份 `Z:/sata1-18015598002/BoxERP/backups/20260929-155308-3fa02a9d.tmbackup`，已验证；SHA256 `bd85f08922659bd032dd5ced14d2b0beaea26edb29a5cb045d1b47d6bad2f5ca`。
- 正式目录 `D:/TianmingERP`；数据库 `shared/data/carton_erp.sqlite3`；唯一 head `ed0928ml`，未执行迁移。
- 305张表停服备份前后及切换程序启动前事实完全相同。启动后只有 `email_intake_settings` 的同步开始/结束时间及接收计数变化；其他表一致，打印校准文件一致。`integrity_check=ok`，外键0。
- `127.0.0.1:18000`、`192.168.3.80:8000`、`172.16.1.26:8000` 健康均200；主页、手机入口及添加页面/脚本哈希全部匹配签名包。未登录默认位置、货位及地图数据接口均401。
- 本轮没有代录正式库存，没有批量回填默认位置，没有自动点击正式页面。完整证据在 `D:/ERP-UAT/product-default-location-20260929/`；NAS包与独立回执归档记录另附。
- NAS更新源 `BoxERP/desktop-assistant/releases` 已发布v537，包身份与正式运行一致。独立回执：`天明ERP知识库/04_开发记录/任务回执/20260929-常用箱默认货架位置-v537/回执.md`。
