@echo off
setlocal EnableExtensions
chcp 65001 >nul
title Tianming ERP Starter

set "ROOT=%~dp0"
cd /d "%ROOT%"

call "%ROOT%scripts\windows\start_erp.bat"

if errorlevel 1 (
    echo.
    echo ERP startup failed. Please check logs\erp_startup.log
    pause
    exit /b 1
)

exit /b 0
