"""Statusmodell per fil (t.ex. saknar metadata, validerad, skriven, fel).

Motsvarar granskningsstatus-stämplingen i baslinjen
(.old/python_v5/src/version_1.py): en verklig, dokumentinbäddad
statusmarkering ("för granskning"/"godkänd") som för DOCX visas som en
diagonal vattenstämpel (samma VML-preset som Words inbyggda
"Granskning krävs"-vattenstämpel) och för XLSX skrivs som text i cell
A1 på det första bladet. Detta är INTE bara en UI-färgmarkering (även
om app_v5_desktop.py också visar status i UI:t via version_4.py:s
``apply_review_watermark_state``) — statusen skrivs faktiskt in i
dokumentfilen och överlever alltså att filen skickas vidare/öppnas av
någon annan.

Kravkälla: docs/kravsparning.md, modul backend/file_status,
TB-krav om "statusmodell per fil" (Steg 4, Fas 2 i docs/plan.md).

Den batch-nivålogik som i baslinjen ligger i version_4.py
(``apply_review_watermark``/``apply_review_watermark_state``, som
avgör VILKEN status varje dokument ska få utifrån UI:ts
kryssrutetillstånd) porteras separat till
app/controllers/metadata_controller.py i Fas 4, eftersom den är en
UI-orkestrerande funktion snarare än ren filstatuslogik.
"""

from __future__ import annotations

import os
import re
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from backend.file_discovery import find_documents

_NS_SPREADSHEET = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"

REVIEW_STATUS_VALUES = {"för granskning", "for granskning"}
WATERMARK_TEXT = "FÖR GRANSKNING"

_HEADER_PART_RE = re.compile(r"^word/header\d+\.xml$", re.IGNORECASE)

# En äkta, Word-native diagonal vattenstämpel använder VML-preset
# "_x0000_t136" (samma preset Word självt sätter in via Design >
# Vattenstämpel). Att matcha mot denna gör att en vattenstämpel kan
# hittas/ersättas/tas bort oavsett var den ligger (sidhuvud, eller som
# reservfall i dokumentets brödtext) utan att behöva känna till
# resten av dokumentstrukturen.
_WATERMARK_SHAPE_RE = re.compile(
    r'<v:shape\b[^>]*\btype="#_x0000_t136"[^>]*>.*?</v:shape>',
    re.DOTALL,
)
_TEXTPATH_STRING_RE = re.compile(r'(<v:textpath\b[^>]*\bstring=")[^"]*(")')
_PARAGRAPH_START_RE = re.compile(r"<w:p(?=[\s/>])")

_STATUS_COMMENT_START = "<!--datasamordnare:status-->"
_STATUS_COMMENT_END = "<!--/datasamordnare:status-->"
_OWN_STATUS_PARAGRAPH_RE = re.compile(
    re.escape(_STATUS_COMMENT_START) + r".*?" + re.escape(_STATUS_COMMENT_END),
    re.DOTALL,
)
_LEGACY_STATUS_PARAGRAPH_RE = re.compile(
    r'<w:p><w:r><w:t xml:space="preserve">'
    r"(?:för granskning|for granskning|godkänd)"
    r"</w:t></w:r></w:p>$",
    re.IGNORECASE,
)


