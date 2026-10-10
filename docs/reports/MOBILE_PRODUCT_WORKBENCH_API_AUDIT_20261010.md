# 产品工作台 API 只读审查

2026-10-10，MOBILE-PRODUCT-WORKBENCH-RELIABILITY-20261010。审查已完成；以下是复现事实与建议，尚未修复或发布。当前正式v606/4b66fc4c104cf49bc48b065c20b5aeed17f9403a/en1009hp状态来自根已核验事实，本代理没有访问正式业务数据。

只读根树开始HEAD eb809c30c543b89ed64f786d12c085557b69c2f1，结束HEAD4df10c8a（根新增任务卡授权，非本代理改动）。8个相关API/service/test/权限/单位文件LF字节与正式4b66完全相同；source-fingerprints.json保存真实SHA。仓库始终干净；全部探针、报告只写本artifact/api，无服务、PID、浏览器或正式库操作。启动已依CODEX_START→NAS AI_START→本卡→PLATFORM→主需求2/3/5/14/15/18/19和章程3～9，跨模块仅精读单位和产品資料第10章及实际匹配契约。

## 复现与分类

1. 已加工output_piece单位冲突（P2）。真实GET products/{id} 的processed_component summary/group为“片”，但positions[0].unit和扁平inventory.items[].unit为“张”。真实reverse_source→GET reverse使用processed_state=output_piece，候选lot_unit也为“张”。同一实际批次数量实存10/可用8/预占2没有变化。processed-unit.json保留两个完整HTTP及库存前后。原因：workbench复用_inventory_group(unit='片')，共享_position_payload对非finished硬编码“张”；本服务透传row.unit。reverse也硬编码lot_unit='张'。一类单位投影根因，不重复计三处Bug，不改数量/库存台账。

2. 直接ID及offset缺数据库整数上界（P2）。输入9223372036854775808，即2^63，产品Path ID、实际片料lot_id、search customer_id及page四条HTTP均裸500；正常产品ID200，库存事实不变。捕获实际OverflowError: Python int too large to convert to SQLite INTEGER。integer-boundary.json保存请求输入/回包/异常类与前后库存，已排除只是静态猜测。page的offset=(page-1)*page_size也必须整体有界，不能仅将page限制为最大int后仍乘溢出。一类输入边界根因。

3. 正常门禁对照，不登记Bug：当前客户范围搜索不含其他客户产品，直接其他product ID404；普通缺批次999999返回404；停用品仍可查资料但drawings unavailable_inactive；跨客户原批次无绑定404，正式显式allowed-product绑定后同批次反查200且原owner隐藏、候选仅当前授权客户。scope-lifecycle.json保存实际HTTP。未扩大为未经证实的价格/单位换算/权限或匹配算法缺陷。缺附件预览复用现有secure_upload与手机工程图纸端点，本轮没有新增文件渲染测试，也没有宣称所有附件和异常档案已扫描。

## 最小方案及后续合同

只允许app/api/product_workbench.py、app/services/product_workbench.py、tests/test_product_workbench.py。局部processed_component位置投影明确unit='片'，再输出扁平items，普通semi/raw仍='张'；reverse按已核实际profile.output_piece区分片/张。不修改共享_position_payload、数量字段、匹配核心或finished冻结单位。

输入边界对product_id/customer_id/known_customer_id/lot_id做数据库整型上界校验；实际SQL分页offset整体<=2^63-1。推荐非法范围422可读detail，不触发SQL绑定异常；正常范围缺ID404、现有权限401/403及客户范围保持。仍允许缺省/null可选参数，已有十进制尺寸合同不收紧。保留现有items/summary/groups/reverse_source字段形状，仅正确更改已加工片的unit/lot_unit；不换数量，不移除来源/权限/地图字段。

后续红绿建议：在现有processed_output测试加positions/items和实际reverse单位断言，原料对照片/张；参数化整数ID、offset过大422及正常/缺档ID404；复用现有scope/cross-binding/禁止价格资料回归。不得把本轮观测assert通过当修复通过。独立审者按最终候选/source/真实HTTP核边界，浏览器与管理员人工验收仍单列。

## 运行与证据

api.xml：3个观测节点通过/9.24s，断言的是现有错误表现和正常门禁，不是修复绿。补捕实际异常的integer-cause.xml同一节点通过/4.24s，只有3个唯一节点，不把重复运行算4项独立覆盖。test-nodes.json为真实XML classname节点。runner显式加载安全tests/conftest，--noconftest防重复监听；复用tests/test_product_workbench夹具，Base.metadata在tmp_path合成库，未复制正式库。

实际探针入口：仓库.venv Python执行本目录run_probe.py；workdir根tree，默认只跑本artifact/probe.py。evidence-manifest.json记录3HTTP包及探针SHA，source-fingerprints.json记录正式等价。测试JWT短key警告来自既有合成fixture，不影响登录或客户门禁。一次rg传Windows通配路径未展开，随后rg --files定位文档；定位错误无业务/源码影响。

根已于4df10c8a另授权上述最小实施，但本报告保持只读阶段结论；下一阶段另分支从正式4b66开始，旧候选保留，不发布/push/改正式数据。
