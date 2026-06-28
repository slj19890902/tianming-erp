@echo off
setlocal EnableExtensions
set "ROOT=%~dp0..\.."
cd /d "%ROOT%"
title 天明ERP启动中

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_erp.ps1"
if errorlevel 1 (
  echo ERP startup failed. 请把这个窗口截图发给管理员。
  pause
  exit /b 1
)

exit /b 0
