"""DWG-tabeller (namnruta och modell), bakgrundsläsning, skrivning och plottning.

Mixin till ``app.ui.main_window.MainWindow`` (utbruten 2026-10-02,
granskningspunkt "dela upp main_window.py", utanför TB). Metoderna förutsätter
widgetar och tillstånd som skapas i ``MainWindow.__init__``/``_build_ui``.
"""

from __future__ import annotations

import queue
import threading
import time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QMessageBox, QTableWidgetItem

from app.controllers import dwg_controller
from app.ui import theme
from app.ui.task_runner import run_background_task, run_dwg_task
from backend.naming import NamingError
from dwg import DwgAttributeResult, DwgError, TRVJ_ATTRIBUTE_SCHEMA
from dwg.pdf_sync import clear_pdf_sync_record, load_status_records, record_verified_pdf


class DwgPanelMixin:
    def _dwg_result_for(self, dwg_path: Path) -> DwgAttributeResult | None:
        """Hämtar cachat attributresultat för en DWG-sökväg.

        Slår upp både på sökvägen som den lagras i explorer-trädet och på
        dess ``resolve()``-form. ``dwg.rust_bridge`` nycklar nämligen sina
        resultat på upplösta sökvägar, och på nätverksenheter kan samma
        fil nås både som ``P:\\...`` och som ``\\\\server\\utdelning\\...``.
        Utan denna normalisering hittar tabellen aldrig sitt resultat och
        visar "Ej inläst" trots att inläsningen lyckats.
        """
        result = self._dwg_attribute_results.get(dwg_path)
        if result is not None:
            return result
        try:
            resolved = dwg_path.resolve()
        except OSError:
            return None
        if resolved != dwg_path:
            return self._dwg_attribute_results.get(resolved)
        return None

    def _dwg_is_loading(self, dwg_path: Path) -> bool:
        """Som ``_dwg_result_for``, men för pågående inläsningar."""
        if dwg_path in self._dwg_attribute_loading:
            return True
        try:
            return dwg_path.resolve() in self._dwg_attribute_loading
        except OSError:
            return False

    def _model_result_for(self, dwg_path: Path) -> DwgAttributeResult | None:
        result = self._model_attribute_results.get(dwg_path)
        if result is not None:
            return result
        try:
            resolved = dwg_path.resolve()
        except OSError:
            return None
        return self._model_attribute_results.get(resolved)

    def _refresh_dwg_table(self) -> None:
        selected_paths = tuple(self._checked_dwg_paths())
        results = {path: self._dwg_result_for(path) for path in selected_paths}
        dwg_paths = tuple(
            path
            for path in selected_paths
            if results[path] is not None and results[path].error is None
        )

        # Läs av pågående redigeringar INNAN nyckeltabellerna nollställs,
        # annars tappas allt användaren skrivit så fort en bakgrunds-
        # inläsning blir klar och tabellen byggs om.
        pending_name_edits: dict[Path, str] = {}
        for row_index, path in self._dwg_row_paths.items():
            item = self.dwg_table.item(row_index, 0)
            if item is not None and item.text() != self._dwg_original_names.get(row_index, path.name):
                pending_name_edits[path] = item.text()

        pending_attribute_edits: dict[tuple[Path, str], str] = {}
        for (row_index, column_index), key in self._dwg_attribute_cell_keys.items():
            item = self.dwg_table.item(row_index, column_index)
            if item is not None:
                pending_attribute_edits[key] = item.text()

        self._dwg_row_paths.clear()
        self._dwg_original_names.clear()
        self._dwg_attribute_cell_keys.clear()
        self._dwg_attribute_original_values.clear()

        self.dwg_table.clear()

        self.dwg_table.setRowCount(len(dwg_paths))
        self.dwg_table.setColumnCount(1 + len(TRVJ_ATTRIBUTE_SCHEMA))
        self.dwg_table.setHorizontalHeaderLabels(["Filnamn"] + [label for _tag, label in TRVJ_ATTRIBUTE_SCHEMA])

        for row_index, dwg_path in enumerate(dwg_paths):
            name_item = QTableWidgetItem(dwg_path.name)
            name_item.setFlags(name_item.flags() | Qt.ItemIsEditable)
            name_item.setToolTip(str(dwg_path))
            if dwg_path in pending_name_edits:
                name_item.setText(pending_name_edits[dwg_path])
            self.dwg_table.setItem(row_index, 0, name_item)

            attribute_result = results[dwg_path]
            fields_by_tag = {field.key: field for field in attribute_result.fields}
            for offset, (tag, label) in enumerate(TRVJ_ATTRIBUTE_SCHEMA):
                column_index = 1 + offset
                field = fields_by_tag.get(tag)
                if field is not None:
                    attribute_item = QTableWidgetItem(field.value)
                    attribute_item.setToolTip(f"{dwg_path.name}\n{label}")
                    if field.editable:
                        attribute_item.setFlags(attribute_item.flags() | Qt.ItemIsEditable)
                        key = (dwg_path, tag)
                        self._dwg_attribute_cell_keys[(row_index, column_index)] = key
                        self._dwg_attribute_original_values[key] = field.value
                        override_value = pending_attribute_edits.get(key)
                        if override_value is not None:
                            attribute_item.setText(override_value)
                    else:
                        attribute_item.setFlags(attribute_item.flags() & ~Qt.ItemIsEditable)
                        attribute_item.setBackground(QColor(theme.STATUS_READONLY))
                        attribute_item.setToolTip(f"{dwg_path.name}\n{label}\nKonstant attribut (ej redigerbart).")
                else:
                    attribute_item = QTableWidgetItem("")
                    attribute_item.setFlags(attribute_item.flags() & ~Qt.ItemIsEditable)
                    attribute_item.setBackground(QColor(theme.STATUS_READONLY))
                    attribute_item.setToolTip(f"{dwg_path.name}\n{label}\nAttributet hittades inte i ritningen.")
                self.dwg_table.setItem(row_index, column_index, attribute_item)

            self._dwg_row_paths[row_index] = dwg_path
            self._dwg_original_names[row_index] = dwg_path.name

        missing_count = sum(result is not None and result.error is not None for result in results.values())
        pending_count = sum(result is None for result in results.values())
        status_parts = [
            f"{len(dwg_paths)}/{len(selected_paths)} markerade DWG-filer har TRVJ_NAMNRUTA."
        ]
        if pending_count:
            status_parts.append(f"Läser {pending_count} ritning(ar).")
        if missing_count:
            status_parts.append(f"{missing_count} saknar läsbart TRVJ_NAMNRUTA-block.")
        if self._last_dwg_load_summary is not None:
            status_parts.append(self._last_dwg_load_summary)
            self._last_dwg_load_summary = None
        self.dwg_status.setText(" ".join(status_parts))

        self._refresh_model_table(selected_paths)
        self._start_lazy_dwg_attribute_load(selected_paths)

    def _refresh_model_table(self, dwg_paths: tuple[Path, ...]) -> None:
        """Visar modellnamnrutans dynamiska attribut i en separat tabell."""
        pending_edits: dict[tuple[Path, str], str] = {}
        for (row, column), key in self._model_attribute_cell_keys.items():
            item = self.model_table.item(row, column)
            if item is not None:
                pending_edits[key] = item.text()
        self._model_attribute_cell_keys.clear()
        self._model_attribute_original_values.clear()

        results: dict[Path, DwgAttributeResult | None] = {}
        ordered_tags: list[str] = []
        for path in dwg_paths:
            result = self._model_result_for(path)
            results[path] = result
            if result is not None and result.error is None:
                for field in result.fields:
                    if field.key not in ordered_tags:
                        ordered_tags.append(field.key)
        model_paths = tuple(
            path
            for path in dwg_paths
            if results[path] is not None and results[path].error is None
        )

        self.model_table.clear()
        self.model_table.setRowCount(len(model_paths))
        self.model_table.setColumnCount(1 + len(ordered_tags))
        self.model_table.setHorizontalHeaderLabels(["Filnamn", *ordered_tags])
        for row, path in enumerate(model_paths):
            filename = QTableWidgetItem(path.name)
            filename.setFlags(filename.flags() & ~Qt.ItemIsEditable)
            filename.setToolTip(str(path))
            self.model_table.setItem(row, 0, filename)
            result = results[path]
            values = {field.key: field.value for field in result.fields}
            for column, tag in enumerate(ordered_tags, start=1):
                field = next((field for field in result.fields if field.key == tag), None)
                item = QTableWidgetItem(values.get(tag, ""))
                if field is not None and field.editable:
                    item.setFlags(item.flags() | Qt.ItemIsEditable)
                    key = (path, tag)
                    self._model_attribute_cell_keys[(row, column)] = key
                    self._model_attribute_original_values[key] = field.value
                    if key in pending_edits:
                        item.setText(pending_edits[key])
                else:
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                    item.setBackground(QColor(theme.STATUS_READONLY))
                self.model_table.setItem(row, column, item)

        self.update_model_metadata_button.setEnabled(bool(model_paths))
        if not dwg_paths:
            self.model_status.setText("Modellnamnruta: inga markerade DWG-filer.")
        else:
            pending_count = sum(results[path] is None for path in dwg_paths)
            error_count = sum(results[path] is not None and results[path].error is not None for path in dwg_paths)
            summary = (
                f"{len(model_paths)}/{len(dwg_paths)} markerade DWG-filer har "
                "TRVJ_NAMNRUTA_MODELL."
            )
            if pending_count:
                summary += f" Läser {pending_count} ritning(ar)."
            if error_count:
                summary += f" {error_count} saknar läsbart modellblock."
            self.model_status.setText(summary)

    def _open_pdf_for_dwg_row(self, row: int, _column: int) -> None:
        drawing = self._dwg_row_paths.get(row)
        if drawing is not None:
            self._show_pdf_preview(drawing.with_suffix(".pdf"))

    def plot_selected_dwgs(self) -> None:
        self._plot_dwgs(self._checked_dwg_paths())

    def _plot_dwgs(self, paths: list[Path]) -> None:
        if not paths:
            QMessageBox.information(self, "Inga DWG-filer", "Markera först minst en DWG-fil.")
            return
        project_root = self._project_root()
        try:
            results = self._run_dwg_task(
                "Plottar DWG-filer till flersidiga PDF:er…",
                len(paths),
                lambda progress_callback: dwg_controller.plot_dwgs_to_pdfs(
                    paths,
                    standard_metadata={
                        path.resolve(): result
                        for path in paths
                        if (result := self._dwg_result_for(path)) is not None
                    },
                    model_metadata={
                        path.resolve(): result
                        for path in paths
                        if (result := self._model_result_for(path)) is not None
                    },
                    progress_callback=progress_callback,
                ),
            )
        except DwgError as exc:
            QMessageBox.critical(self, "Kunde inte plotta DWG-filer", str(exc))
            return

        errors: list[str] = []
        warnings: list[str] = []
        created_count = 0
        verified_count = 0
        for drawing, result in results.items():
            if result.error is not None:
                errors.append(f"{drawing.name}: {result.error}")
                continue
            created_count += 1
            if result.warning is not None:
                warnings.append(f"{drawing.name}: {result.warning}")
                try:
                    clear_pdf_sync_record(project_root, drawing)
                except (OSError, ValueError) as exc:
                    errors.append(f"{drawing.name}: PDF skapades, men synkstatus kunde inte rensas: {exc}")
                continue
            try:
                record_verified_pdf(project_root, drawing, result.output)
                verified_count += 1
            except (OSError, ValueError) as exc:
                errors.append(f"{drawing.name}: PDF skapades, men synkstatus kunde inte sparas: {exc}")

        try:
            self._pdf_status_records = load_status_records(project_root)
        except ValueError as exc:
            errors.append(f"PDF-synkstatus kunde inte läsas efter plotten: {exc}")
        self._refresh_pdf_status_items()
        self._reload_pdf_table()

        if errors:
            QMessageBox.warning(
                self,
                "DWG-plotten slutfördes med fel",
                f"{created_count}/{len(paths)} PDF-filer skapades; "
                f"{verified_count} verifierades och markerades synkade.\n\n"
                + "\n".join(errors + warnings),
            )
        elif warnings:
            QMessageBox.warning(
                self,
                "PDF skapades men metadata avviker",
                f"{created_count} PDF-fil(er) skapades. Ingen PDF med avvikelser markerades som synkad.\n\n"
                + "\n".join(warnings),
            )
        else:
            QMessageBox.information(
                self,
                "DWG-plotten klar",
                f"{created_count} flersidig(a) PDF-filer skapades och verifierades mot DWG-metadata.",
            )

    def _start_lazy_dwg_attribute_load(self, dwg_paths: tuple[Path, ...]) -> None:
        """Läser namnrute-attribut i bakgrunden för markerade DWG-filer som
        varken redan finns i cachen eller redan håller på att läsas in.

        Direkt port av ``app_v5_desktop.py:_start_lazy_dwg_attribute_load``.
        """
        pending = tuple(
            path
            for path in dwg_paths
            if (
                (self._dwg_result_for(path) is None and not self._dwg_is_loading(path))
                or (self._model_result_for(path) is None and path not in self._model_attribute_loading)
            )
        )
        if not pending:
            return

        standard_pending = tuple(path for path in pending if self._dwg_result_for(path) is None)
        model_pending = tuple(path for path in pending if self._model_result_for(path) is None)
        self._dwg_attribute_loading.update(standard_pending)
        self._model_attribute_loading.update(model_pending)

        events: queue.Queue[tuple[str, object, object]] = queue.Queue()
        start_time = time.perf_counter()

        def worker_target() -> None:
            try:
                standard_results = {}
                model_results = {}
                if standard_pending:
                    standard_results = dwg_controller.extract_attributes_fast(
                        standard_pending,
                        progress_callback=lambda done, total: events.put(("progress", done, total)),
                    )
                if model_pending:
                    model_results = dwg_controller.extract_model_attributes_fast(
                        model_pending,
                        progress_callback=lambda done, total: events.put(
                            ("model_progress", len(standard_pending) + done, len(standard_pending) + total)
                        ),
                    )
            except Exception as exc:  # noqa: BLE001 - visas bara i DWG-statusraden, avbryter inte gränssnittet
                events.put(("error", exc, None))
            else:
                events.put(("result", standard_results, None))
                events.put(("model_result", model_results, None))

        thread = threading.Thread(target=worker_target, daemon=True)

        timer = QTimer(self)
        timer.setInterval(80)

        def poll() -> None:
            while True:
                try:
                    kind, first, second = events.get_nowait()
                except queue.Empty:
                    break
                elapsed = time.perf_counter() - start_time
                if kind == "progress":
                    done, total = first, second
                    percent = round(done / total * 100) if total else 100
                    self.dwg_status.setText(
                        f"Läser DWG-attribut i bakgrunden (Rust/LibreDWG, parallellt): "
                        f"{done}/{total} filer klara ({percent} %) – {elapsed:.1f} s…"
                    )
                elif kind == "model_progress":
                    done, total = first, second
                    percent = round(done / total * 100) if total else 100
                    self.model_status.setText(
                        f"Läser modellnamnruta (Rust/LibreDWG, parallellt): "
                        f"{done}/{total} steg klara ({percent} %) – {elapsed:.1f} s…"
                    )
                elif kind == "error":
                    timer.stop()
                    self._dwg_attribute_loading.difference_update(standard_pending)
                    self._model_attribute_loading.difference_update(model_pending)
                    error_message = str(first)
                    for path in standard_pending:
                        self._dwg_attribute_results[path] = DwgAttributeResult(path, (), error_message)
                    for path in model_pending:
                        self._model_attribute_results[path] = DwgAttributeResult(path, (), error_message)
                    self.dwg_status.setText(
                        f"Kunde inte läsa DWG-attribut automatiskt (efter {elapsed:.1f} s): {first}"
                    )
                    self.model_status.setText(f"Kunde inte läsa modellnamnruta: {first}")
                    self._refresh_dwg_table()
                elif kind == "result":
                    timer.stop()
                    self._dwg_attribute_loading.difference_update(standard_pending)
                    self._dwg_attribute_results.update(first)
                    self._last_dwg_load_summary = f"{len(standard_pending)} namnruta(r) inlästa på {elapsed:.1f} s."
                    self._refresh_dwg_table()
                elif kind == "model_result":
                    self._model_attribute_loading.difference_update(model_pending)
                    self._model_attribute_results.update(first)
                    self._refresh_model_table(tuple(self._checked_dwg_paths()))

        timer.timeout.connect(poll)
        timer.start()
        thread.start()

    def _collect_dwg_renames(self) -> dict[Path, str]:
        renames: dict[Path, str] = {}
        for row_index, path in self._dwg_row_paths.items():
            item = self.dwg_table.item(row_index, 0)
            if item is None:
                continue
            new_name = item.text()
            if new_name != self._dwg_original_names.get(row_index, ""):
                renames[path] = new_name
        return renames

    def _collect_dwg_attribute_changes(self) -> dict[Path, dict[str, str]]:
        changes_by_path: dict[Path, dict[str, str]] = {}
        for (row_index, column_index), (path, tag) in self._dwg_attribute_cell_keys.items():
            item = self.dwg_table.item(row_index, column_index)
            if item is None:
                continue
            new_value = item.text()
            if new_value != self._dwg_attribute_original_values.get((path, tag), ""):
                changes_by_path.setdefault(path, {})[tag] = new_value
        return changes_by_path

    def _collect_dwg_model_attribute_changes(self) -> dict[Path, dict[str, str]]:
        changes_by_path: dict[Path, dict[str, str]] = {}
        for (row, column), (path, tag) in self._model_attribute_cell_keys.items():
            item = self.model_table.item(row, column)
            if item is None:
                continue
            value = item.text()
            if value != self._model_attribute_original_values.get((path, tag), ""):
                changes_by_path.setdefault(path, {})[tag] = value
        return changes_by_path

    def _run_dwg_task(self, title: str, total_files: int, work):
        return run_dwg_task(self, title, total_files, work)

    def _run_background_task(self, title: str, work):
        return run_background_task(self, title, work)

    def _apply_dwg_attribute_changes(
        self, *, include_standard: bool = True, include_model: bool = True
    ) -> tuple[int, list[str]]:
        """Sparar ändrade attribut i båda DWG-namnrutorna och returnerar (antal filer med
        lyckad uppdatering, lista med felmeddelanden per misslyckad fil).

        Direkt port av ``app_v5_desktop.py:_apply_dwg_attribute_changes``,
        men går via ``dwg_controller.write_attribute_changes`` (backup +
        read-back-verifiering, se Fas 3) i stället för att skriva direkt.
        """
        standard_changes = self._collect_dwg_attribute_changes() if include_standard else {}
        model_changes = self._collect_dwg_model_attribute_changes() if include_model else {}
        if not standard_changes and not model_changes:
            return 0, []

        project_root = self._project_root()
        updated_count = 0
        errors: list[str] = []
        for changes_by_path, is_model in ((standard_changes, False), (model_changes, True)):
            if not changes_by_path:
                continue
            block_name = "TRVJ_NAMNRUTA_MODELL" if is_model else "TRVJ_NAMNRUTA"
            writer = (
                dwg_controller.write_model_attribute_changes
                if is_model
                else dwg_controller.write_attribute_changes
            )
            results = self._run_dwg_task(
                f"Sparar attribut ({block_name}) till DWG-filer…",
                len(changes_by_path),
                lambda progress_callback, changes_by_path=changes_by_path, writer=writer: writer(
                    changes_by_path, project_root=project_root, progress_callback=progress_callback
                ),
            )
            updated_count += sum(1 for result in results.values() if result.error is None)
            errors.extend(
                f"{path.name} ({block_name}): {result.error}"
                for path, result in results.items()
                if result.error is not None
            )

            cache = self._model_attribute_results if is_model else self._dwg_attribute_results
            for path, new_values in changes_by_path.items():
                result = results.get(path)
                if result is None or result.error is not None:
                    continue
                cached = self._model_result_for(path) if is_model else self._dwg_result_for(path)
                if cached is None:
                    continue
                updated_fields = tuple(
                    field.__class__(field.key, field.label, new_values[field.key], field.editable, field.location)
                    if field.key in new_values
                    else field
                    for field in cached.fields
                )
                cache[cached.path] = cached.__class__(path=cached.path, fields=updated_fields, error=None)

        self._refresh_pdf_status_items()
        self._refresh_model_table(tuple(self._checked_dwg_paths()))
        return updated_count, errors

    def update_dwg_model_metadata(self) -> None:
        changes = self._collect_dwg_model_attribute_changes()
        if not changes:
            QMessageBox.information(
                self,
                "Inga ändringar",
                "Det finns inga ändrade DWG-modellattribut att spara.",
            )
            return
        try:
            updated_count, errors = self._apply_dwg_attribute_changes(
                include_standard=False,
                include_model=True,
            )
        except DwgError as exc:
            QMessageBox.critical(self, "Kunde inte uppdatera DWG-modellmetadata", str(exc))
            return
        if errors:
            QMessageBox.warning(
                self,
                "DWG-modellmetadata kunde inte uppdateras",
                f"{updated_count} DWG-fil(er) uppdaterades.\n\n" + "\n".join(errors),
            )
            return
        QMessageBox.information(
            self,
            "DWG-modellmetadata uppdaterad",
            f"{updated_count} DWG-fil(er) fick modellmetadata uppdaterad.",
        )

    def update_and_plot_dwgs(self) -> None:
        selected_paths = self._checked_dwg_paths()
        if not selected_paths:
            QMessageBox.information(self, "Inga DWG-filer", "Markera först minst en DWG-fil.")
            return

        if self._collect_dwg_model_attribute_changes():
            QMessageBox.information(
                self,
                "DWG-modellmetadata väntar",
                "Uppdatera först modellmetadata med knappen under DWG-modellnamnrutan.",
            )
            return

        has_changes = bool(
            self._collect_dwg_renames()
            or self._collect_dwg_attribute_changes()
        )
        renamed: dict[Path, Path] = {}
        if has_changes:
            result = self.write_dwg_changes()
            if result is None:
                return
            renamed = result

        plot_paths = [renamed.get(path.resolve(), path.resolve()) for path in selected_paths]
        self._plot_dwgs(plot_paths)

    def write_dwg_changes(self) -> dict[Path, Path] | None:
        """Skriver ändrade namnrute-attribut och filnamnsbyten till DWG-filer.

        Direkt port av ``app_v5_desktop.py:write_dwg_changes``.
        """
        dwg_renames = self._collect_dwg_renames()
        dwg_attribute_changes = self._collect_dwg_attribute_changes()

        if not dwg_renames and not dwg_attribute_changes:
            QMessageBox.information(
                self,
                "Inga DWG-ändringar",
                "Det finns inga ändrade DWG-filnamn eller namnrute-attribut att skriva.",
            )
            return {}

        try:
            dwg_attribute_updated_count, dwg_attribute_errors = self._apply_dwg_attribute_changes(
                include_model=False
            )
            renamed = dwg_controller.rename_files(dwg_renames) if dwg_renames else {}
        except (DwgError, NamingError) as exc:
            QMessageBox.critical(self, "Kunde inte skriva DWG-ändringar", str(exc))
            return None

        if dwg_attribute_errors:
            QMessageBox.warning(
                self,
                "Vissa DWG-attribut kunde inte sparas",
                "Följande DWG-filer fick inte alla attribut uppdaterade:\n\n" + "\n".join(dwg_attribute_errors),
            )

        if renamed:
            self.refresh_folder()
        else:
            self._refresh_dwg_table()

        QMessageBox.information(
            self,
            "DWG-ändringar sparade",
            f"{len(renamed)} DWG-filer döptes om.\n"
            f"{dwg_attribute_updated_count} DWG-filer fick namnrute-attribut uppdaterade.",
        )
        return None if dwg_attribute_errors else renamed
