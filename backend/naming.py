"""Filnamnsstandarder och namnbytesregler.

Motsvarar namnbyteslogiken för DWG i baslinjen (version_5.py,
projektstandardnamngivning).

Kravkälla: docs/kravsparning.md, modul backend/naming, TB-sektion
AFA (namngivning enligt projektstandard) samt EB (tre-punkts
filnamnsinspektion mot Trafikverkets referensfil).

EB-inspektionen (tre-punkts filnamn mot JSON-referensfil) är ett
verifierat gap som saknas helt i baslinjen och kräver ett separat
beslut om referensfilens källa innan den implementeras. Den
implementeras därför INTE här ännu.
"""

from __future__ import annotations

from pathlib import Path

from backend.file_discovery import DWG_EXTENSION

_FORBIDDEN_FILENAME_CHARACTERS = set('\\/:*?"<>|')
"""Direkt port av version_5.py:s ``_FORBIDDEN_FILENAME_CHARACTERS``."""


class NamingError(RuntimeError):
    """Kastas när ett filnamnsbyte inte kan utföras säkert.

    Motsvarar version_5.py:s ``DwgError`` i den del av dess ansvar som
    handlar om namnbyte. Ges ett eget namn här (i stället för att
    återanvända ``DwgError``) eftersom ``dwg``-paketet (rust-bryggan,
    läsare/skrivare/validerare) ännu är oimplementerat (skelett) och
    namnbytet konceptuellt hör hemma i backend/naming.py enligt
    modulindelningen i docs/plan.md, inte i dwg-paketet.
    """


def rename_dwg_file(path: str | Path, new_name: str) -> Path:
    """Byter namn på en DWG-fil i samma mapp och returnerar den nya sökvägen.

    Direkt port av ``version_5.py:rename_dwg_file``. ``new_name`` får
    anges med eller utan ``.dwg``-ändelse; ändelsen tvingas alltid till
    ``.dwg`` så filen förblir en giltig DWG-fil på disk. Ingen
    namnbyte sker (och ingen fil rörs) om det städade målnamnet redan
    är samma som källfilens namn.
    """
    dwg_path = Path(path)
    if dwg_path.suffix.lower() != DWG_EXTENSION:
        raise NamingError(f"Filen är inte en DWG-fil: {dwg_path}")
    if not dwg_path.is_file():
        raise NamingError(f"Filen finns inte: {dwg_path}")

    cleaned_name = new_name.strip()
    if not cleaned_name:
        raise NamingError("Filnamnet får inte vara tomt.")
    if _FORBIDDEN_FILENAME_CHARACTERS.intersection(cleaned_name):
        raise NamingError(f"Filnamnet innehåller otillåtna tecken: {cleaned_name}")

    stem = Path(cleaned_name).stem if cleaned_name.lower().endswith(DWG_EXTENSION) else cleaned_name
    if not stem:
        raise NamingError("Filnamnet får inte vara tomt.")

    target_path = dwg_path.with_name(f"{stem}{DWG_EXTENSION}")
    if target_path == dwg_path:
        return dwg_path
    if target_path.exists():
        raise NamingError(f"En fil med namnet {target_path.name} finns redan.")

    return dwg_path.rename(target_path)


def rename_many_dwg_files(new_names_by_path: dict[Path, str]) -> dict[Path, Path]:
    """Byter namn på flera DWG-filer och returnerar {ursprunglig_sökväg: ny_sökväg}.

    Direkt port av ``version_5.py:rename_many_dwg_files``. Byter namn
    en fil i taget i den ordning ``new_names_by_path`` itereras; ett
    fel på en fil avbryter resten (ingen inbyggd rollback), precis som
    i baslinjen.
    """
    renamed: dict[Path, Path] = {}
    for path, new_name in new_names_by_path.items():
        renamed[path] = rename_dwg_file(path, new_name)
    return renamed

