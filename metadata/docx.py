"""Läsning/skrivning av DOCX-metadata via Word Content Controls.

Motsvarar logiken i .old/python_v5/src/version_3.py för att läsa och
skriva Content Controls i "Fyll i Försättsida"-flödet.

Kravkälla: docs/kravsparning.md, modul metadata/docx.

OBS: Skrivning sker via Word-automatisering (VBScript + cscript.exe),
precis som i baslinjen — inte via ren XML-manipulation. Det innebär
att skrivvägen kräver att Microsoft Word är installerat och att
Windows Script Host (cscript.exe) är tillgängligt. Detta är en direkt
port, inte en ny lösning; se docs/kravsparning.md om detta någon gång
ska bytas ut mot ren OOXML-skrivning.
"""

from __future__ import annotations

import re
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from backend.office_automation import (
    WORD_IMAGE,
    BatchOutcome,
    OfficeScriptHostMissing,
    OfficeScriptTimeout,
    batch_timeout,
    parse_batch_output,
    run_office_script,
    vbscript_literal,
)
from metadata import DocumentMetadata, MetadataError, MetadataField

DOCX_EXTENSION = ".docx"

_WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W = f"{{{_WORD_NS}}}"


def extract_docx_metadata(path: str | Path) -> DocumentMetadata:
    """Läser Content Controls (taggar/alias/text) ur brödtext, sidhuvud och sidfot.

    Direkt port av ``version_3.py:_extract_docx_metadata``, men med
    egen filvalidering (sökväg/filändelse) i stället för att förlita
    sig på den delade dispatch-funktionen, så att modulen kan användas
    fristående. Varje `<w:sdt>`-kontroll med både tagg (nyckel) och
    alias (etikett) blir ett `MetadataField`; kontroller utan bådadera,
    eller med redan sedd nyckel, hoppas över (matchar baslinjens
    ``seen_keys``-spärr mot dubbletter).
    """
    document_path = Path(path).expanduser().resolve()
    if not document_path.is_file():
        raise FileNotFoundError(f"Filen finns inte: {document_path}")
    if document_path.suffix.lower() != DOCX_EXTENSION:
        raise ValueError(f"Filtypen stöds inte av DOCX-läsaren: {document_path.suffix}")

    try:
        with ZipFile(document_path, "r") as archive:
            parts = [(part, ET.fromstring(archive.read(part))) for part in _content_control_parts(archive.namelist())]
    except (BadZipFile, ET.ParseError, KeyError) as exc:
        raise MetadataError(f"Dokumentet kunde inte läsas: {document_path.name}") from exc

    fields: list[MetadataField] = []
    seen_keys: set[str] = set()
    for part, root in parts:
        for key, label, value, locked in _iter_controls(root):
            if key in seen_keys:
                continue
            seen_keys.add(key)
            # Låsta kontroller i sidhuvud/-fot (t.ex. SharePoints _dlc_DocId)
            # kan Word inte skriva till; brödtextens fält följer baslinjen.
            editable = part == _BODY_PART or not locked
            fields.append(MetadataField(key, label, value, editable, key))

    if not fields:
        raise MetadataError("DOCX-filen saknar identifierade metadatakontroller.")
    return DocumentMetadata(document_path, "docx", tuple(fields))


def _iter_controls(root: ET.Element):
    """Ger (tagg, alias, text, innehållslåst) för kontroller med tagg och alias."""
    for control in root.iter(_W + "sdt"):
        properties = control.find(_W + "sdtPr")
        content = control.find(_W + "sdtContent")
        if properties is None or content is None:
            continue
        tag_node = properties.find(_W + "tag")
        alias_node = properties.find(_W + "alias")
        key = tag_node.attrib.get(_W + "val", "").strip() if tag_node is not None else ""
        label = alias_node.attrib.get(_W + "val", "").strip() if alias_node is not None else ""
        if not key or not label:
            continue
        lock_node = properties.find(_W + "lock")
        lock = lock_node.attrib.get(_W + "val", "") if lock_node is not None else ""
        value = "".join(node.text or "" for node in content.iter(_W + "t"))
        yield key, label, value, lock in {"contentLocked", "sdtContentLocked"}


def docx_body_has_content_controls(path: str | Path) -> bool:
    """Sant om brödtexten har kontroller som baslinjen läser.

    ``metadata.grid`` kompletterar med tabelltext bara när detta är falskt,
    dvs. när baslinjen hade fallit tillbaka på tabelltext. Så ändras inte
    rutnätet för mallar vars brödtextkontroller redan fungerade.
    """
    try:
        with ZipFile(Path(path), "r") as archive:
            root = ET.fromstring(archive.read(_BODY_PART))
    except (BadZipFile, ET.ParseError, KeyError, OSError):
        return False
    return next(_iter_controls(root), None) is not None


_BODY_PART = "word/document.xml"


_HEADER_FOOTER_PART = re.compile(r"^word/(?:header|footer)\d*\.xml$")


def _content_control_parts(names: list[str]) -> list[str]:
    """Delar där Content Controls läses: brödtext först, sedan sidhuvud/-fot.

    Avvikelse från baslinjen (användarbeslut 2026-10-02, utanför TB): mallar
    med försättsblad har ibland sina kontroller i sidhuvudet
    (``word/header3.xml``). Baslinjen läste bara ``word/document.xml`` och
    rapporterade då filen som skrivskyddad. Skrivvägen
    (``_docx_update_script``) går därför igenom alla ``StoryRanges``.
    ``word/glossary/`` (byggblock) ingår inte – det är inte dokumentinnehåll.
    """
    headers_and_footers = sorted(name for name in names if _HEADER_FOOTER_PART.match(name))
    return ["word/document.xml", *headers_and_footers]


