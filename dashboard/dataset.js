const datasetMaxBytes = 5_000_000;
const datasetMaxRows = 10_000;
const datasetBatchSize = 100;
const datasetDisplayRows = 100;
const datasetSample = [
  "grade,air_temperature_k,process_temperature_k,rotational_speed_rpm,torque_nm,tool_wear_min,asset_id",
  "L,299.3,310.4,1629,32.6,177,CNC-01",
  "L,303.6,312.0,1309,52.9,86,CNC-02",
].join("\r\n") + "\r\n";

function parseCsv(text) {
  const records = [];
  let cells = [];
  let field = "";
  let quoted = false;
  let closedQuote = false;
  let rowNumber = 1;

  function finishField() {
    if (cells.length >= 256) throw new Error("The CSV can contain up to 256 columns.");
    cells.push(field);
    field = "";
    closedQuote = false;
  }

  function finishRow() {
    finishField();
    if (cells.some((cell) => cell.trim() !== "")) records.push({ cells, rowNumber });
    if (records.length > datasetMaxRows + 1) throw new Error("The CSV can contain up to 10,000 readings.");
    cells = [];
    rowNumber++;
  }

  for (let index = 0; index < text.length; index++) {
    const char = text[index];
    if (quoted) {
      if (char !== '"') {
        field += char;
      } else if (text[index + 1] === '"') {
        field += '"';
        index++;
      } else {
        quoted = false;
        closedQuote = true;
      }
    } else if (char === ",") {
      finishField();
    } else if (char === "\r" || char === "\n") {
      finishRow();
      if (char === "\r" && text[index + 1] === "\n") index++;
    } else if (closedQuote || (char === '"' && field !== "")) {
      throw new Error("Check the quotes in CSV row " + rowNumber + ".");
    } else if (char === '"') {
      quoted = true;
    } else {
      field += char;
    }
  }

  if (quoted) throw new Error("Close the quoted field in CSV row " + rowNumber + ".");
  if (field || cells.length || closedQuote) finishRow();
  return records;
}

function parseDataset(text, fields, grades) {
  const [header, ...readings] = parseCsv(text);
  if (!header) throw new Error("Choose a CSV with headings and at least one reading.");

  const required = ["grade", ...fields.map(({ key }) => key)];
  const aliases = {
    type: "grade",
    product_grade: "grade",
    air_temperature_c: "air_temperature_k",
    process_temperature_c: "process_temperature_k",
    asset: "asset_id",
  };
  const columns = new Map();
  header.cells.forEach((heading, index) => {
    const normalized = heading.trim().toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "");
    const name = Object.hasOwn(aliases, normalized) ? aliases[normalized] : normalized;
    if (name !== "asset_id" && !required.includes(name)) return;
    if (columns.has(name)) throw new Error("Use only one column for " + name + ".");
    columns.set(name, { index, celsius: normalized.endsWith("_temperature_c") });
  });

  const missing = required.filter((name) => !columns.has(name));
  if (missing.length) throw new Error("Add these CSV columns to continue. " + missing.join(", ") + ".");
  if (!readings.length) throw new Error("Add at least one reading below the headings.");

  return readings.map(({ cells, rowNumber }) => {
    const asset = cells[columns.get("asset_id")?.index]?.trim() ?? "";
    const result = { rowNumber, asset };
    if (cells.length !== header.cells.length) return { ...result, error: "The number of columns differs from the header." };

    const grade = cells[columns.get("grade").index].trim().toUpperCase();
    if (!grades.has(grade)) return { ...result, error: "Grade must be H, M, or L." };
    const features = { grade };
    for (const { key, label, unit, min, max } of fields) {
      const column = columns.get(key);
      const raw = cells[column.index].trim();
      const value = Number(raw) + (column.celsius ? 273.15 : 0);
      if (!/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$/i.test(raw) || !Number.isFinite(value) || value < min || value > max) {
        return { ...result, error: label + " must be between " + min + " and " + max + " " + unit + "." };
      }
      features[key] = value;
    }
    return { ...result, features };
  });
}

function datasetResultLabel(result) {
  if (result.error) return "Rejected";
  return result.review_flag ? "Review" : "Normal score";
}

function datasetResultCsv(results) {
  const escapeCell = (value) => {
    const text = String(value ?? "");
    const safe = /^\s*[=+@-]/.test(text) || /^[\t\r\n]/.test(text) ? "'" + text : text;
    return '"' + safe.replaceAll('"', '""') + '"';
  };
  const rows = results.map((result) => [
    result.rowNumber, result.asset, result.model_score, datasetResultLabel(result),
    result.error || "", result.model_version,
  ].map(escapeCell).join(","));
  return ["csv_row,asset_id,model_score,result,detail,model_version", ...rows].join("\r\n") + "\r\n";
}

