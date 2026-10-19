# 今日工作台产品详情500修复 — 2026-10-19

状态：开发验证通过，尚未正式发布。正式现场v0.22.671/010d8ef1，签名安装源码摘要吻合；健康接口200。独立分支codex/product-detail-20261019。

## 原因与范围

产品详情调用product_activity.activity，遇到产品对应订单的供应商采购明细时读取si.created_at。但SupplierRequisitionOrderItem模型没有这个字段，创建时间在采购单SupplierRequisitionOrder.created_at。由此抛AttributeError，服务器返回纯文本Internal Server Error；前端先无条件response.json()，再判断状态码，导致员工看到Unexpected token I而无法打开整份资料。

错误行由2026-10-10提交1aa61d059加入。原详情定向测试未建立含采购明细的产品场景，未覆盖这个分支。不是产品缺资料或权限不足，重新录入资料不能修复。

正式日志受系统权限保护，本次没有读取；复用有来源回执的2026-10-17隔离快照和当前正式代码复现。SY00003/SY00002/SY00001、21311409、10250961、CX00001、CPN130098、CPN130096均复现同一AttributeError。该快照证明具体代码触发条件，不冒充2026-10-19全部正式数据审计。

## 修复

- 采购活动时间改读so.created_at，历史空时间仍为空，不猜填、不写回。
- 前端正确处理非JSON失败响应，保留401、403及结构化业务错误；取消请求保留AbortError；200但格式错误给明确提示，不能把读取失败变成空产品。
- 不新增数据库字段、不做迁移，不改产品资料、库存数量/成本/预占、客户权限或历史记录。

## 验证

先建立红灯用例，后端重现同一AttributeError，前端重现纯文本500及非JSON401解析错误。修复后后端23项、前端45项定向回归通过；新增采购单有时间/无时间、确认/作废、库存读前后不变、500/502、401/403、异常200、取消请求覆盖。Python/JS语法与差异检查通过；唯一head er1016pa。

使用SQLite mode=ro + query_only，对已有隔离快照3848款未删除产品执行完整详情读取与JSON序列化，3848/3848通过，0异常。未新建整库副本、未安装依赖、未提权或停服务，未执行正式页面自动点击。

本轮完成原因定位及修复候选；正式部署尚未执行。发布时需从当时实际正式基线整合这两处运行代码，沿用签名/恢复/切换协议，不能覆盖并行候选或把本回执当成已上线。

证据：/Users/kimiyf/ERP-Migration/evidence/product-detail-20261019/result.json、detail-diagnosis.json、detail-after.json、backend-before/after.log、frontend-before/after.log。

人工验收：正式发布后，在今日工作台搜索上述任一实际编码，打开“查看资料”，确认基本资料、库存/订单与采购记录可查看；本轮尚未执行该正式验收。
