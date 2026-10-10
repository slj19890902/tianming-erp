# 产品工作台 API 最小修复回执

2026-10-10，MOBILE-PRODUCT-WORKBENCH-RELIABILITY-20261010。API候选37361f51e5d5fa11809547bfe230f166b14a4e19，父为正式v606源4b66fc4c104cf49bc48b065c20b5aeed17f9403a。managed树group-save-recovery-api-20261010，分支codex/mobile-product-workbench-api-20261010；旧组套候选分支保留。根授权卡4df10c8a；启动与只读发现见REPORT-AUDIT.md，本报告只陈述API候选，尚未正式发布。

只修改app/api/product_workbench.py、app/services/product_workbench.py、tests/test_product_workbench.py，共66新增/1删除。局部加工子件positions和平铺items统一片，实际output_piece反查lot_unit片；原料张、成品冻结单位与底层lot单位保持。没有乘产出因子或改数量算法。product/customer/known_customer/lot ID以及整个SQL分页offset核SQLite有效整数范围；超范围返回中文422，合法但不存在ID仍404。正常最大有效offset可查询；缺省/null可选字段和原权限/客户规则保持。没有共享helper、匹配核心、模型/迁移或版本改动。

## 红绿与真实HTTP

先将专用测试新增断言跑在未修复运行源码上：red-fix.xml两个节点失败/7.45s，分别实际位置单位张而预期片、超大product ID裸500而预期422。原只读api.xml 3 passed/9.24s断言现有错误表现，不作修复绿；integer-cause.xml重复同一观察节点，不加算覆盖。

最终fix-green.xml为tests/test_product_workbench.py全部14项通过/31.05s。包含scope直接product拒绝、订单权限隐藏、实际批次原料单位、加工片全部投影/反查、cross-customer显式绑定、尺寸/报料资料与价格不泄露，及7类ID/分页有界输入、最大合法ID404和最大合法offset200。完整精确classname::node见fix-test-nodes.json。

固定候选后fix-export.xml单一artifact导出节点通过/5.64s，产生14个真实GET包：normal-detail、search、free-reverse、raw-reverse、zero-nullable-detail、processed-detail、processed-reverse、inactive-detail、permissions-hidden-detail、cross-customer-product、invalid-product-id、invalid-offset、valid-missing-product、valid-largest-offset。fix-http各JSON保存原request/actorId、真实status/headers/body与候选及3文件原字节SHA，不手写业务成功数据。manifest保存库存quantity_available/quantity_reserved/version前后完全一致。阶段先正常raw，再仅合成元资料添加output_piece profile/停用product；不把两种形态冒作同时存在的同一事实。

所有HTTP使用真实FastAPI TestClient、既有mobile_erp_app和安全conftest，临时合成SQLite，不复制正式库；run_fix.py显式--noconftest避免重复安全监听。JWT短key警告来自既有合成fixture；未改变认证或测试安全门禁。每条运行远少于10分钟。git diff --check通过，最终工作树干净。fix-source-fingerprints.json同时列3修改文件原字节/LF SHA与5个相关共享文件的正式LF等价；fix-evidence-manifest.json列报告、XML、探针和全部HTTP SHA。

## 交接与验证边界

CONTRACT.md已同步根、UI和独立审者；200正常包络不新增字段、不收紧合法nullable。授权库存始终summary/groups/items，无权限库存/订单为hidden_by_permission+items:[]；停用产品可查资料、工程图纸unavailable_inactive。API错误头沿既有FastAPI行为，不承诺全部框架422具no-store；客户端坏2xx门禁由UI候选负责。

本代理未做浏览器、屏幕、服务或正式数据库操作；没验证全ERP、所有附件/异常档案或完整物理单位换算。独立复核与UI真实回执消费由其他负责人执行，管理员现场验收仍由根记录。没有发布、push、迁移、正式数据改写或停/启任何PID。总持续Goal不因此完成。NAS独立API回执与本报告内容一致并核对回读SHA。
