"""Operator screen for the live pump feed and its manual guidance."""

from __future__ import annotations

import json
import os
from pathlib import Path
from secrets import compare_digest
from urllib.parse import urlsplit
from uuid import uuid4

import altair as alt
import pandas as pd
import requests
import streamlit as st
from reading_input import FORMATS, FORMAT_HELP, MAX_ROWS, SAMPLE_CSV, parse_readings

DEFAULT_API = "http://127.0.0.1:8000"
PORTS_PATH = Path(__file__).resolve().parents[1] / ".runtime" / "operator-ports.json"
MANUAL_PATH = os.environ.get("PUMP_MANUAL_DOWNLOAD")
IST = "Asia/Kolkata"
SENSORS = [
    ("Vibration", "vibration_mm_s", "mm/s", 2),
    ("Temperature", "temperature_c", "°C", 1),
    ("Pressure", "pressure_bar", "bar", 2),
    ("Motor current", "motor_current_amp", "A", 1),
]
REASONS = {
    "key_missing": "Using retrieved manual steps. Add a Groq API key to enable AI selection.",
    "provider_unavailable": "Groq is unavailable. Showing the retrieved manual steps.",
    "invalid_provider_response": "Showing the manual checklist after validating the Groq response.",
}

st.set_page_config(page_title="Pump Telemetry Copilot", page_icon="⚙", layout="wide")
st.markdown("""
<style>
[data-testid="stAppViewBlockContainer"] { max-width: 1390px; padding-top: 2.2rem; padding-bottom: 2rem; }
[data-testid="stHeader"] { background: transparent; }
.pump-banner { background: #102C3B; padding: 1.45rem 1.7rem; color: #fff; border-radius: 12px; margin-bottom: .85rem; }
.pump-banner h1 { color: #fff; font-size: clamp(1.7rem, 3.4vw, 2.6rem); margin: 0; padding: 0; line-height: 1.13; }
.pump-banner p { color: #BED9DD; margin: .45rem 0 0; font-size: .92rem; }
div[data-testid="stMetricValue"] { color: #07666D; font-weight: 700; }
div[data-testid="stMetric"] { background: #fff; min-height: 101px; padding: .8rem .9rem; }
div[data-testid="stVerticalBlockBorderWrapper"] { background: #fff; }
</style>
<div class="pump-banner">
<h1>Pump Telemetry Copilot</h1>
<p>PUMP_UNIT_01 &nbsp;·&nbsp; Operator monitor &nbsp;·&nbsp; India Standard Time</p>
</div>
""", unsafe_allow_html=True)


def api_addresses() -> tuple[str, str]:
    configured_api = os.environ.get("PUMP_API_URL", "").rstrip("/")
    public_api = os.environ.get("PUMP_PUBLIC_API_URL", "").rstrip("/")
    api = configured_api or DEFAULT_API
    if api == DEFAULT_API:
        try:
            port = json.loads(PORTS_PATH.read_text(encoding="utf-8"))["api"]
        except (OSError, ValueError, KeyError, TypeError):
            port = None
        if type(port) is int and 1 <= port <= 65535:
            api = f"http://127.0.0.1:{port}"
    if not public_api or public_api == configured_api:
        public_api = api
    return api, public_api


def request_api(api: str, method: str, path: str, *, timeout: float = 5, **kwargs):
    with requests.Session() as session:
        location = urlsplit(api)
        if location.scheme == "http" and location.hostname in {"127.0.0.1", "localhost", "::1"}:
            session.trust_env = False
        response = session.request(method, f"{api}{path}", timeout=timeout, **kwargs)
        response.raise_for_status()
        return response.json()


def show_connection_error(api: str, error: Exception):
    if isinstance(error, requests.HTTPError) and error.response is not None:
        detail = f"HTTP {error.response.status_code}"
    elif isinstance(error, requests.Timeout):
        detail = "request timed out"
    elif isinstance(error, requests.ConnectionError):
        detail = "connection failed"
    else:
        detail = "invalid response"
    st.error(
        f"Telemetry API at {api} is unavailable ({detail}). "
        "Open the address printed next to **Operator screen ready** in the launch window."
    )


def clock(value: str) -> str:
    return pd.Timestamp(value).tz_convert(IST).strftime("%H:%M:%S")


