"""Läsning/skrivning av XLSX-metadata (bladet "Fyll i Försättsida").

Motsvarar logiken i .old/python_v5/src/version_3.py för XLSX-baserad
metadatahantering (rå XML-manipulation av arbetsboken, till skillnad
från DOCX-vägen som går via Word-automatisering).

Kravkälla: docs/kravsparning.md, modul metadata/xlsx.

Fältlistan (semantisk mappning från version_4.py:s
``_FALLBACK_METADATA_CATEGORIES``) hör till multi-edit-grid-flödet och
porteras separat i ett senare steg av Fas 2, i takt med att
app/controllers/metadata_controller.py byggs upp — se docs/plan.md.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile, ZipInfo

from metadata import DocumentMetadata, MetadataError, MetadataField

XLSX_EXTENSION = ".xlsx"
METADATA_SHEET_NAME = "Fyll i Försättsida"

_SHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"

_S = f"{{{_SHEET_NS}}}"
_R = f"{{{_REL_NS}}}"
_PR = f"{{{_PACKAGE_REL_NS}}}"


def _shared_strings(archive: ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    return ["".join(node.text or "" for node in item.iter(_S + "t")) for item in root.findall(_S + "si")]


def _cell_value(cell: ET.Element | None, shared_strings: list[str]) -> str:
    if cell is None:
        return ""
    if cell.attrib.get("t") == "inlineStr":
        return "".join(node.text or "" for node in cell.iter(_S + "t"))
    value_node = cell.find(_S + "v")
    value = value_node.text if value_node is not None and value_node.text is not None else ""
    if cell.attrib.get("t") == "s" and value:
        try:
            return shared_strings[int(value)]
        except (IndexError, ValueError) as exc:
            raise MetadataError("Arbetsboken innehåller en ogiltig shared-string-referens.") from exc
    return value


def _xlsx_metadata_part(archive: ZipFile) -> str:
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    targets = {
        relationship.attrib["Id"]: relationship.attrib["Target"]
        for relationship in relationships.findall(_PR + "Relationship")
    }

    sheets = workbook.find(_S + "sheets")
    if sheets is None:
        raise MetadataError("Arbetsboken saknar arbetsblad.")
    for sheet in sheets.findall(_S + "sheet"):
        if sheet.attrib.get("name") != METADATA_SHEET_NAME:
            continue
        relationship_id = sheet.attrib.get(_R + "id")
        target = targets.get(relationship_id or "")
        if target is None:
            break
        part = PurePosixPath(target.lstrip("/"))
        if not str(part).startswith("xl/"):
            part = PurePosixPath("xl") / part
        return str(part)
    raise MetadataError(f"Arbetsboken saknar metadatafliken '{METADATA_SHEET_NAME}'.")


def extract_xlsx_metadata(path: str | Path) -> DocumentMetadata:
    """Läser fält från metadatafliken "Fyll i Försättsida".

    Direkt port av ``version_3.py:_extract_xlsx_metadata`` med egen
    filvalidering. Rader tolkas kolumnvis: A = etikett, B = värde
    (redigerbart om cellen saknar formel), D = nyckel. Rader utan
    både etikett och nyckel hoppas över.
    """
    document_path = Path(path).expanduser().resolve()
    if not document_path.is_file():
        raise FileNotFoundError(f"Filen finns inte: {document_path}")
    if document_path.suffix.lower() != XLSX_EXTENSION:
        raise ValueError(f"Filtypen stöds inte av XLSX-läsaren: {document_path.suffix}")

    try:
        with ZipFile(document_path, "r") as archive:
            shared_strings = _shared_strings(archive)
            sheet_root = ET.fromstring(archive.read(_xlsx_metadata_part(archive)))
    except (BadZipFile, ET.ParseError, KeyError) as exc:
        raise MetadataError(f"Dokumentet kunde inte läsas: {document_path.name}") from exc

    cells = {cell.attrib["r"]: cell for cell in sheet_root.iter(_S + "c") if "r" in cell.attrib}
    rows = sorted(
        {
            int("".join(character for character in reference if character.isdigit()))
            for reference in cells
            if reference.startswith("A")
        }
    )
    fields: list[MetadataField] = []
    for row in rows:
        label = _cell_value(cells.get(f"A{row}"), shared_strings).strip()
        key = _cell_value(cells.get(f"D{row}"), shared_strings).strip()
        value_cell = cells.get(f"B{row}")
        if not label or not key or value_cell is None:
            continue
        value = _cell_value(value_cell, shared_strings)
        editable = value_cell.find(_S + "f") is None
        fields.append(MetadataField(key, label, value, editable, f"B{row}"))

    if not fields:
        raise MetadataError(f"Inga metadatafält hittades på fliken '{METADATA_SHEET_NAME}'.")
    return DocumentMetadata(document_path, "xlsx", tuple(fields))


def _replace_xlsx_cell(sheet_xml: str, location: str, value: str) -> str:
    cell_pattern = re.compile(
        rf'(<c\b(?=[^>]*\br="{re.escape(location)}")[^>]*)(?:/>|>.*?</c>)',
        re.DOTALL,
    )
    match = cell_pattern.search(sheet_xml)
    if match is None:
        raise MetadataError(f"Metadatafältets cell saknas: {location}")

    start_tag = re.sub(r'\s+t="[^"]*"', "", match.group(1)).rstrip()
    replacement = f'{start_tag} t="inlineStr">' f'<is><t xml:space="preserve">{escape(value)}</t></is>' "</c>"
    return sheet_xml[: match.start()] + replacement + sheet_xml[match.end() :]


def _force_xlsx_recalculation(workbook_xml: str) -> str:
    calc_pattern = re.compile(r"<calcPr\b[^>]*/>")
    match = calc_pattern.search(workbook_xml)
    if match is None:
        insertion_point = workbook_xml.rfind("</workbook>")
        if insertion_point == -1:
            raise MetadataError("Arbetsbokens workbook.xml är ogiltig.")
        calc_xml = '<calcPr calcMode="auto" fullCalcOnLoad="1" forceFullCalc="1"/>'
        return workbook_xml[:insertion_point] + calc_xml + workbook_xml[insertion_point:]

    calc_xml = match.group(0)
    for attribute, value in (
        ("calcMode", "auto"),
        ("fullCalcOnLoad", "1"),
        ("forceFullCalc", "1"),
    ):
        attribute_pattern = re.compile(rf'\s+{attribute}="[^"]*"')
        if attribute_pattern.search(calc_xml):
            calc_xml = attribute_pattern.sub(f' {attribute}="{value}"', calc_xml)
        else:
            calc_xml = calc_xml[:-2] + f' {attribute}="{value}"/>'
    return workbook_xml[: match.start()] + calc_xml + workbook_xml[match.end() :]


def _write_xlsx_updates(
    source: Path,
    destination: Path,
    metadata_part: str,
    changes: dict[str, tuple[str, str]],
) -> None:
    with ZipFile(source, "r") as source_archive:
        sheet_xml = source_archive.read(metadata_part).decode("utf-8")
        for location, value in changes.values():
            sheet_xml = _replace_xlsx_cell(sheet_xml, location, value)
        workbook_xml = _force_xlsx_recalculation(source_archive.read("xl/workbook.xml").decode("utf-8"))
        overrides = {
            metadata_part: sheet_xml.encode("utf-8"),
            "xl/workbook.xml": workbook_xml.encode("utf-8"),
        }
        members: list[tuple[ZipInfo, bytes]] = []
        for item in source_archive.infolist():
            members.append((item, overrides.get(item.filename, source_archive.read(item.filename))))

    with ZipFile(destination, "w", compression=ZIP_DEFLATED) as destination_archive:
        for item, data in members:
            destination_archive.writestr(item, data)


def write_xlsx_metadata(
    source: Path,
    destination: Path,
    fields_by_key: dict[str, MetadataField],
    changes: dict[str, str],
) -> None:
    """Skriver uppdaterade cellvärden till ``destination`` via ren XML-manipulation.

    Direkt port av XLSX-grenen i ``version_3.py:update_metadata``: slår
    upp varje ändrad nyckels cellreferens (``location``), skriver om
    dess `<c>`-element som en inline-sträng, tvingar fram omräkning av
    arbetsboken (``_force_xlsx_recalculation``) och skriver en ny
    zip-arkivfil med de ändrade delarna utbytta.
    """
    locations = {key: (fields_by_key[key].location, value) for key, value in changes.items()}
    with ZipFile(source, "r") as archive:
        metadata_part = _xlsx_metadata_part(archive)
    _write_xlsx_updates(source, destination, metadata_part, locations)

