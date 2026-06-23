# 天华超净 Batch 4 主库迁移后只读验收

- 执行时间：2026-06-20 13:32:00
- 本轮是否执行 Batch 5：否
- 本轮是否修改数据库：否
- 后端数据库路径：`D:\纸箱厂erp软件搭建\data\carton_erp.sqlite3`

## 1. API 验收结果

- 健康检查正常，返回 `orders_table=sales_orders`、`orders_count=11104`、`order_items_count=11169`
- `GET /api/orders?page=1&page_size=20`：总数 11,104，20 条，95.62 ms
- `GET /api/orders?keyword=RUIDA-&page=1&page_size=20`：总数 11,100，20 条，13.59 ms
- `GET /api/orders?keyword=RUIDA-&page=100&page_size=20`：总数 11,100，20 条，13.93 ms
- `GET /api/orders?keyword=RUIDA-&page=200&page_size=20`：总数 11,100，20 条，38.24 ms
- `GET /api/orders?keyword=RUIDA-&page=555&page_size=20`：总数 11,100，20 条，15.62 ms
- `GET /api/orders?customer_name=苏州天华超净科技股份有限公司&keyword=RUIDA-`：总数 11,100
- 订单号精确搜索样例 `RUIDA-42594 / 42596 / 42597 / 39982` 均返回 1 条
- 抽查 20 张历史订单详情：Batch 1 = 3、Batch 2 = 4、Batch 3 = 5、Batch 4 = 8，明细数、订单金额、客户、日期、产品快照均与数据库一致

## 2. 数据库交叉校验结果

- `sales_orders = 11104`
- `sales_order_items = 11169`
- `sales_orders WHERE order_number LIKE 'RUIDA-%' = 11100`
- 天华超净 `RUIDA-` 订单数 = 11100
- `migration_ruida_sales_order_map = 11100`
- `migration_ruida_sales_item_map = 11162`
- Batch 1 / 2 / 3 / 4 / 5 订单数 = `100 / 1000 / 5000 / 5000 / 0`
- Batch 1 / 2 / 3 / 4 / 5 明细数 = `100 / 1006 / 5054 / 5002 / 0`
- `PRAGMA integrity_check = ok`
- `PRAGMA foreign_key_check = 0`
- 孤儿明细 0
- 无效 `product_id` 0
- 重复 `order_number` 0
- 数量 `<= 0` 异常 0
- `subtotal IS NULL` 异常 0
- 费用关键词命中 0

## 3. 前端页面验收结果

- 前端首页和订单页可正常打开，当前服务地址为 `http://127.0.0.1:8000/`
- 订单菜单显示 `订单管理 11104`
- 订单列表页正常显示 `共 11104 条，第 1 / 223 页`
- 前端搜索 `RUIDA-` 后显示 `共 11100 条，第 1 / 222 页`
- 前端组合筛选 `RUIDA- + 苏州天华超净科技股份有限公司` 后仍显示 `11100` 条
- 前端翻到第 2 页正常，列表内容更新为新的 `RUIDA-` 订单
- 订单详情抽屉可打开，样例 `RUIDA-42729` 显示客户、客户单号、日期、状态、金额、产品编码、规格、材质、数量、单价、金额
- 浏览器 console 未发现 error / warn

说明：

- 前端分页控件当前只有“上一页 / 下一页”，没有直接跳页输入；中间页和最后一页已通过只读 GET 接口完成深分页校验
- 前端详情抽屉已验证可打开；本轮未执行任何新增、编辑、删除或其他写操作

## 4. 抽查的 20 个 `RUIDA-` 订单号

- Batch 1：`RUIDA-42594`、`RUIDA-42596`、`RUIDA-42597`
- Batch 2：`RUIDA-39982`、`RUIDA-39983`、`RUIDA-39984`、`RUIDA-39986`
- Batch 3：`RUIDA-26399`、`RUIDA-26403`、`RUIDA-26404`、`RUIDA-26411`、`RUIDA-26412`
- Batch 4：`RUIDA-12602`、`RUIDA-12603`、`RUIDA-12604`、`RUIDA-12605`、`RUIDA-12606`、`RUIDA-12607`、`RUIDA-12608`、`RUIDA-12616`

## 5. Batch 1 / 2 / 3 / 4 样例订单详情摘要

- Batch 1 `RUIDA-42594`：1 条明细，`¥81.60`，产品 `21301201 / 白底黑字内箱60*120`
- Batch 2 `RUIDA-39982`：1 条明细，`¥226.00`，产品 `23201018 / 包装纸箱52*42*9`
- Batch 3 `RUIDA-26399`：1 条明细，`¥21.02`，产品 `21311020 / 中性外箱24*36THB10`
- Batch 4 `RUIDA-12602`：1 条明细，`¥386.34`，产品 `21301093 / 白底黑字内箱24*36`

## 6. 页面性能观察

- 列表 API：最小 13.59 ms / 中位 20.34 ms / 平均 29.06 ms / 最大 95.62 ms
- 详情 API：最小 6.94 ms / 中位 8.02 ms / 平均 12.76 ms / 最大 32.20 ms
- `LIKE 'RUIDA-%'` 在 11,100 张历史订单规模下仍可接受
- 前端订单列表加载、搜索、组合筛选、翻页到第 2 页均无明显卡顿
- 未发现接口超时或页面报错

## 7. 是否发现问题

- 未发现阻塞 Batch 5 申请的 API 或页面错误
- 观察项：前端分页控件缺少直接跳页能力，深分页更多依赖 API；这不是本轮阻塞项
- 观察项：建议继续沿用静态资源版本号策略，避免旧浏览器缓存旧页面

## 8. 是否建议增加索引或改写查询

- 当前不建议在 Batch 5 前临时加索引
- 当前不建议改写 `RUIDA-` 查询逻辑
- 理由：已有 11,100 条历史订单下，列表与详情接口耗时仍处于可接受范围，且此前已确认关键索引存在

## 9. 是否允许申请 Batch 5 授权

- 结论：允许申请 Batch 5 授权
- 前提：仍需用户单独明确授权，且执行前必须重新停机、锁检查、时点备份、Batch 5 dry-run、SHA-256 校验

## 10. 风险点

- Batch 5 仍需严格限制为剩余 manifest 订单，不得滑批
- 执行前必须再次确认服务停机、主库独占写入和外键开启
- 若浏览器端仍有旧缓存，应强制刷新后再做主库正式验收

## 11. 下一步建议

1. 保持当前主库不变，等待用户是否授权 `batch_5`
2. 如授权，先做新的只读 preflight：停机、锁检查、时点备份、Batch 5 dry-run
3. Dry-run 与 manifest 完全一致后，才可执行主库 `batch_5` apply
