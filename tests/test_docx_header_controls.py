"""Content Controls i sidhuvud/sidfot (syntetiskt försättsblad).

Användarbeslut 2026-10-02, utanför TB: kontroller i ``word/header*.xml`` och
``word/footer*.xml`` ska vara redigerbara precis som kontroller i brödtexten.
Baslinjen läste bara ``word/document.xml`` och visade då filen som skrivskyddad.
Själva Word-skrivningen kräver Word/COM och testas inte här; skriptets
innehåll kontrolleras i stället.
"""

from __future__ import annotations

from pathlib import Path
from zipfile import ZipFile

from metadata import extract_metadata
from metadata.docx import _docx_update_script
from metadata.grid import _with_table_metadata, extract_metadata_grid

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _sdt(tag: str, alias: str, value: str) -> str:
    return (
        f'<w:sdt><w:sdtPr><w:alias w:val="{alias}"/><w:tag w:val="{tag}"/></w:sdtPr>'
        f"<w:sdtContent><w:r><w:t>{value}</w:t></w:r></w:sdtContent></w:sdt>"
    )


def _header_controls_docx(path: Path) -> Path:
    header = (
        f'<?xml version="1.0" encoding="UTF-8"?><w:hdr xmlns:w="{_W_NS}">'
        "<w:tbl>"
        "<w:tr><w:tc><w:p><w:r><w:t>Projekt</w:t></w:r></w:p></w:tc></w:tr>"
        "<w:tr><w:tc><w:p><w:r><w:t>Ostlänken</w:t></w:r></w:p></w:tc></w:tr>"
        "</w:tbl>"
        f'<w:p>{_sdt("pw_TrackSection", "Bandel", "421")}</w:p>'
        "</w:hdr>"
    )
    footer = f'<?xml version="1.0" encoding="UTF-8"?><w:ftr xmlns:w="{_W_NS}"><w:p>{_sdt("pw_StartKM", "Start KM", "56")}</w:p></w:ftr>'
    glossary = f'<?xml version="1.0" encoding="UTF-8"?><w:glossaryDocument xmlns:w="{_W_NS}"><w:p>{_sdt("Byggblock", "Byggblock", "x")}</w:p></w:glossaryDocument>'
    document = f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{_W_NS}"><w:body><w:p><w:r><w:t>Text</w:t></w:r></w:p></w:body></w:document>'
    with ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", document)
        archive.writestr("word/header3.xml", header)
        archive.writestr("word/footer1.xml", footer)
        archive.writestr("word/glossary/document.xml", glossary)
    return path


def test_controls_in_header_and_footer_are_editable(tmp_path):
    document = extract_metadata(_header_controls_docx(tmp_path / "4001.docx"))

    fields = {field.key: field for field in document.fields}
    # Byggblock i word/glossary/ är inte dokumentinnehåll och ska inte med.
    assert set(fields) == {"pw_TrackSection", "pw_StartKM"}
    assert fields["pw_TrackSection"].value == "421"
    assert all(field.editable for field in fields.values())


def test_grid_keeps_controls_editable_and_adds_table_text_read_only(tmp_path):
    path = _header_controls_docx(tmp_path / "4001.docx")
    document = _with_table_metadata(extract_metadata(path))

    fields = {field.key: field for field in document.fields}
    assert fields["pw_TrackSection"].editable
    table_fields = [field for key, field in fields.items() if key not in {"pw_TrackSection", "pw_StartKM"}]
    assert table_fields
    assert any(field.value == "Ostlänken" for field in table_fields)
    assert not any(field.editable for field in table_fields)
    (column,) = extract_metadata_grid([path]).columns
    assert column.error is None


def test_update_script_reaches_header_and_footer_stories_and_reports_missing_tags(tmp_path):
    script = _docx_update_script(tmp_path / "4001.docx", {"pw_TrackSection": "422"})

    assert "document.StoryRanges" in script
    assert "NextStoryRange" in script
    assert "For Each control In document.ContentControls" not in script
    assert "Content Control saknas" in script


def test_locked_header_control_is_read_only(tmp_path):
    locked = (
        '<w:sdt><w:sdtPr><w:alias w:val="Dok-ID"/><w:tag w:val="_dlc_DocId"/><w:lock w:val="contentLocked"/></w:sdtPr>'
        "<w:sdtContent><w:r><w:t>IMS2-1</w:t></w:r></w:sdtContent></w:sdt>"
    )
    path = tmp_path / "locked.docx"
    with ZipFile(path, "w") as archive:
        archive.writestr(
            "word/document.xml",
            f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{_W_NS}"><w:body><w:p/></w:body></w:document>',
        )
        archive.writestr(
            "word/footer1.xml",
            f'<?xml version="1.0" encoding="UTF-8"?><w:ftr xmlns:w="{_W_NS}"><w:p>{locked}</w:p></w:ftr>',
        )
    (field,) = extract_metadata(path).fields
    assert field.key == "_dlc_DocId"
    assert not field.editable