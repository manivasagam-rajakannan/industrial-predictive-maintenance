"""Bundle the exact dashboard frontend with scored, standalone replay fixtures."""

import csv
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from monitor import ROOT, load_replay, replay_records
from scoring import FailureModel
from storage import COLUMNS


def main():
    dashboard = ROOT / "dashboard"
    model_path = ROOT / "artifacts" / "model.json"
    model = FailureModel(model_path)
    validation = json.loads((ROOT / "artifacts" / "validation.json").read_text(encoding="utf-8"))
    replay = load_replay(ROOT / "data" / "replay.csv")
    sample_end = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    sample = list(replay_records(model, replay, len(replay), sample_end, "tableau", spacing=10))
    with (ROOT / "data" / "tableau_replay_sample.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(sample)
    rows = [{key: value for key, value in row.items() if key not in {"observed_at", "ingested_at", "event_id"}} for row in sample]
    fixture = {
        "rows": rows,
        "model": json.loads(model_path.read_text(encoding="utf-8")),
        "validation": validation,
        "columns": COLUMNS,
    }
    data = json.dumps(fixture, separators=(",", ":"), allow_nan=False).replace("<", r"\u003c")
    css = (dashboard / "dashboard.css").read_text(encoding="utf-8")
    html = (dashboard / "index.html").read_text(encoding="utf-8")
    html = html.replace('<link rel="stylesheet" href="/dashboard.css">', f"<style>\n{css}\n</style>")
    html = html.replace('  <link rel="manifest" href="./manifest.webmanifest">\n', "")
    html = html.replace('  <link rel="icon" href="./app-icon-192.png">\n', "")
    scripts = [f'<script type="application/json" id="previewFixture">{data}</script>']
    for name in ("dataset.js", "dashboard.js"):
        html = html.replace(f'  <script src="/{name}" defer></script>\n', "")
        javascript = (dashboard / name).read_text(encoding="utf-8")
        scripts.append(f"<script>\n{javascript}\n</script>")
    html = html.replace("</body>", "\n".join(scripts) + "\n</body>")
    (ROOT / "MaintenanceWatch_Preview.html").write_text(html, encoding="utf-8")


if __name__ == "__main__":
    main()
