"""Katalogsökning och filklassificering (DOCX/XLSX/DWG/DGN).

Motsvarar katalogträdet och filidentifieringen i baslinjen:
    .old/python_v5/src/version_1.py  (find_documents: rekursiv DOCX/XLSX-sökning)
    .old/python_v5/app_v5_desktop.py (_create_tree_item: klassificering per
                                       katalognivå i explorer-trädet: mapp/
                                       docx-xlsx/pdf/dwg; PDF döljs nu i trädet)

Denna modul porterar den rena klassificerings-/sökvägslogiken (ingen
PySide6-widgetkod) så den kan testas och återanvändas fristående från
UI-lagret, som TB-styrningen kräver (Steg 3 i docs/plan.md: "UI ska
separeras från backend/logik").

Kravkälla: docs/kravsparning.md, modul backend/file_discovery,
TB-sektion AFC.1.

Verifierat gap: DGN-formatet klassificeras inte alls i baslinjen —
varken i `find_documents` eller i explorer-trädet. Denna modul
speglar det medvetet: `.dgn`-filer faller igenom som ``EntryKind.OTHER``
precis som i baslinjen, snarare än att tyst få nytt beteende. Se
docs/kravsparning.md, öppna frågor, för beslut om DGN-omfattning.
"""

from __future__ import annotations

import enum
from pathlib import Path

#: DOCX/XLSX-ändelser som `version_1.py:find_documents` känner igen.
SUPPORTED_DOCUMENT_EXTENSIONS = frozenset({".docx", ".xlsx"})

#: Samma värde som `version_5.py:DWG_EXTENSION`.
DWG_EXTENSION = ".dwg"

#: PDF hanteras separat i explorer-trädet (genererade leveransfiler).
PDF_EXTENSION = ".pdf"

#: Verifierat gap (se moduldocstring ovan): baslinjen klassificerar inte
#: DGN-filer alls. Definierad här endast för att kunna identifiera och
#: räkna DGN-förekomster i Fas 2/3, inte för att ge dem särbehandling än.
DGN_EXTENSION = ".dgn"

#: Filnamnsprefix som alltid ska ignoreras i katalogvyer: Office-lockfiler
#: ("~$...") och dolda filer/mappar (".").
_IGNORED_NAME_PREFIXES = ("~$", ".")


class EntryKind(enum.Enum):
    """Motsvarar de fyra kategorier `_create_tree_item` sätter som
    ``Qt.UserRole + 1`` i baslinjens explorer-träd."""

    DIRECTORY = "directory"
    DOCUMENT = "document"  # DOCX/XLSX, motsvarar baslinjens "document"
    PDF = "pdf"
    DWG = "dwg"
    OTHER = "other"  # inkl. DGN idag (verifierat gap, se moduldocstring)


def should_ignore(name: str) -> bool:
    """True om filen/mappen alltid ska döljas i katalogvyer.

    Motsvarar villkoret ``not entry.name.startswith(("~$", "."))`` i
    ``app_v5_desktop.py:_create_tree_item``.
    """
    return name.startswith(_IGNORED_NAME_PREFIXES)


def classify_entry(path: Path) -> EntryKind:
    """Klassificerar en enskild fil/mapp precis som baslinjens
    ``_create_tree_item`` (app_v5_desktop.py), men utan att skapa något
    UI-objekt.

    Katalogen returneras alltid som ``EntryKind.DIRECTORY``, oavsett
    innehåll — trädet expanderar mappar lazy, en nivå i taget.
    """
    if path.is_dir():
        return EntryKind.DIRECTORY

    suffix = path.suffix.lower()
    if suffix == PDF_EXTENSION:
        return EntryKind.PDF
    if suffix == DWG_EXTENSION:
        return EntryKind.DWG
    if suffix in SUPPORTED_DOCUMENT_EXTENSIONS:
        return EntryKind.DOCUMENT
    return EntryKind.OTHER


#: Källformat som explorer-trädet visar (utöver mappar, som alltid visas).
#: PDF-filer är sidofiler och representeras med synkstatus på källfilens rad.
_VISIBLE_FILE_EXTENSIONS = SUPPORTED_DOCUMENT_EXTENSIONS | {DWG_EXTENSION}


def list_directory_entries(directory: str | Path) -> list[Path]:
    """Listar synliga poster i en enda katalognivå, sorterade som i
    baslinjens explorer-träd: mappar först, sedan filer, båda
    skiftlägesokänsligt på namn.

    Endast en nivå listas (ej rekursivt), precis som trädets
    lazy-expansion. PDF-filer filtreras bort eftersom deras status visas
    på respektive källfilsrad.
    """
    root = Path(directory).expanduser().resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"Inte en katalog: {root}")

    entries = [
        entry
        for entry in root.iterdir()
        if not should_ignore(entry.name)
        and (entry.is_dir() or entry.suffix.lower() in _VISIBLE_FILE_EXTENSIONS)
    ]
    entries.sort(key=lambda entry: (not entry.is_dir(), entry.name.casefold()))
    return entries


