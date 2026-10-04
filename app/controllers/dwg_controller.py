"""Kontroller för DWG-namngivning, attributläsning/skrivning och backup.

Motsvarar logiken i:
    .old/python_v5/src/version_5.py (accoreconsole/AutoLISP, TRVJ_NAMNRUTA)
    .old/python_v5/src/version_5_rust.py (Rust/LibreDWG snabbläsning)

Kravkälla: docs/kravsparning.md, krav-ID:n under sektion CBE/D/J.

Status: klar (Steg 4, Fas 4). Tunn kontroller-yta ovanpå de redan
portade och parity-testade modulerna ``dwg.rust_bridge`` (snabb
läsning), ``dwg.writers`` (skrivning med backup + read-back-
verifiering, se Fas 3) och ``backend.naming`` (filnamnsbyten). UI-lagret
(``app/ui/main_window.py``) pratar bara med denna modul, inte direkt
med ``dwg``/``backend``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path

from backend.naming import rename_many_dwg_files
from dwg import (
    DwgAttributeResult,
    DwgUpdateResult,
    TRVJ_ATTRIBUTE_SCHEMA,
    TRVJ_MODEL_BLOCK_NAME,
)
from dwg.readers import extract_many_dwg_attributes_for_block
from dwg.pdf_plotter import DwgPdfJob, DwgPdfPlotResult, plot_many
from dwg.rust_bridge import (
    extract_many_dwg_attributes_fast,
    extract_many_dwg_model_attributes,
)
from dwg.rust_bridge import is_available as is_rust_dwg_extractor_available
from dwg.writers import (
    DEFAULT_AUTOCAD_WRITE_WORKERS,
    update_many_dwg_attributes,
    update_many_dwg_model_attributes,
)

__all__ = [
    "TRVJ_ATTRIBUTE_SCHEMA",
    "extract_attributes_fast",
    "extract_model_attributes_fast",
    "is_rust_dwg_extractor_available",
    "plot_dwgs_to_pdfs",
    "rename_files",
    "write_attribute_changes",
    "write_model_attribute_changes",
]


def extract_attributes_fast(
    paths: Iterable[str | Path],
    *,
    max_workers: int = 1,
    progress_callback: Callable[[int, int], None] | None = None,
) -> dict[Path, DwgAttributeResult]:
    """Läser TRVJ_NAMNRUTA-attribut för flera DWG-filer via Rust/LibreDWG.

    Motsvarar den automatiska bakgrundsinläsning baslinjen gör så fort
    en DWG-fil kryssas i (se app_v5_desktop.py:_start_lazy_dwg_attribute_load).
    """
    return extract_many_dwg_attributes_fast(paths, max_workers=max_workers, progress_callback=progress_callback)


def extract_model_attributes_fast(
    paths: Iterable[str | Path],
    *,
    max_workers: int = 1,
    progress_callback: Callable[[int, int], None] | None = None,
) -> dict[Path, DwgAttributeResult]:
    """Läser modellnamnrutan med Rust och reparerar kända MText-taggekon via AutoCAD."""
    resolved_paths = tuple(Path(path).expanduser().resolve() for path in paths)
    if not resolved_paths:
        return {}

    results = extract_many_dwg_model_attributes(
        resolved_paths,
        max_workers=max_workers,
        progress_callback=progress_callback,
    )
    fallback_paths = tuple(
        path
        for path in resolved_paths
        if (result := results[path]).error is None
        and any(field.value == field.key for field in result.fields)
    )
    if not fallback_paths:
        return results

    fallback_results = extract_many_dwg_attributes_for_block(
        fallback_paths,
        TRVJ_MODEL_BLOCK_NAME,
        max_workers=max_workers,
    )
    for path in fallback_paths:
        fallback = fallback_results[path]
        if fallback.error is not None:
            results[path] = DwgAttributeResult(
                path,
                (),
                "Rust/LibreDWG läste ett eller flera MText-attribut som taggnamn, "
                f"och AutoCAD-reservläsningen misslyckades: {fallback.error}",
            )
        else:
            results[path] = fallback
    return results


def plot_dwgs_to_pdfs(
    paths: Iterable[str | Path],
    *,
    standard_metadata: dict[Path, DwgAttributeResult] | None = None,
    model_metadata: dict[Path, DwgAttributeResult] | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
) -> dict[Path, DwgPdfPlotResult]:
    """Plottar valda DWG-filer; metadataavvikelser varnar men stoppar inte plotten."""
    resolved_paths = tuple(dict.fromkeys(Path(path).expanduser().resolve() for path in paths))
    if not resolved_paths:
        return {}

    standard_metadata = dict(standard_metadata or {})
    missing_standard = tuple(path for path in resolved_paths if path not in standard_metadata)
    if missing_standard:
        standard_metadata.update(extract_many_dwg_attributes_fast(missing_standard))

    model_metadata = dict(model_metadata or {})
    missing_model = tuple(path for path in resolved_paths if path not in model_metadata)
    if missing_model:
        model_metadata.update(extract_many_dwg_model_attributes(missing_model))
    jobs: list[DwgPdfJob] = []
    for drawing in resolved_paths:
        standard_result = standard_metadata[drawing]
        verification_issue = standard_result.error
        expected_values = [
            (field.label, field.value)
            for field in standard_result.fields
            if field.value.strip()
        ]
        model_result = model_metadata[drawing]
        if model_result.error is None:
            expected_values.extend(
                (field.label, field.value)
                for field in model_result.fields
                if field.value.strip()
            )
        if verification_issue is None and not expected_values:
            verification_issue = "DWG-filen saknar ifyllda namnrutevärden att jämföra med PDF-texten."
        jobs.append(
            DwgPdfJob(
                drawing,
                drawing.with_suffix(".pdf"),
                tuple(dict.fromkeys(expected_values)),
                verification_issue,
            )
        )
    return plot_many(
        jobs,
        progress_callback=(
            lambda done, _total, _result: progress_callback(done, len(resolved_paths))
            if progress_callback is not None
            else None
        ),
    )


def write_attribute_changes(
    changes_by_path: dict[Path, dict[str, str]],
    *,
    project_root: str | Path,
    max_workers: int = DEFAULT_AUTOCAD_WRITE_WORKERS,
    progress_callback: Callable[[int, int], None] | None = None,
) -> dict[Path, DwgUpdateResult]:
    """Skriver ändrade namnrute-attribut till DWG-filer via accoreconsole.

    Går via ``dwg.writers.update_many_dwg_attributes``, som (till
    skillnad från baslinjen) tar en backup-kopia av varje fil innan
    skrivning och läser tillbaka/verifierar filen efteråt, med
    automatisk återställning från backup om verifieringen misslyckas
    (se docs/plan.md, Fas 3).
    """
    return update_many_dwg_attributes(
        changes_by_path, project_root=project_root, max_workers=max_workers, progress_callback=progress_callback
    )


def write_model_attribute_changes(
    changes_by_path: dict[Path, dict[str, str]],
    *,
    project_root: str | Path,
    max_workers: int = DEFAULT_AUTOCAD_WRITE_WORKERS,
    progress_callback: Callable[[int, int], None] | None = None,
) -> dict[Path, DwgUpdateResult]:
    """Skriver modellnamnrute-attribut med backup och read-back-verifiering."""
    return update_many_dwg_model_attributes(
        changes_by_path,
        project_root=project_root,
        max_workers=max_workers,
        progress_callback=progress_callback,
    )


def rename_files(new_names_by_path: dict[Path, str]) -> dict[Path, Path]:
    """Byter namn på flera DWG-filer. Direkt vidarebefordran till
    ``backend.naming.rename_many_dwg_files``."""
    return rename_many_dwg_files(new_names_by_path)
