# 常用箱补全资料与库存定位返回

状态：v0.22.379 技术发布完成，待管理员人工验收。已串行保留 DESKTOP014 v378 与 OPT001 v377 的正式改动。

## 现场结果

- PDF、邮箱 PDF 和新建订单常用箱列表中的“待完善”可直接进入该产品编辑。保存后返回原核单/选箱界面；未保存返回保留原草稿。
- 保留原订单对象、PDF 来源及邮箱关联、数量、手填价格、备注覆盖、图纸、BOM 单行覆盖、已勾选数量、筛选与分页。只刷新对应产品主档资料；产品尺寸/材质等库存依据变化时只重新核对对应行，不能沿用旧库存计算。仅说明或完善状态变化不会清除已选库存。
- 完善状态按产品详情接口判断，不把“保存成功”直接等同“资料齐全”。保存后读取失败留在编辑页，返回重试只读刷新，不重复保存。保存进行中不能关闭返回上下文。
- 库存位置本身可点击。在原订单之上打开只读地图，携带实际楼层、货位与批次；展开对应货架、选择产品标签，用黄色突出当前格位与标签。无地图绑定时打开只读库存台账；移位/清零/无权限明确提示，不错误指向其他库存。
- 返回按钮或浏览器返回关闭定位层，原订单及库存弹层不重新创建；不跳转顶层页面、不移动库存。

## 原因与实现

原完善状态是文本；旧编辑返回用全单重新匹配或重新选产品，会覆盖草稿或手填单价。新增 `static/ui/order-context.js` 管理编辑期间的原对象、会话归属及产品资料回读，复用原产品写入接口、权限、版本与审计。真实 Vue 隔离验证还发现原始对象与响应式代理比较造成异步归属误判，已改为保存后读取响应式上下文。

旧库存定位未传楼层，且定位层低于库存弹层；现在使用独立只读定位入口和较高展示层级。仓库复用既有正式货位/货架映射校验，只有有实物且精确匹配的批次才展开标签。普通地图的搜索、盘点、移货及原订单追溯权限保持。

本次无后端业务接口变更、无 migration、无正式业务数据写入；唯一代码 head `mq0912`。历史冻结订单/采购/库存不回填。主档编辑只发生在管理员实际点击保存时。

## 定向验证

- Python 15 项：`test_p0_order_readiness_frontend.py`、`test_p1_18_pdf_inventory_frontend.py`、`test_p1_146_order_trace_map_deeplink.py`。
- Node 14 项：`orderContext.test.mjs`、`warehouseSearchRack.test.mjs`、`unifiedDeliveryPicker.test.mjs`；覆盖原对象/数量/价格/选择保护、失败回读、鉴权失效、只读入口、实际楼层和精确物理批次、相邻行选中及 Vue 模板编译。
- `npm run build`：TypeScript 与 Vite 通过；只保留本次实际入口引用的新 bundle，既有哈希资源仍供打开中的旧页面使用。已有大包/非模块成本脚本提示不影响构建。
- `tests/ui/order_context_chrome.cjs`：纯 localhost 合成 API，无正式库或转发。Chrome 验证 PDF 编辑/保存/返回、常用箱选择取消及保存返回、Vue 响应式归属、保存中禁止离开、地图货架标签定位与返回；零应用错误、零仓库写请求。测试用产品保存 API 是合成内存接口，未验证为真实生产写入。
- 截图与运行结果：`C:/erp-ui-plans/20260912-context/validation/`。1366×768 下编辑按钮、返回按钮、货架标签与常用箱选择正常可见。
- `git diff --check` 通过。旧结构测试按 v375 已发布布局更新定位锚点及等价权限断言，未删除权限保护；构建契约改为校验当前入口实际引用的 bundle。

本次不运行无关全量测试，不自动点击正式页面；正式发布后只核对服务、权限接口和静态资产，管理员现场验收结果另记。工具未提供本任务真实 token 用量。

## 现场最短验收

1. PDF/邮箱 PDF 或常用箱选箱页：先填数量、单价或勾选数量，点击待完善→编辑保存→返回，核对资料状态和原输入。
2. 点击该订单库存位置：核对楼层、格位、产品标签及黄色提示；点返回订单，原数量/单价和勾选应保持。

