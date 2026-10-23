# CONTROLLED-UAT-ADMISSION-V717-20261023

状态：仅离线开发和验证；未批准或执行签名、安装、读取实际来源元信息、捕获或导出。

范围：把已验证 exporter 的七个运行模块移植到实际 v717 源码 3a8d76000ea01685ea6ba0ca465c007e1826dd73，并补齐阶段 A 的有限管理员代码准入。远端 factory-current-baseline 当前仍为 b9a077bc115cc649cdc2753cab3dfb3d75269d1c，不代表已运行 v717；本候选从已发布源码及其回执提交 0cf641cb 建立独立 raw worktree，保留旧 v716 工作树及未提交任务卡。

唯一写入负责人：本 ERP3 根任务。文件范围为 scripts/admin/controlled_uat_admission_v717.py、scripts/uat/prepare_controlled_uat_admission_v717.py、tests/desktop_assistant/test_controlled_uat_admission_v717.py、本任务卡和独立 evidence/erp3-next-20261023/controlled-export-v717。继承原 exporter 合同，不改固定任务、接收者、目录、原件白名单或凭据规则；不纳入财务更正、fa1022df 或业务 UI。运行包固定为 9a890345c0f7efaec94b2191f1c2df2a9182ef1d，准入源码另行提交，不改变已冻结包。

阶段 A 的有限执行：新目录 /Library/TianmingERP-UAT-Export-v717-9a890345c0f7，只允许精确 admin ZIP 入场（25源码加清单）；签固定 manifest；按已批准 v717 runtime 合成签名 ZIP；在既有 publication 锁下追加该派生 SHA 的 Mac 信任并保留所有旧项；调用既有 publication 追加工具；固定 uat-export-plan 只检查来源身份并生成实际计划。不得调用业务启动、停止、更新、迁移、Keychain 初始化或 uat-export-apply。migration=null 不充当技术权限边界。

安全要求：正常 macOS 认证入口先校验固定准入源码 SHA，再按原字节复制到 root 只读 transaction 后执行。输入限已冻结程序包与 admin ZIP；无自由路径、UID、脚本、命令参数。新目标若已存在或部分完成，拒绝并保留；不覆盖或修权限。所有 root 代码必须先用精确既有 bootstrap 和代码清单核验，不能从用户树直接 import。对新增目录和文件使用 FD 校验、无链接/硬链接/ACL、root:wheel 0755/0444。失败保留本批现场，不自动重试副作用。

验收：合成档案及临时目录验证正常入场、重复/缺失/额外成员、路径穿越、链接、摘要/源提交变化、输出覆盖和禁止阶段 B；原 exporter 定向回归。新代码可审查后按原任务要求进行独立只读安全复审，作者不改复审文件。真实原生流程和跨 UID 捕获继续标 not_run。有限阶段 A 仍需用户新授权；取得实际计划后阶段 B 单独审查，不借旧发布认证。

当前业务约束：录单正在继续；本任务不安排停服。新的有效隔离来源必须由批准后的受控流程取得；已保留的 v717 失败数据库缺 PDF，不能关闭训练样本来伪装完整来源。

## 离线结果

- v717 unsigned ZIP `55859eb61263cd66f02ee9edd753a07faa42414a3bbfe8250fcbc54614735f74`；manifest `7a1d41d10c6243afec6faf3cc80ec34f2e101d152ce77920642562c261584e73`。29602成员全量摘要校验，29595成员复用未变压缩内容，7运行模块变化/4新增/0删除；业务、前端、schema、reader及依赖保持v717。
- 管理员 ZIP `cd4279cc867649c81523cf3138d652c09656a16230de5d5383b8ccec7eed8556`；清单 `facc089e18a349604729af3105a816977ebc0f7e5221848b180a2fd1a1da8840`。新准入解析器实际校验25源码加清单，并与冻结提交逐字节比对通过。
- 383项不同定向测试通过，含36项新准入拒绝/留存测试。管理员构建回归初次213通过、1项Apple codesign受沙箱限制；该只读系统解释器验证单独解除沙箱重跑1通过。原失败日志保留，不冒充首轮全绿。
- Apple Python3.9语法和实际管理员模块纯导入通过；无批准launcher的直接用户态执行在读取受保护现场前拒绝。
- 独立只读复核无可报告缺陷、无静态阻断项，详细结论由复核者独占写入 evidence/erp3-next-20261023/controlled-export-v717/independent-review.json。它不代表原生验收。

最小下一步：用户先批准本次新增阶段A范围，再运行已冻结 authorize.applescript 并完成正常系统认证。读取本事务 stage-a-result.json、publication-result.json、export-plan-result.json 及实际计划SHA；确认来源仍为v717和原supervisor且captured=false。随后才对实际未过期计划申请阶段B批准。阶段A失败保留整个目标目录，不覆盖或自动重跑。未执行阶段A，故实际signed SHA/实际plan SHA均为null，当前没有新的可用隔离源。
