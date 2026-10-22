# 固定任务在线 UAT 副本导出候选

状态：独立候选开发；未认证、未导出、未改正式安装、未发布。不属于 v715，也不包含 fa1022df 事实迁移。

目的：为 DELIVERY-FULFILLMENT-FACTS-20261022 提供可核验的新鲜正式来源副本。起始源基线 c6af868826b2b2764949f1039e2b840c7407a5f7；发布负责人确认v715完成后已将候选rebase到80e2fff6b786d2bb460435d37d3786a01e8e45ff，业务代码无冲突。最终安装前仍须重新绑定实际应用和独立supervisor身份。

合同：只允许固定当前 console 身份 kimiyf / UID 501，输出到 `/Users/kimiyf/ERP-Migration/isolated-sources/delivery-fulfillment-20261022/<attempt>/`。不接受源路径、输出路径、UID、脚本或命令参数。复用受审查管理员代码包、固定签名管理 worker、已有维护锁；只读 SQLite online backup，业务进程持续运行。复制真实地图和固定数据库引用白名单内原件；仅在私有副本清除已知 ERP 凭据并重新生成紧凑库。源身份、原件和转换摘要必须回读一致，否则无成功交付。输出父目录先由接收用户准备，root 不改其 owner/mode；root 只在受保护 BASE 下本批新 0700 外层中验证并移交新 payload，再以原生 RENAME_EXCL 原子发布。

不变量：不读 Keychain/备份凭据/AI 配置，不停止或启动服务，不写正式业务库，不放宽普通管理动作，不更改正式 ACL/owner/mode；只修改本批新目录。凭据处理不等于业务 PII 匿名化，原业务资料仍为私有文件。缺价订单不因此继续收料。新能力和精确模块均须签名/管理员代码清单绑定；旧管理包不支持时明确拒绝。

允许文件：新增 `desktop_assistant/uat_export_contract.py`、`uat_export.py`、`mac_uat_export.py`；受控修改 `mac_service_manage.py`、`mac_keychain_install.py`、`build.py` 及定向测试/本任务文档。采用固定 plan/apply 入口，不新增通用 manage 动作。后端负责人 history_correction_review；preview_review 独占新增 `desktop_assistant/uat_export_sanitize.py` 和 `tests/desktop_assistant/test_uat_export_sanitize.py`，随后交叉安全复审，不并发写同一文件。

构建依赖补充：`scripts/uat/build_mac_keychain_installer.py` 的独立精确白名单必须同步这三个管理员模块，并补齐 bootstrap 原本已要求的 mac_publisher_trust.py；这是本候选能够构建完整受审查包的必要入口，不修改安装现场。

验证：隔离临时 SQLite 与假管理现场，覆盖未知凭据字段拒绝、凭据无空闲页残留、非凭据业务内容不变、并发快照、计划/身份/目录替换、链接/覆盖拒绝、原件变化失败、失败无公开完成目录、权限和包能力拒绝。正常 macOS 认证和真实副本验收保留为明确未完成发布门禁。

固定 home ACL 只允许已核验的 macOS everyone deny delete 单项，绑定其 SHA，不删除/修改原 ACL。服务 UID 只检查 console/pwd 和批准计划中的固定 ACL SHA，不穿透用户 home；管理员各阶段实际复核 ACL。

原件仅覆盖 product_drawings.image_path/thumbnail_path、pdf_order_training_samples.file_path、shared_file_versions.reference、finance_invoice_attachments.stored_name、supplier_monthly_invoices.attachment_stored_name。不是全量图纸恢复，未包括 drawing_releases、旧产品/订单/BOM 图纸字段，也未复制 twin 独立数据库。缺件/越界/配置非固定根/冻结hash或size不符都拒绝；没有“忽略缺件继续成功”的开关。

本目录为 raw git worktree，不受 Codex app 管理，不使用 archive_worktree 清理。
