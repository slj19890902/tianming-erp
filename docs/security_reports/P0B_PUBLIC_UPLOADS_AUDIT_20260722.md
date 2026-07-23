# P0-B 家庭环境公开上传目录只读审计

## 审计边界

- 审计日期：2026-07-22
- 当前电脑：家庭开发/UAT 电脑，不是工厂正式主机
- 只读目标：`D:\纸箱厂erp软件搭建\static\uploads`
- 审计操作：只检查目录是否存在、文件类型、大小和名称；未创建、移动、修改或删除任何上传文件

## 结果

- 家庭正式目录副本中不存在 `static/uploads` 目录。
- 因目录不存在，本轮未发现异常类型、超大文件、主动内容或疑似数据库/密钥文件。
- 该结论只适用于家庭电脑副本，不能替代明早在工厂主机上的同项只读检查。
- 当前没有发现需要立即轮换会话密钥的文件泄露证据，因此本轮未修改或轮换任何密钥。

## 工厂主机复核命令

在工厂主机更新代码前，使用只读脚本检查真实目录：

```powershell
python scripts/audit/public_uploads_audit.py --uploads-dir "D:\纸箱厂erp软件搭建\static\uploads"
```

脚本没有删除或 apply 模式。若工厂输出含 `active_content`、`sensitive_type`、`oversized` 或 `signature_mismatch`，先保存报告并停止发布，不得擅自删除文件。
