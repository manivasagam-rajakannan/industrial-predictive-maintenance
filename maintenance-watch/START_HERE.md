# Maintenance Watch

Maintenance Watch scores industrial readings, keeps a timestamped history, and highlights readings above the review threshold. The browser dashboard and Power BI report use the same telemetry contract.

## Open the Power BI demo

Extract the ZIP and double-click **OPEN_POWER_BI_DEMO.pbip** in the extracted **MaintenanceWatch** folder. This opens the report in Power BI Desktop with a built-in synthetic snapshot of 360 readings. If the visuals initially show blanks, click **Home → Refresh** once to load the snapshot. Keep the **powerbi_demo** folder beside the project file.

## Start the live report with the launcher

Double-click **REPAIR_AND_START_LIVE.cmd** from the same extracted folder. If Docker Desktop is already running, the launcher starts the database and monitor. The updated launcher displays **Maintenance Watch live launcher v2**. When the Docker engine is unavailable or Compose reports a Docker startup error, it pauses so you can save open WSL work, attempts one repair, and retries startup. It displays the error and status if Windows still blocks startup. When the monitor is ready, it opens the DirectQuery project in Power BI Desktop. Follow the database sign-in and page refresh steps below.

## Open the preview

Extract the ZIP and double-click **MaintenanceWatch_Preview.html**. The preview starts a synthetic replay and adds a scored reading about every four seconds while the tab is active. Try the asset filter, time window, review table, and **Score a reading** form. The preview contains the scoring model and works offline.

## Score a dataset and hear an alert

