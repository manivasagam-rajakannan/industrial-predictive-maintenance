"""Build the live Power BI project and its self-contained sample report."""

import csv
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
from shutil import copytree, ignore_patterns
from uuid import NAMESPACE_URL, uuid5


ROOT = Path(__file__).resolve().parent
PROJECT = ROOT / "powerbi"
NAME = "MaintenanceWatch"
REPORT = PROJECT / f"{NAME}.Report"
MODEL = PROJECT / f"{NAME}.SemanticModel"
DEFINITION = REPORT / "definition"
SCHEMAS = "https://developer.microsoft.com/json-schemas/fabric/"
REPORT_SCHEMAS = "item/report/definition/"
BASE_THEME = "CY25SU12"
CUSTOM_THEME = "MaintenanceWatch.json"
TABLE = "Telemetry"
CANVAS = "#09111D"
PANEL = "#101C2C"
BORDER = "#29394E"
TEXT = "#EDF4FE"
MUTED = "#A9BED2"
TEAL = "#54DFC4"
BLUE = "#A0B8FB"
AMBER = "#FFC17A"

COLUMNS = [
    ("Event ID", "event_id", "string", None),
    ("Asset", "asset_id", "string", None),
    ("Data source", "data_source", "string", None),
    ("Reading UTC", "reading_utc", "dateTime", "MMM d HH:mm:ss"),
    ("Received UTC", "ingestion_utc", "dateTime", "MMM d HH:mm:ss"),
    ("Minute UTC", "minute_utc", "dateTime", "MMM d HH:mm"),
    ("Grade", "grade", "string", None),
    ("Air temperature K", "air_temperature_k", "double", "0.0"),
    ("Process temperature K", "process_temperature_k", "double", "0.0"),
    ("Rotational speed rpm", "rotational_speed_rpm", "double", "#,0"),
    ("Torque Nm", "torque_nm", "double", "0.0"),
    ("Tool wear min", "tool_wear_min", "double", "#,0"),
    ("Model score", "model_score", "double", "0.0%"),
    ("Review flag", "review_flag", "int64", "#,0"),
    ("Score status", "score_status", "string", None),
    ("Replay failure label", "observed_failure", "int64", "#,0"),
    ("Model version", "model_version", "string", None),
]
MEASURES = [
    ("Readings", "COALESCE ( COUNTROWS ( 'Telemetry' ), 0 )", "#,0"),
    ("Assets reporting", "COALESCE ( DISTINCTCOUNT ( 'Telemetry'[Asset] ), 0 )", "#,0"),
    ("Review readings", "COALESCE ( SUM ( 'Telemetry'[Review flag] ), 0 )", "#,0"),
    ("Last received", "MAX ( 'Telemetry'[Received UTC] )", "MMM d HH:mm:ss"),
    ("Peak score", "MAX ( 'Telemetry'[Model score] )", "0%"),
    ("Mean score", "AVERAGE ( 'Telemetry'[Model score] )", "0%"),
]


