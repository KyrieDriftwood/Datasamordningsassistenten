from __future__ import annotations

import io
from pathlib import Path

from app.controllers import dwg_controller
from dwg import DwgAttributeResult, MetadataField, pdf_plotter
from dwg.pdf_plotter import DwgPdfJob, DwgPdfPlotResult
from dwg.pdf_sync import clear_pdf_sync_record, load_status_records, record_verified_pdf, status_for


def test_pdf_sync_status_tracks_both_dwg_and_pdf_signatures(tmp_path):
    drawing = tmp_path / "drawing.dwg"
    output = tmp_path / "drawing.pdf"
    drawing.write_bytes(b"dwg-v1")
    output.write_bytes(b"pdf-v1")

    records = load_status_records(tmp_path)
    assert status_for(tmp_path, drawing, records)[0] == "stale"

    record_verified_pdf(tmp_path, drawing, output)
    records = load_status_records(tmp_path)
    assert status_for(tmp_path, drawing, records)[0] == "synced"

    drawing.write_bytes(b"dwg-v2-with-different-size")
    assert status_for(tmp_path, drawing, records)[0] == "stale"

    record_verified_pdf(tmp_path, drawing, output)
    records = load_status_records(tmp_path)
    output.write_bytes(b"pdf-v2-with-different-size")
    assert status_for(tmp_path, drawing, records)[0] == "stale"


def test_plot_many_decodes_backend_results_and_reports_progress(tmp_path, monkeypatch):
    backend = tmp_path / "backend.exe"
    backend.touch()
    monkeypatch.setattr(pdf_plotter, "locate_pdf_backend", lambda: backend)

    class FakeProcess:
        def __init__(self, command, **kwargs):
            manifest_path = Path(command[2])
            manifest = manifest_path.read_text(encoding="ascii").splitlines()
            assert len(manifest) == 2
            self.stdout = io.StringIO(
                "###RESULT\t1\tWARN\t6d69736d61746368\n"
                "###RESULT\t0\tOK\t\n"
            )
            self.return_code = 0

        def wait(self):
            return self.return_code

        def kill(self):
            self.return_code = -9

    monkeypatch.setattr(pdf_plotter.subprocess, "Popen", FakeProcess)
    jobs = (
        DwgPdfJob(tmp_path / "a.dwg", tmp_path / "a.pdf", (("Name", "value-a"),)),
        DwgPdfJob(tmp_path / "b.dwg", tmp_path / "b.pdf"),
    )
    progress = []

    results = pdf_plotter.plot_many(jobs, progress_callback=lambda done, total, result: progress.append(
        (done, total, result.drawing)
    ))

    assert results[(tmp_path / "a.dwg").resolve()] == DwgPdfPlotResult(
        (tmp_path / "a.dwg").resolve(), (tmp_path / "a.pdf").resolve()
    )
    assert results[(tmp_path / "b.dwg").resolve()].warning == "mismatch"
    assert [item[:2] for item in progress] == [(1, 2), (2, 2)]


def test_pdf_sync_reports_missing_pdf_after_a_created_record(tmp_path):
    drawing = tmp_path / "drawing.dwg"
    output = tmp_path / "drawing.pdf"
    drawing.write_bytes(b"dwg")
    output.write_bytes(b"pdf")
    record_verified_pdf(tmp_path, drawing, output)
    output.unlink()

    state, recorded_output = status_for(tmp_path, drawing, load_status_records(tmp_path))

    assert state == "missing"
    assert recorded_output == output


def test_controller_passes_metadata_to_pdf_check(tmp_path, monkeypatch):
    drawing = (tmp_path / "drawing.dwg").resolve()
    drawing.touch()
    captured_jobs = []
    standard = DwgAttributeResult(
        drawing,
        (MetadataField("Slm", "Sträcka", "Exempelö", True, "Slm"),),
    )
    model = DwgAttributeResult(
        drawing,
        (MetadataField("Modell", "Modellnamn", "DEMO 00+100", False, "Modell"),),
    )
    monkeypatch.setattr(dwg_controller, "extract_many_dwg_attributes_fast", lambda paths: {drawing: standard})
    monkeypatch.setattr(dwg_controller, "extract_many_dwg_model_attributes", lambda paths: {drawing: model})

    def fake_plot_many(jobs, *, progress_callback):
        captured_jobs.extend(jobs)
        result = DwgPdfPlotResult(drawing, drawing.with_suffix(".pdf"))
        progress_callback(1, 1, result)
        return {drawing: result}

    monkeypatch.setattr(dwg_controller, "plot_many", fake_plot_many)
    progress = []

    results = dwg_controller.plot_dwgs_to_pdfs(
        [drawing],
        progress_callback=lambda done, total: progress.append((done, total)),
    )

    assert captured_jobs == [
        DwgPdfJob(
            drawing,
            drawing.with_suffix(".pdf"),
            (("Sträcka", "Exempelö"), ("Modellnamn", "DEMO 00+100")),
            None,
        )
    ]
    assert results[drawing].error is None
    assert progress == [(1, 1)]


