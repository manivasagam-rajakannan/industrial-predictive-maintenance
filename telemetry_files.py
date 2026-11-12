"""Decode tabular uploads without changing the models' sensor contracts."""

import csv
from datetime import date, datetime, time
from decimal import Decimal
import io
import json
from pathlib import Path
import re
from zipfile import ZipFile, is_zipfile


TEXT_FORMATS = ("csv", "tsv", "txt", "log", "dat")
WORKBOOK_FORMATS = ("xlsx", "xls", "xlsm", "ods")
JSON_FORMATS = ("json", "jsonl", "ndjson")
FORMATS = (*TEXT_FORMATS, *WORKBOOK_FORMATS, *JSON_FORMATS, "xml", "parquet", "docx", "pdf")
FORMAT_HELP = "CSV, TSV, delimited text, Excel, OpenDocument, JSON, XML, Parquet, Word tables, and text PDF tables"
MAX_COLUMNS = 256
MAX_EXPANDED_BYTES = 24_000_000
MAX_PDF_PAGES = 20


class UploadError(ValueError):
    """An uploaded file could not be read as a sensor table."""


def normalize_heading(heading: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", heading.strip().lower()).strip("_")


def decode_text(content: bytes) -> str:
    if content.startswith((b"\xff\xfe", b"\xfe\xff")):
        return content.decode("utf-16")
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return content.decode("cp1252")


def read_delimited(content: bytes):
    text = decode_text(content)
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", text):
        raise UploadError("Save the sensor text as UTF-8 or UTF-16 with column headings.")
    lines = io.StringIO(text, newline="")
    header = next((line for line in lines if line.strip()), "")
    try:
        delimiter = csv.Sniffer().sniff(header, delimiters=",\t;|").delimiter
    except csv.Error as error:
        raise UploadError("Separate the columns with commas, tabs, semicolons, or pipes.") from error
    lines.seek(0)
    reader = csv.reader(lines, delimiter=delimiter, strict=True)
    for cells in reader:
        yield reader.line_num, cells


def cell_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (date, datetime, time)):
        return value.isoformat()
    if isinstance(value, (str, int, float, Decimal)):
        return str(value).strip()
    raise UploadError("Use a flat table with one value in each cell.")


def validate_archive(content: bytes):
    with ZipFile(io.BytesIO(content)) as archive:
        entries = archive.infolist()
        if len(entries) > 2_000 or sum(entry.file_size for entry in entries) > MAX_EXPANDED_BYTES:
            raise UploadError("The workbook or document expands beyond the upload limit. Export just the readings.")
        if any(entry.flag_bits & 1 for entry in entries):
            raise UploadError("Upload an unlocked copy of the workbook or document.")


def read_workbook(content: bytes, max_rows: int):
    from python_calamine import CalamineWorkbook

    if is_zipfile(io.BytesIO(content)):
        validate_archive(content)
    with CalamineWorkbook.from_filelike(io.BytesIO(content)) as workbook:
        sheet = workbook.get_sheet_by_index(0)
        if sheet.width > MAX_COLUMNS or sheet.height > max_rows + 1:
            raise UploadError(f"Put up to {max_rows:,} readings and {MAX_COLUMNS} columns on the first sheet.")
        return enumerate(sheet.to_python(), (sheet.start[0] + 1) if sheet.start else 1)


def unique_object(items):
    row = dict(items)
    if len(row) != len(items):
        raise UploadError("Each JSON record must use unique field names.")
    return row


def record_rows(records, max_rows: int):
    if not isinstance(records, list) or not all(isinstance(row, dict) for row in records):
        raise UploadError("Use a list of flat reading objects with one object per reading.")
    if len(records) > max_rows:
        raise UploadError(f"Upload up to {max_rows:,} readings at a time.")
    headings = list(dict.fromkeys(key for row in records for key in row))
    if len(headings) > MAX_COLUMNS:
        raise UploadError(f"Use up to {MAX_COLUMNS} columns.")
    yield 1, headings
    for number, row in enumerate(records, 2):
        yield number, [row.get(key) for key in headings]


def read_json(content: bytes, extension: str, max_rows: int):
    source = decode_text(content)
    if extension == "json":
        data = json.loads(source, object_pairs_hook=unique_object)
        if isinstance(data, dict):
            arrays = (data[key] for key in ("readings", "records", "data") if isinstance(data.get(key), list))
            data = next(arrays, [data])
    else:
        data = []
        for line in io.StringIO(source):
            if line.strip():
                data.append(json.loads(line, object_pairs_hook=unique_object))
                if len(data) > max_rows:
                    raise UploadError(f"Upload up to {max_rows:,} readings at a time.")
    return record_rows(data, max_rows)


