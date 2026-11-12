@echo off & goto :windows
r"""Launch Maintenance Watch with the database started by Pump Telemetry Copilot.

:windows
setlocal EnableExtensions DisableDelayedExpansion
title Maintenance Watch - Local Live Feed
set "PRED_MAINT_WATCH_LAUNCHER=%~f0"
powershell.exe -NoLogo -NoProfile -NonInteractive -Command "$source = Get-Content -LiteralPath $env:PRED_MAINT_WATCH_LAUNCHER -Raw; $match = [regex]::Match($source, '(?ms)^# BEGIN WATCH BOOTSTRAP\r?\n(.*?)^# END WATCH BOOTSTRAP\r?$'); if (-not $match.Success) { throw 'Download the complete Watch launcher again.' }; & ([scriptblock]::Create($match.Groups[1].Value))"
if errorlevel 1 goto failed
exit /b 0

:failed
echo.
pause
exit /b 1

# BEGIN WATCH BOOTSTRAP
$ErrorActionPreference = 'Stop'

function Test-Project([string] $folder) {
    return $folder -and (Test-Path -LiteralPath (Join-Path $folder 'maintenance-watch\monitor.py') -PathType Leaf) `
        -and (Test-Path -LiteralPath (Join-Path $folder 'telemetry_files.py') -PathType Leaf)
}

$launcher = $env:PRED_MAINT_WATCH_LAUNCHER
$project = [IO.Path]::GetDirectoryName($launcher)
if (-not (Test-Project $project)) {
    $projectsFolder = 'D:\Projects\Industrial Predictive Maintenance'
    $project = Join-Path $projectsFolder 'Industrial_Predictive_Maintenance_Apps'
    if (-not (Test-Project $project)) {
        $project = Get-ChildItem -LiteralPath $projectsFolder -Directory -Filter 'Industrial_Predictive_Maintenance_Apps*' -ErrorAction SilentlyContinue |
            Where-Object { Test-Project $_.FullName } |
            Sort-Object LastWriteTime -Descending |
            Select-Object -First 1 -ExpandProperty FullName
    }
}
if (-not (Test-Project $project)) {
    Write-Host 'Extract the project on D: or put this launcher beside the two app folders.'
    exit 1
}

# Find the interpreter from a running Pump copy, including a copy outside D:.
$localPython = Join-Path $project 'pump-telemetry\.runtime\operator-venv\Scripts\python.exe'
$running = @(Get-CimInstance Win32_Process -Filter 'Name = ''python.exe''' -OperationTimeoutSec 6 -ErrorAction SilentlyContinue |
    Where-Object { $_.ExecutablePath -like '*\.runtime\operator-venv\Scripts\python.exe' } |
    Select-Object -ExpandProperty ExecutablePath -Unique)
$candidates = @($running | Sort-Object { $_ -ne $localPython }) + @($localPython)
foreach ($python in $candidates) {
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { continue }
    $pump = (Get-Item -LiteralPath $python).Directory.Parent.Parent.Parent.FullName
    $database = Join-Path $pump '.runtime\operator-database\postgres\postmaster.pid'
    if ((Test-Path -LiteralPath $database -PathType Leaf) -and
        (Test-Path -LiteralPath (Join-Path $pump 'model\live_db.py') -PathType Leaf)) {
        $env:PRED_MAINT_WATCH_ROOT = $project
        $env:PRED_MAINT_PUMP_ROOT = $pump
        & $python -x $launcher
        exit $LASTEXITCODE
    }
}
Write-Host 'Start Pump Telemetry Copilot and wait for Operator screen ready, then open Watch again.'
exit 1
# END WATCH BOOTSTRAP
"""

import argparse
import json
import os
from pathlib import Path
import re
from secrets import token_urlsafe
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from urllib.request import ProxyHandler, build_opener
import webbrowser


ROOT = Path(os.environ.get("PRED_MAINT_WATCH_ROOT") or Path(__file__).resolve().parent).resolve()
PUMP = Path(os.environ.get("PRED_MAINT_PUMP_ROOT") or ROOT / "pump-telemetry").resolve()
WATCH = ROOT / "maintenance-watch"
RUNTIME = WATCH / "runtime" / "local-launch"
REPORT_NAME = "MaintenanceWatch.pbip"


