"""Multi-dokument-metadatagrid (SharePoint-stil multiredigering).

Direkt port av ``.old/python_v5/src/version_4.py``. Bygger ett
kategoribaserat rutnät (rader = metadatakategorier, kolumner =
markerade dokument) ovanpå den enskilda dokumentläsningen i
``metadata/__init__.py``/``metadata/docx.py``/``metadata/xlsx.py``,
samt den DOCX-specifika tabellmetadata-fallbacken (för DOCX-filer utan
Content Controls) och orkestreringen spara + PDF-export.

Kravkälla: docs/kravsparning.md, TB-sektion EAA/EAB/EAC (multiredigering),
använd av app/controllers/metadata_controller.py.
"""

from __future__ import annotations

import re
import threading
import unicodedata
import xml.etree.ElementTree as ET
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from backend.pdf_export import PdfExporter, plot_documents
from metadata import DocumentMetadata, MetadataError, MetadataField, extract_metadata
from metadata import update_many_metadata as _update_many_metadata
from metadata.docx import docx_body_has_content_controls

# Läscache för dokumentmetadata (granskningspunkt 2026-10-02, utanför TB).
# Varje kryss i filträdet bygger om alla rutnät; utan cache lästes varje
# markerad DOCX tre gånger per klick, över nätverksenhet. Nyckeln innehåller
# mtime_ns och storlek, så en ändrad fil (även via Word utanför appen) läses
# om. Både resultat och läsfel cachas; låset gör cachen säker att använda
# från UI:ts bakgrundstråd.
_CACHE_LIMIT = 1024
_cache: OrderedDict[tuple, tuple[bool, object]] = OrderedDict()
_cache_lock = threading.Lock()
_READ_ERRORS = (MetadataError, ValueError, FileNotFoundError)


def clear_metadata_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _cached_read(kind: str, path: Path, loader: Callable[[Path], object]) -> object:
    try:
        status = path.stat()
    except OSError:
        return loader(path)
    key = (kind, str(path).casefold(), status.st_mtime_ns, status.st_size)
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None:
            _cache.move_to_end(key)
    if hit is None:
        try:
            hit = (True, loader(path))
        except _READ_ERRORS as exc:
            hit = (False, exc)
        with _cache_lock:
            _cache[key] = hit
            while len(_cache) > _CACHE_LIMIT:
                _cache.popitem(last=False)
    succeeded, value = hit
    if not succeeded:
        raise value  # type: ignore[misc]
    return value

_FALLBACK_METADATA_CATEGORIES = (
    ("PROJECT_NAME", "Projekt"),
    ("pw_CaseNo", "Ärendenummer"),
    ("DOC_PROP_FILENAME", "Dokumentnamn"),
    ("pw_CreatedBy", "Skapad av"),
    ("pw_ReviewedBy", "Granskad av"),
    ("pw_ApprovedBy", "Godkänd av"),
    ("pw_DocumentDate", "Datum"),
    ("Current Rev", "Ändr"),
    ("Date Published", "Ändr Datum"),
    ("pw_Contract", "Delprojekt"),
    ("pw_PlaceDescription", "Delsträcka"),
    ("pw_TrackSection", "Bandel"),
    ("pw_StartKM", "Start KM"),
    ("pw_StartM", "Start M"),
    ("pw_StopKM", "Slut KM"),
    ("pw_StopM", "Slut M"),
    ("Document ID", "Handlingsnummer"),
    ("Document Title", "Dokumentitel"),
    ("pw_DisciplinDescription", "Teknikområde"),
    ("pw_TitleLine2", "Beskrivning 1"),
    ("pw_TitleLine3", "Beskrivning 2"),
    ("pw_TitleLine4", "Beskrivning 3"),
    ("Current State", "Tillfällig statusmarkering"),
    ("pw_StatusTitle", "Handlingstyp"),
    ("Rev Status", "Granskningsstatus"),
)


@dataclass(frozen=True, slots=True)
class MetadataCategory:
    key: str
    label: str


@dataclass(frozen=True, slots=True)
class MetadataColumn:
    path: Path
    document_type: str
    fields: dict[str, MetadataField]
    error: str | None = None


@dataclass(frozen=True, slots=True)
class MetadataRow:
    key: str
    label: str
    document_types: tuple[str, ...]

    @property
    def exists_in_both_docx_and_xlsx(self) -> bool:
        return {"docx", "xlsx"}.issubset(self.document_types)

    @property
    def source_label(self) -> str:
        return " / ".join(document_type.upper() for document_type in self.document_types)


