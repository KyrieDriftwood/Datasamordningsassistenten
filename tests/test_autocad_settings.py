"""CBF/CBG/G: Core Console fallback and persistent installation choice."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton, QWidget

import dwg
from app.ui.autocad_settings import (
    ENVIRONMENT_KEY,
    SETTINGS_KEY,
    AutocadSettingsMixin,
    validate_core_path,
)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def settings_window(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setenv(ENVIRONMENT_KEY, "")

    class SettingsWindow(AutocadSettingsMixin, QWidget):
        def __init__(self):
            super().__init__()
            self._settings = QSettings(str(tmp_path / "settings.ini"), QSettings.IniFormat)
            self.autocad_button = QPushButton(self)
            self._dwg_attribute_loading = set()
            self._model_attribute_loading = set()
            self._restore_autocad_settings()

    window = SettingsWindow()
    yield window
    window.close()
    app.processEvents()


def test_custom_core_path_is_saved_and_used_by_shared_override(settings_window, tmp_path):
    core = tmp_path / "Annan installation" / "AutoCAD 2025" / "accoreconsole.exe"
    core.parent.mkdir(parents=True)
    core.touch()
    assert settings_window._save_autocad_path(str(core))
    assert dwg.locate_accoreconsole() == core.resolve()
    assert os.environ[ENVIRONMENT_KEY] == str(core.resolve())
    assert settings_window._settings.value(SETTINGS_KEY) == str(core.resolve())
    assert str(core.resolve()) in settings_window.autocad_button.toolTip()
    # The Rust subprocess inherits this same environment variable.
    assert os.environ.copy()[ENVIRONMENT_KEY] == str(core.resolve())


def test_saved_core_path_is_restored_on_start(settings_window, tmp_path, monkeypatch):
    core = tmp_path / "accoreconsole.exe"
    core.touch()
    settings_window._settings.setValue(SETTINGS_KEY, str(core))
    monkeypatch.setenv(ENVIRONMENT_KEY, "")
    settings_window._restore_autocad_settings()
    assert dwg.locate_accoreconsole() == core.resolve()


def test_external_override_wins_and_is_restored_by_reset(settings_window, tmp_path, monkeypatch):
    external = tmp_path / "external" / "accoreconsole.exe"
    manual = tmp_path / "manual" / "accoreconsole.exe"
    for path in (external, manual):
        path.parent.mkdir()
        path.touch()
    settings_window._settings.setValue(SETTINGS_KEY, str(manual))
    monkeypatch.setenv(ENVIRONMENT_KEY, str(external))
    settings_window._restore_autocad_settings()
    assert dwg.locate_accoreconsole() == external
    assert settings_window._save_autocad_path(str(manual))
    assert settings_window._save_autocad_path("")
    assert dwg.locate_accoreconsole() == external


def test_reset_without_external_override_returns_to_auto_detection(settings_window, tmp_path):
    core = tmp_path / "accoreconsole.exe"
    core.touch()
    assert settings_window._save_autocad_path(str(core))
    assert settings_window._save_autocad_path("")
    assert ENVIRONMENT_KEY not in os.environ
    assert settings_window._settings.value(SETTINGS_KEY) == ""


@pytest.mark.parametrize("name", ["acad.exe", "saknas\\accoreconsole.exe"])
def test_invalid_choice_keeps_previous_configuration(settings_window, tmp_path, monkeypatch, name):
    messages = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: messages.append(args[-1]))
    wrong = tmp_path / name
    if wrong.name == "acad.exe":
        wrong.touch()
    assert not settings_window._save_autocad_path(str(wrong))
    assert not os.environ.get(ENVIRONMENT_KEY)
    assert settings_window._settings.value(SETTINGS_KEY, "") == ""
    assert messages


def test_validation_normalizes_and_accepts_existing_core(tmp_path):
    executable = tmp_path / "ACCORECONSOLE.EXE"
    executable.touch()
    assert validate_core_path(str(executable)) == executable.resolve()


def test_auto_detection_respects_program_files_and_prefers_newer_autocad(tmp_path, monkeypatch):
    monkeypatch.setenv(ENVIRONMENT_KEY, "")
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    for product in ("AutoCAD 2023", "AutoCAD 2026", "DWG TrueView 2027"):
        directory = tmp_path / "Autodesk" / product
        directory.mkdir(parents=True)
        (directory / "accoreconsole.exe").touch()
    assert dwg.locate_accoreconsole() == tmp_path / "Autodesk" / "AutoCAD 2026" / "accoreconsole.exe"


def test_no_installation_reports_an_explicit_error(tmp_path, monkeypatch):
    monkeypatch.setenv(ENVIRONMENT_KEY, "")
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    with pytest.raises(dwg.AccoreconsoleNotFoundError, match="Autodesk"):
        dwg.locate_accoreconsole()


def test_cannot_change_installation_during_dwg_loading(settings_window, tmp_path, monkeypatch):
    core = tmp_path / "accoreconsole.exe"
    core.touch()
    settings_window._model_attribute_loading.add(tmp_path / "model.dwg")
    messages = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: messages.append(args[-1]))
    assert not settings_window._save_autocad_path(str(core))
    assert not os.environ.get(ENVIRONMENT_KEY)
    assert messages
