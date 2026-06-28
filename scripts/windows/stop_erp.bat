@echo off
setlocal EnableExtensions
cd /d "%~dp0..\.."
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop_erp.ps1"
if errorlevel 1 (
  echo 关闭 ERP 失败，请把这个窗口截图发给管理员。
  pause
  exit /b 1
)
exit /b 0