def write_json(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def schema(path):
    return SCHEMAS + path + "/schema.json"


def report_schema(path):
    return schema(REPORT_SCHEMAS + path)


def id_for(label):
    return hashlib.sha256(f"{NAME}/{label}".encode()).hexdigest()[:20]


def lineage(label):
    return str(uuid5(NAMESPACE_URL, f"maintenance-watch/{label}"))


def format_property(value):
    if isinstance(value, bool):
        literal = str(value).lower()
    elif isinstance(value, (int, float)):
        literal = str(value) + "D"
    else:
        literal = "'" + value.replace("'", "''") + "'"
    return {"expr": {"Literal": {"Value": literal}}}


def fill(color):
    return {"solid": {"color": format_property(color)}}


def query_field(name, kind="Column", entity=TABLE):
    return {kind: {"Expression": {"SourceRef": {"Entity": entity}}, "Property": name}}


def projection(name, kind="Column"):
    return {"field": query_field(name, kind), "queryRef": f"{TABLE}.{name}", "nativeQueryRef": name}


def filter_value(name, value):
    return {
        "Version": 2,
        "From": [{"Name": "t", "Entity": TABLE, "Type": 0}],
        "Where": [{"Condition": {"In": {
            "Expressions": [{"Column": {"Expression": {"SourceRef": {"Source": "t"}}, "Property": name}}],
            "Values": [[{"Literal": {"Value": "'" + value.replace("'", "''") + "'"}}]],
        }}}],
    }


def panel_title(title):
    return {
        "title": [{"properties": {
            "show": format_property(True),
            "text": format_property(title),
            "fontColor": fill(TEXT),
            "fontSize": format_property(13),
        }}],
        "background": [{"properties": {"show": format_property(True), "color": fill(PANEL), "transparency": format_property(0)}}],
        "border": [{"properties": {"show": format_property(True), "color": fill(BORDER), "radius": format_property(10)}}],
    }


def visual(page, label, visual_type, position, z, title=None, roles=None, objects=None, sort=None, filters=None):
    name = id_for(label)
    x, y, width, height = position
    config = {"visualType": visual_type, "drillFilterOtherVisuals": True}
    if roles:
        config["query"] = {"queryState": {key: {"projections": fields} for key, fields in roles.items()}}
        if sort:
            column, kind, direction = sort
            config["query"]["sortDefinition"] = {
                "sort": [{"field": query_field(column, kind), "direction": direction}],
                "isDefaultSort": True,
            }
    if title:
        config["visualContainerObjects"] = panel_title(title)
    if objects:
        config["objects"] = objects
    result = {
        "$schema": report_schema("visualContainer/2.7.0"),
        "name": name,
        "position": {"x": x, "y": y, "z": z, "width": width, "height": height, "tabOrder": z},
        "visual": config,
    }
    if filters:
        result["filterConfig"] = {"filters": filters}
    write_json(page / "visuals" / name / "visual.json", result)
    return name


def text_objects(lines):
    return {"general": [{"properties": {"paragraphs": [
        {"textRuns": [{"value": value, "textStyle": {
            "color": color,
            "fontSize": f"{size}pt",
            "fontFamily": "Segoe UI",
            "fontWeight": "bold" if bold else "normal",
        }}]}
        for value, color, size, bold in lines
    ]}}]}


def write_theme():
    theme = {
        "name": "Maintenance Watch",
        "dataColors": [TEAL, BLUE, AMBER, "#EA8F91", "#B6A6E9", "#6FB5DB"],
        "foreground": TEXT,
        "foregroundNeutralSecondary": MUTED,
        "foregroundNeutralTertiary": "#8195AD",
        "background": PANEL,
        "backgroundLight": "#142238",
        "backgroundNeutral": BORDER,
        "tableAccent": TEAL,
        "good": TEAL,
        "neutral": AMBER,
        "bad": "#EA8F91",
        "textClasses": {
            "callout": {"color": TEXT, "fontFace": "Segoe UI Semibold", "fontSize": 29},
            "title": {"color": TEXT, "fontFace": "Segoe UI Semibold", "fontSize": 13},
            "header": {"color": MUTED, "fontFace": "Segoe UI Semibold", "fontSize": 11},
            "label": {"color": MUTED, "fontFace": "Segoe UI", "fontSize": 10},
        },
        "visualStyles": {
            "*": {"*": {
                "background": [{"show": True, "color": {"solid": {"color": PANEL}}, "transparency": 0}],
                "title": [{"fontColor": {"solid": {"color": TEXT}}}],
            }},
        },
    }
    write_json(REPORT / "StaticResources" / "RegisteredResources" / CUSTOM_THEME, theme)


def write_semantic_model():
    write_json(MODEL / "definition.pbism", {
        "$schema": schema("item/semanticModel/definitionProperties/1.0.0"),
        "version": "4.0",
        "settings": {},
    })
    columns = []
    for display, source, data_type, format_string in COLUMNS:
        column = {
            "name": display, "dataType": data_type, "sourceColumn": source,
            "summarizeBy": "none", "lineageTag": lineage(display),
        }
        if format_string:
            column["formatString"] = format_string
        if display in {"Review flag", "Model version"}:
            column["isHidden"] = True
        columns.append(column)
    measures = [
        {"name": name, "expression": dax, "formatString": format_string, "lineageTag": lineage(name)}
        for name, dax, format_string in MEASURES
    ]
    write_json(MODEL / "model.bim", {
        "name": NAME,
        "compatibilityLevel": 1601,
        "model": {
            "culture": "en-US",
            "sourceQueryCulture": "en-US",
            "defaultMode": "directQuery",
            "defaultPowerBIDataSourceVersion": "powerBI_V3",
            "dataAccessOptions": {"legacyRedirects": True, "returnErrorValuesAsNull": True},
            "tables": [{
                "name": TABLE,
                "description": "Scored readings over the most recent 24 hours. Filter Data source to isolate replay or live telemetry.",
                "lineageTag": lineage(TABLE),
                "columns": columns,
                "measures": measures,
                "partitions": [{
                    "name": TABLE,
                    "mode": "directQuery",
                    "source": {"type": "m", "expression": [
                        "let",
                        '    Source = PostgreSQL.Database("127.0.0.1:5433", "maintenance"),',
                        '    Readings = Source{[Schema="public", Item="powerbi_telemetry"]}[Data]',
                        "in",
                        "    Readings",
                    ]},
                }],
            }],
            "annotations": [
                {"name": "PBI_QueryOrder", "value": '["Telemetry"]'},
                {"name": "__PBI_TimeIntelligenceEnabled", "value": "0"},
            ],
        },
    })


def write_report():
    write_json(PROJECT / f"{NAME}.pbip", {
        "$schema": schema("pbip/pbipProperties/1.0.0"),
        "version": "1.0",
        "artifacts": [{"report": {"path": f"{NAME}.Report"}}],
        "settings": {"enableAutoRecovery": True},
    })
    write_json(REPORT / "definition.pbir", {
        "$schema": schema("item/report/definitionProperties/2.0.0"),
        "version": "4.0",
        "datasetReference": {"byPath": {"path": f"../{NAME}.SemanticModel"}},
    })
    write_json(DEFINITION / "version.json", {"$schema": report_schema("versionMetadata/1.0.0"), "version": "2.0.0"})
    theme_version = {"visual": "2.5.0", "report": "3.1.0", "page": "2.3.0"}
    write_json(DEFINITION / "report.json", {
        "$schema": report_schema("report/3.2.0"),
        "themeCollection": {
            "baseTheme": {"name": BASE_THEME, "reportVersionAtImport": theme_version, "type": "SharedResources"},
            "customTheme": {"name": CUSTOM_THEME, "reportVersionAtImport": theme_version, "type": "RegisteredResources"},
        },
        "resourcePackages": [
            {"name": "SharedResources", "type": "SharedResources", "items": [
                {"name": BASE_THEME, "path": f"BaseThemes/{BASE_THEME}.json", "type": "BaseTheme"},
            ]},
            {"name": "RegisteredResources", "type": "RegisteredResources", "items": [
                {"name": CUSTOM_THEME, "path": CUSTOM_THEME, "type": "CustomTheme"},
            ]},
        ],
        "settings": {"useStylableVisualContainerHeader": True, "useEnhancedTooltips": True, "defaultFilterActionIsDataFilter": True},
    })
    page_id = id_for("fleet-overview")
    page = DEFINITION / "pages" / page_id
    write_json(DEFINITION / "pages" / "pages.json", {
        "$schema": report_schema("pagesMetadata/1.0.0"),
        "pageOrder": [page_id],
        "activePageName": page_id,
    })
    write_json(page / "page.json", {
        "$schema": report_schema("page/2.1.0"),
        "name": page_id, "displayName": "Fleet overview", "displayOption": "FitToPage", "width": 1440, "height": 900,
        "objects": {"background": [{"properties": {"color": fill(CANVAS), "transparency": format_property(0)}}]},
    })
    visual(page, "header", "textbox", (32, 22, 925, 96), 1, objects=text_objects([
        ("INDUSTRIAL ANALYTICS  /  OPERATIONS", TEAL, 11, True),
        ("Maintenance Watch", TEXT, 31, True),
        ("24-hour rolling telemetry · UTC · Select one source for review", MUTED, 11, False),
    ]))
    visual(page, "mode", "slicer", (969, 33, 242, 76), 2, "Data source", {"Values": [projection("Data source")]}, {
        "data": [{"properties": {"mode": format_property("Dropdown")}}],
        "selection": [{"properties": {"singleSelect": format_property(True)}}],
        "general": [{"properties": {"filter": {"filter": filter_value("Data source", "Synthetic replay")}}}],
    })
    visual(page, "asset", "slicer", (1223, 33, 185, 76), 3, "Asset", {"Values": [projection("Asset")]}, {
        "data": [{"properties": {"mode": format_property("Dropdown")}}],
    })
    cards = [("Readings", "Readings in window"), ("Assets reporting", "Assets reporting"),
             ("Review readings", "Readings above 50%"), ("Last received", "Last received UTC")]
    for index, (measure, title) in enumerate(cards):
        visual(page, measure, "cardVisual", (32 + index * 348, 124, 332, 106), 4 + index, title, {"Data": [projection(measure, "Measure")]})
    visual(page, "trend", "lineChart", (32, 248, 874, 284), 8, "Failure score by minute · UTC", {
        "Category": [projection("Minute UTC")], "Y": [projection("Peak score", "Measure"), projection("Mean score", "Measure")],
    }, sort=("Minute UTC", "Column", "Ascending"))
    visual(page, "alerts-by-asset", "barChart", (923, 248, 485, 284), 9, "Review readings by asset", {
        "Category": [projection("Asset")], "Y": [projection("Review readings", "Measure")],
    }, sort=("Review readings", "Measure", "Descending"))
    fields = ("Reading UTC", "Asset", "Model score", "Grade", "Torque Nm", "Rotational speed rpm", "Tool wear min", "Event ID")
    visual(page, "queue", "tableEx", (32, 550, 1376, 296), 10, "Recent readings flagged for review", {
        "Values": [projection(name) for name in fields],
    }, sort=("Reading UTC", "Column", "Descending"), filters=[{
        "name": id_for("queue-filter"),
        "field": query_field("Score status"),
        "type": "Categorical",
        "filter": filter_value("Score status", "Review"),
    }])
    validation = json.loads((ROOT / "artifacts" / "validation.json").read_text(encoding="utf-8"))
    caption = (
        f"OFFLINE VALIDATION  ·  {validation['accuracy']:.1%} accuracy  ·  "
        f"{validation['failure_recall']:.1%} failure recall  ·  "
        f"{validation['confusion']['false_negative']} missed failures  ·  AI4I synthetic holdout"
    )
    visual(page, "footer", "textbox", (32, 860, 1376, 32), 11, objects=text_objects([(caption, MUTED, 10, False)]))


def snapshot_expression(rows):
    m_types = {"string": "text", "dateTime": "datetime", "double": "number", "int64": "number"}
    type_fields = ", ".join(f"{source} = {m_types[data_type]}" for _, source, data_type, _ in COLUMNS)
    serialized = []
    for row in rows:
        reading = datetime.fromisoformat(row["observed_at"])
        received = datetime.fromisoformat(row["ingested_at"])
        record = {
            **row,
            "data_source": "Synthetic replay",
            "reading_utc": reading,
            "ingestion_utc": received,
            "minute_utc": reading.replace(second=0, microsecond=0),
            "score_status": "Review" if row["review_flag"] == "1" else "Normal score",
        }
        fields = []
        for _, source, data_type, _ in COLUMNS:
            value = record[source]
            if data_type == "string":
                fields.append('"' + value.replace('"', '""') + '"')
            elif data_type == "dateTime":
                parts = (value.year, value.month, value.day, value.hour, value.minute, value.second)
                fields.append("#datetime(" + ", ".join(map(str, parts)) + ")")
            elif data_type == "int64":
                fields.append(str(int(value)) if value else "null")
            else:
                number = float(value)
                if not math.isfinite(number):
                    raise ValueError(f"Non-finite replay value in {source}")
                fields.append(repr(number))
        serialized.append("            {" + ", ".join(fields) + "}")
    return [
        "let",
        "    Snapshot = #table(",
        f"        type table [{type_fields}],",
        "        {",
        *[line + ("," if index < len(serialized) - 1 else "") for index, line in enumerate(serialized)],
        "        }",
        "    ),",
        '    Typed = Table.TransformColumnTypes(Snapshot, {{"review_flag", Int64.Type}, {"observed_failure", Int64.Type}})',
        "in",
        "    Typed",
    ]


def write_snapshot_project():
    target = ROOT / "powerbi_demo"
    demo_name = "MaintenanceWatchDemo"
    report = target / f"{demo_name}.Report"
    model = target / f"{demo_name}.SemanticModel"
    skip_desktop_cache = ignore_patterns(".pbi")
    copytree(REPORT, report, dirs_exist_ok=True, ignore=skip_desktop_cache)
    copytree(MODEL, model, dirs_exist_ok=True, ignore=skip_desktop_cache)

    with (ROOT / "data" / "tableau_replay_sample.csv").open(newline="", encoding="utf-8") as source:
        sample = list(csv.DictReader(source))
    if not sample or any(row["source_mode"] != "replay" for row in sample):
        raise ValueError("The Power BI demo requires a synthetic replay sample.")

    def read_json(path):
        return json.loads(path.read_text(encoding="utf-8"))

    dataset_reference = read_json(report / "definition.pbir")
    dataset_reference["datasetReference"]["byPath"]["path"] = f"../{demo_name}.SemanticModel"
    write_json(report / "definition.pbir", dataset_reference)
    definition = read_json(model / "model.bim")
    definition["name"] = demo_name
    definition["model"]["defaultMode"] = "import"
    table = definition["model"]["tables"][0]
    table["description"] = "Embedded AI4I synthetic replay snapshot. Timestamps were assigned for the demo."
    partition = table["partitions"][0]
    partition["mode"] = "import"
    partition["source"]["expression"] = snapshot_expression(sample)
    write_json(model / "model.bim", definition)

    page = report / "definition" / "pages" / id_for("fleet-overview")
    page_info = read_json(page / "page.json")
    page_info["displayName"] = "Fleet demo"
    write_json(page / "page.json", page_info)
    header = page / "visuals" / id_for("header") / "visual.json"
    header_visual = read_json(header)
    header_visual["visual"]["objects"] = text_objects([
        ("INDUSTRIAL ANALYTICS  /  EMBEDDED SAMPLE", TEAL, 11, True),
        ("Maintenance Watch", TEXT, 31, True),
        (f"{len(sample)} synthetic readings · UTC · Sample captured for this demo", MUTED, 11, False),
    ])
    write_json(header, header_visual)
    for name, title in (("Readings", "Readings in sample"), ("Last received", "Last sample UTC")):
        card = page / "visuals" / id_for(name) / "visual.json"
        card_visual = read_json(card)
        card_visual["visual"]["visualContainerObjects"] = panel_title(title)
        write_json(card, card_visual)
    project = read_json(PROJECT / f"{NAME}.pbip")
    project["artifacts"][0]["report"]["path"] = f"powerbi_demo/{demo_name}.Report"
    write_json(ROOT / "OPEN_POWER_BI_DEMO.pbip", project)


if __name__ == "__main__":
    write_theme()
    write_semantic_model()
    write_report()
    write_snapshot_project()
    print(PROJECT / f"{NAME}.pbip")
    print(ROOT / "OPEN_POWER_BI_DEMO.pbip")