def submit_upload(content: bytes, filename: str, *, sample: bool):
    api, _ = api_addresses()
    st.session_state.pop("upload_result", None)
    try:
        readings = parse_readings(content, filename)
        if sample:
            for reading in readings:
                reading["event_id"] = str(uuid4())
        result = request_api(api, "POST", "/readings/batch", timeout=35, json=readings)
        st.session_state["upload_result"] = result
        flagged = [reading for reading in result["readings"] if reading["anomaly_flag"]]
        if flagged:
            st.session_state["selected_alert"] = flagged[-1]["reading_id"]
    except requests.HTTPError as error:
        if error.response is not None and error.response.status_code in {409, 422}:
            detail = error.response.json()["detail"]
            if isinstance(detail, list):
                field = detail[0]
                detail = " · ".join(str(item) for item in field["loc"][1:]) + " · " + field["msg"]
            st.error(detail)
        else:
            show_connection_error(api, error)
    except requests.RequestException as error:
        show_connection_error(api, error)
    except ValueError as error:
        st.error(str(error))


def upload_readings():
    with st.expander("Upload pump readings", expanded=False):
        st.caption(f"Score up to {MAX_ROWS} readings per file (1 MB) and send them to the live feed and Power BI.")
        st.download_button("Download sample CSV", SAMPLE_CSV, "pump-sample.csv", "text/csv")
        file = st.file_uploader(
            "Pump readings file", type=list(FORMATS), max_upload_size=1,
            help=f"{FORMAT_HELP}. Use the sample headings and put readings on the first worksheet or in a bordered table.",
        )
        required_token = os.environ.get("PUMP_UPLOAD_TOKEN", "")
        token = st.text_input("Upload token", type="password") if required_token else ""
        upload, sample = st.columns(2)
        if upload.button("Score and send to live feed", disabled=file is None):
            if required_token and not compare_digest(token.encode(), required_token.encode()):
                st.error("Enter the upload token to send your file to the live feed.")
            else:
                submit_upload(file.getvalue(), file.name, sample=False)
        if sample.button("Run sample CSV"):
            submit_upload(SAMPLE_CSV.encode(), "pump-sample.csv", sample=True)
        result = st.session_state.get("upload_result")
        if result is not None:
            rows = result["readings"]
            flagged = sum(reading["anomaly_flag"] for reading in rows)
            st.success(f"Saved {result['created']} · Flagged {flagged} · Already in feed {result['replayed']}")
            st.dataframe(pd.DataFrame([{
                "Reading": reading["reading_id"],
                "Time (IST)": clock(reading["received_at"]),
                "Result": reading["suspected_fault"] or "Normal score",
                "Fault score": f"{reading['fault_score']:.1%}",
            } for reading in rows]), hide_index=True, width="stretch")


def render_trend(timeline: list[dict]):
    if not timeline:
        st.info("Waiting for recent sensor readings.")
        return
    values = pd.DataFrame(timeline)
    values["time_ist"] = pd.to_datetime(values["received_at"], format="mixed", utc=True).dt.tz_convert(IST).dt.tz_localize(None)
    series = alt.Chart(values).encode(
        x=alt.X("time_ist:T", title=None, axis=alt.Axis(format="%H:%M:%S", labelOverlap=True)),
        y=alt.Y("fault_score:Q", title="Fault score", scale=alt.Scale(domain=[0, 1]), axis=alt.Axis(format="%")),
        tooltip=[alt.Tooltip("time_ist:T", title="Time (IST)", format="%H:%M:%S"),
                 alt.Tooltip("fault_score:Q", title="Fault score", format=".1%")],
    )
    cutoff = float(values["alert_cutoff"].iloc[-1])
    rule = alt.Chart(pd.DataFrame({"cutoff": [cutoff]})).mark_rule(
        color="#BE531E", strokeDash=[7, 4], strokeWidth=1.6
    ).encode(y="cutoff:Q")
    line = series.mark_line(color="#087C7D", strokeWidth=2.4)
    points = series.transform_filter("datum.anomaly_flag == 1").mark_circle(
        color="#BE531E", size=76, opacity=1
    )
    chart = (rule + line + points).properties(height=246)
    st.altair_chart(chart, width="stretch")
    st.caption(f"Dashed line marks the {cutoff:.0%} alert cutoff.")


def manual_citation(page: int, public_api: str) -> str:
    if MANUAL_PATH:
        return f"(p. {page})"
    return f"[p. {page}]({public_api}/manual#page={page})"


def open_manual(public_api: str):
    if MANUAL_PATH:
        st.download_button("Download training manual", Path(MANUAL_PATH).read_bytes(), "pump_manual.pdf", "application/pdf", on_click="ignore")
    else:
        st.link_button("Open training manual", f"{public_api}/manual")


