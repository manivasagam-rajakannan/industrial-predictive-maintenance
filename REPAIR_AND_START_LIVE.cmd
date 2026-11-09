@echo off
setlocal
title Maintenance Watch - Start live Power BI
cd /d "%~dp0"
if not exist "compose.yaml" goto extract
if not exist "Dockerfile" goto extract
where docker.exe >nul 2>&1
if errorlevel 1 goto no_docker
set "MW_REPAIR_DONE="
set "MW_COMPOSE_LOG="

echo Maintenance Watch live launcher v2
echo Checking Docker Desktop...
docker container ls --quiet >nul 2>&1
if not errorlevel 1 goto start_project

:repair_docker
set "MW_REPAIR_DONE=1"
echo.
echo Repairing Docker Desktop startup. This closes any open WSL sessions.
echo Save work in your Linux terminals, then press a key to continue.
pause >nul
docker desktop stop --timeout 60 >nul 2>&1
echo Updating Windows Subsystem for Linux...
wsl.exe --update
if errorlevel 1 goto wsl_failed
wsl.exe --shutdown
if errorlevel 1 goto wsl_failed
echo.
echo Starting Docker Desktop...
docker desktop start --timeout 120
echo Waiting for the Docker engine...
set "MW_ENGINE_ATTEMPTS=0"

:wait_for_engine
docker container ls --quiet >nul 2>&1
if not errorlevel 1 goto start_project
set /a MW_ENGINE_ATTEMPTS+=1 >nul
if %MW_ENGINE_ATTEMPTS% geq 15 goto startup_failed
timeout /t 2 /nobreak >nul
goto wait_for_engine

:start_project
echo.
echo Starting the Maintenance Watch database and monitor...
echo The first build may take a few minutes.
set "MW_COMPOSE_LOG=%TEMP%\MaintenanceWatch-compose-%RANDOM%-%RANDOM%.log"
docker compose up --build -d >"%MW_COMPOSE_LOG%" 2>&1
set "MW_COMPOSE_RESULT=%ERRORLEVEL%"
type "%MW_COMPOSE_LOG%"
if "%MW_COMPOSE_RESULT%"=="0" goto wait_for_monitor

findstr /I /L /C:"Docker Desktop is unable to start" /C:"Cannot connect to the Docker daemon" /C:"open //./pipe/dockerDesktopLinuxEngine" /C:"open //./pipe/docker_engine" "%MW_COMPOSE_LOG%" >nul
if errorlevel 1 goto compose_failed
if defined MW_REPAIR_DONE goto startup_failed
del "%MW_COMPOSE_LOG%" >nul 2>&1
set "MW_COMPOSE_LOG="
goto repair_docker

:wait_for_monitor
del "%MW_COMPOSE_LOG%" >nul 2>&1
set "MW_COMPOSE_LOG="
docker compose ps
echo Waiting for the monitor...
for /l %%i in (1,1,15) do (
    curl.exe --silent --fail --max-time 2 http://127.0.0.1:8000/api/health >nul 2>&1
    if not errorlevel 1 goto ready
    timeout /t 2 /nobreak >nul
)
echo The monitor did not become ready. Recent logs follow.
docker compose logs --tail 25 db monitor
goto failed

:ready
echo.
echo The monitor is ready at http://127.0.0.1:8000/
echo Power BI sign-in: Database, user maintenance_bi, password maintenance_local_reader.
echo For a custom database password, use the value configured before database initialization.
echo The page refresh instructions are in START_HERE.md.
start "" "%~dp0powerbi\MaintenanceWatch.pbip"
pause
exit /b 0

:extract
echo Right-click MaintenanceWatch.zip, choose Extract All, then open this launcher from the extracted folder.
goto failed

:no_docker
echo Windows could not find Docker Desktop. Open OPEN_POWER_BI_DEMO.pbip to use the built-in sample.
goto failed

:wsl_failed
echo.
echo WSL reported an error. Its current status follows.
wsl.exe --status
goto failed

:startup_failed
echo.
echo The Docker engine still could not start after the repair. Docker and WSL status follow.
docker desktop status
wsl.exe --status
goto failed

:compose_failed
echo.
echo Docker returned a project startup error above.

:failed
echo.
if defined MW_COMPOSE_LOG echo Compose output saved at "%MW_COMPOSE_LOG%".
echo You can open OPEN_POWER_BI_DEMO.pbip while the live data service is being set up.
echo Keep this window open to capture the error and status.
pause
exit /b 1
