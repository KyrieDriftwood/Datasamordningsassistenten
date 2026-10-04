"""Kontroller för panelen PDF-metadata (valbar vattenstämpel, standard KONTROLLÄRENDE).

Nytt krav utanför TB (rapporterat enligt AFC.27, kravsparning PDF-1).
Tunn yta mellan UI:t och ``backend/pdf_watermark.py`` som dessutom håller
DWG-PDF-synkstatusen (CBG-5) intakt: stämplingen ändrar PDF:ens storlek
och ändringstid, vilket annars skulle visa en nyss plottad PDF som
inaktuell trots att den fortfarande motsvarar ritningen.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from backend.file_discovery import find_pdf_files
from backend.pdf_watermark import (
    MAX_TEXT_LENGTH,
    WATERMARK_TEXT,
    PdfWatermarkError,
    PdfWatermarkResult,
    apply_control_watermark_state,
    read_watermark_text,
    validate_watermark_text,
)
from dwg.pdf_sync import load_status_records, record_verified_pdf, status_for

__all__ = [
    "MAX_TEXT_LENGTH",
    "WATERMARK_TEXT",
    "PdfEntry",
    "PdfWatermarkError",
    "PdfWatermarkResult",
    "apply_watermarks",
    "list_pdfs",
    "read_watermark_states",
    "validate_watermark_text",
]


@dataclass(frozen=True)
class PdfEntry:
    path: Path
    watermark_text: str | None
    error: str | None = None

    @property
    def has_watermark(self) -> bool | None:
        """None när filen inte kunde läsas."""
        return None if self.error is not None else self.watermark_text is not None


def list_pdfs(project_root: Path) -> list[Path]:
    return find_pdf_files(project_root, recursive=True)


def read_watermark_states(paths: Iterable[Path]) -> list[PdfEntry]:
    """Läser stämpeltext per fil; oläsbara filer får ``error`` satt."""
    entries: list[PdfEntry] = []
    for path in paths:
        try:
            entries.append(PdfEntry(path, read_watermark_text(path)))
        except PdfWatermarkError as exc:
            entries.append(PdfEntry(path, None, str(exc)))
    return entries


def _synced_drawings(project_root: Path, pdfs: Iterable[Path]) -> dict[Path, Path]:
    """PDF → syskon-DWG för de PDF:er som är synkade med sin ritning nu."""
    records = load_status_records(project_root)
    synced: dict[Path, Path] = {}
    for pdf in pdfs:
        drawing = pdf.with_suffix(".dwg")
        if not drawing.is_file():
            continue
        try:
            state, output = status_for(project_root, drawing, records)
        except ValueError:
            continue
        if state == "synced" and output.resolve() == pdf.resolve():
            synced[pdf] = drawing
    return synced


def apply_watermarks(
    project_root: Path, desired: Mapping[Path, bool], *, text: str = WATERMARK_TEXT
) -> list[PdfWatermarkResult]:
    """Sätter stämpeln ``text`` (True) eller tar bort den (False) enligt
    ``desired`` och bevarar DWG-synkstatus.

    Synkstatus förnyas bara för PDF:er som var synkade före stämplingen och
    som faktiskt ändrades; en redan inaktuell PDF förblir inaktuell.
    """
    root = Path(project_root).resolve()
    text = validate_watermark_text(text)
    synced_before = _synced_drawings(root, desired)
    results = apply_control_watermark_state(dict(desired), text=text)
    final: list[PdfWatermarkResult] = []
    for result in results:
        drawing = synced_before.get(result.path)
        if result.changed and result.error is None and drawing is not None:
            try:
                record_verified_pdf(root, drawing, result.path)
            except (OSError, ValueError) as exc:
                result = PdfWatermarkResult(
                    result.path,
                    True,
                    f"Vattenstämpeln sattes men DWG-synkstatus kunde inte sparas: {exc}",
                )
        final.append(result)
    return final
