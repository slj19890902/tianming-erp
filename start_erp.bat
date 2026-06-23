@echo off
setlocal EnableExtensions
set "ROOT=%~dp0"
cd /d "%ROOT%"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ROOT%scripts\admin\start_erp_background.ps1"
if errorlevel 1 (
  echo ERP startup failed. See logs\erp_autostart.log.
  pause
  exit /b 1
)

start "" "http://127.0.0.1:8000/"
exit /b 0
