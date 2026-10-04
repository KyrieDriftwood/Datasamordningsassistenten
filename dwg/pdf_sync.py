"""Persistent, signature-based DWG/PDF synchronization status."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Literal

_STATUS_FILE = ".datasamordning_pdf_status.json"
_logger = logging.getLogger(__name__)

PdfSyncState = Literal["synced", "stale", "missing"]
FileSignature = dict[str, int]
StatusRecord = dict[str, object]


def _signature(path: Path) -> FileSignature | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def load_status_records(project_root: Path) -> dict[str, StatusRecord]:
    status_file = project_root / _STATUS_FILE
    if not status_file.exists():
        return {}
    try:
        data = json.loads(status_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Kunde inte läsa PDF-synkstatus från {status_file}: {exc}") from exc
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("drawings"), dict):
        raise ValueError(f"PDF-synkstatus har ett okänt format: {status_file}")
    records = data["drawings"]
    if not all(isinstance(key, str) and isinstance(value, dict) for key, value in records.items()):
        raise ValueError(f"PDF-synkstatus innehåller ogiltiga ritningsposter: {status_file}")
    return records


def _drawing_key(project_root: Path, drawing: Path) -> str:
    try:
        return drawing.resolve().relative_to(project_root.resolve()).as_posix()
    except ValueError:
        raise ValueError(f"DWG-filen ligger utanför projektmappen: {drawing}") from None


def status_for(
    project_root: Path,
    drawing: Path,
    records: dict[str, StatusRecord],
) -> tuple[PdfSyncState, Path]:
    key = _drawing_key(project_root, drawing)
    record = records.get(key)
    output = drawing.with_suffix(".pdf")
    current_drawing = _signature(drawing)
    current_pdf = _signature(output)
    if current_pdf is None:
        return "missing", output
    if (
        record is not None
        and record.get("pdf") == output.name
        and record.get("dwg_signature") == current_drawing
        and record.get("pdf_signature") == current_pdf
    ):
        return "synced", output
    return "stale", output


def record_verified_pdf(project_root: Path, drawing: Path, output: Path) -> None:
    drawing_signature = _signature(drawing)
    pdf_signature = _signature(output)
    if drawing_signature is None:
        raise ValueError(f"DWG-filen saknas efter plotten: {drawing}")
    if pdf_signature is None:
        raise ValueError(f"Verifierad PDF saknas efter plotten: {output}")

    status_file = project_root / _STATUS_FILE
    records = load_status_records(project_root)
    key = _drawing_key(project_root, drawing)
    records[key] = {
        "pdf": output.name,
        "dwg_signature": drawing_signature,
        "pdf_signature": pdf_signature,
    }
    _save_status_records(status_file, records, "spara")


def clear_pdf_sync_record(project_root: Path, drawing: Path) -> None:
    status_file = project_root / _STATUS_FILE
    records = load_status_records(project_root)
    key = _drawing_key(project_root, drawing)
    if records.pop(key, None) is not None:
        _save_status_records(status_file, records, "rensa")


def _save_status_records(status_file: Path, records: dict[str, StatusRecord], operation: str) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=status_file.parent,
            prefix=f"{_STATUS_FILE}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            json.dump({"version": 1, "drawings": records}, temporary_file, ensure_ascii=False, indent=2)
            temporary_file.write("\n")
        os.replace(temporary_path, status_file)
    except OSError as exc:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                _logger.warning("Kunde inte rensa temporär synkstatusfil %s", temporary_path, exc_info=True)
        raise OSError(f"Kunde inte {operation} PDF-synkstatus i {status_file}: {exc}") from exc
