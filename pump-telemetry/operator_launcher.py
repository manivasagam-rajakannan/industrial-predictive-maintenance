"""Start the project's database, API and operator screen on the local PC."""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import webbrowser
from collections import deque
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

import fasteners
import psycopg

from model.live_db import HOST, PORT, LiveDatabase

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / ".runtime"
PORTS_PATH = RUNTIME / "operator-ports.json"
API_PORT = 8000
SCREEN_PORT = 8501
LOCAL_HTTP = build_opener(ProxyHandler({}))


def address(port: int) -> str:
    return f"http://{HOST}:{port}"


def response_from(url: str, *, timeout: float = 1, limit: int = 2048) -> str:
    try:
        with LOCAL_HTTP.open(url, timeout=timeout) as response:
            return response.read(limit).decode("utf-8", errors="replace")
    except OSError:
        return ""


def api_ready(port: int) -> bool:
    api = address(port)
    try:
        health = json.loads(response_from(f"{api}/health"))
        if (
            not isinstance(health, dict)
            or health.get("status") != "ok"
            or not {"simulator_enabled", "groq_configured"} <= health.keys()
        ):
            return False
        snapshot = json.loads(response_from(f"{api}/operator", timeout=4, limit=1024 * 1024))
        fields = {"feed", "latest", "timeline", "alerts", "simulator_enabled"}
        return isinstance(snapshot, dict) and fields <= snapshot.keys()
    except ValueError:
        return False


def screen_ready(port: int) -> bool:
    return response_from(f"{address(port)}/_stcore/health").strip().lower() == "ok"


def choose_port(preferred: int, service: str) -> int:
    with socket.socket() as candidate:
        if os.name != "nt":
            candidate.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            if not port_in_use(preferred):
                candidate.bind((HOST, preferred))
                return preferred
        except OSError:
            pass
        candidate.bind((HOST, 0))
        port = candidate.getsockname()[1]
    print(f"Port {preferred} is unavailable. Using port {port} for {service}.")
    return port


def active_ports() -> tuple[int, int] | None:
    try:
        ports = json.loads(PORTS_PATH.read_text(encoding="utf-8"))
        api_port, screen_port = ports["api"], ports["screen"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if any(type(port) is not int or not 1 <= port <= 65535 for port in (api_port, screen_port)):
        return None
    if api_ready(api_port) and screen_ready(screen_port):
        return api_port, screen_port
    return None


def port_in_use(port: int) -> bool:
    with socket.socket() as connection:
        connection.settimeout(0.35)
        return connection.connect_ex((HOST, port)) == 0


def log_tail(path: Path) -> str:
    with path.open(encoding="utf-8", errors="replace") as file:
        return "".join(deque(file, maxlen=12)).strip()


def start_service(module: str, args: list[str], env: dict[str, str], log: Path):
    with log.open("w", encoding="utf-8") as file:
        return subprocess.Popen(
            [sys.executable, "-m", module, *args],
            cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
            stdout=file, stderr=subprocess.STDOUT,
        )


def wait_for_service(label, process, ready, log: Path, timeout: float):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"{label} stopped during startup.\n{log_tail(log)}")
        if ready():
            return
        time.sleep(0.35)
    raise RuntimeError(f"{label} did not become ready. See {log}.\n{log_tail(log)}")


def stop_service(process):
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def open_screen(api_port: int, screen_port: int, no_browser: bool):
    screen = address(screen_port)
    print(f"Scoring API  {address(api_port)}")
    print(f"Operator screen ready  {screen}")
    if no_browser:
        return
    try:
        opened = webbrowser.open(screen, new=2)
    except (OSError, webbrowser.Error):
        opened = False
    if not opened:
        print("Open the address above in your browser.")


