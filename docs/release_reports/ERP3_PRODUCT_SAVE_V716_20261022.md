# v0.22.716 常用箱结构与外购资料保存修复

2026-10-22：**技术发布及原生收尾完成，待管理员人工验收**。22:09:36完成收尾，22:06:48健康恢复；停服请求至服务就绪439.018204秒，这是维护时间，不是页面性能测量。

## 发布身份

- 部署源码：`b19e58a0071fab5516908bbf177ece55e888cd20`。四个业务运行成员与交付源`c8c9eeb133c84aa2f166e91b98bc2f3340f93259`逐字节相同。
- 签名包：`507bc02a66bfe96ce427d66d9296f0026db7629a442a19e8d0348f85477651bb`。
- manifest：`2835111c846c78ae5a0f6e1cec4ec498d112e85f639ff1fe298a9aad6005faca`。
- 前置v715源码80e2fff6 / 包318782c4；revision仍ez1022hc，独立supervisor仍a1df45a6，未升级。
- [正式最终回执](/Library/TianmingERP-Release-erp3-next-20261022-v0-22-716-b19e58a0071f/final-result.json)：healthy=true、manual_stop=false、backup_work_retained=false。

## 行为与边界

历史常用箱修改材质、报料或印刷时保留原兼容箱型及未知拼箱、片数、舌宽和工艺。正常编辑逐项记录明确修改意图，可按证据恢复未确认并在原预览核对；未知结构不触发公式猜填，新建默认规则保持。

全面共用产品修改外购资料时，供货方式、类别、规格、采购单位、供应商候选和采购比例作为完整字段族，在每个接收客户范围验证后原子保存。非全面共用、私有候选等不适用情况整体拒绝，双方CAS、预览token、审计和事务保持。新增GET布尔shared_catalog_policy_enabled仅表示已有策略，不授予跨客户库存资格。保存失败区分真实重复、外购约束及版本记录冲突，不泄露SQL或参数。

部署仅改app/api/products.py、app/services/shared_product_master.py、static/index.html、新static/js/product-structure-edit.js及app/version.py。无migration、无自动历史主档纠正、无正式业务补录；controlled exporter管理员能力与delivery facts迁移不在此包。不能恢复整个旧产品版本撤销本轮已确认资料。

## 验证与保留

- 源负责人整合51个新案例完整覆盖：首轮38通过13跳过，补bundled Node后前端14通过0跳过，按唯一案例计51。源任务相邻119与corner37通过，存在重叠，不合计为独立数量；独立复审无剩余阻断。
- 发布根版本说明及真实JWT/router定向16项通过0跳过，经受管UAT，拒绝数据库连接的测试合同保持；发布准备器18项精确成员、额外权限模块/删除/错哈希拒绝及驱动渲染核验。
- unsigned全部29597成员逐项SHA匹配manifest，仅5变更、29592原字节保持，schema/runtime/reader合同不变。新helper SHA为475bbb9a5e4a57f372dbf85f950890a50a48e1c86d91820bf4e683472800b700，HTML使用?v=475bbb9a5e4a。
- 原生Mac副本验证333张原表及schema一致，新版正常启动、端口故障拒绝、旧程序恢复、实际已装supervisor接续、两次完整恢复及业务原件核对通过。未用正式实例造单。
- 正式worker完成revision/schema/FK、ERP3/PDF相邻只读与静态检查。最终回执后，普通用户独立执行本机与局域网共76次GET，签名身份、版本纯模块合同、新helper及原手机等资源SHA、匿名版本401全部通过；不登录、不连接DB，不冒充页面人工验收。
- 新备份20261022-215954-7dc8d203.tmbackup，SHA为d3724d2a4c4a9e2c021b97907b9e19509f9cc07029e105c8e7d3a3617ca4b8f0。原11份逐项保留，连新共12份；前置程序和已发布签名包保留，不以旧备份覆盖后来业务。
- coordinator、worker、native runner已退出；业务PID15131、UID499、PPID1对应精确新程序。所有权/空闲守卫正常回收38,276,059,351字节演练、启动、回读临时物及重复阶段归档。

未运行全仓库回归。基线已有单位、旧静态布局及价格精度显示断言差异保留在候选报告，未删除断言或改无关业务凑全绿。版本测试受管副本因空闲确认守卫保留，未绕过清理保护。剩余重型查询、全模块UI统一、正式页面性能测量及历史数据治理仍在ERP3后续范围。

## 人工验收与交接

1. 整页刷新，核对版本v0.22.716；打开常用箱编辑，核对未知结构和已确认材质、报料、印刷同时保留。
2. 仅按实际业务编辑，预览应只含明确变更；全面共用护角保存后回读双方版本、外购资料、未改名称及售价。
3. 浏览原订单、报料和手机入口；没有实际修改需求时退出编辑即可。

已明确通知补录负责人维护结束，先正常只读核实新版本和策略字段，再继续原授权业务。人工页面验收pending，实际回读另补证据。

随后补录任务通过原登录Chrome正常只读GET版本及产品1995/1870，根独立比较本地响应文件：版本响应与当前完整说明精确相同；两产品均仍v2，仅增加shared_catalog_policy_enabled=true，原业务字段逐项相同。证据authenticated-response-check-v716.json。该对照限定恢复补录前的时点，后续正常业务变更不应按旧文件覆盖。人工页面验收状态保持pending。

GitHub push因认证失败未同步；本地提交与受验证增量source bundle保留。证据目录为`/Users/kimiyf/ERP-Migration/evidence/erp3-next-20261022`，包含final-result-v716.json、final-state-verified-v716.json、post-release-http-v716.json、v716-independent-package-review.json、release-prep/review-v716.json及NAS独立回执。
