# DESKTOP015 首次接入目录链接检查

老板反馈首次接入“原数据含目录链接，需先核对真实位置”。只读发现唯一链接为原ERP data/work/d1_confirmation_worklist_20260820/node_modules，指向Codex运行时依赖；助手在正常停服后才扫描全部数据目录，从而因非业务开发依赖中断复制。D:/TianmingERP已有备份设置及before_stop已验证备份，无已发布shared/state/managed marker。与379发布任务串行协调原服务恢复，不抢占锁、不覆盖配置。

最小闭环：开发工作目录中的node_modules依赖不纳入原数据复制，原本backups/__pycache__忽略策略保持；扫描和复制共用规则，在停服前完成链接检查，业务链接仍拒绝并给出相对位置。保留原文件与链接、业务数据库、附件、密码、状态、签名、版本、进程身份、幂等及事务门禁。复制过程也复核，失败不得发布不完整shared。预检实测与定向隔离回归后发布完整助手/Setup及签名ERP包，再更新用户指定D:/TianmingERP且保留设置，由管理员首次接入验收。

单代理、唯一写入人本任务。基线实时origin/factory-current-baseline ae07bdda；379候选窗口由原任务占用，整合其完成版后顺延380。允许desktop_assistant/import_existing.py、onboarding.py和必要直接辅助/测试、version/任务/指南/回执。无DDL、无业务补数、不删除外部链接、不跟随未核实业务链接。部署安全门禁按常规长期授权与执行章程执行。状态：定位完成，修复中。
