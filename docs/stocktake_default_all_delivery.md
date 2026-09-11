# v0.22.341 盘点默认全部客户
2026-09-11。用户要求：默认全部客户，仅知道客户时输入名称缩小范围。
分支codex/stocktake-default-all，承接v340正式对账及v339成本功能。电脑初始、切换货位及类型默认all；输入客户名称需选具体客户，清空恢复all且保留规格。手机初始和位置重置默认all，搜索空客户保留all，明确产品链接预填客户仍保留。
全部客户选中产品后已有库存匹配使用该产品真实customer_id，避免all非数字导致漏查。没有改变数量、产品主档、成本、权限或保存流程。
验证：既有stocktakeAllCustomers脚本2 passed；TypeScript/Vite构建、JS语法和diff检查通过；无新migration，单head rw10v8x9z71。Chrome已知自动连接超时，本轮不重复尝试，未宣称视觉验收通过。
新仓库资源warehouseTwin-BKeZOdm4.js，保留v339 warehouse-costs.js入口。未修改正式业务事实。开发验证通过，正式 ERP 人工验收待完成。
现场：刷新盘点直接输入规格；需要时输入客户并选择，再清空客户检查范围恢复。

正式发布：2026-09-11 13:38:00，v0.22.341，代码7ef4de351a8faa00b02229765be0695a5f138503，release_runtime_20260911_133719.json completed。正式库SHA前后一致、integrity ok、FK0、head不变。备份D:\纸箱厂erp软件搭建\data\backups\carton_erp_before_release_20260911_133720.sqlite3，SHA256 db189804e4c7aba422914008e9e536c615ea247ee700035b00dfb94dfe355aec。正式HTML和手机运行脚本200且新引用存在。首次Prepare在停服前发现版本引用问题，已修复并通过门禁；无正式业务写入。
