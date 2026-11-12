@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Pump Telemetry Copilot - Operator Screen

if not exist "backend\main.py" goto extract_first
if not exist "frontend\app.py" goto extract_first
if not exist "rag\pump_manual.pdf" goto extract_first
if not exist ".runtime" mkdir ".runtime"
if errorlevel 1 goto failed

set "PUMP_OPERATOR_VENV=.runtime\operator-venv"
set "PUMP_OPERATOR_PY=%PUMP_OPERATOR_VENV%\Scripts\python.exe"
if exist "%PUMP_OPERATOR_PY%" goto install_packages

set "PUMP_BOOT_PY="
py -3.12 -c "import platform,sys; assert sys.maxsize > 2**32 and platform.machine().lower() in ('amd64','x86_64')" >nul 2>&1
if not errorlevel 1 set "PUMP_BOOT_PY=py -3.12"
if defined PUMP_BOOT_PY goto system_python

py -3.11 -c "import platform,sys; assert sys.maxsize > 2**32 and platform.machine().lower() in ('amd64','x86_64')" >nul 2>&1
if not errorlevel 1 set "PUMP_BOOT_PY=py -3.11"
if defined PUMP_BOOT_PY goto system_python

python -c "import sys,platform; assert sys.version_info.major == 3 and 11 <= sys.version_info.minor <= 12 and sys.maxsize > 2**32 and platform.machine().lower() in ('amd64','x86_64')" >nul 2>&1
if not errorlevel 1 set "PUMP_BOOT_PY=python"
if defined PUMP_BOOT_PY goto system_python

set "PUMP_UV=.runtime\operator-tools\uv.exe"
if exist "%PUMP_UV%" goto managed_python

echo Downloading the project-local Python installer...
powershell.exe -NoLogo -NoProfile -NonInteractive -Command "$ErrorActionPreference='Stop'; $archive='.runtime\operator-uv.zip'; Invoke-WebRequest -UseBasicParsing -Uri 'https://github.com/astral-sh/uv/releases/download/0.13.7/uv-x86_64-pc-windows-msvc.zip' -OutFile $archive; if ((Get-FileHash -Algorithm SHA256 -LiteralPath $archive).Hash -ne '0cdc5b38fe5a8168ed6a25814f5ba43c21d313dd3187314fccb37b7cec46e021') { throw 'The downloaded Python installer did not match the published checksum.' }; Expand-Archive -LiteralPath $archive -DestinationPath '.runtime\operator-tools' -Force; Remove-Item -LiteralPath $archive"
if errorlevel 1 goto failed
if not exist "%PUMP_UV%" goto failed

:managed_python
echo Preparing Python 3.12 inside this project...
set "UV_PYTHON_INSTALL_DIR=%CD%\.runtime\operator-python"
set "UV_CACHE_DIR=%CD%\.runtime\operator-uv-cache"
set "UV_PYTHON_INSTALL_REGISTRY=0"
"%PUMP_UV%" venv --managed-python --seed --python cpython-3.12-windows-x86_64 "%PUMP_OPERATOR_VENV%"
if errorlevel 1 goto failed
goto install_packages

:system_python
echo Preparing the operator environment...
%PUMP_BOOT_PY% -m venv "%PUMP_OPERATOR_VENV%"
if errorlevel 1 goto failed

:install_packages
if exist "%PUMP_OPERATOR_VENV%\operator-ready" goto run
echo First start downloads the operator packages and local database runtime.
"%PUMP_OPERATOR_PY%" -m pip --version >nul 2>&1
if not errorlevel 1 goto pip_ready
"%PUMP_OPERATOR_PY%" -m ensurepip --upgrade
if errorlevel 1 goto failed

:pip_ready
"%PUMP_OPERATOR_PY%" -m pip install --disable-pip-version-check -r operator-requirements.txt
if errorlevel 1 goto failed
type nul > "%PUMP_OPERATOR_VENV%\operator-ready"

:run
"%PUMP_OPERATOR_PY%" -u operator_launcher.py
if errorlevel 1 goto failed
exit /b 0

:extract_first
echo Extract the whole ZIP using Extract All, then run this file in the extracted folder.
pause
exit /b 1

:failed
echo.
echo The operator screen could not start. The error above shows which step failed.
pause
exit /b 1