## 发布记录

发布代码 `6fccf67669809698dc0ab62d47b018411bf03ab2`，版本 v0.22.379，唯一 head mq0912。正式报告 `D:/纸箱厂erp软件搭建/docs/migration_reports/release_runtime_20260912_160939.json` completed；2026-09-12 16:11:19 服务启动与版本复检通过。正式源仍为 `D:/纸箱厂erp软件搭建/data/carton_erp.sqlite3`，本次技术发布未执行助手首次接入。

- 发布源/应用后 SHA256 均为 `b10a13675324f07fb0c0bb59829b27f28dacb7a108a8922108b0f10f3faf42aa`；完整性 ok、外键 0、核心业务表计数一致。无新 DDL 或业务事实写入。
- 时点备份 `data/backups/carton_erp_before_release_20260912_160943.sqlite3`，隔离演练 `data/release_rehearsals/carton_erp_release_rehearsal_20260912_160943.sqlite3`；二者 SHA256 均为 `bc17153be9e3cdf27472a74deff0dd5b7ed895a08dce9628c1f62a0d1c9a7234`，mq0912、完整性/FK/计数通过。同 head 的正式发布门禁未执行无关历史迁移。
- 内网 192.168.3.80:8000、loopback 18000 与原公网入口健康 200；24 项只读烟测通过，包括内外网根入口、新模块、内网七个 UI 资源和地图入口全部依赖字节哈希、未登录产品/仓库接口 401/403。没有自动点击正式页面。证据 `C:/erp-ui-plans/20260912-context/production-smoke.json`。
- 资源：`order-context.js` SHA 前12位 `217850427bf4`；`workspace.css` `c840a5c202b5`；地图入口引用 `warehouseTwin-DRuVYMtc.js` 与 `warehouseTwin-BZPqLXfl.css`，源码/构建/正式响应一致。Vue 合并 v377/v378 后编译通过。
- 整段 Prepare → 内部证据核对 → Apply → bootstrap 同步持续持有 `D:/TianmingERP` 的 Manager.lock；每阶段确认无正式 managed marker、无已接入 state。原 preferences 字节保持，未代填密码或执行接入。旧 bootstrap 保存至 `D:/TianmingERP/control/installer-before-v379.zip`，其 SHA 为 v378 的 `c11c0844bf7931c98f3787d189deeb8225e55c72b9c10670fac0fbb472edfb4a`。
- 签名更新包 SHA256 `1177b3cd60c051cac175c5e3943b21fd37f84dbe5302c419a5df7971c1620b7a`；签名和逐文件校验通过，已同步本机 bootstrap 及 `Z:/sata1-18015598002/BoxERP/desktop-assistant/releases/latest.json`（379/6fccf676）。旧签名包保留。
- 完整安装器 `release-v379-20260912/TianmingERP-Setup.exe` SHA256 `699334ad76d32e5ee4ba8d1fc12efb98aac61ea3604df213b8817b91dd6120f1`；内嵌上述379业务包与已验证的378助手 EXE（SHA `ed10aaf5dbf07d22894b77a15007363eed8907056e37f075663378e16c498376`），Setup 自检和内嵌逐字节核验通过。助手自身版本与 ERP 版本分别标识。

发布排障如实记录：最初外部编排进程没有显式设置控制台 UTF-8，两次版本元数据读取被乱码阻止，均在新的备份/正式应用之前失败；未修改产品或数据库规避门禁。改为外部 UTF-8 包装后正常 Prepare/Apply，失败日志保留 `prepare-v379-encoding-failed*.log`。现场服务此前由助手首次接入于16:02停下，接入因开发依赖目录链接失败；本次完整门禁恢复了 ERP。该目录链接问题及复制容量由独立 DESKTOP015 v380 处理，已交接不要在旧378助手重复接入；不删除任何链接/旧文件或宣称首次接入完成。

NAS 独立回执：`Z:/sata1-18015598002/天明ERP知识库/04_开发记录/任务回执/20260912-v379-订单补全资料与货位往返.md`。正式页面人工结果未记录。
