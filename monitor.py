"""Run the local monitoring dashboard, replay, and JSON ingestion endpoints."""

import argparse
import csv
from datetime import datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import logging
import os
from pathlib import Path
import re
from secrets import compare_digest
from threading import Event, Thread
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from scoring import FEATURES, INPUT_FIELDS, REVIEW_THRESHOLD, FailureModel, validate_features
from storage import COLUMNS, EventConflict, TelemetryStore, utc_now, utc_string


ROOT = Path(__file__).resolve().parent
LOGGER = logging.getLogger("maintenance-watch")
ASSET_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}\Z")
READING_FIELDS = {*INPUT_FIELDS, "event_id", "asset_id", "observed_at"}
SOURCES = {"live", "replay"}
WINDOWS = {15, 60, 1440}
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/dashboard.css": ("dashboard.css", "text/css; charset=utf-8"),
    "/dashboard.js": ("dashboard.js", "text/javascript; charset=utf-8"),
    "/dataset.js": ("dataset.js", "text/javascript; charset=utf-8"),
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
    "/app-icon-192.png": ("app-icon-192.png", "image/png"),
    "/app-icon-512.png": ("app-icon-512.png", "image/png"),
}


class RequestProblem(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def checked_id(value, field):
    if not isinstance(value, str) or not ASSET_ID.fullmatch(value):
        raise ValueError(f"{field} must be 1–64 characters using letters, numbers, dot, underscore, dash, or colon.")
    return value


def predict_reading(model, features):
    if not isinstance(features, dict) or features.keys() != INPUT_FIELDS:
        raise ValueError("Provide grade and the five sensor fields.")
    return model.predict(validate_features(features))


def checked_timestamp(value):
    if not isinstance(value, str):
        raise ValueError("observed_at must be an ISO 8601 timestamp with a timezone.")
    try:
        observed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("observed_at must be an ISO 8601 timestamp with a timezone.") from error
    if observed.utcoffset() is None or observed > utc_now() + timedelta(minutes=5):
        raise ValueError("observed_at must have a timezone and be within five minutes of the current clock or earlier.")
    return utc_string(observed)


def score_record(model, features, event_id, asset_id, source_mode, observed_at, ingested_at, failure=None, source_udi=None):
    score = model.predict(features)
    return {
        "event_id": event_id,
        "asset_id": asset_id,
        "source_mode": source_mode,
        "observed_at": observed_at,
        "ingested_at": ingested_at,
        **features,
        "model_score": score["model_score"],
        "review_flag": score["review_flag"],
        "observed_failure": failure,
        "source_udi": source_udi,
        "model_version": score["model_version"],
    }


def load_replay(path):
    with Path(path).open(newline="", encoding="utf-8") as handle:
        return [
            (
                checked_id(row["asset_id"], "asset_id"),
                validate_features({"grade": row["grade"], **{name: float(row[name]) for name in FEATURES}}),
                int(row["observed_failure"]),
                int(row["source_udi"]),
            )
            for row in csv.DictReader(handle)
        ]


def replay_records(model, rows, count, now, session, start=0, spacing=10):
    for offset in range(count):
        index = start + offset
        asset, features, failure, source_udi = rows[index % len(rows)]
        time = utc_string(now - timedelta(seconds=(count - offset - 1) * spacing))
        yield score_record(model, features, f"replay-{session}-{index}", asset, "replay", time, time, failure, source_udi)


def start_replay(model, store, rows, interval, stop):
    if len(rows) < 90:
        raise ValueError("Replay requires at least 90 fixtures.")
    session = uuid4().hex[:12]
    seeded = store.recent_count("replay", 15) == 0
    if seeded:
        now = utc_now() - timedelta(seconds=2)
        store.save(replay_records(model, rows, 90, now, session))

    def feed():
        index = 90 if seeded else 0
        pending = None
        while not stop.wait(interval):
            if pending is None:
                pending = list(replay_records(model, rows, 1, utc_now(), session, start=index))
            try:
                store.save(pending)
            except Exception as error:
                LOGGER.warning("Replay write failed (%s). Retrying on the next interval.", type(error).__name__)
            else:
                pending = None
                index += 1

    worker = Thread(target=feed, name="telemetry-replay", daemon=True)
    worker.start()
    return worker


class MonitorServer(ThreadingHTTPServer):
    def __init__(self, address, model, validation, store, token, default_source):
        super().__init__(address, MonitorHandler)
        self.model = model
        self.validation = validation
        self.store = store
        self.token = token
        self.default_source = default_source


class MonitorHandler(BaseHTTPRequestHandler):
    server_version = "MaintenanceWatch/1.0"

    def send_bytes(self, status, data, mime, headers=None):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, status, data):
        self.send_bytes(status, json.dumps(data, allow_nan=False).encode(), "application/json; charset=utf-8")

    def read_json(self, limit=4096):
        if self.headers.get_content_type() != "application/json":
            raise RequestProblem(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Use Content-Type: application/json.")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise RequestProblem(HTTPStatus.BAD_REQUEST, "Invalid Content-Length.") from error
        if not 0 < length <= limit:
            raise RequestProblem(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, f"Send one JSON object of at most {limit // 1024} KB.")
        try:
            data = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("Invalid JSON body.") from error
        if not isinstance(data, dict):
            raise ValueError("The request body must be a JSON object.")
        return data

    def query_filters(self, query):
        params = parse_qs(query)
        source = params.get("source", [self.server.default_source])[0]
        if source not in SOURCES:
            raise ValueError("source must be live or replay.")
        minutes = int(params.get("minutes", ["15"])[0])
        if minutes not in WINDOWS:
            raise ValueError("minutes must be 15, 60, or 1440.")
        asset = params.get("asset", [None])[0]
        return source, minutes, checked_id(asset, "asset") if asset else None

    def get_route(self):
        route = urlsplit(self.path)
        if route.path in STATIC_FILES:
            name, mime = STATIC_FILES[route.path]
            self.send_bytes(HTTPStatus.OK, (ROOT / "dashboard" / name).read_bytes(), mime)
        elif route.path == "/api/snapshot":
            mode, minutes, asset = self.query_filters(route.query)
            data = self.server.store.snapshot(mode, minutes, asset)
            self.send_json(HTTPStatus.OK, {
                **data,
                "source": mode,
                "minutes": minutes,
                "asset": asset,
                "now": utc_string(utc_now()),
                "model_version": self.server.model.version,
                "review_threshold": REVIEW_THRESHOLD,
                "validation": self.server.validation,
            })
        elif route.path == "/api/health":
            self.server.store.recent_count(self.server.default_source, 15)
            self.send_json(HTTPStatus.OK, {"status": "ready", "model_version": self.server.model.version})
        elif route.path == "/api/telemetry.csv":
            mode, minutes, asset = self.query_filters(route.query)
            rows, truncated = self.server.store.export(mode, minutes, asset)
            output = io.StringIO(newline="")
            writer = csv.DictWriter(output, fieldnames=COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
            self.send_bytes(HTTPStatus.OK, output.getvalue().encode("utf-8"), "text/csv; charset=utf-8", {
                "Content-Disposition": f'attachment; filename="maintenance-{mode}.csv"',
                "X-Export-Truncated": "true" if truncated else "false",
            })
        else:
            raise RequestProblem(HTTPStatus.NOT_FOUND, "This route is unavailable.")

    def post_route(self):
        path = urlsplit(self.path).path
        if path == "/api/score":
            self.send_json(HTTPStatus.OK, predict_reading(self.server.model, self.read_json()))
        elif path == "/api/score-batch":
            data = self.read_json(limit=32_768)
            readings = data.get("readings")
            if data.keys() != {"readings"} or not isinstance(readings, list) or not 1 <= len(readings) <= 100:
                raise ValueError("Provide 1 to 100 readings in the readings array.")
            predictions = []
            for features in readings:
                try:
                    predictions.append(predict_reading(self.server.model, features))
                except ValueError as error:
                    predictions.append({"error": str(error)})
            self.send_json(HTTPStatus.OK, {"predictions": predictions})
        elif path == "/api/readings":
            if not self.server.token:
                raise RequestProblem(HTTPStatus.SERVICE_UNAVAILABLE, "Configure MAINTENANCE_INGEST_TOKEN to enable external telemetry.")
            actual = self.headers.get("Authorization", "").encode()
            expected = f"Bearer {self.server.token}".encode()
            if not compare_digest(actual, expected):
                raise RequestProblem(HTTPStatus.UNAUTHORIZED, "Provide a valid bearer token.")
            data = self.read_json()
            if data.keys() != READING_FIELDS:
                raise ValueError("Provide event_id, asset_id, observed_at, grade, and the five sensor fields.")
            event_id = checked_id(data["event_id"], "event_id")
            if event_id.lower().startswith("replay-"):
                raise ValueError("The replay- event ID prefix is reserved for synthetic playback.")
            record = score_record(
                self.server.model, validate_features(data),
                event_id,
                checked_id(data["asset_id"], "asset_id"),
                "live", checked_timestamp(data["observed_at"]), utc_string(utc_now()),
            )
            (saved, created), = self.server.store.save([record])
            self.send_json(HTTPStatus.CREATED if created else HTTPStatus.OK, {"created": created, "reading": saved})
        else:
            raise RequestProblem(HTTPStatus.NOT_FOUND, "This route is unavailable.")

    def run_route(self, route):
        try:
            route()
        except RequestProblem as error:
            self.send_json(error.status, {"error": str(error)})
        except EventConflict as error:
            self.send_json(HTTPStatus.CONFLICT, {"error": str(error)})
        except ValueError as error:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as error:
            LOGGER.error("Request failed (%s).", type(error).__name__)
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "The monitoring service could not complete this request."})

    def do_GET(self):
        self.run_route(self.get_route)

    def do_POST(self):
        self.run_route(self.post_route)

    def log_request(self, code="-", size="-"):
        if str(code).startswith("5"):
            LOGGER.warning("%s returned %s", self.command, code)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true", help="Replay held-out synthetic data on a separate source.")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", default=8000, type=int)
    parser.add_argument("--replay-interval", default=4.0, type=float)
    parser.add_argument("--db", default=ROOT / "runtime" / "telemetry.sqlite", type=Path)
    args = parser.parse_args()
    if not 0.25 <= args.replay_interval <= 60:
        parser.error("--replay-interval must be between 0.25 and 60 seconds.")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    model = FailureModel(ROOT / "artifacts" / "model.json")
    validation = json.loads((ROOT / "artifacts" / "validation.json").read_text(encoding="utf-8"))
    if validation["model_version"] != model.version:
        raise ValueError("The validation report belongs to a different model version.")
    store = TelemetryStore(args.db, os.getenv("MAINTENANCE_DATABASE_URL"))
    stop = Event()
    with MonitorServer(
        (args.bind, args.port), model, validation, store,
        os.getenv("MAINTENANCE_INGEST_TOKEN"), "replay" if args.demo else "live",
    ) as server:
        worker = start_replay(model, store, load_replay(ROOT / "data" / "replay.csv"), args.replay_interval, stop) if args.demo else None
        LOGGER.info("Maintenance Watch ready on http://%s:%s (model %s)", args.bind, server.server_port, model.version)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()
            if worker:
                worker.join(timeout=5)


if __name__ == "__main__":
    main()
