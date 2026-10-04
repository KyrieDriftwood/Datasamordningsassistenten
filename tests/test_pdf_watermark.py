"""Tester för vattenstämpeln KONTROLLÄRENDE i PDF (backend/pdf_watermark.py)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject, NameObject, NumberObject

from backend import pdf_watermark
from backend.pdf_watermark import (
    WATERMARK_TEXT,
    PdfWatermarkError,
    apply_control_watermark_state,
    has_control_watermark,
    set_control_watermark,
)


def _make_pdf(path: Path, sizes=((595, 842),), rotations=(0,)) -> Path:
    writer = PdfWriter()
    for (width, height), rotation in zip(sizes, rotations, strict=True):
        page = writer.add_blank_page(width, height)
        if rotation:
            page.rotate(rotation)
    writer.write(path)
    return path


def test_add_and_remove_round_trip_keeps_original_bytes(tmp_path):
    pdf = _make_pdf(tmp_path / "plan.pdf", sizes=((842, 595), (595, 842)), rotations=(0, 90))
    original = pdf.read_bytes()

    assert not has_control_watermark(pdf)
    assert set_control_watermark(pdf, True) is True
    assert has_control_watermark(pdf)
    stamped = pdf.read_bytes()
    # Inkrementell skrivning: originalets bytes ligger kvar oförändrade först i filen.
    assert stamped.startswith(original)
    reader = PdfReader(pdf)
    assert len(reader.pages) == 2
    assert all(WATERMARK_TEXT in page.extract_text() for page in reader.pages)

    assert set_control_watermark(pdf, False) is True
    assert not has_control_watermark(pdf)
    reader = PdfReader(pdf)
    assert all(WATERMARK_TEXT not in page.extract_text() for page in reader.pages)
    assert [entry.name for entry in tmp_path.iterdir()] == ["plan.pdf"]


def test_setting_same_state_is_a_no_op(tmp_path):
    pdf = _make_pdf(tmp_path / "plan.pdf")
    assert set_control_watermark(pdf, False) is False
    set_control_watermark(pdf, True)
    before = pdf.read_bytes()
    assert set_control_watermark(pdf, True) is False
    assert pdf.read_bytes() == before


def test_partially_stamped_pdf_is_completed(tmp_path):
    pdf = _make_pdf(tmp_path / "plan.pdf")
    set_control_watermark(pdf, True)
    writer = PdfWriter(clone_from=pdf)
    writer.add_blank_page(595, 842)
    writer.write(pdf)

    assert has_control_watermark(pdf)
    assert set_control_watermark(pdf, True) is True
    reader = PdfReader(pdf)
    assert all(pdf_watermark._page_has_watermark(page) for page in reader.pages)


def test_shared_and_inherited_resources_are_not_mutated(tmp_path):
    writer = PdfWriter()
    first = writer.add_blank_page(595, 842)
    second = writer.add_blank_page(842, 595)
    shared = writer._add_object(
        DictionaryObject({NameObject("/ProcSet"): ArrayObject([NameObject("/PDF")])})
    )
    del first[NameObject("/Resources")]
    del second[NameObject("/Resources")]
    pages_root = writer._root_object["/Pages"].get_object()
    pages_root[NameObject("/Resources")] = shared
    pdf = tmp_path / "inherited.pdf"
    writer.write(pdf)

    set_control_watermark(pdf, True)

    reader = PdfReader(pdf)
    inherited = reader.trailer["/Root"]["/Pages"]["/Resources"]
    assert "/XObject" not in inherited
    forms = [page["/Resources"]["/XObject"]["/DSAKontrollarende"].get_object() for page in reader.pages]
    assert [list(map(float, form["/BBox"])) for form in forms] == [[0, 0, 595, 842], [0, 0, 842, 595]]
    assert all(page["/Resources"]["/ProcSet"] == ["/PDF"] for page in reader.pages)

    set_control_watermark(pdf, False)
    assert all("/DSAKontrollarende" not in page["/Resources"].get("/XObject", {}) for page in PdfReader(pdf).pages)


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_watermark_is_centred_on_page_for_every_rotation(rotation):
    content = pdf_watermark._watermark_content((0, 0, 842, 595), rotation, WATERMARK_TEXT).decode("latin-1")
    assert "(KONTROLL\xc4RENDE) Tj" in content
    tokens = content.split(" Tm")[0].split()
    a, b, c, d, start_x, start_y = map(float, tokens[-6:])
    size = float(content.split(" Tf")[0].split()[-1])
    width = sum(pdf_watermark._HELVETICA_WIDTHS[char] for char in WATERMARK_TEXT) / 1000 * size
    cap = pdf_watermark._HELVETICA_CAP_HEIGHT * size
    centre_x = start_x + a * width / 2 + c * cap / 2
    centre_y = start_y + b * width / 2 + d * cap / 2
    assert centre_x == pytest.approx(421, abs=0.01)
    assert centre_y == pytest.approx(297.5, abs=0.01)
    assert width < (842**2 + 595**2) ** 0.5


def test_encrypted_pdf_is_refused(tmp_path):
    writer = PdfWriter()
    writer.add_blank_page(595, 842)
    writer.encrypt("hemligt")
    pdf = tmp_path / "krypterad.pdf"
    writer.write(pdf)
    before = pdf.read_bytes()

    with pytest.raises(PdfWatermarkError, match="krypterad"):
        set_control_watermark(pdf, True)
    assert pdf.read_bytes() == before


def test_signed_pdf_is_refused(tmp_path):
    writer = PdfWriter()
    writer.add_blank_page(595, 842)
    writer._root_object[NameObject("/AcroForm")] = DictionaryObject(
        {NameObject("/Fields"): ArrayObject(), NameObject("/SigFlags"): NumberObject(3)}
    )
    pdf = tmp_path / "signerad.pdf"
    writer.write(pdf)

    with pytest.raises(PdfWatermarkError, match="signerad"):
        set_control_watermark(pdf, True)
    assert not has_control_watermark(pdf)


def test_invalid_pdf_raises_watermark_error(tmp_path):
    pdf = tmp_path / "trasig.pdf"
    pdf.write_bytes(b"inte en pdf")
    with pytest.raises(PdfWatermarkError):
        has_control_watermark(pdf)
    with pytest.raises(PdfWatermarkError):
        set_control_watermark(pdf, True)
    assert pdf.read_bytes() == b"inte en pdf"


def test_failed_read_back_leaves_original_untouched(tmp_path, monkeypatch):
    pdf = _make_pdf(tmp_path / "plan.pdf")
    before = pdf.read_bytes()
    monkeypatch.setattr(pdf_watermark, "_add_to_page", lambda *_args, **_kwargs: None)

    with pytest.raises(PdfWatermarkError, match="Read-back"):
        set_control_watermark(pdf, True)
    assert pdf.read_bytes() == before
    assert [entry.name for entry in tmp_path.iterdir()] == ["plan.pdf"]


def test_locked_file_is_reported_and_temp_file_removed(tmp_path, monkeypatch):
    pdf = _make_pdf(tmp_path / "plan.pdf")
    before = pdf.read_bytes()

    def locked(*_args):
        raise PermissionError("filen används av en annan process")

    monkeypatch.setattr(os, "replace", locked)
    with pytest.raises(PdfWatermarkError, match="öppen i en PDF-läsare"):
        set_control_watermark(pdf, True)
    assert pdf.read_bytes() == before
    assert [entry.name for entry in tmp_path.iterdir()] == ["plan.pdf"]


def test_batch_continues_after_a_failing_file(tmp_path):
    good = _make_pdf(tmp_path / "bra.pdf")
    bad = tmp_path / "trasig.pdf"
    bad.write_bytes(b"inte en pdf")

    results = apply_control_watermark_state({bad: True, good: True})

    assert [(result.path, result.changed, result.error is None) for result in results] == [
        (bad, False, False),
        (good, True, True),
    ]
    assert has_control_watermark(good)


def test_custom_text_is_stored_and_replaced(tmp_path):
    from backend.pdf_watermark import read_watermark_text

    pdf = _make_pdf(tmp_path / "plan.pdf", sizes=((842, 595), (595, 842)), rotations=(0, 0))
    assert read_watermark_text(pdf) is None

    assert set_control_watermark(pdf, True, text="Utkast (rev B)") is True
    assert read_watermark_text(pdf) == "Utkast (rev B)"
    assert set_control_watermark(pdf, True, text="Utkast (rev B)") is False

    assert set_control_watermark(pdf, True, text="FÖR GRANSKNING") is True
    reader = PdfReader(pdf)
    texts = [page.extract_text() for page in reader.pages]
    assert all("FÖR GRANSKNING" in text and "Utkast" not in text for text in texts)
    assert all(len([ref for ref in page["/Contents"] if pdf_watermark._is_marker_stream(ref)]) == 2 for page in reader.pages)
    assert read_watermark_text(pdf) == "FÖR GRANSKNING"


def test_repeated_incremental_updates_never_reuse_object_numbers(tmp_path):
    # Regression: pypdf placed new objects on the number of the previous
    # update's xref stream, so the reader resolved the stamp to the wrong object.
    pdf = _make_pdf(tmp_path / "plan.pdf")
    from backend.pdf_watermark import read_watermark_text

    for step, text in enumerate(["A", "B", None, "C", "D", None, "E"]):
        set_control_watermark(pdf, text is not None, text=text or "X")
        assert read_watermark_text(pdf) == text, step
    assert set_control_watermark(pdf, True, text="E") is False


@pytest.mark.parametrize(
    ("text", "message"),
    [("", "tom"), ("   ", "tom"), ("A" * 41, "högst"), ("Pris €", "inte stöds"), ("Tab\u00a7", "inte stöds")],
)
def test_invalid_text_is_refused_before_file_is_touched(tmp_path, text, message):
    pdf = _make_pdf(tmp_path / "plan.pdf")
    before = pdf.read_bytes()
    with pytest.raises(PdfWatermarkError, match=message):
        set_control_watermark(pdf, True, text=text)
    assert pdf.read_bytes() == before


def test_validate_watermark_text_normalises_whitespace():
    from backend.pdf_watermark import validate_watermark_text

    assert validate_watermark_text("  Utkast   rev 2 ") == "Utkast rev 2"
