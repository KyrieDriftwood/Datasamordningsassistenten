"""G/Y: Windows packaging, user decision 2026-10-04, outside TB."""

from __future__ import annotations

import json
import os
import runpy
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def test_self_test_loads_ui_assets_and_native_prerequisites(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication

    from dwg import pdf_plotter, rust_bridge

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(rust_bridge, "locate_rust_extractor", lambda: Path("reader.exe"))
    monkeypatch.setattr(pdf_plotter, "locate_pdf_backend", lambda: Path("plotter.exe"))
    calls = []

    def fake_run(arguments, **kwargs):
        calls.append(arguments)
        return subprocess.CompletedProcess(arguments, 2, b"", b"Missing arguments")

    monkeypatch.setattr(subprocess, "run", fake_run)
    entry = runpy.run_path(str(ROOT / "packaging" / "windows_entry.py"))
    report = tmp_path / "self-test.json"
    entry["self_test"](report)
    result = json.loads(report.read_text(encoding="utf-8"))
    assert result["window_class"] == "MainWindow"
    assert "check.svg" in result["icons"]
    assert result["dwg_reader"] == "reader.exe"
    assert calls == [["reader.exe"], ["plotter.exe"]]
    assert QApplication.instance() is app


def test_self_test_rejects_native_loader_failure(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication

    from dwg import pdf_plotter, rust_bridge

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(rust_bridge, "locate_rust_extractor", lambda: Path("reader.exe"))
    monkeypatch.setattr(pdf_plotter, "locate_pdf_backend", lambda: Path("plotter.exe"))
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 0xC0000135, b"", b""),
    )
    entry = runpy.run_path(str(ROOT / "packaging" / "windows_entry.py"))
    report = tmp_path / "self-test.json"
    with pytest.raises(RuntimeError, match="Native runtime failed to load"):
        entry["self_test"](report)
    assert not report.exists()
    assert QApplication.instance() is app


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell export")
def test_source_export_excludes_project_data_and_refuses_overwrite(tmp_path):
    destination = tmp_path / "source"
    command = [
        "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", str(ROOT / "scripts" / "Export-Source.ps1"),
        "-Destination", str(destination),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stderr
    assert (destination / "app" / "main.py").is_file()
    assert (destination / "packaging" / "windows.spec").is_file()
    assert (destination / "packaging" / "README.md").is_file()
    for name in ("Examples", "ErrorReports", ".old", ".venv", ".git", "docs", "dist"):
        assert not (destination / name).exists()
    assert not list(destination.rglob("*.odt"))
    assert not list(destination.rglob("*.exe"))
    assert not list(destination.rglob("*.dll"))
    repeat = subprocess.run(command, capture_output=True, text=True, timeout=90)
    assert repeat.returncode != 0
    assert (destination / "app" / "main.py").is_file()
