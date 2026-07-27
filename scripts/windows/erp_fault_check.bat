@echo off
chcp 65001 >nul
set "ERP_ROOT=%~dp0..\.."
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ERP_ROOT%\scripts\admin\rollback_erp.ps1"
echo.
pause
