"""Receive pump telemetry, score it, and queue a cited guide for every alert."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from threading import Event, Thread
from typing import Annotated, Literal
from uuid import UUID, uuid4

import numpy as np
import pandas as pd
import psycopg
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from backend.database import Database, EventConflict
from model.pump_pipeline import CALIBRATION_END, FAULTS, FEATURES, fit_model, score_readings, simulate_telemetry
from rag.rag_engine import MANUAL, RagEngine

LOGGER = logging.getLogger(__name__)
FaultType = Literal["Bearing wear", "Overheating", "Cavitation"]
SENSOR_LIMIT = float(np.finfo(np.float32).max)
SensorValue = Annotated[float, Field(ge=-SENSOR_LIMIT, le=SENSOR_LIMIT)]


class SensorReading(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    pump_id: Literal["PUMP_UNIT_01"] = "PUMP_UNIT_01"
    timestamp: AwareDatetime | None = None
    event_id: UUID | None = None
    vibration_mm_s: SensorValue
    temperature_c: SensorValue
    pressure_bar: SensorValue
    motor_current_amp: SensorValue


class FaultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fault: FaultType = "Overheating"


class PumpService:
    def __init__(self):
        self.database = Database()
        self.database.initialize()
        telemetry = simulate_telemetry()
        self.model = fit_model(telemetry)
        scored = score_readings(telemetry.iloc[CALIBRATION_END:], self.model)
        self.nominal = scored.loc[
            scored["injected_fault"].eq("Nominal") & scored["anomaly_flag"].eq(0), FEATURES
        ].to_dict("records")
        self.fault_samples = {}
        for fault in FAULTS:
            examples = scored.loc[
                scored["injected_fault"].eq(fault) & scored["suspected_fault"].eq(fault)
            ].nlargest(1, "fault_score")
            self.fault_samples[fault] = examples[FEATURES].iloc[0].to_dict()
        self.rag = RagEngine()
        self.simulator_enabled = os.environ.get("PUMP_SIMULATOR_ENABLED", "1").lower() in {
            "1", "true", "yes"
        }
        self.stop = Event()

    def ingest(self, reading: SensorReading, source: str):
        return self.ingest_many([reading], source)[0]

    def ingest_many(self, readings: list[SensorReading], source: str):
        values = [{feature: getattr(reading, feature) for feature in FEATURES} for reading in readings]
        scores = score_readings(pd.DataFrame(values, columns=FEATURES), self.model)
        now = datetime.now(UTC)
        records = []
        for reading, channels, score in zip(readings, values, scores.itertuples(), strict=True):
            source_time = (reading.timestamp or now).astimezone(UTC).replace(tzinfo=None)
            record = {
                **channels,
                "event_id": reading.event_id or uuid4(),
                "source_time": source_time,
                "pump_id": reading.pump_id,
                "fault_score": float(score.fault_score),
                "alert_cutoff": float(score.alert_cutoff),
                "anomaly_flag": int(score.anomaly_flag),
                "suspected_fault": score.suspected_fault,
                "review_cue": score.review_cue,
                "source": source,
            }
            records.append((record, reading.timestamp is not None))
        return self.database.save_readings(records)

    def simulator(self):
        index = 0
        while not self.stop.is_set():
            reading = SensorReading(**self.nominal[index])
            try:
                self.ingest(reading, "demo_stream")
            except psycopg.OperationalError:
                LOGGER.warning("The simulated feed is waiting for PostgreSQL.")
                self.stop.wait(2)
                continue
            index = (index + 1) % len(self.nominal)
            self.stop.wait(1)

    def guidance_worker(self):
        while not self.stop.is_set():
            try:
                job = self.database.claim_guidance()
                if job is None:
                    self.stop.wait(0.5)
                    continue
                try:
                    result = self.rag.advise(job)
                except Exception:
                    LOGGER.exception("Manual guidance failed for reading %s.", job["reading_id"])
                    result = None
                self.database.finish_guidance(job["reading_id"], job["claim_token"], result)
            except psycopg.OperationalError:
                LOGGER.warning("The guidance worker is waiting for PostgreSQL.")
                self.stop.wait(2)


@asynccontextmanager
async def lifespan(app: FastAPI):
    service = PumpService()
    app.state.pump = service
    workers = [Thread(target=service.guidance_worker, name="guidance", daemon=True)]
    if service.simulator_enabled:
        workers.append(Thread(target=service.simulator, name="simulator", daemon=True))
    for worker in workers:
        worker.start()
    try:
        yield
    finally:
        service.stop.set()
        for worker in workers:
            worker.join(timeout=15)
        if service.rag.client is not None:
            service.rag.client.close()


app = FastAPI(
    title="Pump Telemetry Copilot",
    description="Live sensor ingestion and manual guidance for a simulated pump.",
    version="1.0.0",
    lifespan=lifespan,
)


@app.exception_handler(psycopg.OperationalError)
async def database_unavailable(request: Request, error: psycopg.OperationalError):
    return JSONResponse(status_code=503, content={"detail": "PostgreSQL is temporarily unavailable."})


@app.exception_handler(RequestValidationError)
async def invalid_reading(request: Request, error: RequestValidationError):
    details = [
        {key: item[key] for key in ("loc", "msg", "type")}
        for item in error.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": details})


@app.get("/health")
def health(request: Request):
    service = request.app.state.pump
    service.database.ping()
    return {
        "status": "ok",
        "groq_configured": service.rag.client is not None,
        "simulator_enabled": service.simulator_enabled,
    }


@app.post("/readings", status_code=201)
def create_reading(payload: SensorReading, request: Request, response: Response):
    service = request.app.state.pump
    try:
        reading, replayed = service.ingest(payload, "api")
    except EventConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    if replayed:
        response.status_code = 200
    return reading


@app.post("/readings/batch", status_code=201)
def create_batch(payload: Annotated[list[SensorReading], Field(min_length=1, max_length=200)],
                 request: Request, response: Response):
    try:
        results = request.app.state.pump.ingest_many(payload, "csv_upload")
    except EventConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    readings = [reading for reading, _ in results]
    replayed = sum(replay for _, replay in results)
    if replayed == len(readings):
        response.status_code = 200
    return {"created": len(readings) - replayed, "replayed": replayed, "readings": readings}


@app.get("/readings/{reading_id}")
def get_reading(reading_id: int, request: Request):
    record = request.app.state.pump.database.reading(reading_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Reading not found.")
    return record


@app.post("/simulate/fault", status_code=201)
def simulate_fault(payload: FaultRequest, request: Request):
    service = request.app.state.pump
    reading = SensorReading(**service.fault_samples[payload.fault])
    result, _ = service.ingest(reading, "demo_fault")
    return result


@app.get("/operator")
def operator_snapshot(request: Request):
    service = request.app.state.pump
    return {
        **service.database.operator_snapshot(),
        "groq_configured": service.rag.client is not None,
        "simulator_enabled": service.simulator_enabled,
    }


@app.get("/manual")
def training_manual():
    return FileResponse(MANUAL, media_type="application/pdf", filename=None)