def prepare_report(server, database):
    target = RUNTIME / "powerbi"
    report = target / REPORT_NAME
    if report.is_file():
        return report

    with TemporaryDirectory(dir=RUNTIME, prefix="report-") as temporary:
        staged = Path(temporary) / "powerbi"
        shutil.copytree(WATCH / "powerbi", staged, ignore=shutil.ignore_patterns(".pbi"))
        model_path = staged / "MaintenanceWatch.SemanticModel" / "model.bim"
        model = json.loads(model_path.read_text(encoding="utf-8"))
        expressions = {item["name"]: item for item in model["model"]["expressions"]}
        for name, value in {"WatchPostgresServer": server, "WatchPostgresDatabase": database}.items():
            parameter = expressions[name]
            metadata = parameter["expression"].split(" meta ", 1)[1]
            parameter["expression"] = f"{json.dumps(value)} meta {metadata}"
        model_path.write_text(json.dumps(model, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        staged.rename(target)
    return report


def write_private(path, content):
    temporary = path.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)
    temporary.replace(path)


def wait_for_watch(process, log):
    deadline = monotonic() + 30
    http = build_opener(ProxyHandler({}))
    while process.poll() is None and monotonic() < deadline:
        output = log.read_text(encoding="utf-8", errors="replace")
        match = re.search(r"Maintenance Watch ready on (http://127\.0\.0\.1:\d+)", output)
        if match:
            address = match.group(1)
            try:
                with http.open(address + "/api/health", timeout=2) as response:
                    if json.load(response).get("status") == "ready":
                        return address
            except (OSError, ValueError):
                pass
        sleep(0.25)
    raise RuntimeError(f"Maintenance Watch could not start. See {log}")


def run_watch(no_browser):
    if not (WATCH / "monitor.py").is_file() or not (ROOT / "telemetry_files.py").is_file():
        raise RuntimeError("Put this launcher in the extracted project beside both app folders.")

    try:
        import fasteners
        from psycopg import OperationalError, sql
        from psycopg.conninfo import make_conninfo
    except ModuleNotFoundError as error:
        raise RuntimeError(f"Pump setup still needs {error.name}. Finish opening Pump and try Watch again.") from error

    sys.path.insert(0, str(PUMP))
    from model.live_db import DATABASE, HOST, OWNER, PORT, READER, LiveDatabase

    database = LiveDatabase(PUMP / ".runtime" / "operator-database")
    if not database.is_running():
        raise RuntimeError("Open Pump Telemetry Copilot first and keep its launcher window open.")
    passwords = json.loads(database.credentials_path.read_text(encoding="utf-8"))
    RUNTIME.mkdir(parents=True, exist_ok=True)
    lock = fasteners.InterProcessLock(str(RUNTIME / "launcher.lock"))
    if not lock.acquire(blocking=False):
        raise RuntimeError("Maintenance Watch is already starting or running. Open its existing launcher window.")

    process = None
    try:
        server = f"{HOST}:{PORT}"
        report = prepare_report(server, DATABASE)
        token_path = RUNTIME / "ingest-token.txt"
        if not token_path.is_file():
            write_private(token_path, token_urlsafe(24) + "\n")
        token = token_path.read_text(encoding="utf-8").strip()

        # Both reports use the same local data source and Power BI login.
        # Reuse the Watch schema and grant the pump reader only the reporting view.
        schema = (WATCH / "storage_schema.sql").read_text(encoding="utf-8")
        view = (WATCH / "postgres" / "powerbi.sql").read_text(encoding="utf-8")
        try:
            with database.writer(passwords) as connection, connection.transaction():
                connection.execute(schema)
                reader = sql.Identifier(READER).as_string(connection)
                connection.execute(view.replace("TO maintenance_bi;", f"TO {reader};"))
        except OperationalError as error:
            raise RuntimeError("The Pump database is unavailable. Keep the Pump launcher running and try again.") from error

        environment = os.environ.copy()
        environment.pop("MAINTENANCE_BI_PASSWORD", None)
        environment.update({
            "MAINTENANCE_DATABASE_URL": make_conninfo(
                host=HOST, port=PORT, dbname=DATABASE, user=OWNER,
                password=passwords["owner"], sslmode="disable",
            ),
            "MAINTENANCE_INGEST_TOKEN": token,
        })
        log = RUNTIME / "maintenance-watch.log"
        with log.open("wb") as output:
            process = subprocess.Popen(
                [sys.executable, "-u", "monitor.py", "--bind", HOST, "--port", "0", "--demo"],
                cwd=WATCH, env=environment, stdout=output, stderr=subprocess.STDOUT,
            )
        address = wait_for_watch(process, log)
        connection_file = RUNTIME / "PowerBI_connection.txt"
        write_private(connection_file, "\n".join([
            f"Dashboard address    {address}",
            f"Server               {server}",
            f"Database             {DATABASE}",
            f"Username             {READER}",
            f"Password             {passwords['reader']}",
            f"Live ingest token    {token}",
            f"Power BI project     {report}",
            "",
        ]))
        print(f"Maintenance Watch ready  {address}", flush=True)
        print(f"Pump database folder  {PUMP}", flush=True)
        print(f"Power BI project  {report}", flush=True)
        print(f"Power BI login and upload token  {connection_file}", flush=True)
        print("Keep this window and the Pump launcher open. Press Ctrl+C to stop Maintenance Watch.", flush=True)
        if not no_browser:
            webbrowser.open(address, new=2)
        if process.wait():
            raise RuntimeError(f"Maintenance Watch stopped. See {log}")
        return 0
    except KeyboardInterrupt:
        return 0
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        lock.release()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Open Maintenance Watch on the running Pump database.")
    parser.add_argument("--no-browser", action="store_true")
    try:
        raise SystemExit(run_watch(parser.parse_args().no_browser))
    except (ImportError, OSError, RuntimeError, ValueError, KeyError) as error:
        print(f"Maintenance Watch startup error\n{error}", file=sys.stderr)
        raise SystemExit(1)