def _is_ignored_below(root: Path, candidate: Path) -> bool:
    """True om någon katalog mellan `root` och `candidate` (eller filen
    själv) ska döljas enligt `should_ignore`.

    Behövs eftersom `Path.glob("**/*")` även går ner i dolda kataloger.
    Utan detta filter skulle rekursiva sökningar plocka upp DWG-
    backupkopiorna under `.datasamordning_backup/` (TB, CBE) och visa
    dem som vanliga projektfiler.
    """
    return any(should_ignore(part) for part in candidate.relative_to(root).parts)


def find_documents(base_path: str | Path, recursive: bool = False) -> list[Path]:
    """Direkt port av ``version_1.py:find_documents``.

    Hittar DOCX/XLSX-filer under ``base_path``. Om ``base_path`` pekar på
    en enskild fil returneras den (i en lista) endast om den har en
    stödd ändelse.
    """
    root = Path(base_path).expanduser().resolve()
    if not root.exists():
        raise FileNotFoundError(f"Sökväg hittades inte: {root}")

    if root.is_file():
        return [root] if root.suffix.lower() in SUPPORTED_DOCUMENT_EXTENSIONS else []

    pattern = "**/*" if recursive else "*"
    matches = [
        candidate
        for candidate in root.glob(pattern)
        if candidate.is_file() and candidate.suffix.lower() in SUPPORTED_DOCUMENT_EXTENSIONS
        and not _is_ignored_below(root, candidate)
    ]
    return sorted(matches)


def find_dwg_files(base_path: str | Path, recursive: bool = False) -> list[Path]:
    """Hittar DWG-filer under ``base_path``, med samma sök-/sorteringslogik
    som ``find_documents`` (men för `.dwg` i stället för DOCX/XLSX).

    Detta är en ny, symmetrisk hjälpfunktion (inte en 1:1-kopia av en
    enskild baslinjefunktion) byggd av samma två primitiver som redan är
    verifierade var för sig: sökmönstret från `find_documents`
    (version_1.py) och filtret ``is_dwg_file``/`DWG_EXTENSION` från
    `version_5.py:list_dwg_files`. Sorteringen speglar
    ``list_dwg_files`` exakt: på filnamn (``name.casefold()``), inte på
    hela sökvägen, eftersom DWG-filer i baslinjen kan hämtas från flera
    olika kataloger samtidigt (markerade rader i UI:t) och då ska visas
    i namnordning oavsett vilken mapp de kommer från.
    """
    root = Path(base_path).expanduser().resolve()
    if not root.exists():
        raise FileNotFoundError(f"Sökväg hittades inte: {root}")

    if root.is_file():
        return [root] if root.suffix.lower() == DWG_EXTENSION else []

    pattern = "**/*" if recursive else "*"
    matches = [
        candidate
        for candidate in root.glob(pattern)
        if candidate.is_file() and candidate.suffix.lower() == DWG_EXTENSION
        and not _is_ignored_below(root, candidate)
    ]
    return sorted(matches, key=lambda dwg_path: dwg_path.name.casefold())


def find_pdf_files(base_path: str | Path, recursive: bool = True) -> list[Path]:
    """Hittar PDF-filer för panelen PDF-metadata (vattenstämpel KONTROLLÄRENDE).

    Söker som standard rekursivt eftersom PDF:er döljs i explorerträdet
    och panelen ska visa alla PDF:er i projektmappen. Dolda kataloger
    (t.ex. backupmappen) och dolda temporärfiler från stämplingen hoppas
    över. Sorteras på relativ sökväg så att filer i samma mapp hamnar ihop.
    """
    root = Path(base_path).expanduser().resolve()
    if not root.exists():
        raise FileNotFoundError(f"Sökväg hittades inte: {root}")

    if root.is_file():
        return [root] if root.suffix.lower() == PDF_EXTENSION else []

    pattern = "**/*" if recursive else "*"
    matches = [
        candidate
        for candidate in root.glob(pattern)
        if candidate.suffix.lower() == PDF_EXTENSION
        and candidate.is_file()
        and not _is_ignored_below(root, candidate)
    ]
    return sorted(matches, key=lambda pdf_path: tuple(part.casefold() for part in pdf_path.relative_to(root).parts))
