"""Kontroller för leverans-/exportflödet (PDF-export, ACC-leverans).

Motsvarar logiken i:
    .old/python_v5/src/version_2.py (PDF-export via Word/Excel COM)

Kravkälla: docs/kravsparning.md, krav-ID:n under sektion A/AFA (ACC-leverans).

Status: delvis klar (Steg 4, Fas 4). PDF-exportdelen är portad (se
``backend/pdf_export.py``, en direkt port av ``version_2.py``) och
exponeras här för UI-lagret. ACC-delen är fortsatt ett verifierat gap
(ingen ACC-integration finns i baslinjen) och kräver ett separat
designbeslut innan acc/-modulerna kopplas in här.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from backend.pdf_export import PdfExportError, PdfExporter, plot_document, plot_documents

__all__ = ["PdfExportError", "PdfExporter", "export_document_to_pdf", "export_documents_to_pdf"]


def export_document_to_pdf(path: str | Path, *, exporter: PdfExporter | None = None) -> Path:
    """Exporterar ett enskilt DOCX/XLSX-dokument till PDF."""
    return plot_document(path, exporter=exporter)


def export_documents_to_pdf(
    paths: Iterable[str | Path],
    output_dir: str | Path | None = None,
    *,
    exporter: PdfExporter | None = None,
) -> list[Path]:
    """Exporterar flera DOCX/XLSX-dokument till PDF."""
    return plot_documents(paths, output_dir, exporter=exporter)
