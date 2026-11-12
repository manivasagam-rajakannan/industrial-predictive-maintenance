"""Manage the project-local PostgreSQL instance and its read-only Power BI login."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
from collections import deque
from importlib.util import find_spec
from pathlib import Path
from tempfile import TemporaryDirectory

import psycopg
from psycopg import sql

HOST = "127.0.0.1"
PORT = 55432
DATABASE = "pump_live"
OWNER = "pump_owner"
READER = "pump_reader"
VIEWS_SQL = Path(__file__).with_name("live_views.sql")


class LiveDatabase:
    def __init__(self, runtime: Path):
        self.runtime = runtime.resolve()
        self.data_dir = self.runtime / "postgres"
        self.log = self.runtime / "postgres.log"
        self.setup_log = self.runtime / "postgres-setup.log"
        package = find_spec("pgserver")
        if package is None or package.origin is None:
            raise RuntimeError("Install the local PostgreSQL package using OPEN_OPERATOR_SCREEN.cmd.")
        self.bin = Path(package.origin).parent / "pginstall" / "bin"
        self.credentials_path = self.runtime / "credentials.json"
        self.connection_details = self.runtime / "PowerBI_connection.txt"

    def _command(self, tool: str, *args: str) -> list[str]:
        extension = ".exe" if os.name == "nt" else ""
        executable = self.bin / f"{tool}{extension}"
        if not executable.is_file():
            raise RuntimeError(f"PostgreSQL runtime is incomplete. Missing {executable.name}.")
        return [str(executable), *args]

    def _command_error(self, message: str) -> RuntimeError:
        with self.setup_log.open(encoding="utf-8", errors="replace") as output:
            recent = "".join(deque(output, maxlen=12)).strip()
        return RuntimeError(
            f"{message}\n{recent}\nSetup log  {self.setup_log}\nDatabase log  {self.log}"
        )

    def _run(self, tool: str, *args: str, timeout: int = 45) -> None:
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            with self.setup_log.open("ab") as output:
                output.write(f"\nRunning {tool}\n".encode("ascii"))
                output.flush()
                # PostgreSQL can inherit the output handles after pg_ctl exits.
                result = subprocess.run(
                    self._command(tool, *args), stdin=subprocess.DEVNULL,
                    stdout=output, stderr=subprocess.STDOUT, timeout=timeout,
                    creationflags=flags,
                )
        except subprocess.TimeoutExpired as error:
            raise self._command_error(f"{tool} timed out after {timeout} seconds.") from error
        if result.returncode:
            raise self._command_error(f"{tool} exited with code {result.returncode}.")

    def is_running(self) -> bool:
        return (self.data_dir / "PG_VERSION").is_file() and subprocess.run(
            self._command("pg_ctl", "-D", str(self.data_dir), "status"),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).returncode == 0

    def _credentials(self) -> dict[str, str]:
        if self.credentials_path.is_file():
            return json.loads(self.credentials_path.read_text(encoding="utf-8"))
        values = {"owner": secrets.token_urlsafe(24), "reader": secrets.token_urlsafe(16)}
        descriptor = os.open(
            self.credentials_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(values, handle)
        return values

    def _initialize_cluster(self, password: str) -> None:
        if (self.data_dir / "PG_VERSION").is_file():
            return
        print("Initializing PostgreSQL files on first startup...", flush=True)
        with TemporaryDirectory(prefix="init-", dir=self.runtime) as folder:
            staging = Path(folder)
            pgdata = staging / "data"
            secret = staging / "password"
            secret.write_text(password + "\n", encoding="utf-8")
            self._run(
                "initdb", "-D", str(pgdata), "-U", OWNER,
                "--auth=scram-sha-256", "--encoding=UTF8", "--no-locale",
                f"--pwfile={secret}", timeout=120,
            )
            with (pgdata / "postgresql.conf").open("a", encoding="utf-8") as config:
                config.write(
                    f"\nlisten_addresses = '{HOST}'\nport = {PORT}\n"
                    "unix_socket_directories = ''\nmax_connections = 40\n"
                )
            pgdata.rename(self.data_dir)

    def _connect(self, database: str, user: str, password: str) -> psycopg.Connection:
        return psycopg.connect(
            host=HOST, port=PORT, dbname=database, user=user, password=password,
            connect_timeout=5, autocommit=True, sslmode="disable",
        )

    def start(self) -> dict[str, str]:
        self.runtime.mkdir(parents=True, exist_ok=True)
        passwords = self._credentials()
        self._initialize_cluster(passwords["owner"])
        if not self.is_running():
            print("Starting the PostgreSQL server...", flush=True)
            self._run("pg_ctl", "-D", str(self.data_dir), "-l", str(self.log),
                      "-w", "-t", "30", "start")
        print("Preparing the pump database...", flush=True)
        with self._connect("postgres", OWNER, passwords["owner"]) as conn:
            if conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (DATABASE,)).fetchone() is None:
                conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(DATABASE)))
            if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (READER,)).fetchone() is None:
                conn.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(READER), sql.Literal(passwords["reader"]),
                ))
        with self.writer(passwords) as conn:
            conn.execute(VIEWS_SQL.read_text(encoding="utf-8"))
        details = (
            f"Server      {HOST}:{PORT}\n"
            f"Database    {DATABASE}\n"
            f"Username    {READER}\n"
            f"Password    {passwords['reader']}\n"
        )
        self.connection_details.write_text(details, encoding="utf-8")
        return passwords

    def writer(self, passwords: dict[str, str]) -> psycopg.Connection:
        return self._connect(DATABASE, OWNER, passwords["owner"])

    def stop(self) -> None:
        if self.is_running():
            self._run("pg_ctl", "-D", str(self.data_dir), "-w", "-t", "30", "-m", "fast", "stop")
            print("The local database has stopped.")
        else:
            print("The local database is already stopped.")
