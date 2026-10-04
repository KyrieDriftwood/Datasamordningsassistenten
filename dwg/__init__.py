"""DWG/DGN-paket för isolerad och säker CAD-behandling.

Delade typer, fel och den AutoCAD-headless-hjälplogik (accoreconsole-
lokalisering/anrop, AutoLISP-strängescaping, extraheringsparsning) som
``dwg.readers`` och ``dwg.writers`` båda bygger på ligger här, direkt
porterad från ``.old/python_v5/src/version_5.py``. Detta mönster
speglar ``metadata/__init__.py``.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from metadata import MetadataField

#: Blockets namn i AutoCAD-ritningen. Direkt port av
#: ``version_5.py:TRVJ_BLOCK_NAME``.
TRVJ_BLOCK_NAME = "TRVJ_NAMNRUTA"

TRVJ_MODEL_BLOCK_NAME = "TRVJ_NAMNRUTA_MODELL"

#: Attributtaggar i den ordning de listas i systembeskrivningen. Direkt
#: port av ``version_5.py:TRVJ_ATTRIBUTE_SCHEMA``.
TRVJ_ATTRIBUTE_SCHEMA: tuple[tuple[str, str], ...] = (
    ("Slm", "Slm"),
    ("Slkm", "Slkm"),
    ("Stm", "Stm"),
    ("Ritn_typ", "Ritn_typ"),
    ("Besk_3", "Besk_3"),
    ("Besk_2", "Besk_2"),
    ("Granskad_av", "Granskad_av"),
    ("Handlingstyp", "Handlingstyp"),
    ("Objekt_1", "Objekt_1"),
    ("Datum", "Datum"),
    ("Stkm", "Stkm"),
    ("Anltyp", "Anltyp"),
    ("Skapad_av", "Skapad_av"),
    ("Godkand_av", "Godkand_av"),
    ("Besk_1", "Besk_1"),
    ("Format", "Format"),
    ("Blad", "Blad"),
    ("Nasta_blad", "Nasta_blad"),
    ("Ritnr_proj", "Ritnr_proj"),
    ("Ritnr_forv", "Ritnr_forv"),
    ("Andr", "Andr"),
    ("Skala", "Skala"),
    ("Bdl", "Bdl"),
)

_ACCORECONSOLE_ENV_OVERRIDE = "DATASAMORDNING_ACCORECONSOLE"
_ACCORECONSOLE_TIMEOUT_SECONDS = 60
_ANSI_OUTPUT_ENCODING = "cp1252"

DEFAULT_MAX_PARALLEL_WORKERS = 4
"""Direkt port av ``version_5.py:_DEFAULT_MAX_PARALLEL_WORKERS``, gjord
publik här eftersom både readers.py och writers.py delar samma
standardvärde."""


class DwgError(RuntimeError):
    """Kastas när en DWG-fil inte kan listas, döpas om, läsas eller skrivas."""


class DwgAttributeError(DwgError):
    """Kastas när namnrute-attribut inte kan läsas eller skrivas."""


class AccoreconsoleNotFoundError(DwgAttributeError):
    """Kastas när ingen AutoCAD-installation med accoreconsole.exe hittas."""


@dataclass(frozen=True, slots=True)
class DwgAttributeResult:
    """Resultatet av en attributextraktion för en enskild DWG-fil."""

    path: Path
    fields: tuple[MetadataField, ...]
    error: str | None = None


@dataclass(frozen=True, slots=True)
class DwgUpdateResult:
    """Resultatet av en attributuppdatering för en enskild DWG-fil.

    `fields_before` är AutoCADs egen läsning av blocket direkt före
    ändringen i samma skrivsession. Den används internt av
    dwg/writers.py för AutoCAD-verifieringen och påverkar inte likhet.
    """

    path: Path
    error: str | None = None
    fields_before: tuple[MetadataField, ...] | None = field(default=None, compare=False, repr=False)


def to_lisp_path(path: Path) -> str:
    """AutoLISP föredrar snedstreck framåt, annars krävs dubbla bakstreck.

    Direkt port av ``version_5.py:_to_lisp_path``.
    """
    return str(path).replace("\\", "/")


def lisp_string_literal(value: str) -> str:
    """Escapar en Python-sträng till en giltig AutoLISP-strängliteral.

    Direkt port av ``version_5.py:_lisp_string_literal``.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _select_best_accoreconsole(candidates: Iterable[Path]) -> Path:
    """Väljer den bästa accoreconsole.exe-kandidaten ur en lista sökvägar.

    Direkt port av ``version_5.py:_select_best_accoreconsole``. Full
    AutoCAD prioriteras framför DWG TrueView (som saknar vissa
    kommandon), och nyare versioner (baserat på produktmappens namn)
    prioriteras framför äldre.
    """
    candidate_list = list(candidates)
    if not candidate_list:
        raise AccoreconsoleNotFoundError("Ingen accoreconsole.exe-kandidat angavs.")

    def sort_key(exe_path: Path) -> tuple[bool, str]:
        name = exe_path.parent.name.casefold()
        is_full_autocad = "trueview" not in name
        return (is_full_autocad, name)

    candidate_list.sort(key=sort_key, reverse=True)
    return candidate_list[0]