@dataclass(frozen=True, slots=True)
class MetadataGrid:
    rows: tuple[MetadataRow, ...]
    columns: tuple[MetadataColumn, ...]

    def field_at(self, row: MetadataRow, column: MetadataColumn) -> MetadataField | None:
        return column.fields.get(row.key)

    def value_at(self, row: MetadataRow, column: MetadataColumn) -> str:
        field = self.field_at(row, column)
        return field.value if field is not None else ""

    def editable_at(self, row: MetadataRow, column: MetadataColumn) -> bool:
        field = self.field_at(row, column)
        return field.editable if field is not None else False


@dataclass(frozen=True, slots=True)
class BatchOperationResult:
    updated_documents: tuple[Path, ...]
    created_pdfs: tuple[Path, ...]


def load_metadata_categories() -> tuple[MetadataCategory, ...]:
    return tuple(MetadataCategory(key, label) for key, label in _FALLBACK_METADATA_CATEGORIES)


_SEMANTIC_XLSX_KEY_ALIASES = {
    "Current Rev": ("tb_Version",),
    "Date Published": ("tb_Rev_Date",),
    "Document ID": ("tb_TitleLine1.1",),
    "Document Title": ("tb_TitleLine1.2",),
    "Rev Status": ("pw_ReviewStatus",),
}

_SEMANTIC_LABEL_ALIASES = {
    "andring": "andr",
    "andr datum": "andr datum",
    "andr andr datum": "andr datum",
    "arendenummer": "arendenummer",
    "arendenummer*": "arendenummer",
    "bandel start km": "start km",
    "bandel start m": "start m",
    "bandel stop km": "slut km",
    "bandel stop m": "slut m",
    "delprojekt nummer": "delprojekt",
    "delstracka namn": "delstracka",
    "dokumenttitel": "dokumentitel",
    "filnamn": "dokumentnamn",
    "granskningsstatus syfte": "granskningsstatus",
    "handl nr": "handlingsnummer",
    "namn": "dokumentnamn",
    "produkt": "handlingstyp",
    "skapad av leverantor": "skapad av",
    "granskad av leverantor": "granskad av",
    "godkand av leverantor": "godkand av",
    "teknikomrade namngiven leverans": "teknikomrade",
    "project": "project",
}

_WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W = f"{{{_WORD_NS}}}"


def _normalize_metadata_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.casefold())
    without_accents = "".join(character for character in normalized if not unicodedata.combining(character))
    without_parentheses = re.sub(r"\([^)]*\)", " ", without_accents)
    words = re.findall(r"[a-z0-9]+", without_parentheses)
    return " ".join(words)


def _semantic_label(value: str) -> str:
    normalized = _normalize_metadata_text(value)
    return _SEMANTIC_LABEL_ALIASES.get(normalized, normalized)


def _docx_table_cell_text(cell: ET.Element) -> str:
    paragraphs: list[str] = []
    for paragraph in cell.iter(_W + "p"):
        text = "".join(node.text or "" for node in paragraph.iter(_W + "t")).strip()
        if text:
            paragraphs.append(text)
    return " ".join(paragraphs).strip()


def _docx_table_rows(root: ET.Element) -> list[list[str]]:
    rows: list[list[str]] = []
    for table in root.iter(_W + "tbl"):
        for table_row in table.findall(_W + "tr"):
            cells = [_docx_table_cell_text(cell) for cell in table_row.findall(_W + "tc")]
            if any(cells):
                rows.append(cells)
    return rows


def _docx_paragraph_texts(root: ET.Element) -> list[str]:
    texts: list[str] = []
    for paragraph in root.iter(_W + "p"):
        text = "".join(node.text or "" for node in paragraph.iter(_W + "t")).strip()
        if text:
            texts.append(text)
    return texts


def _add_table_metadata(
    metadata_fields: dict[str, MetadataField],
    key: str,
    label: str,
    value: str,
    location: str,
) -> None:
    cleaned_value = value.strip()
    if not cleaned_value:
        return
    metadata_fields[key] = MetadataField(key, label, cleaned_value, False, location)


