"""Kontroller för metadataflödet (Fyll i Försättsida, Content Controls).

Motsvarar logiken i:
    .old/python_v5/src/version_3.py (DOCX Content Controls, XLSX-läsning)
    .old/python_v5/src/version_4.py (multi-edit, semantisk fältmappning)

Kravkälla: docs/kravsparning.md, krav-ID:n under sektion AFC.1/EAA/EAB/EAC.

Status: klar (Steg 4, Fas 4). Tunn kontroller-yta ovanpå
``metadata/grid.py`` (multiredigeringslogiken, redan portad och
parity-testad i Fas 2/4) — inga nya affärsregler läggs till här, bara
den samlingspunkt UI-lagret (``app/ui/main_window.py``) pratar med, så
att fönsterkoden slipper importera ``metadata``/``backend`` direkt.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from metadata.grid import (
    BatchOperationResult,
    MetadataCategory,
    MetadataGrid,
    extract_delivery_list_grid,
    extract_metadata_grid,
    is_delivery_list_document,
    load_metadata_categories,
    update_many_metadata,
    update_metadata_and_create_pdfs,
)

__all__ = [
    "BatchOperationResult",
    "MetadataCategory",
    "MetadataGrid",
    "build_delivery_list_grid",
    "build_grid",
    "default_categories",
    "is_delivery_list_document",
    "save_changes",
    "save_changes_and_create_pdfs",
]


def default_categories() -> tuple[MetadataCategory, ...]:
    """De fallback-metadatakategorier multiredigeringstabellen visar."""
    return load_metadata_categories()


def build_grid(paths: Iterable[str | Path]) -> MetadataGrid:
    """Bygger multiredigeringsrutnätet för de markerade dokumenten."""
    return extract_metadata_grid(paths)


def build_delivery_list_grid(paths: Iterable[str | Path]) -> MetadataGrid:
    """Bygger rutnätet för DOCX-filer i leveransförteckningsmallen (eget flöde)."""
    return extract_delivery_list_grid(paths)


def save_changes(changes_by_document: dict[Path, dict[str, str]]) -> tuple[Path, ...]:
    """Sparar metadataändringar och returnerar de uppdaterade dokumenten.

    Vattenstämpel sätts inte här (användarbeslut 2026-10-02, utanför TB):
    den hanteras bara i PDF-panelen.
    """
    return update_many_metadata(changes_by_document)


def save_changes_and_create_pdfs(
    checked_documents: Iterable[str | Path],
    changes_by_document: dict[Path, dict[str, str]],
) -> BatchOperationResult:
    """Sparar metadata och exporterar varje markerat dokument till PDF."""
    return update_metadata_and_create_pdfs(checked_documents, changes_by_document)
