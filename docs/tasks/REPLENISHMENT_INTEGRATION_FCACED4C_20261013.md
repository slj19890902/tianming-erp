# 指定产品补库与已发布 YL 模具凭据修正的整合候选

本次独立目录为 `/Users/kimiyf/ERP-Migration/replenishment-supply-integration-20261013`，分支 `codex/replenishment-supply-integration-20261013`，从已发布 `fcaced4c878b078e5f2615b553aea4e8a1592f92` 完整复制。按顺序整合原候选 `688c0f9f`、`232b9fe3`、`5ee08d52`、`dfbf3c9c`；`a8ed2766` 仅为原组合候选验收文档，没有重复合入已发布的 `9de9`。

自动 BOM 补库保留组件生产及父项身份冻结。指定产品补库仍按客户、产品、冻结规格工艺及完整配比匹配，全体有效需求 FIFO；计划与实物、可送数量分开。原采购保存事务锁、幂等、真实产出优先、取消释放重算均保留。无 schema、历史回填或直接正式业务修正。

`fcaced4c` 的 `stock_purchase_identity.py`、`finished_stock_identity.py` 和 `frozen_assembly_mold_proof.py` 保持原字节；新增补库使用同一冻结 capture/resolve 合同，没有覆盖旧模具凭据或引用今日主档替代历史事实。打印解耦、Mac 发布及接管代码也保持原字节。

另按主管明确要求修正 BOM 库存采用后的界面刷新：仅确认 POST 成功、窗口及账号仍归属原操作时，失效订单缓存并只读刷新 BOM 核对及订单列表。失败或未知 POST 保留原请求/原幂等键；双击仍阻断，GET 刷新失败仍明确操作已完成，不自动再次提交 POST。

定向 Python 六组为 80 通过，另两个调用 Node 的用例因初始 PATH 缺 Node 未运行成功；以固定 bundled Node PATH 仅重跑这两个，均通过。因此本轮 82 个用例均已通过验证，包括补库 FIFO、多供应商 BOM、25 项冻结模具凭据、YL 邻接、取消及备库身份。7 项补库 Node UI 和 7 项新增采用刷新/未知结果/旧账号/读取失败用例通过，原 YL parent guard 脚本通过。初始环境失败及后续定向重跑均保存在候选证据说明中，没有用门禁放宽消除失败。

原 a8ed 组合证据、fcaced 模具凭据候选及实际技术发布回执的原字节副本与 SHA256，保存在 `/Users/kimiyf/ERP-Migration/evidence/replenishment-supply-integration-20261013`。原 a8ed 的 24 项基线旧失败不是本次整合通过的声明；原 combined NAS 回执仍为 `unknown_timeout`，未重写成已确认。生产回执是既有时点事实，不能当作本候选已经发布。

本次仅交付可直接构建的源码候选。未停服、部署、访问正式业务库、改订单/库存或附件，也未生成或调用真实发布私钥；当前运行仍由主管维护，急单优先。