def _extract_docx_cover_block_metadata(
    texts: list[str],
    metadata_fields: dict[str, MetadataField],
    location: str,
) -> None:
    for text in texts:
        contract_match = re.match(r"^(OLE\d+)\s+(.+)$", text, re.IGNORECASE)
        if contract_match is not None:
            _add_table_metadata(metadata_fields, "pw_Contract", "Delprojekt", contract_match.group(1), location)
            _add_table_metadata(
                metadata_fields,
                "pw_PlaceDescription",
                "Delsträcka",
                contract_match.group(2),
                location,
            )
            continue

        bandel_match = re.search(
            r"bandel\s+(?P<bandel>\d+).*?KM\s+"
            r"(?P<start_km>\d+)\+(?P<start_m>\d+)\D+"
            r"(?P<stop_km>\d+)\+(?P<stop_m>\d+)",
            text,
            re.IGNORECASE,
        )
        if bandel_match is not None:
            _add_table_metadata(metadata_fields, "pw_TrackSection", "Bandel", bandel_match.group("bandel"), location)
            _add_table_metadata(metadata_fields, "pw_StartKM", "Start KM", bandel_match.group("start_km"), location)
            _add_table_metadata(metadata_fields, "pw_StartM", "Start M", bandel_match.group("start_m"), location)
            _add_table_metadata(metadata_fields, "pw_StopKM", "Slut KM", bandel_match.group("stop_km"), location)
            _add_table_metadata(metadata_fields, "pw_StopM", "Slut M", bandel_match.group("stop_m"), location)
            continue

        title_match = re.match(r"^\d+(?:\.\d+)+\s+(.+)$", text)
        if title_match is not None:
            _add_table_metadata(metadata_fields, "Document Title", "Dokumentitel", text, location)
            continue

        description_match = re.match(r"^\d{5}\s+.+$", text)
        if description_match is not None:
            _add_table_metadata(metadata_fields, "pw_TitleLine2", "Beskrivning 1", text, location)
            continue

        status_text = _semantic_label(text)
        if "for granskning" in status_text or "godkand" in status_text:
            status_parts = re.split(r"\s+[–—-]\s+", text, maxsplit=1)
            if len(status_parts) == 1 and "bygghandling" in status_text and "for granskning" in status_text:
                status_parts = ("bygghandling", "för granskning")
            if status_parts:
                _add_table_metadata(metadata_fields, "pw_StatusTitle", "Handlingstyp", status_parts[0].upper(), location)
            if len(status_parts) > 1:
                _add_table_metadata(
                    metadata_fields, "Rev Status", "Granskningsstatus", status_parts[1].upper(), location
                )


def _extract_docx_table_metadata(path: Path) -> DocumentMetadata:
    metadata_fields: dict[str, MetadataField] = {}
    try:
        with ZipFile(path, "r") as archive:
            word_parts = [name for name in archive.namelist() if name.startswith("word/") and name.endswith(".xml")]
            for part_name in word_parts:
                root = ET.fromstring(archive.read(part_name))
                rows = _docx_table_rows(root)
                for index, labels in enumerate(rows[:-1]):
                    values = rows[index + 1]
                    if len(labels) != len(values):
                        continue
                    for label, value in zip(labels, values, strict=True):
                        normalized_label = _semantic_label(label)
                        if not normalized_label or not value.strip():
                            continue
                        metadata_fields.setdefault(
                            normalized_label,
                            MetadataField(normalized_label, label, value.strip(), False, part_name),
                        )
                _extract_docx_cover_block_metadata(_docx_paragraph_texts(root), metadata_fields, part_name)
    except (BadZipFile, ET.ParseError, KeyError) as exc:
        raise MetadataError(f"DOCX-filen kunde inte läsas som tabellmetadata: {path.name}") from exc

    if not metadata_fields:
        raise MetadataError("DOCX-filen saknar identifierade metadatafält i tabeller.")
    return DocumentMetadata(path, "docx", tuple(metadata_fields.values()))


def _map_fields_to_categories(
    document: DocumentMetadata,
    categories: tuple[MetadataCategory, ...] | None,
) -> dict[str, MetadataField]:
    if categories is None:
        return {field.key: field for field in document.fields}

    fields_by_key = {field.key: field for field in document.fields}
    fields_by_label = {_semantic_label(field.label): field for field in document.fields}
    mapped: dict[str, MetadataField] = {}
    for category in categories:
        field = fields_by_key.get(category.key)
        if field is None and document.document_type == "xlsx":
            for alias in _SEMANTIC_XLSX_KEY_ALIASES.get(category.key, ()):
                field = fields_by_key.get(alias)
                if field is not None:
                    break
        if field is None:
            field = fields_by_label.get(_semantic_label(category.label))
        if field is not None:
            mapped[category.key] = field
    return mapped


