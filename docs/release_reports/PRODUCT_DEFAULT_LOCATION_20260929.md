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

正式发布的包、备份、完整性、健康与NAS发布证据将在部署后追加。此时为开发验证通过，尚未正式发布；真实手机与管理员页面验收尚待现场确认。环境没有提供本轮真实token统计，不估算。