def test_controller_reuses_complete_metadata_cache(tmp_path, monkeypatch):
    drawing = (tmp_path / "drawing.dwg").resolve()
    drawing.touch()
    standard = DwgAttributeResult(
        drawing,
        (MetadataField("Slm", "Sträcka", "Exempelö", True, "Slm"),),
    )
    model = DwgAttributeResult(
        drawing,
        (MetadataField("Modell", "Modellnamn", "DEMO 00+100", False, "Modell"),),
    )
    monkeypatch.setattr(
        dwg_controller,
        "extract_many_dwg_attributes_fast",
        lambda _paths: (_ for _ in ()).throw(AssertionError("standard cache was not reused")),
    )
    monkeypatch.setattr(
        dwg_controller,
        "extract_many_dwg_model_attributes",
        lambda _paths: (_ for _ in ()).throw(AssertionError("model cache was not reused")),
    )
    monkeypatch.setattr(
        dwg_controller,
        "plot_many",
        lambda jobs, *, progress_callback: {
            drawing: DwgPdfPlotResult(drawing, drawing.with_suffix(".pdf"))
        },
    )

    results = dwg_controller.plot_dwgs_to_pdfs(
        [drawing],
        standard_metadata={drawing: standard},
        model_metadata={drawing: model},
    )

    assert results[drawing].error is None


def test_model_metadata_uses_autocad_when_rust_echoes_attribute_tags(tmp_path, monkeypatch):
    drawing = (tmp_path / "drawing.dwg").resolve()
    rust_result = DwgAttributeResult(
        drawing,
        (
            MetadataField("DATUM", "DATUM", "DATUM", True, "DATUM"),
            MetadataField("PRODUKT", "PRODUKT", "PRODUKT", True, "PRODUKT"),
        ),
    )
    autocad_result = DwgAttributeResult(
        drawing,
        (
            MetadataField("DATUM", "DATUM", "2010-10-10", True, "DATUM"),
            MetadataField("PRODUKT", "PRODUKT", "BYGGHANDLING", True, "PRODUKT"),
        ),
    )
    monkeypatch.setattr(
        dwg_controller,
        "extract_many_dwg_model_attributes",
        lambda paths, **_kwargs: {drawing: rust_result},
    )
    calls = []

    def extract_with_autocad(paths, block_name, *, max_workers=4, progress_callback=None):
        calls.append((tuple(paths), block_name))
        return {drawing: autocad_result}

    monkeypatch.setattr(dwg_controller, "extract_many_dwg_attributes_for_block", extract_with_autocad)

    result = dwg_controller.extract_model_attributes_fast([drawing])

    assert result == {drawing: autocad_result}
    assert calls == [((drawing,), "TRVJ_NAMNRUTA_MODELL")]


def test_model_metadata_reports_autocad_fallback_failure(tmp_path, monkeypatch):
    drawing = (tmp_path / "drawing.dwg").resolve()
    rust_result = DwgAttributeResult(
        drawing,
        (MetadataField("DATUM", "DATUM", "DATUM", True, "DATUM"),),
    )
    fallback_result = DwgAttributeResult(drawing, (), "AutoCAD Core Console svarade inte")
    monkeypatch.setattr(
        dwg_controller,
        "extract_many_dwg_model_attributes",
        lambda paths, **_kwargs: {drawing: rust_result},
    )
    monkeypatch.setattr(
        dwg_controller,
        "extract_many_dwg_attributes_for_block",
        lambda paths, block_name, **_kwargs: {drawing: fallback_result},
    )

    result = dwg_controller.extract_model_attributes_fast([drawing])[drawing]

    assert result.fields == ()
    assert result.error is not None
    assert "AutoCAD-reservläsningen misslyckades" in result.error
    assert "AutoCAD Core Console svarade inte" in result.error


def test_controller_plots_even_when_title_block_is_missing(tmp_path, monkeypatch):
    drawing = (tmp_path / "no-block.dwg").resolve()
    drawing.touch()
    captured_jobs = []
    standard = DwgAttributeResult(drawing, (), "Hittade inget block med namnet TRVJ_NAMNRUTA.")
    model = DwgAttributeResult(drawing, (), "Hittade inget block med namnet TRVJ_NAMNRUTA_MODELL.")
    monkeypatch.setattr(dwg_controller, "extract_many_dwg_attributes_fast", lambda paths: {drawing: standard})
    monkeypatch.setattr(dwg_controller, "extract_many_dwg_model_attributes", lambda paths: {drawing: model})

    def fake_plot_many(jobs, *, progress_callback):
        captured_jobs.extend(jobs)
        result = DwgPdfPlotResult(drawing, drawing.with_suffix(".pdf"), warning="metadata missing")
        progress_callback(1, 1, result)
        return {drawing: result}

    monkeypatch.setattr(dwg_controller, "plot_many", fake_plot_many)

    results = dwg_controller.plot_dwgs_to_pdfs([drawing])

    assert len(captured_jobs) == 1
    assert captured_jobs[0].expected_values == ()
    assert "TRVJ_NAMNRUTA" in captured_jobs[0].verification_issue
    assert results[drawing].warning == "metadata missing"


def test_pdf_sync_record_can_be_cleared_after_metadata_mismatch(tmp_path):
    drawing = tmp_path / "drawing.dwg"
    output = tmp_path / "drawing.pdf"
    drawing.write_bytes(b"dwg")
    output.write_bytes(b"pdf")
    record_verified_pdf(tmp_path, drawing, output)
    assert status_for(tmp_path, drawing, load_status_records(tmp_path))[0] == "synced"

    clear_pdf_sync_record(tmp_path, drawing)

    assert status_for(tmp_path, drawing, load_status_records(tmp_path))[0] == "stale"
