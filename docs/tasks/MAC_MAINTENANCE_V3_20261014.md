# 第三版维护前置修复

状态：隔离开发验证中，尚未正式发布。未停止、备份或写入正式ERP。本地分支codex/erp-plan-v3-20261014基于已核实正式源61e5fafb；远端fetch失败的说明见总任务卡。

## 已实现闭环

- 原服务启动只等60秒，遇正常较慢启动会被提前停止。改为单调时钟300秒有界等待；只接受本次nonce的ready，子进程退出仍立即失败，不接受旧ready。真实Manager.start合成测试修复前3失败，修复后与原启动/暂停准入共16通过。
- 普通运行中的SMB完整备份，先预检，再停止、完成本地完整密文与认证、fsync密文和父目录，再由Manager恢复并等ready，之后进行独立上传和回读。原archive_stopped、暂停、接管与更新冷备契约保留。原管理锁、签名树绑定、文件Lease、独立连接、认证密文和state CAS未放宽。
- 在线传输末尾不再用已经继续营业的数据库和附件与备份时点比较；冻结密文、管理状态和回读仍必须匹配。回执仍描述捕获的恢复点，verified_at是验证完成时间。
- 本地构建失败时恢复原运行服务；恢复失败不开始网络、不在finally重复启动；普通NAS失败保持已恢复ERP运行。停用在capture期间出现则不恢复；在传输期间出现则返回时仅停止本次恢复的同一进程，使用原身份/nonce和正常退出规则。
- 提取prepare_migration，在备份/停服前校验实际签名迁移树、目标revision、既存固定数据库、入口哈希、隔离解释器和环境；真正执行前再次校验。原生Mac迁移用独立环境，不调用当前server-only的_environment门禁；Windows环境行为保持。没有结构迁移/DDL。

## 验证与边界

新增迁移预检用例修复前3失败；真实原生Mac子进程、签名迁移树准入、跨版本演练/恢复和数量事实原有门禁继续验证。合成Windows迁移测试保留路径/签名/哈希检查，仅对不会实际启动的字节夹具跳过宿主平台匹配；不因此修改产品门禁。

普通备份完整路由Manager→target→receipt→builder已验证preflight→stop→start ready→transfer；nightly同路由，update和迁移链明确验证transfer时停止。传输中修改合成数据库同一行值（行数不变）和附件，恢复包保留旧时点，在线数据保留新值；邮箱凭据只允许合法重新包装，业务字段与版本/游标完整比较。原密文污染、回读污染、state冲突、恢复失败、NAS失败、停用时序有专用用例。

已完成组合153项通过：test_migration_before_stop、test_native_migration_process、test_cross_revision、test_update_chain、test_backup_resume_before_network、test_mac_smb_stream_target、test_mac_backup_storage。此前原builder/receipt/target/pause/nightly组合132通过、1新测试因正常凭据重包装比较过严失败，已修正测试并纳入后续通过组。新增fsync失败用例另行复验。非全量全绿声明。

独立GPT-6 Astra只读审查提出晚停用及fsync两点，均已补代码和回归；最终diff复核未发现新增阻断。追加fsync失败测试和原builder/receipt/pause/受保护发布树回归组合156通过。全部使用合成私有临时库，正式health只读返回ok。

## 尚未完成、不能混称已解决

- 当前本地打包/加解密仍在停服窗口内；正式数据量下尚未计时，不能承诺几秒完成。网络阶段已移出普通备份停服窗口。
- 传输仍持有维护锁；进程意外退出时监护重启和中途停用可能等待网络任务。晚停用返回时修正不代表途中即时响应。
- _update_locked旧NAS更新仍走冷备；本项不能称正式发布长停服已解除。同schema候选应评估既有local_code_update短窗口，本地恢复证明与NAS独立设备证明明确区分；不得把在线后的旧演练当正式最新迁移事实。
- 新代码必须签名发布并绑定stream控制/monitor。单换app不能更新旧受保护worker。
- “仅留一份可恢复备份”尚未执行；新包独立恢复及回执/CAS成功后才能精确退役旧份。现有stream协议没有远端删除接口，不能通配删除或先删唯一旧份。
- NAS本次独立回执挂载无响应，本地回执保留待回写，不声称已成功上传。

本轮不执行旧combined维护脚本，不使用聊天密码，不关闭正式页面，不做自动页面点击验收。真实token用量不可获取，未估算。