def build_metadata_grid(
    documents: Iterable[DocumentMetadata],
    metadata_categories: Iterable[MetadataCategory] | None = None,
    extraction_errors: dict[Path, str] | None = None,
) -> MetadataGrid:
    columns: list[MetadataColumn] = []
    labels_by_key: dict[str, str] = {}
    document_types_by_key: dict[str, set[str]] = {}
    category_order: list[str] = []
    allowed_keys: set[str] | None = None

    categories = tuple(metadata_categories) if metadata_categories is not None else None

    if categories is not None:
        allowed_keys = set()
        for category in categories:
            if category.key in allowed_keys:
                continue
            allowed_keys.add(category.key)
            category_order.append(category.key)
            labels_by_key[category.key] = category.label
            document_types_by_key[category.key] = {"docx"}

    for document in documents:
        fields = _map_fields_to_categories(document, categories)
        columns.append(
            MetadataColumn(
                document.path,
                document.document_type,
                fields,
                None if extraction_errors is None else extraction_errors.get(document.path),
            )
        )
        for category_key, field in fields.items():
            labels_by_key.setdefault(category_key, field.label)
            document_types_by_key.setdefault(category_key, set()).add(document.document_type)
            if category_key not in category_order:
                category_order.append(category_key)

    if allowed_keys is not None:
        rows = tuple(
            MetadataRow(key, labels_by_key[key], tuple(sorted(document_types_by_key.get(key, set()))))
            for key in category_order
            if key in labels_by_key
        )
    else:
        rows = tuple(
            sorted(
                (
                    MetadataRow(key, labels_by_key[key], tuple(sorted(document_types_by_key[key])))
                    for key in labels_by_key
                ),
                key=lambda row: (not row.exists_in_both_docx_and_xlsx, row.label.casefold()),
            )
        )
    return MetadataGrid(rows, tuple(columns))


def _with_table_metadata(document: DocumentMetadata) -> DocumentMetadata:
    """Kompletterar Content Control-fält med skrivskyddad tabelltext.

    Mallar som 4001 har bara ett fåtal kontroller (Delsträcka, Bandel, KM)
    medan titel, status m.m. står som vanlig text i sidhuvudet. Kontrollerna
    vinner alltid per nyckel och förblir redigerbara; tabelltexten läggs bara
    till för nycklar som saknas och är aldrig redigerbar (``update_metadata``
    skriver endast Content Controls).
    """
    try:
        table_document = _extract_docx_table_metadata(document.path)
    except MetadataError:
        return document
    keys = {field.key for field in document.fields}
    extra = tuple(field for field in table_document.fields if field.key not in keys)
    if not extra:
        return document
    return DocumentMetadata(document.path, document.document_type, document.fields + extra)


def _read_grid_document(document_path: Path) -> tuple[DocumentMetadata, str | None]:
    """Läser ett dokument för standardrutnätet; returnerar (dokument, läsfel)."""
    try:
        document = _read_document(document_path)
        if document.document_type == "docx" and not docx_body_has_content_controls(document_path):
            document = _with_table_metadata(document)
        return document, None
    except _READ_ERRORS as exc:
        suffix = document_path.suffix.lower()
        document_type = "xlsx" if suffix == ".xlsx" else "docx"
        if document_type == "docx":
            try:
                return _extract_docx_table_metadata(document_path), None
            except _READ_ERRORS:
                pass
        return DocumentMetadata(document_path, document_type, ()), str(exc)


def _read_document(document_path: Path) -> DocumentMetadata:
    return _cached_read("document", document_path, extract_metadata)  # type: ignore[return-value]


def extract_metadata_grid(
    paths: Iterable[str | Path],
    metadata_categories: Iterable[MetadataCategory] | None = None,
) -> MetadataGrid:
    categories = load_metadata_categories() if metadata_categories is None else tuple(metadata_categories)
    documents: list[DocumentMetadata] = []
    extraction_errors: dict[Path, str] = {}
    for path in paths:
        document_path = Path(path).expanduser().resolve()
        document, error = _cached_read("grid", document_path, _read_grid_document)  # type: ignore[misc]
        documents.append(document)
        if error is not None:
            extraction_errors[document_path] = error
    return build_metadata_grid(documents, categories, extraction_errors)


