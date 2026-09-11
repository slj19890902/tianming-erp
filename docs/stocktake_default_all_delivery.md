# v0.22.340 盘点默认全部客户
2026-09-11。用户要求：默认全部客户，仅知道客户时输入名称缩小范围。
分支codex/stocktake-default-all，承接v339正式成本功能。电脑初始、切换货位及类型默认all；输入客户名称需选具体客户，清空恢复all且保留规格。手机初始和位置重置默认all，搜索空客户保留all，明确产品链接预填客户仍保留。
全部客户选中产品后已有库存匹配使用该产品真实customer_id，避免all非数字导致漏查。没有改变数量、产品主档、成本、权限或保存流程。
验证：既有stocktakeAllCustomers脚本2 passed；TypeScript/Vite构建、JS语法和diff检查通过；无新migration，单head rw10v8x9z71。Chrome已知自动连接超时，本轮不重复尝试，未宣称视觉验收通过。
新仓库资源warehouseTwin-BKeZOdm4.js，保留v339 warehouse-costs.js入口。未修改正式业务事实。开发验证通过，正式 ERP 人工验收待完成。
现场：刷新盘点直接输入规格；需要时输入客户并选择，再清空客户检查范围恢复。
