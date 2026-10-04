"""Tester för Office-batchning: protokoll, tidsgränsstädning och delresultat.

Användarbeslut 2026-10-02, utanför TB: en Office-instans per batch och
avslutning av kvarlämnade automatiseringsprocesser vid tidsgräns.
"""

from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path

import pytest

import metadata.docx as docx_module
from backend import office_automation, pdf_export
from backend.office_automation import BatchOutcome, batch_timeout, parse_batch_output
from metadata import MetadataError, extract_metadata, update_many_metadata
from metadata.docx import _docx_batch_update_script

EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "Examples" / "dev phase 0.2"


def _completed(stdout: str = "", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["cscript.exe"], returncode, stdout, stderr)


def test_parse_batch_output_all_ok():
    assert parse_batch_output(_completed("OK|0\r\nOK|1\r\n"), 2) == BatchOutcome(2)


def test_parse_batch_output_reports_fail_line():
    outcome = parse_batch_output(_completed("OK|0\nFAIL|1|Låst fil\n", returncode=1), 3)
    assert outcome == BatchOutcome(1, 1, "Låst fil")


def test_parse_batch_output_treats_missing_lines_as_failure():
    outcome = parse_batch_output(_completed("OK|0\n", returncode=1, stderr="Skriptfel"), 2)
    assert outcome == BatchOutcome(1, 1, "Skriptfel")


def test_batch_timeout_has_baseline_minimum():
    assert batch_timeout(1) == 180
    assert batch_timeout(4) == 360


def test_timeout_kills_only_new_automation_instances(monkeypatch):
    snapshots = iter([{10}, {10, 20, 30}])
    monkeypatch.setattr(office_automation, "_automation_pids", lambda _image: next(snapshots))
    monkeypatch.setattr(office_automation, "_is_automation_instance", lambda pid: pid == 20)
    killed: list[int] = []
    monkeypatch.setattr(office_automation, "_kill_process", killed.append)

    def time_out(_path, timeout):
        raise subprocess.TimeoutExpired("cscript.exe", timeout)

    monkeypatch.setattr(office_automation, "_run_cscript", time_out)

    with pytest.raises(office_automation.OfficeScriptTimeout, match="1 kvarlämnad"):
        office_automation.run_office_script("WScript.Quit 0", image_name="WINWORD.EXE", timeout=1)
    assert killed == [20]


def test_docx_batch_script_rejects_read_only_documents():
    script = _docx_batch_update_script([(Path("a.docx"), {"Tag": "v"}), (Path("b.docx"), {"Tag": "w"})])
    # Save på ett skrivskyddat dokument öppnar en osynlig dialog som låser Word.
    assert "document.ReadOnly" in script
    assert script.count("UpdateDocument(") == 3  # definition + två anrop i en instans
    assert script.count('CreateObject("Word.Application")') == 1


def _editable_docx_copies(tmp_path: Path, count: int) -> list[tuple[Path, dict[str, str]]]:
    copies: list[tuple[Path, dict[str, str]]] = []
    for fixture in sorted(EXAMPLES_DIR.glob("*.docx")) if EXAMPLES_DIR.is_dir() else []:
        target = tmp_path / fixture.name
        shutil.copy2(fixture, target)
        try:
            field = next((field for field in extract_metadata(target).fields if field.editable), None)
        except Exception:
            field = None
        if field is not None:
            copies.append((target, {field.key: "NYTT"}))
        if len(copies) == count:
            return copies
    pytest.skip("För få redigerbara DOCX-fixturer i Examples/")


def test_update_many_metadata_publishes_in_order_and_stops_at_first_failure(tmp_path, monkeypatch):
    jobs = _editable_docx_copies(tmp_path, 3)
    originals = {path: path.read_bytes() for path, _changes in jobs}
    for path, _changes in jobs:
        path.chmod(stat.S_IREAD)
    seen: list[tuple[Path, dict[str, str]]] = []

    def fake_batch(batch_jobs):
        seen.extend(batch_jobs)
        # Temporärkopian måste vara skrivbar, annars hänger Word på Save.
        assert all(job_path.stat().st_mode & stat.S_IWRITE for job_path, _ in batch_jobs)
        batch_jobs[0][0].write_bytes(originals[jobs[0][0]])
        return BatchOutcome(1, 1, "Låst")

    monkeypatch.setattr(docx_module, "run_docx_update_batch", fake_batch)

    with pytest.raises(MetadataError, match="Låst"):
        update_many_metadata(dict(jobs))

    assert len(seen) == 3, "alla DOCX ska gå till en och samma batch"
    assert [path.read_bytes() for path, _ in jobs] == list(originals.values())
    assert not (jobs[0][0].stat().st_mode & stat.S_IWRITE), "skrivskyddet ska återställas"
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(path.name for path, _ in jobs)


def test_update_many_metadata_validates_everything_before_writing(tmp_path, monkeypatch):
    jobs = _editable_docx_copies(tmp_path, 2)
    monkeypatch.setattr(docx_module, "run_docx_update_batch", lambda _jobs: pytest.fail("ska inte köras"))
    changes = dict(jobs)
    changes[jobs[1][0]] = {"FinnsInte": "x"}

    with pytest.raises(Exception):
        update_many_metadata(changes)
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(path.name for path, _ in jobs)


def test_plot_documents_uses_one_office_batch_per_file_type(tmp_path, monkeypatch):
    sources = [tmp_path / name for name in ("a.docx", "b.xlsx", "c.docx")]
    for source in sources:
        source.write_bytes(b"x")
    calls: list[tuple[str, list[str]]] = []

    def fake_export(suffix, jobs):
        calls.append((suffix, [source.name for source, _output in jobs]))
        for _source, output in jobs:
            output.write_bytes(pdf_export.PDF_HEADER + b" test")
        return BatchOutcome(len(jobs))

    monkeypatch.setattr(pdf_export, "_export_batch_with_office", fake_export)
    output_dir = tmp_path / "pdf"

    outputs = pdf_export.plot_documents(sources, output_dir)

    assert calls == [(".docx", ["a.docx", "c.docx"]), (".xlsx", ["b.xlsx"])]
    assert [output.name for output in outputs] == ["a.pdf", "b.pdf", "c.pdf"]
    assert sorted(path.name for path in output_dir.iterdir()) == ["a.pdf", "b.pdf", "c.pdf"]


def test_plot_documents_stops_at_first_failed_export(tmp_path, monkeypatch):
    sources = [tmp_path / name for name in ("a.docx", "b.docx", "c.docx")]
    for source in sources:
        source.write_bytes(b"x")

    def fake_export(_suffix, jobs):
        jobs[0][1].write_bytes(pdf_export.PDF_HEADER)
        return BatchOutcome(1, 1, "Skrivarfel")

    monkeypatch.setattr(pdf_export, "_export_batch_with_office", fake_export)
    output_dir = tmp_path / "pdf"

    with pytest.raises(pdf_export.PdfExportError, match="b.docx: Skrivarfel"):
        pdf_export.plot_documents(sources, output_dir)
    assert sorted(path.name for path in output_dir.iterdir()) == ["a.pdf"]
