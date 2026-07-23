#Requires -Version 5.1
<#
.SYNOPSIS
    已停用的旧一键更新入口。

.DESCRIPTION
    P0-A 起，普通更新入口不得自动拉取代码、迁移数据库或重启服务。
    正式发布必须使用 release_erp.ps1 的 Prepare / Apply 两阶段门禁。
#>

param([switch]$LibraryOnly)

$ErrorActionPreference = "Stop"

if ($LibraryOnly) { return }

Write-Error (
    "旧的一键更新入口已停用，未执行 Git、数据库或服务操作。" +
    "请使用 scripts\admin\release_erp.ps1：先 -Prepare，" +
    "核对隔离演练报告并取得绑定口令后，再 -Apply。"
)
exit 1