def locate_accoreconsole() -> Path:
    """Hittar accoreconsole.exe i en installerad AutoCAD-produkt.

    Direkt port av ``version_5.py:_locate_accoreconsole``. Sökvägen kan
    override:as med miljövariabeln ``DATASAMORDNING_ACCORECONSOLE``,
    t.ex. för tester eller alternativa installationsplatser.
    """
    override = os.environ.get(_ACCORECONSOLE_ENV_OVERRIDE)
    if override:
        override_path = Path(override)
        if override_path.is_file():
            return override_path
        raise AccoreconsoleNotFoundError(
            f"Miljövariabeln {_ACCORECONSOLE_ENV_OVERRIDE} pekar på en fil som inte finns: {override_path}"
        )

    autodesk_root = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Autodesk"
    if not autodesk_root.is_dir():
        raise AccoreconsoleNotFoundError(f"Hittade ingen Autodesk-installation under {autodesk_root}.")

    candidates: list[Path] = []
    for product_dir in autodesk_root.iterdir():
        if not product_dir.is_dir():
            continue
        exe_path = product_dir / "accoreconsole.exe"
        if exe_path.is_file():
            candidates.append(exe_path)

    if not candidates:
        raise AccoreconsoleNotFoundError(
            f"Hittade ingen accoreconsole.exe under {autodesk_root}. "
            "Kontrollera att AutoCAD eller DWG TrueView är installerat."
        )

    return _select_best_accoreconsole(candidates)


def run_accoreconsole_script(
    first_dwg_path: Path,
    lisp_body: str,
    *,
    timeout: int = _ACCORECONSOLE_TIMEOUT_SECONDS,
) -> None:
    """Kör ett AutoLISP-skript mot en initial DWG-fil via accoreconsole.

    Direkt port av ``version_5.py:_run_accoreconsole_script``. Skriptet
    ansvarar självt för att öppna eventuella ytterligare filer (via
    kommandot OPEN) samt för att skriva resultat till en fil — inte
    till standard ut, vars teckenkodning inte kan läsas tillförlitligt.
    """
    accoreconsole = locate_accoreconsole()

    with tempfile.TemporaryDirectory(prefix="datasamordning_dwg_") as temp_dir:
        temp_path = Path(temp_dir)
        lisp_path = temp_path / "script.lsp"
        scr_path = temp_path / "script.scr"

        lisp_path.write_text(lisp_body, encoding="utf-8")
        scr_content = f'(setvar "SECURELOAD" 0)\n(load "{to_lisp_path(lisp_path)}")\nQUIT\nY\n'
        scr_path.write_text(scr_content, encoding="ascii")

        try:
            completed = subprocess.run(
                [str(accoreconsole), "/i", str(first_dwg_path), "/s", str(scr_path), "/l", "en-US"],
                capture_output=True,
                timeout=timeout,
                check=False,
            )
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout or b"").decode(
                    _ANSI_OUTPUT_ENCODING, errors="replace"
                ).strip()
                suffix = f": {detail[-500:]}" if detail else ""
                raise DwgAttributeError(
                    f"AutoCAD Core Console avslutades med felkod {completed.returncode}{suffix}"
                )
        except subprocess.TimeoutExpired as exc:
            raise DwgAttributeError(
                f"accoreconsole svarade inte inom {timeout} sekunder för {first_dwg_path.name}."
            ) from exc
        except OSError as exc:
            raise DwgAttributeError(f"Kunde inte starta accoreconsole: {exc}") from exc


