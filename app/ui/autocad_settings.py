"""CBF/CBG/G: selectable Core Console (user decision 2026-10-04, outside TB).

The shared environment override reaches both Python writers and the Rust PDF
backend. Never retry a failed write with a different AutoCAD installation:
backup/read-back recovery must finish before any new attempt.
"""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

ENVIRONMENT_KEY = "DATASAMORDNING_ACCORECONSOLE"
SETTINGS_KEY = "autocad/accoreconsole_path"


def validate_core_path(path: str) -> Path:
    executable = Path(path).expanduser().resolve()
    if executable.name.casefold() != "accoreconsole.exe" or not executable.is_file():
        raise ValueError("Välj en befintlig accoreconsole.exe från din AutoCAD-installation.")
    return executable


class AutocadSettingsMixin:
    def _restore_autocad_settings(self) -> None:
        # An externally supplied environment variable remains authoritative.
        self._initial_core_override = os.environ.get(ENVIRONMENT_KEY)
        saved = self._settings.value(SETTINGS_KEY, "", type=str).strip()
        if saved and not self._initial_core_override:
            os.environ[ENVIRONMENT_KEY] = saved
        self._update_autocad_tooltip()

    def _update_autocad_tooltip(self) -> None:
        selected = os.environ.get(ENVIRONMENT_KEY)
        if selected:
            available = Path(selected).is_file()
            self.autocad_button.setToolTip(
                f"AutoCAD Core Console: {selected}"
                + ("" if available else "\nSökvägen saknas. Välj en annan installation via AutoCAD…")
            )
        else:
            self.autocad_button.setToolTip(
                "Automatisk sökning efter AutoCAD Core Console. "
                "Välj en annan version eller installationssökväg via AutoCAD…"
            )

    def _save_autocad_path(self, path: str) -> bool:
        if self._dwg_attribute_loading or self._model_attribute_loading:
            QMessageBox.warning(
                self, "DWG läses fortfarande", "Vänta tills DWG-inläsningen är klar innan du byter AutoCAD."
            )
            return False
        try:
            selected = str(validate_core_path(path)) if path.strip() else ""
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "Ogiltig AutoCAD-sökväg", str(exc))
            return False
        previous = self._settings.value(SETTINGS_KEY, "", type=str)
        self._settings.setValue(SETTINGS_KEY, selected)
        self._settings.sync()
        if self._settings.status() != QSettings.Status.NoError:
            self._settings.setValue(SETTINGS_KEY, previous)
            QMessageBox.warning(self, "Inställningar kunde inte sparas", "AutoCAD-sökvägen kunde inte sparas.")
            return False
        if selected:
            os.environ[ENVIRONMENT_KEY] = selected
        elif self._initial_core_override:
            os.environ[ENVIRONMENT_KEY] = self._initial_core_override
        else:
            os.environ.pop(ENVIRONMENT_KEY, None)
        self._update_autocad_tooltip()
        return True

    def choose_autocad_installation(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("AutoCAD Core Console")
        layout = QVBoxLayout(dialog)
        description = QLabel(
            "Välj accoreconsole.exe om AutoCAD finns på en annan sökväg eller om du vill\n"
            "använda en annan installerad version. Valet gäller DWG-skrivning och PDF-plottning.\n"
            "Tom sökväg återställer standardvalet (miljövariabel eller automatisk sökning).\n"
            "Byt inte installation under pågående DWG-bearbetning."
        )
        layout.addWidget(description)
        path_input = QLineEdit(os.environ.get(ENVIRONMENT_KEY, ""))
        path_input.setPlaceholderText("Automatisk sökning")
        layout.addWidget(path_input)
        browse = QPushButton("Välj accoreconsole.exe…")

        def select_file() -> None:
            selected, _filter = QFileDialog.getOpenFileName(
                dialog, "Välj AutoCAD Core Console", path_input.text(),
                "AutoCAD Core Console (accoreconsole.exe)",
            )
            if selected:
                path_input.setText(selected)

        browse.clicked.connect(select_file)
        layout.addWidget(browse)
        reset = QPushButton("Återställ standardval")
        reset.clicked.connect(path_input.clear)
        layout.addWidget(reset)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(lambda: dialog.accept() if self._save_autocad_path(path_input.text()) else None)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.resize(640, dialog.sizeHint().height())
        dialog.exec()
