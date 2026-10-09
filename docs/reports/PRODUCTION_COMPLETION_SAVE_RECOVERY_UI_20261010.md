# 单项备库加工完成：UI候选交付

日期：2026-10-10。范围仅实际 single_job「保存入库」→ actions(action=complete)，相对正式源6640d536。候选SHA见 final-source-fingerprints.json；五个文件由UI执行者独占，未改后端、group dispose、加工算法、共享Axios、版本、正式数据或旧工作树，不push、不发布。

## 已完成的最小闭环

首个业务POST前按账号及原key独立保存并读回完整原body/endpoint/receipt及产品、来源、数量、目标位置摘要。空、坏、截断、错数量/位置/身份/映射证明均保留未知，不报成功。按receipt保护原请求及其新continuation，无关receipt仍可操作。刷新、关闭再开还原原输入且禁止改数新发；在途保存关闭门禁一致。

原结果通过专用只读接口核对；not_recorded只表示本次未见，显式「按原内容继续」才重发原key/body。legacy_trace只给原内容和可读加工记录，不冒完整成功、不清原请求。只有从未未知且后端明确阶段Rejected、不带Preserve时才能回到更正；未知后即使收到Rejected仍冻结。完整证明后显示本次投入、产出和原位置，列表刷新失败保留成功卡和只读刷新入口；清缓存失败保留confirmed防重，只约束原receipt。

single保存/恢复/货位选择/地图force位置GET使用有限60秒局部Axios实例，保留现有认证默认配置且不安装共享响应拦截器。当前401沿原登录失效处理；旧身份晚401、晚成功不改变新账号/另一个弹窗。不修改全局Axios。成功旧A到达时不覆写当前B窗口或输入。

## 验证与可复核证据

- 安全红：baseline-red.json/xml，真实旧HTML方法14组中7失败，包括四种坏2xx、未知改数换key、关闭重开原量丢失、在途关闭门禁。
- 最终：final-tests.json/xml，49/49通过；final-pytest.xml，Python包装1通过。相邻 tests/stock_processing_workflow_harness.cjs 通过；git diff --check无错误（仅既有换行提示）。
- 合同来源：最终API c02693f8 的真实TestClient HTTP原文normal-partial/full、nullable-customer、units-frozen、legacy-trace、not-recorded、committed-ack-loss，以及独立审查 independent-nullable-real-http.json。CJS嵌入标注原源码指纹的真实档案，页面/mixin和恢复module由实际源加载；错误注入和存储控制使用合成harness。不是手写成功回执，也不是浏览器截图或真实手机验收。
- 首写/刷新后只读恢复分别消费真实部分8/20、全量20/20、nullable及冻结单位档案；正确区分物理产出48只与库存账48 sheets。独立合法包同时customer_id=null/output_unit=null/location_name空也通过，未编造单位/位置。
- 提交后丢ack主链：真实已commit的500 → 新页面同storage → 实际loadStockPreparation消费真实workspace GET的新continuation job3 → 实际openStockDialog恢复原投入8并锁receipt → readonly resolve完整证明 → 实际加工历史方法消费真实trace。/actions业务POST始终1，resolve/trace不产生第二笔完成写。
- 49组包含原body/key不变、双实例多key、跨账号、存储失败零POST、unknown后409/Rejected保留、成功清理失败无关来源可操作、局部超时、single入口/地图旧401与当前401、位置旧成功、实际HTML未知禁改及恢复卡位于data-panel外。

最短独立跨验：在候选树运行 `node tests/stock_preparation_completion_recovery.cjs --filter "真实HTTP首200.*independent-nullable|真实提交后丢ack|真实legacy_trace"`。全组直接运行该CJS；Python入口 tests/test_stock_preparation_completion_recovery_ui.py。

## 可见性与剩余边界

静态检查确认恢复卡位于loading/error/empty data-panel之前，原来源/数量/位置、查原结果、明确原内容继续、可读历史、成功但未刷新操作均有实际模板入口。没有扩大布局或普通保存确认。Chrome工具不可连接，隔离Chrome创建被自动审批拒绝（blocked by policy、未执行、无PID），详见CLOSEOUT.md。截图、可见浏览器交互、真实Cookie登录接线、窄屏/现场员工验收仍pending，不列通过。

没有安全注销接口；版本变化等导致not_recorded原body无法继续时，仍保留原请求并给管理员具体核对依据，不能称所有死路已消除。localStorage不是原子跨页锁，独立key防覆盖/读取保守阻止同receipt，但不宣称跨设备/同时读写全局唯一保证。group dispose剩余风险单列，不是本轮修复。成功卡本页面保留；刷新后已完整确认且已清本机记录者沿后台加工历史查证。

## 进程与源一致性

最终跨合同使用c02693f8真实HTTP导出；API四文件哈希随JSON保存，UI五文件LF SHA256见final-source-fingerprints.json。18163/PID15248是在最终合同前import的专属合成服务，不作为最终HTTP源；按根指令保留不动。旧18161/18162与旧工作树未操作。Chrome创建失败无owned PID可关闭，没有finalize或用户Chrome清理。

交付为技术候选，不是正式发布或管理员验收；NAS独立回执位于标准04_开发记录/任务回执目录。