function initDatasetUpload(predictBatch) {
  const dialog = byId("dataset");
  const fileInput = byId("datasetFile");
  const status = byId("datasetStatus");
  const manualFields = byId("scoreForm").elements;
  const grades = new Set(Array.from(manualFields.namedItem("grade").options, (option) => option.value));
  const fields = sensorFields.map(([key, label, unit]) => {
    const control = manualFields.namedItem(key);
    return { key, label, unit, min: Number(control.min), max: Number(control.max) };
  });
  const uploadControls = [fileInput, byId("scoreDataset"), byId("trySample")];
  let results = [];

  function showResults() {
    const rejected = results.filter((result) => result.error).length;
    const flagged = results.filter((result) => result.review_flag).length;
    set("rowsScored", number.format(results.length - rejected));
    set("rowsFlagged", number.format(flagged));
    set("rowsRejected", number.format(rejected));
    byId("datasetWarning").hidden = flagged === 0;

    const rank = (result) => result.review_flag ? 2 : Number(Boolean(result.error));
    const visible = [...results].sort((a, b) => rank(b) - rank(a)).slice(0, datasetDisplayRows);
    const body = byId("datasetRows");
    body.replaceChildren(...visible.map((result) => {
      const row = element("tr");
      row.dataset.review = String(Boolean(result.review_flag));
      const values = [result.rowNumber, result.asset || "—", percent(result.model_score, 1), datasetResultLabel(result), result.error || "—"];
      row.append(...values.map((value) => element("td", value)));
      return row;
    }));
    set("datasetLimit", visible.length < results.length
      ? "Showing 100 results with flagged rows first. Download the CSV for all " + number.format(results.length) + " rows."
      : "");
    byId("datasetResults").hidden = false;
    status.textContent = flagged ? "Scoring complete. " + number.format(flagged) + " flagged for review." : "Scoring complete.";
    return flagged;
  }

  async function scoreDataset(sample) {
    if (fileInput.disabled) return;
    const alarm = prepareAlarm();
    results = [];
    byId("datasetResults").hidden = true;
    status.dataset.alert = "false";
    status.textContent = "Reading CSV…";
    for (const control of uploadControls) control.disabled = true;

    try {
      let text = datasetSample;
      if (!sample) {
        const file = fileInput.files[0];
        if (!file || !/\.csv$/i.test(file.name)) throw new Error("Choose a .csv file. Save Excel sheets as CSV UTF-8.");
        if (file.size > datasetMaxBytes) throw new Error("Choose a CSV up to 5 MB.");
        try {
          text = new TextDecoder("utf-8", { fatal: true }).decode(await file.arrayBuffer());
        } catch {
          throw new Error("Save your CSV with UTF-8 encoding and try again.");
        }
      }

      results = parseDataset(text, fields, grades);
      const valid = results.filter((result) => !result.error);
      for (let index = 0; index < valid.length; index += datasetBatchSize) {
        const batch = valid.slice(index, index + datasetBatchSize);
        const predictions = await predictBatch(batch.map((row) => row.features));
        if (!Array.isArray(predictions) || predictions.length !== batch.length) throw new Error("The model returned an incomplete batch. Try scoring the file again.");
        predictions.forEach((prediction, offset) => Object.assign(batch[offset], prediction));
        status.textContent = "Scoring readings… " + number.format(index + batch.length) + " / " + number.format(valid.length);
      }
      if (showResults()) playAlarm(alarm);
    } catch (error) {
      status.textContent = error.message || "The file could not be scored.";
      status.dataset.alert = "true";
    } finally {
      for (const control of uploadControls) control.disabled = false;
    }
  }

  byId("openDataset").addEventListener("click", () => dialog.showModal());
  byId("closeDataset").addEventListener("click", () => dialog.close());
  byId("datasetForm").addEventListener("submit", (event) => {
    event.preventDefault();
    scoreDataset(false);
  });
  byId("trySample").addEventListener("click", () => scoreDataset(true));
  byId("downloadSample").addEventListener("click", () => downloadCsv(datasetSample, "maintenance-sample.csv"));
  byId("exportDataset").addEventListener("click", () => downloadCsv(datasetResultCsv(results), "maintenance-results.csv"));
}
