# MOBILE-FIELD-SEARCH-20261010

用户已批准《核查与统一手机查询方案.txt》，按顺序实施并沿既有普通发布授权交付。根任务先修80012273来源编码，再提高手机编码和位置可读性，再整合查询及现场操作。当前正式v613/bcd3510f/eo1010pi；远端正式分支暂为v612，候选采用已安装v613的精确Git对象，发布前重新核对远端及安装基线，保留其他任务更新。

## 范围与验收

1. 流水片料来源编码通过正式外键链投影并可搜索，保留适用产品绑定独立语义；合并报料/权限/数量单位不变。80012273实收500张、预占500张必须仍是同一批片料。
2. 手机编码、模具位置及实物位置置顶同等可读；BOM逐子件模具及位置；保留全部上传图纸、任务冻结资料和v613产品生命周期。
3. 一个现场查询入口与一个主输入，找产品/认纸板/查货位；保留订单、材料、生产、模具搜索及真实有量库存取用能力。产品近似尺寸覆盖无订单/无库存主档，显示差值，不自动认定适用。
4. 货位实存和默认应放分开显示，受限不冒充空；盘点移货进入原正式执行器并返回当前查询上下文，不复制库存写逻辑。保留未识别货物/位置异常入口。

禁止：正式业务回填、改库存/成本/绑定/客户权限、历史数据覆盖；删除版本/幂等/事务/审计/适用资格；IAB与正式页面自动点击。代码阶段预计无迁移。

## 文件所有权与步骤

根：整合、版本/文档、静态引用、发布和只读正式核验；每阶段先定向验证再进入下一依赖阶段。
阶段1执行负责人workbench_backend：独立候选树，warehouse_movement_read、必要来源投影服务、流水schema/API接线、warehouse-movement-view及其专属测试。不得修改product_workbench/mobile_erp。
阶段2-4 API负责人search_review：app/api/product_workbench.py、app/services/product_workbench.py、app/api/mobile_erp.py的只读位置/查询投影、必要新读取服务与专属tests。UI负责人home_frontend：static/mobile_erp.html、static/ui/product-workbench.js/css、新手机查询/上下文模块、static/mobile_stocktake.html仅返回接线及UItests。独立树从a543e785启动；按1→2→3→4依赖顺序在根串行验收、整合。root负责独立复核和最终验收，static/index.html资源引用由root更新。不得重复写同一文件。

## 验证与交付

合成隔离数据库定向测试；需要正式副本时必须scripts/uat/run_task.py管理。正常/无来源/多来源/无权限，近尺寸非自动使用，多模具/多附件，真实与默认位置、返回上下文；一次最短共享回归。无全仓测试。发布前唯一head、准确SHA、最新基线、Manager备份恢复验证/回滚准备，发布后健康、相关资源及只读接口核查。管理员手机实测单列待验收。NAS独立回执。任务产物D:/.codex/workspace_artifacts/mobile-field-search-20261010。