def read_ansi_output(output_path: Path) -> str:
    """Direkt port av ``version_5.py:_read_ansi_output``."""
    if not output_path.is_file():
        raise DwgAttributeError(
            "accoreconsole avslutades utan att skriva någon utdatafil (oväntat fel i AutoCAD)."
        )
    return output_path.read_text(encoding=_ANSI_OUTPUT_ENCODING)


def parse_extract_output(
    text: str,
    paths: tuple[Path, ...],
    *,
    block_name: str = TRVJ_BLOCK_NAME,
    include_unknown_tags: bool = False,
) -> dict[Path, DwgAttributeResult]:
    """Tolkar accoreconsole/Rust-extraherarens gemensamma textprotokoll.

    Direkt port av ``version_5.py:_parse_extract_output``. Formatet är
    medvetet identiskt oavsett om texten kommer från AutoLISP-skriptet
    (``dwg.readers``) eller Rust/LibreDWG-bryggan (``dwg.rust_bridge``),
    så samma parser återanvänds oförändrad av båda.
    """
    results: dict[Path, DwgAttributeResult] = {}
    segments = text.split("###FILE:")
    for segment in segments[1:]:
        header, _, body = segment.partition("\n")
        index = int(header.strip())
        path = paths[index]
        body = body.split("###END", 1)[0]
        raw_lines = [line for line in body.splitlines() if line]
        if raw_lines and raw_lines[0] == "###BLOCK_NOT_FOUND":
            results[path] = DwgAttributeResult(
                path=path,
                fields=(),
                error=f"Hittade inget block med namnet {block_name} i ritningen.",
            )
            continue

        values_by_tag: dict[str, tuple[str, int]] = {}
        for line in raw_lines:
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            tag, value, flags_text = parts
            try:
                flags = int(flags_text)
            except ValueError:
                flags = 0
            values_by_tag[tag] = (value, flags)

        fields: list[MetadataField] = []
        schema = TRVJ_ATTRIBUTE_SCHEMA
        if include_unknown_tags:
            schema = tuple((tag, tag) for tag in values_by_tag)
        for tag, label in schema:
            if tag in values_by_tag:
                value, flags = values_by_tag[tag]
                is_constant = bool(flags & 2)
                fields.append(
                    MetadataField(key=tag, label=label, value=value, editable=not is_constant, location=tag)
                )
        results[path] = DwgAttributeResult(path=path, fields=tuple(fields), error=None)
    return results


def chunk_paths(paths: tuple[Path, ...], max_workers: int) -> list[tuple[Path, ...]]:
    """Direkt port av ``version_5.py:_chunk_paths``."""
    worker_count = max(1, min(max_workers, len(paths)))
    chunks: list[list[Path]] = [[] for _ in range(worker_count)]
    for index, path in enumerate(paths):
        chunks[index % worker_count].append(path)
    return [tuple(chunk) for chunk in chunks if chunk]