def _vbscript_literal(value: str | Path) -> str:
    return vbscript_literal(value)


# VBScript-funktion som uppdaterar ett dokument i den delade Word-instansen.
# Egen ``On Error Resume Next`` krävs: utan den hoppar ett fel ur funktionen
# och anroparen fortsätter efter anropet utan felmeddelande.
_UPDATE_DOCUMENT_FUNCTION = """
Function UpdateDocument(path, updates)
    On Error Resume Next
    Dim document, control, applied, story, storyRange, key, updateError
    Set applied = CreateObject("Scripting.Dictionary")
    Err.Clear
    Set document = application.Documents.Open(path, False, False)
    If Err.Number <> 0 Then
        UpdateDocument = "Dokumentet kunde inte öppnas: " & Err.Description
        Exit Function
    End If
    ' Ett skrivskyddat dokument får Save att öppna en osynlig Spara som-dialog,
    ' och då väntar Word tills tidsgränsen löper ut. Avbryt hellre direkt.
    If document.ReadOnly Then
        document.Close False
        UpdateDocument = "Dokumentet öppnades skrivskyddat (filattribut eller låst av annan användare)."
        Exit Function
    End If
    ' document.ContentControls omfattar bara brödtexten. Sidhuvud/-fot nås via
    ' StoryRanges och deras NextStoryRange-kedjor (en per sektion).
    For Each story In document.StoryRanges
        Set storyRange = story
        Do While Not storyRange Is Nothing
            For Each control In storyRange.ContentControls
                If updates.Exists(control.Tag) Then
                    control.Range.Text = updates(control.Tag)
                    If Err.Number <> 0 Then Exit For
                    applied(control.Tag) = True
                End If
            Next
            If Err.Number <> 0 Then Exit Do
            Set storyRange = storyRange.NextStoryRange
        Loop
        If Err.Number <> 0 Then Exit For
    Next
    updateError = Err.Description
    If Len(updateError) = 0 Then
        For Each key In updates.Keys
            If Not applied.Exists(key) Then
                updateError = "Content Control saknas: " & key
                Exit For
            End If
        Next
    End If
    If Len(updateError) = 0 Then document.Save
    If Len(updateError) = 0 And Err.Number <> 0 Then updateError = Err.Description
    Err.Clear
    document.Close False
    If Len(updateError) > 0 Then updateError = "Metadata kunde inte uppdateras: " & updateError
    UpdateDocument = Replace(Replace(updateError, vbCr, " "), vbLf, " ")
End Function
"""


def _docx_batch_update_script(jobs: list[tuple[Path, dict[str, str]]]) -> str:
    """Ett skript, en Word-instans, alla dokument; stannar vid första fel.

    Protokollet (``OK|i`` / ``FAIL|i|meddelande``) tolkas av
    ``backend.office_automation.parse_batch_output``.
    """
    calls: list[str] = []
    for index, (path, changes) in enumerate(jobs):
        assignments = "\n".join(
            f"updates.Add {_vbscript_literal(key)}, {_vbscript_literal(value)}" for key, value in changes.items()
        )
        calls.append(
            f"""
Set updates = CreateObject("Scripting.Dictionary")
{assignments}
failure = UpdateDocument({_vbscript_literal(path)}, updates)
If Len(failure) > 0 Then
    application.Quit
    WScript.Echo "FAIL|{index}|" & failure
    WScript.Quit 1
End If
WScript.Echo "OK|{index}"
"""
        )
    return f"""
On Error Resume Next
Dim application, updates, failure
Set application = CreateObject("Word.Application")
If Err.Number <> 0 Then
    WScript.Echo "Microsoft Word kunde inte startas: " & Err.Description
    WScript.Quit 1
End If
application.Visible = False
application.DisplayAlerts = 0
{_UPDATE_DOCUMENT_FUNCTION}
{"".join(calls)}
application.Quit
"""


def _docx_update_script(path: Path, changes: dict[str, str]) -> str:
    return _docx_batch_update_script([(path, changes)])


def run_docx_update_batch(jobs: list[tuple[Path, dict[str, str]]]) -> BatchOutcome:
    """Uppdaterar redan kopierade temporärfiler i en Word-instans.

    Returnerar hur många jobb (i ordning) som blev klara och ev. fel.
    Värdfel (cscript saknas) och tidsgräns höjs som ``MetadataError``; vid
    tidsgräns avslutas kvarlämnade Word-instanser (se office_automation).
    """
    if not jobs:
        return BatchOutcome(0)
    try:
        result = run_office_script(
            _docx_batch_update_script(jobs),
            image_name=WORD_IMAGE,
            timeout=batch_timeout(len(jobs)),
        )
    except OfficeScriptHostMissing as exc:
        raise MetadataError(str(exc)) from exc
    except OfficeScriptTimeout as exc:
        raise MetadataError(f"Metadatauppdateringen tog för lång tid. {exc}") from exc
    return parse_batch_output(result, len(jobs))


def write_docx_metadata(source: Path, destination: Path, changes: dict[str, str]) -> None:
    """Skriver uppdaterade Content Control-värden till ``destination``.

    Direkt port av DOCX-grenen i ``version_3.py:update_metadata``: kopierar
    källfilen till ``destination`` (temporärfilen) och låter Word sätta
    varje Content Control-text via dess tagg och spara. Anropande kod
    (``metadata.update_metadata``) ansvarar för zip-validering och atomärt
    utbyte efteråt. Flera dokument skrivs via ``run_docx_update_batch``.
    """
    shutil.copy2(source, destination)
    outcome = run_docx_update_batch([(destination, changes)])
    if outcome.failed_index is not None:
        raise MetadataError(f"Metadatauppdateringen misslyckades för {source.name}: {outcome.message}")