def run_operator(no_browser: bool) -> int:
    PORTS_PATH.unlink(missing_ok=True)
    if api_ready(API_PORT) and screen_ready(SCREEN_PORT):
        print("The operator screen is already running.")
        open_screen(API_PORT, SCREEN_PORT, no_browser)
        return 0

    database = LiveDatabase(RUNTIME / "operator-database")
    backend = frontend = None
    start_database = False
    try:
        if not database.is_running() and port_in_use(PORT):
            raise RuntimeError(
                f"PostgreSQL port {PORT} is already in use. If an earlier pump replay or "
                "Docker stack is running, stop it using its own STOP_LIVE.cmd and retry."
            )

        print("Starting PostgreSQL...")
        start_database = True
        credentials = database.start()
        api_port = choose_port(API_PORT, "the scoring API")
        env = os.environ.copy()
        no_proxy = ",".join(filter(None, [env.get("NO_PROXY"), env.get("no_proxy"), HOST, "localhost"]))
        env.update({
            "PUMP_DB_HOST": HOST,
            "PUMP_DB_PORT": str(PORT),
            "PUMP_DB_OWNER_PASSWORD": credentials["owner"],
            "PUMP_DB_READER_PASSWORD": credentials["reader"],
            "PUMP_API_URL": address(api_port),
            "PUMP_PUBLIC_API_URL": address(api_port),
            "NO_PROXY": no_proxy,
            "no_proxy": no_proxy,
            "PYTHONUNBUFFERED": "1",
            "OMP_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1",
        })

        print("Starting the scoring API...")
        backend_log = RUNTIME / "operator-backend.log"
        backend_args = ["backend.main:app", "--host", HOST, "--port", str(api_port)]
        if (ROOT / ".env").is_file():
            backend_args.extend(["--env-file", str(ROOT / ".env")])
        backend = start_service("uvicorn", backend_args, env, backend_log)
        wait_for_service("FastAPI", backend, lambda: api_ready(api_port), backend_log, timeout=75)

        screen_port = choose_port(SCREEN_PORT, "the operator screen")
        print("Starting the operator screen...")
        frontend_log = RUNTIME / "operator-screen.log"
        frontend = start_service("streamlit", [
            "run", "frontend/app.py", f"--server.address={HOST}",
            f"--server.port={screen_port}", "--server.headless=true",
        ], env, frontend_log)
        wait_for_service("Streamlit", frontend, lambda: screen_ready(screen_port), frontend_log, timeout=45)
        pending_ports = PORTS_PATH.with_suffix(".tmp")
        pending_ports.write_text(json.dumps({"api": api_port, "screen": screen_port}), encoding="utf-8")
        pending_ports.replace(PORTS_PATH)
        open_screen(api_port, screen_port, no_browser)
        print(f"BI connection details  {database.connection_details}")
        print("Keep this window open. Press Ctrl+C to stop the operator screen.")

        while backend.poll() is None and frontend.poll() is None:
            time.sleep(0.75)
        if backend.poll() is not None:
            raise RuntimeError(f"FastAPI stopped. See {backend_log}.\n{log_tail(backend_log)}")
        raise RuntimeError(f"Streamlit stopped. See {frontend_log}.\n{log_tail(frontend_log)}")
    finally:
        stop_service(frontend)
        stop_service(backend)
        if start_database and database.is_running():
            database.stop()
        PORTS_PATH.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-browser", action="store_true", help="Print the ready URL without opening a browser.")
    args = parser.parse_args()
    RUNTIME.mkdir(parents=True, exist_ok=True)
    lock = fasteners.InterProcessLock(str(RUNTIME / "operator.lock"))
    try:
        if not lock.acquire(blocking=False):
            ports = active_ports()
            if ports is None and api_ready(API_PORT) and screen_ready(SCREEN_PORT):
                ports = API_PORT, SCREEN_PORT
            if ports is not None:
                open_screen(*ports, no_browser=args.no_browser)
                return 0
            raise RuntimeError("The operator screen is starting in another window. Watch that window for its status.")
        try:
            return run_operator(args.no_browser)
        finally:
            lock.release()
    except KeyboardInterrupt:
        print("\nOperator screen stopped.")
        return 0
    except (RuntimeError, OSError, psycopg.Error) as error:
        print(f"\nOperator startup error\n{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