def read_xml(content: bytes, max_rows: int):
    from defusedxml.ElementTree import fromstring

    root = fromstring(content, forbid_dtd=True)
    records = []
    for row in root:
        fields = [(field.tag.rsplit("}", 1)[-1], field.text) for field in row]
        if not fields or any(len(field) for field in row) or len(dict(fields)) != len(fields):
            raise UploadError("Use one XML element per reading and one named child per sensor value.")
        records.append(dict(fields))
        if len(records) > max_rows:
            raise UploadError(f"Upload up to {max_rows:,} readings at a time.")
    return record_rows(records, max_rows)


def read_parquet(content: bytes, max_rows: int):
    from pyarrow import parquet

    with parquet.ParquetFile(
        io.BytesIO(content), thrift_string_size_limit=5_000_000, thrift_container_size_limit=100_000,
    ) as source:
        meta = source.metadata
        if meta.num_rows > max_rows or meta.num_columns > MAX_COLUMNS:
            raise UploadError(f"Use up to {max_rows:,} readings and {MAX_COLUMNS} columns.")
        if sum(meta.row_group(index).total_byte_size for index in range(meta.num_row_groups)) > MAX_EXPANDED_BYTES:
            raise UploadError("The Parquet data expands beyond the upload limit. Export fewer columns or readings.")
        yield 1, source.schema_arrow.names
        number = 2
        for batch in source.iter_batches(batch_size=512, use_threads=False):
            for row in zip(*(column.to_pylist() for column in batch.columns), strict=True):
                yield number, row
                number += 1


def read_word(content: bytes):
    from defusedxml.ElementTree import fromstring

    validate_archive(content)
    with ZipFile(io.BytesIO(content)) as archive:
        root = fromstring(archive.read("word/document.xml"), forbid_dtd=True)
    namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    table = root.find(".//w:tbl", namespace)
    if table is None:
        raise UploadError("Put the readings in a Word table with the column headings in its first row.")
    for number, row in enumerate(table.findall("w:tr", namespace), 1):
        cells = []
        for cell in row.findall("w:tc", namespace):
            paragraphs = [
                "".join(node.text or "" for node in paragraph.findall(".//w:t", namespace))
                for paragraph in cell.findall("w:p", namespace)
            ]
            cells.append("\n".join(paragraphs))
        yield number, cells


def read_pdf(content: bytes):
    import pdfplumber

    headings = None
    number = 1
    with pdfplumber.open(io.BytesIO(content)) as document:
        if len(document.pages) > MAX_PDF_PAGES:
            raise UploadError(f"Upload up to {MAX_PDF_PAGES} PDF pages with a bordered sensor table.")
        for page in document.pages:
            for table in page.extract_tables():
                header, *rows = table
                header = [cell_text(cell) for cell in header]
                if not any(header):
                    continue
                if headings is None:
                    headings = header
                    yield number, headings
                    number += 1
                elif header != headings:
                    raise UploadError("Use one bordered sensor table or repeat the same headings on every page.")
                for row in rows:
                    yield number, row
                    number += 1
            page.close()
    if headings is None:
        raise UploadError("Use a bordered PDF table with selectable text. For a scan or photo, export the readings as a spreadsheet.")


def decode_rows(content: bytes, extension: str, max_rows: int):
    if extension in TEXT_FORMATS:
        return read_delimited(content)
    if extension in WORKBOOK_FORMATS:
        return read_workbook(content, max_rows)
    if extension in JSON_FORMATS:
        return read_json(content, extension, max_rows)
    if extension == "xml":
        return read_xml(content, max_rows)
    if extension == "parquet":
        return read_parquet(content, max_rows)
    if extension == "docx":
        return read_word(content)
    return read_pdf(content)


def read_table(content: bytes, filename: str, *, max_rows: int, max_bytes: int) -> list[tuple[int, list[str]]]:
    """Return numbered cells with the header first. The caller validates sensors."""
    if not 0 < len(content) <= max_bytes:
        raise UploadError(f"Choose a file with readings up to {max_bytes // 1_000_000} MB.")
    extension = Path(filename).suffix.lower().lstrip(".")
    if extension not in FORMATS:
        raise UploadError(f"Use one of these formats. {FORMAT_HELP}.")
    rows = []
    decoded_size = 0
    try:
        for number, row in decode_rows(content, extension, max_rows):
            if len(row) > MAX_COLUMNS:
                raise UploadError(f"Use up to {MAX_COLUMNS} columns.")
            cells = [cell_text(value) for value in row]
            if not any(cells):
                continue
            decoded_size += sum(len(cell) for cell in cells) + len(cells)
            if decoded_size > max_bytes * 2:
                raise UploadError("The extracted table is too large. Upload fewer readings or columns.")
            if len(rows) >= max_rows + 1:
                raise UploadError(f"Upload up to {max_rows:,} readings at a time.")
            rows.append((number, cells))
    except UploadError:
        raise
    except ImportError as error:
        raise UploadError("Install the file readers from upload-requirements.txt and restart the app.") from error
    except Exception as error:
        raise UploadError(f"The .{extension} file could not be read. Check the file format and upload an unlocked copy.") from error
    if len(rows) < 2:
        raise UploadError("Add a row of column headings and at least one sensor reading.")
    return rows
