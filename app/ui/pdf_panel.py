"""PDF-förhandsvisning och PDF-panelen med valbar vattenstämpel (krav PDF-1, utanför TB).

Mixin till ``app.ui.main_window.MainWindow`` (utbruten 2026-10-02,
granskningspunkt "dela upp main_window.py", utanför TB). Metoderna förutsätter
widgetar och tillstånd som skapas i ``MainWindow.__init__``/``_build_ui``.
"""

from __future__ import annotations

import queue
import threading
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PySide6.QtPdf import QPdfDocument
from PySide6.QtWidgets import QMessageBox, QTableWidgetItem

from app.controllers import pdf_controller
from dwg.pdf_sync import load_status_records


class PdfPanelMixin:
    def _show_pdf_preview(self, pdf_path: Path) -> None:
        self.pdf_document.close()
        if self._pdf_preview_buffer is not None:
            self._pdf_preview_buffer.close()
            self._pdf_preview_buffer = None
        self._pdf_preview_path = pdf_path
        self.pdf_preview_title.setText(pdf_path.name)
        self.pdf_preview_panel.show()
        if not pdf_path.is_file():
            self.pdf_preview_status.setText(f"PDF saknas: {pdf_path}")
            self._resize_splitter_for_pdf_preview()
            return

        try:
            pdf_data = pdf_path.read_bytes()
        except OSError as exc:
            self.pdf_preview_status.setText(f"PDF kunde inte läsas: {exc}")
            self._resize_splitter_for_pdf_preview()
            return

        buffer = QBuffer(self)
        buffer.setData(QByteArray(pdf_data))
        if not buffer.open(QIODevice.OpenModeFlag.ReadOnly):
            self.pdf_preview_status.setText(f"PDF kunde inte läsas: {buffer.errorString()}")
            buffer.deleteLater()
            self._resize_splitter_for_pdf_preview()
            return

        self._pdf_preview_buffer = buffer
        self.pdf_preview_status.setText("Läser PDF…")
        self.pdf_document.load(buffer)
        self.pdf_view.setDocument(self.pdf_document)
        self.pdf_view.fit_to_view()
        self._resize_splitter_for_pdf_preview()

    def _resize_splitter_for_pdf_preview(self) -> None:
        sizes = self.main_splitter.sizes()
        if len(sizes) != 3 or sizes[2] > 0:
            return
        available_width = self.main_splitter.width()
        preview_width = min(440, max(300, available_width // 4))
        tree_width = sizes[0] or available_width // 4
        metadata_width = available_width - tree_width - preview_width
        if metadata_width < 360:
            tree_width = max(220, available_width - preview_width - 360)
            metadata_width = available_width - tree_width - preview_width
        self.main_splitter.setSizes([tree_width, metadata_width, preview_width])

    def _on_pdf_document_status_changed(self) -> None:
        if self._pdf_preview_path is None:
            return
        status = self.pdf_document.status()
        if status == QPdfDocument.Status.Ready:
            self.pdf_preview_status.setText(f"{self.pdf_document.pageCount()} sida(or)")
        elif status == QPdfDocument.Status.Error:
            self.pdf_preview_status.setText(
                f"PDF kunde inte öppnas ({self.pdf_document.error().name}): {self._pdf_preview_path}"
            )
        else:
            self.pdf_preview_status.setText("Läser PDF…")

    def _close_pdf_preview(self) -> None:
        self._pdf_preview_path = None
        self.pdf_document.close()
        if self._pdf_preview_buffer is not None:
            self._pdf_preview_buffer.close()
            self._pdf_preview_buffer = None
        self.pdf_preview_panel.hide()
        sizes = self.main_splitter.sizes()
        if len(sizes) == 3:
            self.main_splitter.setSizes([sizes[0], sizes[1] + sizes[2], 0])

    def _reload_pdf_table(self) -> None:
        """Startar bakgrundsläsning av alla PDF:er och deras stämpelstatus.

        Thread + Queue + QTimer enligt arkitekturregeln (inte QThread).
        """
        root = self._pdf_status_root
        self._pdf_load_generation += 1
        if root is None:
            self._pdf_load_pending = None
            return
        generation = self._pdf_load_generation
        results: queue.Queue = queue.Queue()

        def worker() -> None:
            try:
                results.put(("result", pdf_controller.read_watermark_states(pdf_controller.list_pdfs(root))))
            except Exception as exc:  # noqa: BLE001 - visas i panelens statusrad
                results.put(("error", exc))

        self._pdf_load_pending = (generation, results)
        self.update_pdf_watermark_button.setEnabled(False)
        self.pdf_status.setText("Läser PDF-filer och vattenstämpelstatus…")
        threading.Thread(target=worker, name="pdf-watermark-load", daemon=True).start()
        self._pdf_load_timer.start()

    def _poll_pdf_table_load(self) -> None:
        pending = self._pdf_load_pending
        if pending is None:
            self._pdf_load_timer.stop()
            return
        generation, results = pending
        try:
            kind, value = results.get_nowait()
        except queue.Empty:
            return
        self._pdf_load_pending = None
        self._pdf_load_timer.stop()
        if generation != self._pdf_load_generation:
            return
        if kind == "error":
            self._populate_pdf_table([])
            self.pdf_status.setText(f"PDF-filer kunde inte listas: {value}")
            return
        self._populate_pdf_table(value)

    def _populate_pdf_table(self, entries: list[pdf_controller.PdfEntry]) -> None:
        root = self._pdf_status_root
        self.pdf_table.blockSignals(True)
        self._pdf_row_paths.clear()
        self._pdf_original_watermarks.clear()
        self._pdf_touched_rows.clear()
        self.pdf_table.clear()
        self.pdf_table.setColumnCount(3)
        self.pdf_table.setHorizontalHeaderLabels(["Dokumentnamn", "Vattenstämpel", "Mapp"])
        self.pdf_table.setRowCount(len(entries))
        unreadable = 0
        for row, entry in enumerate(entries):
            self._pdf_row_paths[row] = entry.path
            name_item = QTableWidgetItem(entry.path.name)
            name_item.setData(Qt.UserRole, str(entry.path))
            name_item.setFlags(name_item.flags() & ~Qt.ItemIsEditable)
            name_item.setToolTip(str(entry.path))
            self.pdf_table.setItem(row, 0, name_item)

            check_item = QTableWidgetItem()
            check_item.setFlags((check_item.flags() | Qt.ItemIsUserCheckable) & ~Qt.ItemIsEditable)
            if entry.has_watermark is None:
                unreadable += 1
                check_item.setFlags(check_item.flags() & ~Qt.ItemIsEnabled & ~Qt.ItemIsUserCheckable)
                check_item.setText("Kan inte läsas")
                check_item.setToolTip(entry.error or "")
            else:
                # Kryssrutans text visar stämpeln som finns i filen just nu.
                self._pdf_original_watermarks[entry.path] = entry.watermark_text
                check_item.setCheckState(Qt.Checked if entry.has_watermark else Qt.Unchecked)
                check_item.setText(entry.watermark_text or "")
            self.pdf_table.setItem(row, 1, check_item)

            try:
                folder = entry.path.parent.relative_to(root).as_posix() if root is not None else ""
            except ValueError:
                folder = str(entry.path.parent)
            folder_item = QTableWidgetItem(folder or ".")
            folder_item.setFlags(folder_item.flags() & ~Qt.ItemIsEditable)
            self.pdf_table.setItem(row, 2, folder_item)
        self.pdf_table.blockSignals(False)

        stamped = sum(text is not None for text in self._pdf_original_watermarks.values())
        summary = f"{len(entries)} PDF-filer, {stamped} med vattenstämpel."
        if unreadable:
            summary += f" {unreadable} kan inte läsas eller stämplas (se verktygstips)."
        self.pdf_status.setText(summary)
        self._update_pdf_watermark_button()

    def _pdf_watermark_text(self) -> str:
        return " ".join(self.pdf_watermark_text_input.text().split())

    def _desired_pdf_watermarks(self) -> dict[Path, bool]:
        """PDF:er som ska ändras: True = lägg på vald text, False = ta bort.

        Ikryssad rad utan stämpel stämplas. Ikryssad rad med annan text
        byts bara om användaren klickat i rutan (kryssat ur och i), så att
        en ändrad text i textrutan aldrig stämplar om andra PDF:er i tysthet.
        """
        text = self._pdf_watermark_text()
        desired: dict[Path, bool] = {}
        for row, path in self._pdf_row_paths.items():
            item = self.pdf_table.item(row, 1)
            if path not in self._pdf_original_watermarks or item is None:
                continue
            original = self._pdf_original_watermarks[path]
            if item.checkState() == Qt.Checked:
                if original is None or (row in self._pdf_touched_rows and original != text):
                    desired[path] = True
            elif original is not None:
                desired[path] = False
        return desired

    def _update_pdf_watermark_button(self) -> None:
        desired = self._desired_pdf_watermarks()
        needs_text = any(desired.values())
        self.update_pdf_watermark_button.setEnabled(
            self._pdf_load_pending is None and bool(desired) and (not needs_text or bool(self._pdf_watermark_text()))
        )

    def _on_pdf_watermark_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() == 1:
            self._pdf_touched_rows.add(item.row())
            self._update_pdf_watermark_button()

    def _open_pdf_for_pdf_row(self, row: int, _column: int) -> None:
        path = self._pdf_row_paths.get(row)
        if path is not None:
            self._show_pdf_preview(path)

    def update_pdf_watermarks(self) -> None:
        desired = self._desired_pdf_watermarks()
        if not desired:
            QMessageBox.information(self, "Inga ändringar", "Ingen kryssruta i PDF-metadata har ändrats.")
            return
        text = self._pdf_watermark_text()
        if any(desired.values()):
            try:
                text = pdf_controller.validate_watermark_text(text)
            except pdf_controller.PdfWatermarkError as exc:
                QMessageBox.warning(self, "Ogiltig vattenstämpel", str(exc))
                return
            self._settings.setValue(self._PDF_WATERMARK_TEXT_KEY, text)
        else:
            text = pdf_controller.WATERMARK_TEXT
        project_root = self._pdf_status_root or self._project_root()
        try:
            results = self._run_background_task(
                f"Uppdaterar vattenstämpel i {len(desired)} PDF-fil(er)…",
                lambda: pdf_controller.apply_watermarks(project_root, desired, text=text),
            )
        except Exception as exc:  # noqa: BLE001 - oväntat fel visas, aldrig tyst
            QMessageBox.critical(self, "Kunde inte uppdatera vattenstämpel", str(exc))
            return

        try:
            self._pdf_status_records = load_status_records(project_root)
        except ValueError:
            self._pdf_status_records = {}
        self._refresh_pdf_status_items()
        if self._pdf_preview_path is not None and any(
            result.changed and result.path == self._pdf_preview_path for result in results
        ):
            self._show_pdf_preview(self._pdf_preview_path)
        self._reload_pdf_table()

        changed = sum(1 for result in results if result.changed and result.error is None)
        errors = [f"{result.path.name}: {result.error}" for result in results if result.error is not None]
        message = f"{changed} PDF-fil(er) fick uppdaterad vattenstämpel."
        if errors:
            QMessageBox.warning(self, "Vattenstämpel delvis uppdaterad", message + "\n\n" + "\n".join(errors))
        else:
            QMessageBox.information(self, "Vattenstämpel uppdaterad", message)