1. Open the browser dashboard and select **Upload dataset → Try sample**. The two sample rows include one reading flagged for review.
2. In the running app, choose [the Excel sample](../samples/machine-readings.xlsx) in **Machine readings file** and click **Score file**. The limit is 10,000 readings and 5 MB per file. [Supported file layouts](../README.md#upload-sensor-files)
3. Check the scored, flagged, and rejected counts. Scores above 50% are highlighted. With sound enabled, a completed file with flagged readings triggers one two-tone beep. Click **Download results CSV** to export the full result.

Use these headings or the original AI4I column names. The optional `asset_id` helps identify machines in the results.

```csv
grade,air_temperature_k,process_temperature_k,rotational_speed_rpm,torque_nm,tool_wear_min,asset_id
L,299.3,310.4,1629,32.6,177,CNC-01
L,303.6,312.0,1309,52.9,86,CNC-02
```

Use `air_temperature_c` and `process_temperature_c` when the values are in Celsius. The running Python app parses and scores the upload. Use **Send scored rows to live feed** with your live ingest token to see the accepted rows in Power BI. The standalone preview runs its CSV scoring in the browser.

Open the hosted app in a browser that supports installing web apps and choose its **Install app** option when offered.

## Run the local app

Open PowerShell inside the extracted **MaintenanceWatch** folder. Use Python 3.11 or newer.

```powershell
py -3 -m pip install -r ../upload-requirements.txt
py -3 monitor.py --demo
```

Open [the local monitoring dashboard](http://127.0.0.1:8000/). The app stores readings in **runtime/telemetry.sqlite** and polls every four seconds. Press **Ctrl+C** in PowerShell to stop it. **Live telemetry** and **Synthetic replay** have separate filters and histories.

## Open the Power BI report on Windows

The report is an editable **Power BI Desktop project**. Keep its report and semantic-model folders beside **MaintenanceWatch.pbip**.

1. Start Docker Desktop. Stop the SQLite demo with **Ctrl+C** if it is using port 8000.
2. In PowerShell, from the **MaintenanceWatch** folder, start the app and PostgreSQL.

   ```powershell
   docker compose up --build -d
   docker compose ps
   ```

3. Open [the local dashboard](http://127.0.0.1:8000/) and confirm that the replay is running. In **Power BI Desktop**, open **powerbi/MaintenanceWatch.pbip**. If Desktop prompts for project or enhanced report support, enable the corresponding preview feature in **File → Options and settings → Options → Preview features** and restart Desktop.
4. For the PostgreSQL connection, select **Database** authentication. The project points to **127.0.0.1:5433**, database **maintenance**. The default local demo reader is **maintenance_bi** with password **maintenance_local_reader**. Use your configured **MAINTENANCE_BI_PASSWORD** if you set one before the database was initialized. The reader can query the monitoring view. If Desktop asks about the local connection's encryption, use the loopback-only demo connection.
5. Click a blank area of the **Fleet overview** page and choose **Format page → Page refresh → On → Fixed interval**. Start with **5 or 10 seconds**. Select **Synthetic replay** and watch **Last received UTC** advance. Use **Live telemetry** when an upstream feed is connected.

The report queries a rolling 24-hour view in PostgreSQL via DirectQuery. Power BI Service refresh depends on the workspace capacity and the data-source gateway or cloud connection. Configure and validate those settings if you publish the report.

- [Power BI Desktop projects](https://learn.microsoft.com/en-us/power-bi/developer/projects/projects-overview)
- [PostgreSQL connector](https://learn.microsoft.com/en-us/power-query/connectors/postgresql)
- [Automatic page refresh](https://learn.microsoft.com/en-us/power-bi/create-reports/desktop-automatic-page-refresh)

## Connect your telemetry producer

Set a private **MAINTENANCE_INGEST_TOKEN** in PowerShell before launching the Python app or Docker Compose. For Docker, run **docker compose up -d** after changing the environment so the monitor container picks up the token. Use a stable event ID per source reading so retries keep one record.

This example posts one manual test reading into **Live telemetry**.

```powershell
$reading = @{
    event_id = "manual-" + [guid]::NewGuid().ToString("N")
    asset_id = "CNC-01"
    observed_at = [DateTime]::UtcNow.ToString("o")
    grade = "L"
    air_temperature_k = 298.2
    process_temperature_k = 308.6
    rotational_speed_rpm = 1433
    torque_nm = 39.5
    tool_wear_min = 7
} | ConvertTo-Json

Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/readings" `
    -Headers @{ Authorization = "Bearer $env:MAINTENANCE_INGEST_TOKEN" } `
    -ContentType "application/json" -Body $reading
```

Then switch the dashboard or report source to **Live telemetry**. A first event returns HTTP 201. An identical retry returns HTTP 200 with the original stored prediction. Reusing the same ID for different input returns HTTP 409. The app uses UTC timestamps throughout. The read-only **POST /api/score** route takes the six feature fields and returns a score for manual checks.

The packaged services bind to loopback on the host. The supplied credentials are for the local demo. For a shared deployment, place the monitor behind an authenticated HTTPS service and use your environment's own database credentials.

## Use the Tableau data

Open **data/tableau_replay_sample.csv** as a text-file source in Tableau. Use **observed_at** as a UTC date-time, **asset_id** as a filter, and **model_score** as a percentage. Plot the maximum score per minute and use **SUM(review_flag)** to count readings flagged for review. **Export CSV** on the browser dashboard exports the selected source, time window, and asset for a new preview. The supplied sample includes synthetic timestamps and a selected mix of holdout cases.

## Model and MLflow

The model reproduces the notebook's tuned Decision Tree with the original six inputs. Training fits categorical encoding and scaling on the training split and exports them with the tree as one versioned JSON artifact. The app scores that artifact using Python's standard library.

| Notebook holdout reproduced | Result |
|---|---:|
| Training / holdout rows | 8,000 / 2,000 |
| Accuracy | 98.3% |
| Failure precision | 72.1% |
| Failure recall | 72.1% |
| Failure F1 | 72.1% |
| Failures caught / missed | 44 / 17 |
| False positives | 17 |

The review rule flags scores **above 50%**. The tree's score is the failure share at its assigned leaf. The evaluation uses the notebook's random holdout from a synthetic snapshot dataset. Measuring advance warning time requires timestamped sensor and fault events with a defined prediction horizon. The demo deliberately includes a mix of holdout failure cases for display.

To regenerate the model and the two dashboard artifacts, run these from the project folder.

```powershell
py -3.12 -m pip install -r requirements-train.txt
py -3.12 train.py
py -3.12 build_preview.py
py -3.12 build_powerbi.py
```

For MLflow, install **mlflow>=3,<4**, set **MLFLOW_TRACKING_URI** to your existing tracker, and run **py -3.12 train.py --mlflow**. The run logs the fitted preprocessing and tree pipeline, the versioned scorer, and the holdout metrics under **Machine_Failure_Prediction**.

## Verification and sources

The local service was tested for replay progression, isolated live ingestion, manual scoring, concurrent retries, conflicts, input validation, and CSV export. Portable scores matched scikit-learn across all 2,000 holdout records. Power BI project links, field bindings, and the embedded demo readings were checked locally. Power BI Desktop rendering and the live launcher still need their first run on Windows.

- S. Matzka, *AI4I 2020 Predictive Maintenance Dataset*, UCI Machine Learning Repository, 2020. [Dataset and CC BY 4.0 license](https://archive.ics.uci.edu/dataset/601/ai4i%2B2020%2Bpredictive%2Bmaintenance%2Bdataset).
- The bundled source CSV uses the public mirror below with normalized encoding and line endings. **artifacts/validation.json** records its SHA-256. [Mirror source](https://github.com/jackquill/ai4i-predictive-maintenance/blob/main/data/ai4i2020.csv).
- The report's shared base theme is from the Microsoft Fabric samples repository. The MIT notice is in **powerbi/Microsoft_Fabric_Samples_LICENSE.txt**. [Microsoft sample](https://github.com/microsoft/fabric-samples).