# Leveransförteckning med mallens egna Content Control-taggar.
# Användarbeslut 2026-10-02, utanför TB: DOCX-filer med den här
# Content Control-strukturen hanteras i ett eget metadataflöde, eftersom
# taggarna inte motsvarar standardkategorierna ovan. Kolumnerna är mallens
# egna taggar i mallens ordning; taggen är också skrivnyckeln, så
# ``update_metadata`` skriver tillbaka till rätt kontroll utan alias.
DELIVERY_LIST_CATEGORIES = (
    ("DatePublished", "Publiceringsdatum"),
    ("CurrentRev", "Aktuell revision"),
    ("Byggherre", "Byggherre"),
    ("Handläggare BAS-P", "Handläggare BAS-P"),
    ("Projekterande konsult", "Projekterande konsult"),
    ("Uppdragsledare", "Uppdragsledare"),
    ("Samordnande teknikansvarig", "Samordnande teknikansvarig"),
    ("Kontaktperson BAS-P", "Kontaktperson BAS-P"),
    ("Leveranspaket", "Leveranspaket"),
    ("Anläggningsdel", "Anläggningsdel"),
    ("Teknikområde", "Teknikområde"),
    ("Leveransens innehåll", "Leveransens innehåll"),
    ("Dokumentnamn", "Dokumentnamn"),
    ("Dokumenttitel", "Dokumenttitel"),
    ("Revisionsdatum", "Revisionsdatum"),
)
# Taggar som bara förekommer i leveransförteckningsmallen. Båda krävs, så att
# ett vanligt dokument med en enstaka liknande tagg inte flyttas av misstag.
DELIVERY_LIST_SIGNATURE_TAGS = frozenset({"Leveranspaket", "Leveransens innehåll"})


def is_delivery_list(document: DocumentMetadata) -> bool:
    """Sant om dokumentets Content Controls följer leveransförteckningsmallen."""
    if document.document_type != "docx":
        return False
    return DELIVERY_LIST_SIGNATURE_TAGS <= {field.key for field in document.fields}


def is_delivery_list_document(path: str | Path) -> bool:
    """Läser DOCX-filens Content Controls och avgör om den är en leveransförteckning.

    Filer som inte kan läsas som Content Controls (saknade kontroller,
    trasig zip, annat format) är inte leveransförteckningar; de hamnar i
    standardflödet som visar och rapporterar läsfelet.
    """
    document_path = Path(path)
    if document_path.suffix.lower() != ".docx":
        return False
    try:
        return is_delivery_list(_read_document(document_path.expanduser().resolve()))
    except (MetadataError, ValueError, FileNotFoundError):
        return False


def _delivery_list_categories(documents: Iterable[DocumentMetadata]) -> tuple[MetadataCategory, ...]:
    """Mallens kolumner först, sedan eventuella extra taggar i dokumentordning."""
    categories = [MetadataCategory(key, label) for key, label in DELIVERY_LIST_CATEGORIES]
    known = {category.key for category in categories}
    for document in documents:
        for field in document.fields:
            if field.key not in known:
                known.add(field.key)
                categories.append(MetadataCategory(field.key, field.label or field.key))
    return tuple(categories)


def extract_delivery_list_grid(paths: Iterable[str | Path]) -> MetadataGrid:
    """Bygger rutnätet för leveransförteckningar (rader = dokument i UI:t)."""
    documents: list[DocumentMetadata] = []
    extraction_errors: dict[Path, str] = {}
    for path in paths:
        document_path = Path(path).expanduser().resolve()
        try:
            documents.append(_read_document(document_path))
        except (MetadataError, ValueError, FileNotFoundError) as exc:
            documents.append(DocumentMetadata(document_path, "docx", ()))
            extraction_errors[document_path] = str(exc)
    return build_metadata_grid(documents, _delivery_list_categories(documents), extraction_errors)


def update_many_metadata(changes_by_document: dict[Path, dict[str, str]]) -> tuple[Path, ...]:
    """Sparar alla ändringar; DOCX skrivs i en Word-instans (se metadata.update_many_metadata)."""
    return _update_many_metadata(changes_by_document)


def update_metadata_and_create_pdfs(
    paths: Iterable[str | Path],
    changes_by_document: dict[Path, dict[str, str]],
    *,
    exporter: PdfExporter | None = None,
) -> BatchOperationResult:
    """Sparar metadata och exporterar alla dokument till PDF i en Office-instans per filtyp.

    Granskningsstämpeln (baslinjens ``apply_review_watermark*``) är borttagen:
    vattenstämpel sätts bara i PDF-panelen (användarbeslut 2026-10-02, utanför TB).
    """
    document_paths = tuple(Path(path).expanduser().resolve() for path in paths)
    updated = update_many_metadata(changes_by_document)
    created_pdfs = tuple(plot_documents(document_paths, exporter=exporter))
    return BatchOperationResult(updated, created_pdfs)
