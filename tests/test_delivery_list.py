"""Leveransförteckningsmallen: igenkänning och eget metadatarutnät.

Användarbeslut 2026-10-02, utanför TB: DOCX-filer med mallens Content
Controls (Leveranspaket, Leveransens innehåll m.fl.) hanteras i en egen
metadatahanteringsruta i stället för i standard-DOCX-tabellen.
"""

from __future__ import annotations

from pathlib import Path
from zipfile import ZipFile

from app.controllers import metadata_controller
from metadata.grid import (
    DELIVERY_LIST_CATEGORIES,
    extract_delivery_list_grid,
    is_delivery_list_document,
)

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _write_docx(path: Path, controls: list[tuple[str, str, str]]) -> Path:
    sdts = "".join(
        f'<w:sdt><w:sdtPr><w:alias w:val="{alias}"/><w:tag w:val="{tag}"/></w:sdtPr>'
        f"<w:sdtContent><w:p><w:r><w:t>{value}</w:t></w:r></w:p></w:sdtContent></w:sdt>"
        for tag, alias, value in controls
    )
    document = f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{_W_NS}"><w:body>{sdts}</w:body></w:document>'
    with ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", document)
    return path


def _delivery_list(path: Path, extra: list[tuple[str, str, str]] | None = None) -> Path:
    return _write_docx(
        path,
        [
            ("Leveranspaket", "Delivery", "1.15.3"),
            ("Anläggningsdel", "Part ID", "33104"),
            ("Leveransens innehåll", "Contents", "Kontroll enligt TRVINFRA 00229"),
            ("CurrentRev", "Current Rev", "Ange status"),
            *(extra or []),
        ],
    )


def test_delivery_list_is_detected_by_template_signature(tmp_path):
    delivery = _delivery_list(tmp_path / "lev.docx")
    standard = _write_docx(tmp_path / "std.docx", [("PROJECT_NAME", "Projekt", "Ostlänken")])
    only_one_signature_tag = _write_docx(tmp_path / "half.docx", [("Leveranspaket", "Delivery", "1")])
    broken = tmp_path / "broken.docx"
    broken.write_bytes(b"inte en zip")

    assert is_delivery_list_document(delivery)
    assert not is_delivery_list_document(standard)
    assert not is_delivery_list_document(only_one_signature_tag)
    assert not is_delivery_list_document(broken)
    assert not is_delivery_list_document(tmp_path / "saknas.docx")
    assert metadata_controller.is_delivery_list_document(delivery)


def test_delivery_list_grid_uses_template_columns_and_keeps_tags_editable(tmp_path):
    delivery = _delivery_list(tmp_path / "lev.docx", extra=[("EgenTagg", "Own", "x")])
    grid = extract_delivery_list_grid([delivery])

    row_keys = [row.key for row in grid.rows]
    template_keys = [key for key, _label in DELIVERY_LIST_CATEGORIES]
    assert row_keys[: len(template_keys)] == template_keys
    # Okända taggar i mallen tappas inte bort utan läggs sist.
    assert row_keys[-1] == "EgenTagg"

    (column,) = grid.columns
    assert column.error is None
    rows = {row.key: row for row in grid.rows}
    assert grid.value_at(rows["Leveranspaket"], column) == "1.15.3"
    assert grid.editable_at(rows["Leveranspaket"], column)
    # Skrivnyckeln är taggen, så sparning träffar rätt Content Control.
    assert grid.field_at(rows["CurrentRev"], column).key == "CurrentRev"
    assert rows["CurrentRev"].label == "Aktuell revision"
    assert grid.value_at(rows["Byggherre"], column) == ""


def test_delivery_list_grid_reports_unreadable_file(tmp_path):
    broken = tmp_path / "broken.docx"
    broken.write_bytes(b"inte en zip")
    grid = metadata_controller.build_delivery_list_grid([broken])
    (column,) = grid.columns
    assert column.error
