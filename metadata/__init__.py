"""Metadata-paket för DOCX/XLSX/DWG/DGN-tolkning och validering.

Delade typer och den fildispatchande in-/utgången (`extract_metadata`,
`update_metadata`) ligger här, direkt porterade från
`.old/python_v5/src/version_3.py`. Den faktiska läs-/skrivlogiken per
filformat ligger i undermodulerna `metadata.docx` och `metadata.xlsx`
(importeras lokalt/sent i funktionerna nedan för att undvika cirkulär
import, eftersom de i sin tur importerar `MetadataField`/
`DocumentMetadata`/`MetadataError` härifrån).

Kravkälla: docs/kravsparning.md, sektion AFC.1/EAA/EAB/EAC.
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from zipfile import ZipFile

SUPPORTED_EXTENSIONS = {".docx", ".xlsx"}
"""Filtyper som metadataflödet kan läsa/skriva. Speglar avsiktligt
version_3.py:s egen ``SUPPORTED_EXTENSIONS`` (som i baslinjen är en
egen, separat definierad konstant skild från version_1.py:s
motsvarighet) i stället för att importera backend.file_discovery:s
konstant över paketgränsen."""


class MetadataError(RuntimeError):
    """Kastas när dokumentmetadata inte kan läsas eller uppdateras säkert."""


@dataclass(frozen=True, slots=True)
class MetadataField:
    key: str
    label: str
    value: str
    editable: bool
    location: str


@dataclass(frozen=True, slots=True)
class DocumentMetadata:
    path: Path
    document_type: Literal["docx", "xlsx"]
    fields: tuple[MetadataField, ...]


def extract_metadata(path: str | Path) -> DocumentMetadata:
    """Läser metadatafält ur ett DOCX- eller XLSX-dokument.

    Direkt port av ``version_3.py:extract_metadata``. Dispatchar till
    ``metadata.docx.extract_docx_metadata`` respektive
    ``metadata.xlsx.extract_xlsx_metadata`` baserat på filändelse.
    """
    document_path = Path(path).expanduser().resolve()
    if not document_path.is_file():
        raise FileNotFoundError(f"Filen finns inte: {document_path}")
    suffix = document_path.suffix.lower()
    if suffix == ".docx":
        from metadata.docx import extract_docx_metadata

        return extract_docx_metadata(document_path)
    if suffix == ".xlsx":
        from metadata.xlsx import extract_xlsx_metadata

        return extract_xlsx_metadata(document_path)
    raise ValueError(f"Filtypen stöds inte: {document_path.suffix}")


def _replace_after_office_release(source: Path, destination: Path) -> None:
    """Ersätter ``destination`` med ``source`` och väntar ut ev. kvarhållet
    filhandtag från Word/Excel. Direkt port av
    ``version_3.py:_replace_after_office_release`` (delad mellan
    DOCX- och XLSX-skrivflödet i baslinjen, därför placerad här i
    stället för i en av filtyps-undermodulerna).
    """
    destination_mode = destination.stat().st_mode
    destination_was_read_only = not bool(destination_mode & stat.S_IWRITE)
    if destination_was_read_only:
        destination.chmod(destination_mode | stat.S_IWRITE)

    last_error: PermissionError | None = None
    try:
        for _ in range(25):
            try:
                os.replace(source, destination)
                if destination_was_read_only:
                    destination.chmod(destination_mode)
                return
            except PermissionError as exc:
                last_error = exc
                time.sleep(0.2)
    except Exception:
        if destination.exists() and destination_was_read_only:
            destination.chmod(destination_mode)
        raise

    if destination.exists() and destination_was_read_only:
        destination.chmod(destination_mode)
    raise MetadataError(
        f"Filen kunde inte ersättas: {destination.name}. "
        "Stäng dokumentet i Word/Excel och försök igen."
    ) from last_error


def _prepare_update(path: str | Path, changes: dict[str, str]):
    document = extract_metadata(path)
    fields_by_key = {field.key: field for field in document.fields}

    from backend.validation import validate_metadata_changes

    validate_metadata_changes(fields_by_key, changes)
    return document, fields_by_key


def _discard_temporary(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except PermissionError:
        pass


def _finalize_update(temporary_path: Path, destination: Path) -> None:
    with ZipFile(temporary_path, "r") as archive:
        bad_member = archive.testzip()
    if bad_member is not None:
        raise MetadataError(f"Office skapade ett skadat dokument: {bad_member}")
    _replace_after_office_release(temporary_path, destination)


def update_metadata(path: str | Path, changes: dict[str, str]) -> Path:
    """Uppdaterar metadatafält i ett DOCX- eller XLSX-dokument.

    Direkt port av ``version_3.py:update_metadata``. Validerar
    ändringarna mot de fält som redan finns via
    ``backend.validation.validate_metadata_changes`` (okända/låsta
    nycklar ger fel), skriver via en temporärfil i samma katalog, och
    ersätter originalet atomärt efter att ha kontrollerat att
    resultatet är en giltig zip-arkivfil.
    """
    document, _fields_by_key = _prepare_update(path, changes)
    if not changes:
        return document.path
    return update_many_metadata({document.path: changes})[0]


def update_many_metadata(changes_by_document: dict[Path, dict[str, str]]) -> tuple[Path, ...]:
    """Uppdaterar flera dokument; alla DOCX skrivs i *en* Word-instans.

    Kontrakt (samma delresultat som baslinjens fil-för-fil-loop):
    * Alla ändringar valideras innan något skrivs; ett valideringsfel
      lämnar alla filer orörda.
    * Varje dokument skrivs till en temporärfil i samma katalog; originalet
      ersätts först efter zip-kontroll (``_finalize_update``).
    * Filerna publiceras i ordning. Vid första fel publiceras inga fler,
      alla återstående temporärfiler tas bort och felet höjs. Redan
      publicerade filer förblir uppdaterade.
    """
    prepared = []
    for path, changes in changes_by_document.items():
        if not changes:
            continue
        document, fields_by_key = _prepare_update(path, changes)
        prepared.append((document, fields_by_key, changes))
    if not prepared:
        return ()

    from metadata.docx import run_docx_update_batch
    from metadata.xlsx import write_xlsx_metadata

    temporaries: list[Path] = []
    try:
        for document, _fields, _changes in prepared:
            fd, temporary_name = tempfile.mkstemp(
                prefix=f".{document.path.stem}-metadata-",
                suffix=document.path.suffix,
                dir=str(document.path.parent),
            )
            os.close(fd)
            temporaries.append(Path(temporary_name))

        docx_jobs: list[tuple[Path, dict[str, str]]] = []
        docx_job_index: dict[int, int] = {}
        for position, (document, fields_by_key, changes) in enumerate(prepared):
            temporary_path = temporaries[position]
            if document.document_type == "docx":
                # copyfile, inte copy2: skrivskyddsattributet får inte följa med
                # till temporärfilen, annars hänger Word på Save.
                shutil.copyfile(document.path, temporary_path)
                docx_job_index[position] = len(docx_jobs)
                docx_jobs.append((temporary_path, changes))
        outcome = run_docx_update_batch(docx_jobs)

        updated: list[Path] = []
        for position, (document, fields_by_key, changes) in enumerate(prepared):
            temporary_path = temporaries[position]
            if document.document_type == "docx":
                job = docx_job_index[position]
                if job >= outcome.completed:
                    message = outcome.message or "Okänt Office-fel"
                    raise MetadataError(
                        f"Metadatauppdateringen misslyckades för {document.path.name}: {message}"
                    )
            else:
                write_xlsx_metadata(document.path, temporary_path, fields_by_key, changes)
            _finalize_update(temporary_path, document.path)
            updated.append(document.path)
        return tuple(updated)
    finally:
        for temporary_path in temporaries:
            if temporary_path.exists():
                _discard_temporary(temporary_path)
