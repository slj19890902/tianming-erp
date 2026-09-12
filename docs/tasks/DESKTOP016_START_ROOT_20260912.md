# DESKTOP016 助手直接双击使用正确安装目录

截图报C:/Users/Administrator/AppData/Local/TianmingERP/installer-release.zip不存在。实际进程为D:/TianmingERP/TianmingERP-Assistant.exe，无--root；桌面快捷方式有正确--root，说明直接打开EXE触发gui默认LOCALAPPDATA。D根已有完整包和备份设置，C根仅设置无业务state，均保留。

最小闭环：打包助手默认取EXE所在安装目录；显式--root继续优先，源码开发默认行为不变；缺离线包在目录选择/停服前给出明确恢复方法。单代理，独立codex/desktop016-start-root-20260912，基线ec3f0d60。允许gui.py、直接测试及task/version/docs；无DDL，不写业务数据或合并两个安装设置。回归覆盖直接启动/快捷方式/计划任务/开发路径、缺包不触发接入、现有首次接入。与GROUPLOC001协调版本和完整EXE/Setup发布窗口，保留381更新。正常发布已有授权，保留签名/备份/完整性/唯一head/健康门禁。管理员手动首次接入验收。
