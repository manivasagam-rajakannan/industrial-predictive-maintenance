"""Serve the pump dashboard and its internal API on a container host."""

import os
from pathlib import Path
import signal
import subprocess
import sys
from time import monotonic, sleep
from urllib.request import ProxyHandler, build_opener


ROOT = Path(__file__).resolve().parents[1]


def wait_for_feed(api, backend):
    http = build_opener(ProxyHandler({}))
    deadline = monotonic() + 75
    while monotonic() < deadline:
        if backend.poll() is not None:
            raise RuntimeError(f"Pump API exited with status {backend.returncode}.")
        try:
            with http.open(f"{api}/operator", timeout=3):
                return
        except OSError:
            sleep(0.5)
    raise RuntimeError("The pump API could not read PostgreSQL during startup.")


def stop(process):
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def main():
    def on_stop(_signal, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, on_stop)
    port = int(os.environ.get("PORT", "10000"))
    api_port = 9001 if port == 9000 else 9000
    api = f"http://127.0.0.1:{api_port}"
    backend = subprocess.Popen([
        sys.executable, "-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1", "--port", str(api_port),
    ], cwd=ROOT)
    frontend = None
    try:
        wait_for_feed(api, backend)
        environment = {
            **os.environ,
            "PUMP_API_URL": api,
            "PUMP_MANUAL_DOWNLOAD": str(ROOT / "rag" / "pump_manual.pdf"),
        }
        frontend = subprocess.Popen([
            sys.executable, "-m", "streamlit", "run", "frontend/app.py", "--server.address=0.0.0.0",
            f"--server.port={port}", "--server.headless=true", "--server.maxUploadSize=1",
        ], cwd=ROOT, env=environment)
        while backend.poll() is None and frontend.poll() is None:
            sleep(0.5)
        raise RuntimeError(f"Pump process exited. API status {backend.poll()}. Dashboard status {frontend.poll()}.")
    except KeyboardInterrupt:
        return 0
    finally:
        stop(frontend)
        stop(backend)


if __name__ == "__main__":
    raise SystemExit(main())
