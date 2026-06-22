@echo off
setlocal EnableExtensions
chcp 65001 >nul

set "ROOT=%~dp0"
cd /d "%ROOT%"

if exist "%ROOT%.env" (
  for /f "usebackq eol=# tokens=1,* delims==" %%A in ("%ROOT%.env") do (
    if not "%%A"=="" set "%%A=%%B"
  )
)

if not defined ERP_ENVIRONMENT set "ERP_ENVIRONMENT=production"
if not defined ERP_DATABASE_PATH set "ERP_DATABASE_PATH=%ROOT%data\carton_erp.sqlite3"
if not defined ERP_BIND_HOST set "ERP_BIND_HOST=0.0.0.0"
if not defined ERP_PORT set "ERP_PORT=8002"
set "PYTHONUTF8=1"

if exist "%ROOT%.venv\Scripts\python.exe" (
  set "PYTHON_EXE=%ROOT%.venv\Scripts\python.exe"
) else if exist "%ROOT%venv\Scripts\python.exe" (
  set "PYTHON_EXE=%ROOT%venv\Scripts\python.exe"
) else (
  set "PYTHON_EXE=python"
)

if not exist "%ROOT%logs" mkdir "%ROOT%logs"
set "LOG_FILE=%ROOT%logs\erp_server.log"

echo [%date% %time%] Starting BoxERP >> "%LOG_FILE%"
echo [%date% %time%] Database: %ERP_DATABASE_PATH% >> "%LOG_FILE%"
"%PYTHON_EXE%" -m uvicorn app.main:app --host %ERP_BIND_HOST% --port %ERP_PORT% --workers 1 >> "%LOG_FILE%" 2>&1
set "EXIT_CODE=%ERRORLEVEL%"
echo [%date% %time%] BoxERP stopped with code %EXIT_CODE% >> "%LOG_FILE%"
exit /b %EXIT_CODE%
