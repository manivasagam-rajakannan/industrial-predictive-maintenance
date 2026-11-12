@echo off
setlocal
cd /d "%~dp0"
title Pump Telemetry Copilot - Docker

if not exist "docker-compose.yml" goto extract_first
docker info --format "{{.ServerVersion}}" >nul 2>&1
if errorlevel 1 goto docker_unavailable

echo Starting the Docker services. The browser opens when they are healthy.
docker compose up --build --wait --wait-timeout 180
if errorlevel 1 goto failed
start "" "http://127.0.0.1:8501/"
exit /b 0

:extract_first
echo Extract the whole ZIP using Extract All, then run this file in the extracted folder.
pause
exit /b 1

:docker_unavailable
echo Docker Desktop is unavailable. Start it, or run OPEN_OPERATOR_SCREEN.cmd.
pause
exit /b 1

:failed
docker compose ps --all
docker compose logs --tail 20
echo.
echo Review the startup error above. If you use the local launcher, stop this stack first with STOP_LIVE.cmd.
pause
exit /b 1
