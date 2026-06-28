@echo off
setlocal EnableExtensions
set "ROOT=%~dp0"
cd /d "%ROOT%"
title 天明ERP启动中

call "%ROOT%scripts\windows\start_erp.bat"
if errorlevel 1 (
  echo ERP startup failed. See logs\erp_autostart.log.
  pause
  exit /b 1
)

exit /b 0
