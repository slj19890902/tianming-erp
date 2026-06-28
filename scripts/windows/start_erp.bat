@echo off
setlocal EnableExtensions
chcp 65001 >nul
title Tianming ERP Starter

set "SCRIPT_DIR=%~dp0"
set "ROOT=%SCRIPT_DIR%..\.."
cd /d "%ROOT%"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ROOT%\scripts\windows\start_erp.ps1"

if errorlevel 1 (
    echo.
    echo ERP startup failed. Please check logs\erp_startup.log
    pause
    exit /b 1
)

exit /b 0
