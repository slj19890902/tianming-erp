# 邮箱 PDF 人工录单兜底交付（2026-09-11）

任务卡：`docs/tasks/MAIL006_PDF_MANUAL_FALLBACK_20260911.md`。

## 实现

- PDF 识别失败或未识别到明细时显示“按原 PDF 人工录入”。
- 操作员选择正式客户，填写客户单号、日期以及每行存货编码、名称、规格、数量和单价，再由后端匹配该客户的正式常用箱。
- 后端只接受原预览签名中的文件名和 SHA256，丢弃客户端伪造的产品 ID、新产品标记和完整性状态；人工录入完成后签发 `manual_confirmed` 状态。
- 保存继续执行客户范围、整数数量、单价、常用箱、生产提醒、库存、重复订单、幂等、事务和安全覆盖审计。
- 无 migration，无正式业务数据回填。

## 验证

- PDF 订单、前端库存草稿及人工兜底相关 75 项通过。
- 邮箱收单、邮件订单关联、暂存恢复、发布元数据与发布安全相关 31 项通过。
- 安装恢复助手 38 项通过；签名包 29,546 个文件逐项校验，Assistant/Setup 自检均为 0，安装器内嵌程序与更新包同外部文件一致。
- 扩大到客户范围旧回归时，一项既有补库收料夹具因缺供应商返回 `SUPPLIER_REQUIRED`，与本次邮箱改动无关；本次客户范围仍由共用 `require_customer_access` 门禁执行。

## 正式发布

- 正式代码：`85667a4028aef1781fb7772d8cf4bf42285b18e0`
- 版本：`v0.22.356 / sp28v8x9z90`
- 发布报告：`docs/migration_reports/release_runtime_20260911_212632.json`，状态 `completed`
- 备份：`data/backups/carton_erp_before_release_20260911_212633.sqlite3`
- 发布前后正式库 SHA256：`b4a5e6891a372494b22b25c2cb4694ec7b99e9eeef5c552d155ea0869360a10a`
- 完整性 `ok`，外键异常 0，核心表数量一致。本机、局域网及公网健康检查均为 200；公网首页包含新入口与新接口。

## 安装恢复助手

- NAS 安装器：`Z:/sata1-18015598002/BoxERP/desktop-assistant/release-v356-20260911/TianmingERP-Setup.exe`
- 安装器 SHA256：`4f898fc1cad22f2d1baa7841bdd6f429aa082a77aac6f8c7b88ac681e41f64f2`
- 更新包 SHA256：`e2ee070ffede652c95320c8c6102889b9c78f62f00a5e6707a56e089ff879d07`
- 固定公钥指纹：`ce14118c44f5b84d45cc84cecbb8b70a6319ea2323b084577299a91b843c0cb2`
- NAS `releases/latest.json` 已指向 v356；v354 原包和更新索引备份保留。

## 人工验收

正式页面不执行自动点击。管理员从一封识别失败 PDF 进入人工录入，核对客户单号、正式常用箱、数量、单价和生产提醒后保存；备用电脑恢复、真实 UNC 夜间备份和 EPSON 实体打印仍按安装说明现场验收。