def _xml_escape_attr(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _watermark_paragraph_xml(watermark_text: str) -> str:
    """Returnerar rå OOXML-markup för ett fristående stycke som bara
    innehåller en diagonal vattenstämpelform, byggd med exakt samma
    VML-preset som Word självt använder för sin inbyggda
    vattenstämpelfunktion (shapetype-id ``"_x0000_t136"``).

    Direkt port av ``version_1.py:_watermark_paragraph_xml``.
    """
    escaped_text = _xml_escape_attr(watermark_text)
    return (
        "<w:p "
        'xmlns:v="urn:schemas-microsoft-com:vml" '
        'xmlns:o="urn:schemas-microsoft-com:office:office" '
        'xmlns:w10="urn:schemas-microsoft-com:office:word">'
        "<w:r><w:rPr><w:noProof/></w:rPr><w:pict>"
        '<v:shapetype id="_x0000_t136" coordsize="21600,21600" o:spt="136" adj="10800" '
        'path="m@7,l@8,m@5,21600l@6,21600e">'
        "<v:formulas>"
        '<v:f eqn="sum #0 0 10800"/><v:f eqn="prod #0 2 1"/><v:f eqn="sum 21600 0 @1"/>'
        '<v:f eqn="sum 0 0 @2"/><v:f eqn="sum 21600 0 @3"/><v:f eqn="if @0 @3 0"/>'
        '<v:f eqn="if @0 21600 @1"/><v:f eqn="if @0 0 @2"/><v:f eqn="if @0 @4 21600"/>'
        '<v:f eqn="mid @5 @6"/><v:f eqn="mid @8 @5"/><v:f eqn="mid @7 @8"/>'
        '<v:f eqn="mid @6 @7"/><v:f eqn="sum @6 0 @5"/>'
        "</v:formulas>"
        '<v:path textpathok="t" o:connecttype="custom" '
        'o:connectlocs="@9,0;@10,10800;@11,21600;@12,10800" o:connectangles="270,180,90,0"/>'
        '<v:textpath on="t" fitshape="t"/>'
        '<v:handles><v:h position="#0,bottomRight" xrange="6629,14971"/></v:handles>'
        '<o:lock v:ext="edit" text="t" shapetype="t"/>'
        "</v:shapetype>"
        '<v:shape id="PowerPlusWaterMarkObjectForGranskning" o:spid="_x0000_s102" '
        'type="#_x0000_t136" '
        'style="position:absolute;margin-left:0;margin-top:0;width:461.85pt;height:197.95pt;'
        "rotation:315;z-index:-251640320;mso-position-horizontal:center;"
        "mso-position-horizontal-relative:margin;mso-position-vertical:center;"
        'mso-position-vertical-relative:margin" '
        'o:allowincell="f" fillcolor="silver" stroked="f">'
        '<v:fill opacity=".5"/>'
        f'<v:textpath style="font-family:&quot;Calibri&quot;;font-size:1pt" string="{escaped_text}"/>'
        '<w10:wrap anchorx="margin" anchory="margin"/>'
        "</v:shape></w:pict></w:r></w:p>"
    )


def _find_enclosing_paragraph_span(text: str, start: int, end: int) -> tuple[int, int] | None:
    """Ges spannet för ett element inuti ``text``, returnerar spannet för
    det närmast omslutande ``<w:p>...</w:p>``-stycket, eller ``None`` om
    det inte kan hittas. Direkt port av
    ``version_1.py:_find_enclosing_paragraph_span``.
    """
    paragraph_start = None
    for match in _PARAGRAPH_START_RE.finditer(text, 0, start):
        paragraph_start = match.start()
    if paragraph_start is None:
        return None
    paragraph_end = text.find("</w:p>", end)
    if paragraph_end == -1:
        return None
    paragraph_end += len("</w:p>")
    return paragraph_start, paragraph_end


def _apply_watermark_to_xml(xml_text: str, watermark_text: str | None, insert_at: int) -> tuple[str, bool]:
    """Applicerar (eller tar bort) den diagonala granskningsvattenstämpeln
    direkt på den avkodade XML-texten för en dokument-/sidhuvudsdel,
    enbart via riktade strängredigeringar.

    Direkt port av ``version_1.py:_apply_watermark_to_xml``. Detta
    undviker medvetet att tolka och serialisera om hela delen med
    ElementTree: riktiga Word-dokument deklarerar dussintals
    namnrymder (mc, w14, w15, wp14, cx1-cx8, ...) kopplade till
    markup-kompatibilitetsmetadata (``mc:Ignorable``). En fullständig
    ElementTree-omvandling byter namn på/kollapsar prefix den inte
    känner igen, vilket tyst förstör den bokföringen — Word vägrar då
    öppna filen. Genom att bara redigera de exakta
    vattenstämpel-/metadatasträngarna lämnas varje annan byte i
    dokumentet orört.

    Returnerar (den ev. oförändrade) XML-texten samt en flagga för om
    en vattenstämpelform finns i den efteråt.
    """
    if watermark_text:
        match = _WATERMARK_SHAPE_RE.search(xml_text)
        if match is None:
            if insert_at < 0 or insert_at > len(xml_text):
                return xml_text, False
            xml_text = xml_text[:insert_at] + _watermark_paragraph_xml(watermark_text) + xml_text[insert_at:]
            return xml_text, True

        while match is not None:
            new_shape_text = _TEXTPATH_STRING_RE.sub(
                lambda m: m.group(1) + _xml_escape_attr(watermark_text) + m.group(2),
                match.group(0),
                count=1,
            )
            xml_text = xml_text[: match.start()] + new_shape_text + xml_text[match.end() :]
            next_match = _WATERMARK_SHAPE_RE.search(xml_text, match.start() + len(new_shape_text))
            match = next_match
        return xml_text, True

    match = _WATERMARK_SHAPE_RE.search(xml_text)
    while match is not None:
        span = _find_enclosing_paragraph_span(xml_text, match.start(), match.end())
        if span is None:
            break
        xml_text = xml_text[: span[0]] + xml_text[span[1] :]
        match = _WATERMARK_SHAPE_RE.search(xml_text)
    return xml_text, False


def _body_insertion_point(document_xml: str) -> int:
    """Returnerar teckenoffset för sista barnet i ``<w:body>``: precis
    innan det dokumentnivå-``w:sectPr`` om det finns, annars precis
    innan avslutande ``</w:body>``-taggen. Direkt port av
    ``version_1.py:_body_insertion_point``.
    """
    start = document_xml.rfind("<w:sectPr")
    if start != -1:
        return start
    body_close = document_xml.rfind("</w:body>")
    if body_close == -1:
        raise ValueError("Could not find <w:body> in document.xml")
    return body_close


def _remove_inserted_status_paragraphs(document_xml: str) -> str:
    """Tar bort statustext som infogats av tidigare versioner av detta
    verktyg. Direkt port av
    ``version_1.py:_remove_inserted_status_paragraphs``.

    Äldre, omärkta stycken tas bara bort om de förekommer omedelbart
    före det dokumentnivå-``w:sectPr`` som verktyget själv lade dem
    intill, för att undvika att ta bort matchande text som tillhör
    dokumentet på riktigt.
    """
    document_xml = _OWN_STATUS_PARAGRAPH_RE.sub("", document_xml)
    insert_at = _body_insertion_point(document_xml)
    body_prefix = document_xml[:insert_at]
    body_suffix = document_xml[insert_at:]

    while True:
        match = _LEGACY_STATUS_PARAGRAPH_RE.search(body_prefix)
        if match is None:
            break
        body_prefix = body_prefix[: match.start()]

    return body_prefix + body_suffix


def _write_zip_atomic(path: Path, members: list[tuple[ZipInfo, bytes]]) -> None:
    """Skriver ett helt nytt arkiv med exakt ``members`` till en
    temporärfil i samma katalog som ``path``, och ersätter sedan
    ``path`` atomärt med den. Direkt port av
    ``version_1.py:_write_zip_atomic``.
    """
    fd, tmp_name = tempfile.mkstemp(suffix=path.suffix, dir=str(path.parent))
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        with ZipFile(tmp_path, "w", compression=ZIP_DEFLATED) as dst_zip:
            for info, data in members:
                dst_zip.writestr(info, data)
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def update_docx_status(path: str | Path, status: str) -> Path:
    """Sätter (eller tar bort) den diagonala granskningsvattenstämpeln i
    en DOCX-fil, beroende på om ``status`` är en granskningsstatus.

    Direkt port av ``version_1.py:update_docx_status``. Idempotent:
    att köra funktionen igen på en redan bearbetad fil skapar aldrig
    dubbla vattenstämplar.
    """
    path = Path(path).expanduser().resolve()

    normalized_status = status.strip().casefold()
    is_review_status = normalized_status in REVIEW_STATUS_VALUES
    watermark_text = WATERMARK_TEXT if is_review_status else None

    with ZipFile(path, "r") as src_zip:
        header_names = [name for name in src_zip.namelist() if _HEADER_PART_RE.match(name)]

        document_xml = src_zip.read("word/document.xml").decode("utf-8")
        document_xml = _remove_inserted_status_paragraphs(document_xml)

        watermark_applied_in_header = False
        updated_headers: dict[str, bytes] = {}
        for header_name in header_names:
            header_xml = src_zip.read(header_name).decode("utf-8")
            header_insert_at = header_xml.rfind("</w:hdr>")
            updated_header, found = _apply_watermark_to_xml(header_xml, watermark_text, header_insert_at)
            updated_headers[header_name] = updated_header.encode("utf-8")
            watermark_applied_in_header = watermark_applied_in_header or found

        # Dokument utan sidhuvudsdelar behöver vattenstämpeln
        # tillagd/borttagen direkt i dokumentets brödtext i stället.
        if not watermark_applied_in_header:
            insert_at = _body_insertion_point(document_xml)
            document_xml, _ = _apply_watermark_to_xml(document_xml, watermark_text, insert_at)

        part_overrides = {"word/document.xml": document_xml.encode("utf-8")}
        part_overrides.update(updated_headers)

        members = []
        for item in src_zip.infolist():
            data = part_overrides.get(item.filename)
            if data is None:
                data = src_zip.read(item.filename)
            members.append((item, data))

    _write_zip_atomic(path, members)

    return path


def has_review_watermark(path: str | Path) -> bool:
    """Returnerar om DOCX-filen innehåller vattenstämpelformen för granskning."""
    path = Path(path).expanduser().resolve()
    with ZipFile(path, "r") as archive:
        word_parts = [
            name
            for name in archive.namelist()
            if name == "word/document.xml" or _HEADER_PART_RE.match(name)
        ]
        return any(
            _WATERMARK_SHAPE_RE.search(archive.read(name).decode("utf-8")) is not None
            for name in word_parts
        )


def update_xlsx_status(path: str | Path, status: str) -> Path:
    """Skriver ``status`` som text i cell A1 på arbetsbokens första blad.

    Direkt port av ``version_1.py:update_xlsx_status``.
    """
    path = Path(path).expanduser().resolve()

    with ZipFile(path, "r") as src_zip:
        worksheet_xml = src_zip.read("xl/worksheets/sheet1.xml")
        root = ET.fromstring(worksheet_xml)
        sheet_data = root.find(f"{{{_NS_SPREADSHEET}}}sheetData")
        if sheet_data is None:
            raise ValueError(f"Could not find sheetData in {path}")

        row = sheet_data.find(f"{{{_NS_SPREADSHEET}}}row")
        if row is None:
            row = ET.SubElement(sheet_data, f"{{{_NS_SPREADSHEET}}}row")
            row.set("r", "1")

        cell = None
        for candidate in row.findall(f"{{{_NS_SPREADSHEET}}}c"):
            if candidate.attrib.get("r") == "A1":
                cell = candidate
                break

        if cell is None:
            cell = ET.SubElement(row, f"{{{_NS_SPREADSHEET}}}c")
            cell.set("r", "A1")
            cell.set("t", "inlineStr")
            is_node = ET.SubElement(cell, "is")
            value_node = ET.SubElement(is_node, "t")
            value_node.text = status
            value_node.set("xml:space", "preserve")
        else:
            if cell.attrib.get("t") != "inlineStr":
                cell.set("t", "inlineStr")
            inline = cell.find("is")
            if inline is None:
                inline = ET.SubElement(cell, "is")
            value_node = inline.find("t")
            if value_node is None:
                value_node = ET.SubElement(inline, "t")
            value_node.text = status
            value_node.set("xml:space", "preserve")

        updated_sheet = ET.tostring(root, encoding="utf-8", xml_declaration=True)

        part_overrides = {"xl/worksheets/sheet1.xml": updated_sheet}
        members = []
        for item in src_zip.infolist():
            data = part_overrides.get(item.filename)
            if data is None:
                data = src_zip.read(item.filename)
            members.append((item, data))

    _write_zip_atomic(path, members)

    return path


def update_document_status(path: str | Path, status: str) -> Path:
    """Uppdaterar granskningsstatus för ett DOCX- eller XLSX-dokument.

    Direkt port av ``version_1.py:update_document_status``: dispatchar
    till ``update_docx_status``/``update_xlsx_status`` baserat på
    filändelse.
    """
    source_path = Path(path).expanduser().resolve()
    if not source_path.exists():
        raise FileNotFoundError(f"File not found: {source_path}")

    suffix = source_path.suffix.lower()
    if suffix == ".docx":
        return update_docx_status(source_path, status)
    if suffix == ".xlsx":
        return update_xlsx_status(source_path, status)
    raise ValueError(f"Unsupported file type: {source_path}")


def process_directory(base_path: str | Path, status: str, recursive: bool = False) -> list[Path]:
    """Uppdaterar granskningsstatus för alla DOCX/XLSX-dokument i en katalog.

    Direkt port av ``version_1.py:process_directory``, men återanvänder
    ``backend.file_discovery.find_documents`` (redan porterad och
    testad i Fas 2) i stället för att duplicera sökmönstret.
    """
    processed: list[Path] = []
    for document_path in find_documents(base_path, recursive=recursive):
        processed.append(update_document_status(document_path, status))
    return processed
