const byId = (id) => document.getElementById(id);
const sourceSelect = byId("source");
const assetSelect = byId("asset");
const fixtureNode = byId("previewFixture");
const fixture = fixtureNode ? JSON.parse(fixtureNode.textContent) : null;
const pollMs = 4000;
const state = { source: fixture ? "replay" : null, asset: "", minutes: 15 };
const sensorFields = [
  ["air_temperature_k", "Air temperature", "K", 1],
  ["process_temperature_k", "Process temperature", "K", 1],
  ["rotational_speed_rpm", "Rotational speed", "rpm", 0],
  ["torque_nm", "Torque", "N·m", 1],
  ["tool_wear_min", "Tool wear", "min", 0],
];
const windows = { 15: "15 minutes", 60: "1 hour", 1440: "24 hours" };
const number = new Intl.NumberFormat("en-GB");
const time = new Intl.DateTimeFormat("en-GB", { timeZone: "UTC", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" });
const shortTime = new Intl.DateTimeFormat("en-GB", { timeZone: "UTC", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
const date = new Intl.DateTimeFormat("en-GB", { timeZone: "UTC", day: "2-digit", month: "short" });
const percent = (value, digits = 0) => Number.isFinite(value) ? (100 * value).toFixed(digits) + "%" : "—";
const set = (id, value) => { byId(id).textContent = value; };

function element(tag, value, cssClass) {
  const node = document.createElement(tag);
  if (value !== undefined) node.textContent = value;
  if (cssClass) node.className = cssClass;
  return node;
}

function svgNode(tag, attributes, value) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, item] of Object.entries(attributes)) node.setAttribute(key, item);
  if (value !== undefined) node.textContent = value;
  return node;
}

function relativeAge(seconds) {
  if (seconds === null) return "—";
  if (seconds < 60) return seconds + "s ago";
  if (seconds < 3600) return Math.floor(seconds / 60) + "m ago";
  return Math.floor(seconds / 3600) + "h ago";
}

function drawTrend(snapshot) {
  const target = byId("trend");
  if (!snapshot.trend.length) {
    target.replaceChildren(element("div", "Readings will appear here as the selected source reports.", "empty"));
    return;
  }
  const chart = svgNode("svg", { viewBox: "0 0 780 205", role: "img", "aria-label": "Peak and mean model score by UTC time" });
  const left = 50, right = 755, top = 12, bottom = 173;
  const stop = Date.parse(snapshot.now);
  const start = stop - snapshot.minutes * 60_000;
  const x = (timestamp) => left + (right - left) * Math.min(1, Math.max(0, (timestamp - start) / (stop - start)));
  const y = (score) => bottom - score * (bottom - top);
  for (const value of [0, 0.25, 0.5, 0.75, 1]) {
    chart.append(
      svgNode("line", { x1: left, x2: right, y1: y(value), y2: y(value), class: "grid-line" }),
      svgNode("text", { x: left - 9, y: y(value) + 4, "text-anchor": "end", class: "chart-label" }, percent(value)),
    );
  }
  for (let index = 0; index < 5; index++) {
    const when = start + (stop - start) * index / 4;
    const label = snapshot.minutes === 1440 ? date.format(when) + " " + shortTime.format(when) : shortTime.format(when);
    chart.append(svgNode("text", {
      x: x(when), y: bottom + 20, "text-anchor": index === 0 ? "start" : index === 4 ? "end" : "middle", class: "chart-label",
    }, label));
  }
  chart.append(svgNode("line", { x1: left, x2: right, y1: y(snapshot.review_threshold), y2: y(snapshot.review_threshold), class: "threshold-line" }));
  const maxGap = snapshot.minutes === 1440 ? 1_350_000 : 90_000;
  for (const [key, css] of [["mean", "mean-line"], ["peak", "peak-line"]]) {
    let previous = null;
    const path = snapshot.trend.map((bucket) => {
      const when = Date.parse(bucket.at);
      const command = previous === null || when - previous > maxGap ? "M" : "L";
      previous = when;
      return command + x(when).toFixed(1) + " " + y(bucket[key]).toFixed(1);
    }).join(" ");
    chart.append(svgNode("path", { d: path, class: css }));
  }
  for (const bucket of snapshot.trend) {
    const dot = svgNode("circle", {
      cx: x(Date.parse(bucket.at)), cy: y(bucket.peak), r: bucket.reviews ? 4.5 : 2.3, class: "review-point",
      fill: bucket.reviews ? "#ffc17a" : "#54dfc4",
    });
    if (!bucket.reviews) dot.classList.remove("review-point");
    dot.append(svgNode("title", {}, shortTime.format(new Date(bucket.at)) + " UTC • Peak " + percent(bucket.peak) + " • " + bucket.reviews + " review readings"));
    chart.append(dot);
  }
  target.replaceChildren(chart);
}

function drawFleet(snapshot) {
  const focused = document.activeElement?.dataset?.asset;
  const container = byId("fleet");
  const buttons = snapshot.fleet.map((row) => {
    const button = element("button", undefined, "fleet-row");
    button.type = "button";
    button.dataset.asset = row.asset_id;
    button.dataset.selected = String(state.asset === row.asset_id);
    button.setAttribute("aria-pressed", button.dataset.selected);
    const title = element("span", undefined, "fleet-row-top");
    title.append(element("strong", row.asset_id), element("span", row.review_flag ? "Review" : "Normal score", row.review_flag ? "status review" : "status"));
    const bar = element("progress", undefined, row.review_flag ? "score-progress review" : "score-progress");
    bar.max = 1;
    bar.value = row.model_score;
    bar.setAttribute("aria-label", "Latest model score");
    const detail = element("span", undefined, "fleet-row-bottom");
    detail.append(element("span", time.format(new Date(row.observed_at)) + " UTC"), element("strong", percent(row.model_score, 1)));
    button.append(title, bar, detail);
    button.addEventListener("click", () => {
      state.asset = state.asset === row.asset_id ? "" : row.asset_id;
      assetSelect.value = state.asset;
      refresh();
    });
    return button;
  });
  container.replaceChildren(...(buttons.length ? buttons : [element("div", "Waiting for assets in this source.", "empty")]));
  if (focused) buttons.find((button) => button.dataset.asset === focused)?.focus({ preventScroll: true });
  const assets = snapshot.fleet.map((row) => row.asset_id);
  if (state.asset && !assets.includes(state.asset)) assets.push(state.asset);
  const options = [new Option("All assets", ""), ...assets.map((asset) => new Option(asset, asset))];
  const existing = Array.from(assetSelect.options).map((option) => option.value);
  if (existing.join("\n") !== ["", ...assets].join("\n")) assetSelect.replaceChildren(...options);
  assetSelect.value = state.asset;
  set("fleetCount", snapshot.fleet.length + " asset" + (snapshot.fleet.length === 1 ? "" : "s"));
}

function drawReadings(snapshot) {
  const rows = snapshot.alerts.map((reading) => {
    const row = element("tr");
    const when = new Date(reading.observed_at);
    const values = [
      date.format(when) + " " + time.format(when),
      reading.asset_id, percent(reading.model_score, 1),
      reading.torque_nm.toFixed(1) + " N·m", number.format(reading.rotational_speed_rpm) + " rpm", reading.tool_wear_min + " min",
    ];
    values.forEach((value, index) => row.append(element("td", value, index === 2 ? "score-cell" : "")));
    return row;
  });
  if (!rows.length) {
    const row = element("tr");
    const message = element("td", "No review readings in this selection.", "empty");
    message.colSpan = 6;
    row.append(message);
    rows.push(row);
  }
  byId("queue").replaceChildren(...rows);
  const latest = snapshot.latest;
  const sensorNodes = sensorFields.map(([key, label, unit, decimals]) => {
    const node = element("div");
    node.append(element("dt", label), element("dd", latest ? latest[key].toLocaleString("en-GB", {
      minimumFractionDigits: decimals, maximumFractionDigits: decimals,
    }) + " " + unit : "—"));
    return node;
  });
  const grade = element("div");
  grade.append(element("dt", "Product grade"), element("dd", latest?.grade ?? "—"));
  byId("sensorValues").replaceChildren(...sensorNodes, grade);
  set("sensorAsset", latest?.asset_id ?? "—");
  set("sensorTime", latest ? "Observed " + time.format(new Date(latest.observed_at)) + " UTC · Score " + percent(latest.model_score, 1) : "Waiting for a reading.");
}

function drawValidation(snapshot) {
  const values = snapshot.validation;
  const metrics = [
    ["Accuracy", percent(values.accuracy, 1)],
    ["Failure precision", percent(values.failure_precision, 1)],
    ["Failure recall", percent(values.failure_recall, 1)],
    ["Missed failures", String(values.confusion.false_negative)],
  ];
  byId("modelMetrics").replaceChildren(...metrics.map(([label, value]) => {
    const item = element("div");
    item.append(element("dt", label), element("dd", value));
    return item;
  }));
  set("modelScope", "AI4I synthetic holdout · " + number.format(values.split.holdout_rows) + " rows");
  set("modelVersion", snapshot.model_version);
}

function render(snapshot) {
  state.source = snapshot.source;
  sourceSelect.value = snapshot.source;
  const replay = snapshot.source === "replay";
  set("sourceTag", replay ? "Synthetic replay" : "Live telemetry");
  set("contextText", replay
    ? "Scored AI4I holdout examples on a demo clock. Replay intentionally includes failure examples."
    : "Readings arrive from the ingestion API. Scores above the review threshold are flagged for inspection.");
  const summary = snapshot.summary;
  set("readings", number.format(summary.readings));
  set("assetCount", number.format(summary.assets));
  set("reviewCount", number.format(summary.reviews));
  set("readingsNote", "Last " + windows[snapshot.minutes] + " · " + (state.asset || "all assets"));
  set("reviewNote", "Score above " + percent(snapshot.review_threshold));
  const age = summary.last_ingested ? Math.max(0, Math.floor((Date.parse(snapshot.now) - Date.parse(summary.last_ingested)) / 1000)) : null;
  set("lastReceived", relativeAge(age));
  set("receivedNote", summary.last_ingested ? time.format(new Date(summary.last_ingested)) + " UTC" : "Waiting for this source");
  set("feedLabel", age === null ? "Awaiting readings" : age > 60 ? "Feed delayed" : replay ? "Replay running" : "Feed active");
  byId("feedSignal").dataset.state = age === null ? "idle" : age > 60 ? "late" : "active";
  set("trendNote", (snapshot.minutes === 1440 ? "15-minute" : "1-minute") + " buckets · UTC · Review boundary " + percent(snapshot.review_threshold));
  byId("connectionError").hidden = true;
  drawTrend(snapshot);
  drawFleet(snapshot);
  drawReadings(snapshot);
  drawValidation(snapshot);
}

const previewRows = [];
let previewIndex = 0;
let lastReplayTime = 0;

function previewRecord(timestamp) {
  const row = fixture.rows[previewIndex % fixture.rows.length];
  const at = new Date(timestamp).toISOString();
  previewIndex++;
  return { ...row, observed_at: at, ingested_at: at, event_id: "preview-" + previewIndex };
}

function previewSnapshot() {
  const now = Date.now();
  if (previewIndex === 0) {
    for (let index = 89; index >= 0; index--) previewRows.push(previewRecord(now - 2000 - index * 10_000));
    lastReplayTime = now;
  } else if (now - lastReplayTime >= pollMs) {
    previewRows.push(previewRecord(now));
    lastReplayTime = now;
  }
  while (previewRows.length && Date.parse(previewRows[0].observed_at) < now - 86_400_000) previewRows.shift();
  const cutoff = now - state.minutes * 60_000;
  const history = previewRows.filter((row) => Date.parse(row.observed_at) >= cutoff);
  const selected = history.filter((row) => !state.asset || row.asset_id === state.asset);
  const fleet = new Map(history.map((row) => [row.asset_id, row]));
  const reviews = selected.filter((row) => row.review_flag);
  const bucketSize = state.minutes === 1440 ? 900_000 : 60_000;
  const buckets = new Map();
  for (const row of selected) {
    const bucket = Math.floor(Date.parse(row.observed_at) / bucketSize) * bucketSize;
    if (!buckets.has(bucket)) buckets.set(bucket, { at: new Date(bucket).toISOString(), peak: 0, sum: 0, readings: 0, reviews: 0 });
    const point = buckets.get(bucket);
    point.peak = Math.max(point.peak, row.model_score);
    point.sum += row.model_score;
    point.readings++;
    point.reviews += row.review_flag;
  }
  return {
    source: "replay", minutes: state.minutes, now: new Date(now).toISOString(),
    summary: { readings: selected.length, assets: new Set(selected.map((row) => row.asset_id)).size, reviews: reviews.length, last_ingested: selected.at(-1)?.ingested_at },
    fleet: [...fleet.values()].sort((first, second) => first.asset_id.localeCompare(second.asset_id)),
    alerts: reviews.slice(-12).reverse(),
    trend: [...buckets.values()].map((point) => ({ ...point, mean: point.sum / point.readings })),
    latest: selected.at(-1),
    review_threshold: fixture.validation.threshold,
    validation: fixture.validation,
    model_version: fixture.validation.model_version,
  };
}

function params() {
  const query = new URLSearchParams({ minutes: String(state.minutes) });
  if (state.source) query.set("source", state.source);
  if (state.asset) query.set("asset", state.asset);
  return query;
}

let refreshTimer;
let refreshSequence = 0;
let controller;
async function refresh() {
  clearTimeout(refreshTimer);
  controller?.abort();
  controller = new AbortController();
  const sequence = ++refreshSequence;
  try {
    let snapshot;
    if (fixture) snapshot = previewSnapshot();
    else {
      const response = await fetch("/api/snapshot?" + params(), { signal: controller.signal, cache: "no-store" });
      if (!response.ok) throw new Error("The monitoring service returned " + response.status + ".");
      snapshot = await response.json();
    }
    if (sequence === refreshSequence) render(snapshot);
  } catch (error) {
    if (sequence === refreshSequence && error.name !== "AbortError") {
      set("feedLabel", "Feed unavailable");
      byId("feedSignal").dataset.state = "error";
      set("connectionError", "The latest readings could not be loaded. Reconnecting automatically.");
      byId("connectionError").hidden = false;
    }
  } finally {
    if (sequence === refreshSequence) refreshTimer = setTimeout(refresh, pollMs);
  }
}

function downloadPreview() {
  const cutoff = Date.now() - state.minutes * 60_000;
  const rows = previewRows.filter((row) => Date.parse(row.observed_at) >= cutoff && (!state.asset || row.asset_id === state.asset));
  const csv = [fixture.columns.join(","), ...rows.map((row) => fixture.columns.map((column) => row[column] ?? "").join(","))].join("\r\n") + "\r\n";
  downloadCsv(csv, "maintenance-replay.csv");
}

function downloadCsv(csv, filename) {
  const url = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
  const link = element("a");
  link.href = url;
  link.download = filename;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function scorePreview(features) {
  const model = fixture.model;
  const values = { ...features, grade_l: Number(features.grade === "L"), grade_m: Number(features.grade === "M") };
  const scaled = model.features.map((key, index) => Math.fround((values[key] - model.mean[index]) / model.scale[index]));
  let node = model.nodes[0];
  while ("feature" in node) node = model.nodes[scaled[node.feature] <= node.threshold ? node.left : node.right];
  const review = node.score > fixture.validation.threshold;
  return {
    model_score: node.score, review_flag: Number(review), status: review ? "Review" : "Normal score",
    model_version: model.model_version,
  };
}

async function postJson(path, body, token) {
  const headers = { "Content-Type": "application/json" };
  if (token) headers.Authorization = "Bearer " + token;
  const response = await fetch(path, { method: "POST", headers, body: JSON.stringify(body) });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "The scoring service returned " + response.status + ".");
  return result;
}

async function scoreBatch(readings) {
  if (fixture) return readings.map(scorePreview);
  const result = await postJson("/api/score-batch", { readings });
  return result.predictions;
}

let alarmAudio;
function prepareAlarm() {
  if (!byId("alarmEnabled").checked) return Promise.resolve(null);
  try {
    const Audio = window.AudioContext || window.webkitAudioContext;
    alarmAudio ??= new Audio();
    if (alarmAudio.state === "running") return Promise.resolve(alarmAudio);
    return alarmAudio.resume().then(() => alarmAudio, () => null);
  } catch {
    return Promise.resolve(null);
  }
}

function playAlarm(ready) {
  void ready.then((audio) => {
    if (audio?.state !== "running" || !byId("alarmEnabled").checked) return;
    const start = audio.currentTime;
    [760, 940].forEach((frequency, index) => {
      const when = start + index * 0.22;
      const tone = audio.createOscillator();
      const volume = audio.createGain();
      tone.frequency.value = frequency;
      volume.gain.setValueAtTime(0.001, when);
      volume.gain.exponentialRampToValueAtTime(0.13, when + 0.015);
      volume.gain.exponentialRampToValueAtTime(0.001, when + 0.17);
      tone.connect(volume).connect(audio.destination);
      tone.start(when);
      tone.stop(when + 0.18);
    });
  }).catch(() => {});
}

sourceSelect.disabled = Boolean(fixture);
sourceSelect.addEventListener("change", () => {
  state.source = sourceSelect.value;
  state.asset = "";
  refresh();
});
assetSelect.addEventListener("change", () => {
  state.asset = assetSelect.value;
  refresh();
});
for (const button of document.querySelectorAll("[data-minutes]")) {
  button.addEventListener("click", () => {
    state.minutes = Number(button.dataset.minutes);
    for (const other of document.querySelectorAll("[data-minutes]")) other.setAttribute("aria-pressed", String(other === button));
    refresh();
  });
}
byId("exportCsv").addEventListener("click", () => {
  if (fixture) downloadPreview();
  else window.location.assign("/api/telemetry.csv?" + params());
});
byId("openScorer").addEventListener("click", () => byId("scorer").showModal());
byId("closeScorer").addEventListener("click", () => byId("scorer").close());
byId("scoreForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const alarm = prepareAlarm();
  const fields = new FormData(event.target);
  const features = { grade: fields.get("grade") };
  for (const [key] of sensorFields) features[key] = Number(fields.get(key));
  const result = byId("scoreResult");
  try {
    const prediction = fixture ? scorePreview(features) : await postJson("/api/score", features);
    result.textContent = prediction.status + " · " + percent(prediction.model_score, 1);
    result.dataset.alert = String(Boolean(prediction.review_flag));
    if (prediction.review_flag) playAlarm(alarm);
  } catch (error) {
    result.textContent = error.message || "Scoring is unavailable.";
    result.dataset.alert = "true";
  }
});
initDatasetUpload(scoreBatch);
refresh();
