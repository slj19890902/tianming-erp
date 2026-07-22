# 订单与产品图纸私有存储部署手册

## 1. 目标与边界

本变更不迁移数据库，也不改写历史 `drawing_file`、`image_path`、`thumbnail_path`。
数据库中的旧 URL 继续保持 `/static/uploads/drawings/<文件名>`，但正式
`app.main:app` 会将这些请求转交给登录及客户范围校验接口，不能再由
`StaticFiles` 匿名下载。

仅自动化测试允许使用项目树内的临时默认目录。正式机即使暂时仍标记为
`development`，也必须显式配置项目树和当前宽松 `D:\` 数据盘之外的专用可信边界：

- `C:\TianmingERPPrivate\drawings`
- `C:\TianmingERPPrivate\order_drafts`

历史正式图纸当前位于：

- `D:\纸箱厂erp软件搭建\static\uploads\drawings`

部署时只复制文件到新的 `drawings` 私有目录，不改写数据库 URL。生产模式默认不再
读取项目树内的旧物理目录；只有显式配置且整条祖先 ACL 均安全时，才允许设置
`ERP_LEGACY_DRAWING_DIR` 作为兼容根。

草稿不再写入 `static`，引用由服务端签名并绑定上传用户、用户
`auth_version`、过期时间和文件 SHA-256。订单提交成功后尽力删除草稿。

## 2. 上线门禁

正式部署前必须同时满足：

1. ERP 服务已停止，8000 端口已经关闭。
2. 已备份数据库、历史图纸、私有目录以及目录 ACL；备份能够读取。
3. 明确 ERP 服务实际使用的 Windows 账号，不得把普通用户组作为服务账号。
4. 私有可信边界、两个私有目录及其全部祖先不得是符号链接、目录联接或其他
   reparse point。
5. 可信边界和两个私有目录必须关闭 ACL 继承。
6. `Authenticated Users`、`Users`、`Everyone`、`Guests` 等宽泛主体不得拥有
   写入、修改、删除、改 ACL 或取得所有权权限。
7. 可信边界的全部祖先不得向宽泛主体授予 `DELETE_CHILD`、删除、改 ACL、取得
   所有权或完全控制；这可防止普通用户重命名或替换已经加固的私有根。
8. 除 `ERP_ENVIRONMENT=test` 外，启动都会只读检查私有根和整条祖先 ACL，
   不符合即拒绝启动；程序不会自动修改 ACL。正式机仍必须尽快切换到 `production`。
9. 静态 CSS、JS、HTML 可以匿名访问；所有旧图纸 URL 的匿名 GET/HEAD 必须返回
   401，同客户登录返回 200，越客户返回 403。
10. 草稿签名复用 ERP 会话密钥。当前项目树内默认
    `data\session_secret.key` 不能作为正式密钥；必须与登录/会话安全项 #18 一并
    验收，显式配置强随机 `ERP_SECRET_KEY`，或把 `ERP_SECRET_KEY_FILE` 放到项目树外
    且使用同等级 ACL 保护。密钥轮换会同时使旧登录会话和未消费草稿失效，轮换
    时间、通知和回退方案必须先获批准。

## 3. 管理员手工初始化（命令模板，不由 Codex 自动执行）

请在工厂主机以管理员 PowerShell 执行。先把 `$ServiceAccount` 改成实际运行
ERP 的专用 Windows 账号；不要原样使用示例账号。

```powershell
$AppRoot = 'D:\纸箱厂erp软件搭建'
$ServiceAccount = "$env:COMPUTERNAME\BoxERPService"
$TrustRoot = 'C:\TianmingERPPrivate'
$PrivateDrawings = Join-Path $TrustRoot 'drawings'
$PrivateDrafts = Join-Path $TrustRoot 'order_drafts'
$LegacyDrawings = Join-Path $AppRoot 'static\uploads\drawings'
$AclBackup = Join-Path $AppRoot ('data\acl-backup-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))

New-Item -ItemType Directory -Force -Path $TrustRoot, $PrivateDrawings, $PrivateDrafts, $LegacyDrawings, $AclBackup
icacls $TrustRoot /save (Join-Path $AclBackup 'trust-root.acl') /t /c
icacls $PrivateDrawings /save (Join-Path $AclBackup 'private-drawings.acl') /t /c
icacls $PrivateDrafts /save (Join-Path $AclBackup 'private-drafts.acl') /t /c
icacls $LegacyDrawings /save (Join-Path $AclBackup 'legacy-drawings.acl') /t /c

icacls $TrustRoot /inheritance:r
icacls $TrustRoot /remove:g 'Authenticated Users' 'BUILTIN\Users' 'Everyone' 'BUILTIN\Guests'
icacls $TrustRoot /grant:r 'SYSTEM:(OI)(CI)F' 'BUILTIN\Administrators:(OI)(CI)F' "$ServiceAccount`:(OI)(CI)RX"

foreach ($Path in @($PrivateDrawings, $PrivateDrafts)) {
    icacls $Path /inheritance:r
    icacls $Path /remove:g 'Authenticated Users' 'BUILTIN\Users' 'Everyone' 'BUILTIN\Guests'
    icacls $Path /grant:r 'SYSTEM:(OI)(CI)F' 'BUILTIN\Administrators:(OI)(CI)F' "$ServiceAccount`:(OI)(CI)M"
}
```

接着以管理员身份做“只补缺失文件”的历史图纸复制；同名但 SHA-256 不同必须中止，
不得覆盖：

```powershell
Get-ChildItem -LiteralPath $LegacyDrawings -File | ForEach-Object {
    $Destination = Join-Path $PrivateDrawings $_.Name
    if (Test-Path -LiteralPath $Destination) {
        $SourceHash = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash
        $DestinationHash = (Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash
        if ($SourceHash -ne $DestinationHash) {
            throw "同名图纸内容冲突，已停止：$($_.Name)"
        }
    } else {
        Copy-Item -LiteralPath $_.FullName -Destination $Destination
    }
}
```

注意：两个私有目录需要服务账号 `Modify`，可信边界本身只需 `Read/Execute`。
不要给 `Authenticated Users` 恢复 `Modify`。不要设置 `ERP_LEGACY_DRAWING_DIR`，
除非安全人员已验证其从叶目录到卷根的整条 ACL。

## 4. 只读核验

```powershell
$Roots = @(
    'C:\TianmingERPPrivate',
    'C:\TianmingERPPrivate\drawings',
    'C:\TianmingERPPrivate\order_drafts'
)

foreach ($Path in $Roots) {
    Get-Item -LiteralPath $Path -Force | Select-Object FullName, Attributes, LinkType, Target
    icacls $Path
}
```

人工确认：

- `Attributes` 不含 `ReparsePoint`，`LinkType` 和 `Target` 为空。
- ACL 中没有普通登录用户组的 `M`、`W`、`F`、删除或 ACL 修改能力。
- 服务账号在两个私有目录有 `M`，在可信边界有 `RX`。
- 管理员和 SYSTEM 保留完全控制。
- 从可信边界父目录一直到卷根，普通用户不存在删除子目录、删除、改 ACL、取得
  所有权或完全控制能力。

## 5. 配置、启动与验收

建议显式配置：

```text
ERP_DRAWING_DIR=C:\TianmingERPPrivate\drawings
ERP_ORDER_DRAFT_DRAWING_DIR=C:\TianmingERPPrivate\order_drafts
ERP_PRIVATE_STORAGE_TRUST_ROOT=C:\TianmingERPPrivate
ERP_ORDER_DRAFT_TTL_SECONDS=3600
ERP_SECRET_KEY=<由管理员安全生成并保管的强随机密钥，不得写入 Git>
ERP_ENVIRONMENT=production
```

还必须满足项目已有的生产 HTTPS、可信代理、Host、Origin 和密钥配置。启动后按顺序
验收：

1. 匿名打开 `/static/index.html` 成功。
2. 匿名 GET/HEAD 任一历史图纸 URL 返回 401。
3. 同客户用户 GET/HEAD 返回 200，响应包含 `Cache-Control: private, no-store`。
4. 越客户用户返回 403；不存在或跨客户歧义文件返回 404。
5. 用户 A 上传草稿后，用户 B 不能预览或用于创建订单。
6. 用户 A 创建订单成功后，草稿物理文件被删除，正式图纸可经鉴权读取。
7. 停用账号或提升 `auth_version` 后，旧草稿引用失效。

## 6. 回退

应用回退前仍须停止 ERP。代码可回退到上一受保护版本；数据库无需反向迁移。
如需恢复 ACL，使用第 3 节生成的 ACL 备份，并在明确核对目标目录后由管理员运行
`icacls <目录父级> /restore <对应 .acl 文件>`。不要为方便回退而重新授予
`Authenticated Users: Modify`。保留私有目录和历史目录原件，禁止直接覆盖或删除。