def render_guidance(record: dict | None, public_api: str):
    if record is None:
        st.info("Simulate a fault to see its manual checklist.")
        return
    st.markdown(f"**{record['suspected_fault']}** · {record['fault_score']:.1%} fault score")
    st.caption(f"Reading {record['reading_id']} · {clock(record['received_at'])} IST")
    state = record["guidance_state"]
    if state in {"queued", "running"}:
        st.info("Finding relevant steps in the pump manual…")
        return
    advice = record["guidance"]
    if state == "error" or not advice:
        st.error("Guidance is unavailable for this reading.")
        open_manual(public_api)
        return
    st.markdown("**Groq-selected checklist**" if advice["mode"] == "groq" else "**Retrieved manual checklist**")
    if advice["reason"] in REASONS:
        st.caption(REASONS[advice["reason"]])
    safety = advice["safety"]
    st.warning(f"{safety['text']} {manual_citation(safety['page'], public_api)}")
    for number, step in enumerate(advice["steps"], start=1):
        st.markdown(f"{number}. {step['text']} {manual_citation(step['page'], public_api)}")
    if MANUAL_PATH:
        open_manual(public_api)


with st.container(border=True):
    selection, action = st.columns([2.1, 1], vertical_alignment="bottom")
    fault = selection.selectbox("Simulated fault", ["Overheating", "Bearing wear", "Cavitation"])
    if action.button("Simulate Pump Fault", type="primary", width="stretch"):
        api, _ = api_addresses()
        try:
            result = request_api(api, "POST", "/simulate/fault", json={"fault": fault})
            if not isinstance(result, dict) or "reading_id" not in result:
                raise ValueError("The API did not return a scored reading.")
            st.session_state["selected_alert"] = result["reading_id"]
            st.toast("Fault reading scored and added to the alert log.", icon="⚠")
        except (requests.RequestException, ValueError) as error:
            show_connection_error(api, error)


upload_readings()


@st.fragment(run_every=2)
def monitor():
    api, public_api = api_addresses()
    try:
        snapshot = request_api(api, "GET", "/operator")
        fields = {"feed", "latest", "timeline", "alerts", "simulator_enabled"}
        if not isinstance(snapshot, dict) or not fields <= snapshot.keys():
            raise ValueError("The API did not return a telemetry snapshot.")
        chosen = st.session_state.get("selected_alert")
        alert = next((item for item in snapshot["alerts"] if item["reading_id"] == chosen), None)
        if chosen is not None and alert is None:
            try:
                alert = request_api(api, "GET", f"/readings/{chosen}")
            except requests.HTTPError as error:
                if error.response.status_code != 404:
                    raise
        if alert is None and snapshot["alerts"]:
            alert = snapshot["alerts"][0]
    except (requests.RequestException, ValueError) as error:
        show_connection_error(api, error)
        return

    latest = snapshot["latest"]
    feed, timestamp, score = st.columns(3)
    feed.metric("Feed status", snapshot["feed"]["feed_state"], border=True)
    timestamp.metric("Latest reading (IST)", snapshot["feed"]["reading_clock_ist"] or "—", border=True)
    score.metric("Latest fault score", f"{latest['fault_score']:.1%}" if latest else "—", border=True)
    if snapshot["simulator_enabled"]:
        st.caption("Simulated sensor feed is active.")
    for column, (label, key, unit, digits) in zip(st.columns(4), SENSORS):
        value = f"{latest[key]:.{digits}f}" if latest else "—"
        column.metric(f"{label} ({unit})", value, border=True)

    trend, guidance = st.columns([1.35, 1], gap="medium")
    with trend:
        with st.container(border=True):
            st.subheader("Fault score trend")
            render_trend(snapshot["timeline"])
        with st.container(border=True):
            st.subheader("Recent alerts")
            if snapshot["alerts"]:
                rows = [{
                    "Time (IST)": clock(record["received_at"]),
                    "Suspected fault": record["suspected_fault"],
                    "Score": f"{record['fault_score']:.1%}",
                    "Guidance": record["guidance_state"].capitalize(),
                } for record in snapshot["alerts"]]
                st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", height=192)
            else:
                st.caption("Waiting for the first fault alert.")
    with guidance:
        with st.container(border=True):
            st.subheader("Troubleshooting")
            render_guidance(alert, public_api)


monitor()
