"""DOCX/XLSX-multiredigering: inläsning (synkront och i bakgrund) och sparande.

Mixin till ``app.ui.main_window.MainWindow`` (utbruten 2026-10-02,
granskningspunkt "dela upp main_window.py", utanför TB). Metoderna förutsätter
widgetar och tillstånd som skapas i ``MainWindow.__init__``/``_build_ui``.
"""

from __future__ import annotations

import queue
import threading
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QMessageBox, QTableWidgetItem

from app.controllers import metadata_controller
from app.ui import theme
from metadata import MetadataError
from app.ui.metadata_table import MetadataTable


class DocumentPanelMixin:
    def refresh_table(self) -> None:
        """Bygger om alla metadatatabeller synkront (används efter sparning)."""
        paths = self._checked_documents()
        self._metadata_load_generation += 1
        self._metadata_load_pending = None
        self._apply_metadata_grids(paths, self._load_metadata_grids(paths))
        self._refresh_non_document_parts()

    def _refresh_table_in_background(self) -> None:
        """Kryss i filträdet: DWG-delen direkt, DOCX/XLSX-läsningen i bakgrunden.

        En generationsräknare gör att ett äldre resultat aldrig skriver över
        ett nyare kryss. Spara-knapparna är avstängda tills tabellerna visar
        den aktuella markeringen, så att inga ändringar sparas mot gamla rutnät.
        """
        paths = self._checked_documents()
        self._metadata_load_generation += 1
        generation = self._metadata_load_generation
        results: queue.Queue = queue.Queue()

        def worker() -> None:
            try:
                results.put(("result", self._load_metadata_grids(paths)))
            except Exception as exc:  # noqa: BLE001 - visas i statusraden
                results.put(("error", exc))

        self._metadata_load_pending = (generation, paths, results)
        self._set_document_save_buttons_enabled(False)
        if paths:
            self.status.setText(f"Läser metadata för {len(paths)} fil(er)…")
        self._refresh_non_document_parts()
        threading.Thread(target=worker, name="metadata-load", daemon=True).start()
        self._metadata_load_timer.start()

    def _poll_metadata_load(self) -> None:
        pending = self._metadata_load_pending
        if pending is None:
            self._metadata_load_timer.stop()
            return
        generation, paths, results = pending
        try:
            kind, value = results.get_nowait()
        except queue.Empty:
            return
        self._metadata_load_pending = None
        self._metadata_load_timer.stop()
        if generation != self._metadata_load_generation:
            return
        if kind == "error":
            self.status.setText(f"Metadata kunde inte läsas: {value}")
            return
        self._apply_metadata_grids(paths, value)

    @staticmethod
    def _load_metadata_grids(paths: list[Path]) -> tuple[set[Path], dict[str, list[Path]], dict]:
        """Läser alla rutnät utan Qt-anrop; säker att köra i bakgrundstråd."""
        docx_paths = [path for path in paths if path.suffix.lower() == ".docx"]
        delivery_paths = {path for path in docx_paths if metadata_controller.is_delivery_list_document(path)}
        paths_by_type = {
            "docx": [path for path in docx_paths if path not in delivery_paths],
            "delivery": [path for path in docx_paths if path in delivery_paths],
            "xlsx": [path for path in paths if path.suffix.lower() == ".xlsx"],
        }
        grid_builders = {
            "docx": metadata_controller.build_grid,
            "delivery": metadata_controller.build_delivery_list_grid,
            "xlsx": metadata_controller.build_grid,
        }
        grids = {
            document_type: grid_builders[document_type](typed_paths)
            for document_type, typed_paths in paths_by_type.items()
            if typed_paths
        }
        return delivery_paths, paths_by_type, grids

    def _set_document_save_buttons_enabled(self, enabled: bool) -> None:
        for button in (
            self.save_docx_button,
            self.save_docx_pdf_button,
            self.save_delivery_button,
            self.save_delivery_pdf_button,
            self.save_xlsx_button,
            self.save_xlsx_pdf_button,
        ):
            button.setEnabled(enabled)

    def _apply_metadata_grids(self, paths: list[Path], loaded) -> None:
        delivery_paths, paths_by_type, grids = loaded
        self._cell_keys.clear()
        self._original_values.clear()
        self._grids_by_type.clear()
        self._delivery_list_paths = delivery_paths
        counts: dict[str, tuple[int, int]] = {}
        for document_type, table in self._document_tables.items():
            grid = grids.get(document_type)
            if grid is None:
                table.clear()
                table.setRowCount(0)
                table.setColumnCount(0)
                counts[document_type] = (0, 0)
                continue
            self._grids_by_type[document_type] = grid
            self._populate_metadata_table(document_type, table, grid)
            counts[document_type] = (
                len(grid.columns),
                sum(1 for column in grid.columns if column.error is not None),
            )

        total_count = len(paths)
        if not total_count:
            self.status.setText("Markera DOCX- eller XLSX-filer i den lokala filläsaren.")
        else:
            failed = counts["docx"][1] + counts["delivery"][1] + counts["xlsx"][1]
            suffix = f" {failed} fil(er) saknar läsbar metadata." if failed else ""
            delivery_text = (
                f" (varav {counts['delivery'][0]} DOCX-verifikat)" if counts["delivery"][0] else ""
            )
            self.status.setText(
                f"{counts['docx'][0] + counts['delivery'][0]} DOCX-{delivery_text} och "
                f"{counts['xlsx'][0]} XLSX-filer markerade.{suffix}"
            )
        self.docx_status.setText(self._metadata_panel_status("DOCX", counts["docx"]))
        self.delivery_status.setText(
            self._metadata_panel_status("DOCX-verifikat", counts["delivery"]).replace(
                "DOCX-verifikat-filer", "DOCX-verifikat"
            )
        )
        self.delivery_panel.setVisible(bool(paths_by_type["delivery"]))
        self.xlsx_status.setText(self._metadata_panel_status("XLSX", counts["xlsx"]))
        self.save_docx_button.setEnabled(bool(paths_by_type["docx"]))
        self.save_docx_pdf_button.setEnabled(bool(paths_by_type["docx"]))
        self.save_delivery_button.setEnabled(bool(paths_by_type["delivery"]))
        self.save_delivery_pdf_button.setEnabled(bool(paths_by_type["delivery"]))
        self.save_xlsx_button.setEnabled(bool(paths_by_type["xlsx"]))
        self.save_xlsx_pdf_button.setEnabled(bool(paths_by_type["xlsx"]))

    def _refresh_non_document_parts(self) -> None:
        has_dwg_paths = bool(self._checked_dwg_paths())
        self.update_dwg_button.setEnabled(has_dwg_paths)
        self.update_plot_dwg_button.setEnabled(has_dwg_paths)
        self._refresh_dwg_table()
        self._refresh_pdf_status_items()

    def _populate_metadata_table(
        self,
        document_type: str,
        table: MetadataTable,
        grid: metadata_controller.MetadataGrid,
    ) -> None:
        grid_key = document_type.upper()
        table.setRowCount(len(grid.columns))
        table.setColumnCount(len(grid.rows) + 1)
        table.setHorizontalHeaderLabels(["Dokumentnamn"] + [row.label.upper() for row in grid.rows])

        for metadata_column_index, row in enumerate(grid.rows, start=1):
            header = table.horizontalHeaderItem(metadata_column_index)
            header.setToolTip(f"{row.key}\nFinns i: {row.source_label}")

        for document_row_index, column in enumerate(grid.columns):
            document_item = QTableWidgetItem(column.path.name)
            document_item.setFlags(document_item.flags() & ~Qt.ItemIsEditable)
            document_tooltip = f"{column.document_type.upper()}\n{column.path}"
            if column.error is not None:
                document_tooltip += f"\n\nMetadata kunde inte läsas:\n{column.error}"
                document_item.setBackground(QColor(theme.STATUS_WARNING))
            document_item.setToolTip(document_tooltip)
            document_item.setData(Qt.UserRole, str(column.path))
            table.setItem(document_row_index, 0, document_item)

            for metadata_column_index, row in enumerate(grid.rows, start=1):
                field = grid.field_at(row, column)
                actual_field_key = field.key if field is not None else row.key
                key = (column.path, actual_field_key)
                value = grid.value_at(row, column)
                item = QTableWidgetItem(value)
                item.setToolTip(
                    f"{column.path.name}\n{row.key}\nKällfält: {actual_field_key}\nFinns i: {row.source_label}"
                )
                if grid.editable_at(row, column):
                    item.setFlags(item.flags() | Qt.ItemIsEditable)
                else:
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                    item.setBackground(QColor(theme.STATUS_READONLY))
                table.setItem(document_row_index, metadata_column_index, item)
                self._cell_keys[(grid_key, document_row_index, metadata_column_index)] = key
                self._original_values[key] = value

    @staticmethod
    def _metadata_panel_status(document_type: str, counts: tuple[int, int]) -> str:
        count, failed = counts
        if count == 0:
            return f"Inga {document_type}-filer markerade."
        if failed:
            return f"{count} markerade {document_type}-filer; {failed} saknar läsbar metadata."
        return f"{count} markerade {document_type}-filer."

    def _checked_documents_of_type(self, document_type: str | None) -> list[Path]:
        """Markerade dokument för ett sparflöde.

        Leveransförteckningar är DOCX men har eget flöde: de får aldrig ingå
        i DOCX-sparningen, eftersom deras metadata redigeras i en annan tabell.
        """
        paths = self._checked_documents()
        if document_type is None:
            return paths
        if document_type == "delivery":
            return [path for path in paths if path in self._delivery_list_paths]
        return [
            path
            for path in paths
            if path.suffix.lower() == f".{document_type}" and path not in self._delivery_list_paths
        ]

    def _collect_changes(self, document_type: str | None = None) -> dict[Path, dict[str, str]]:
        changes: dict[Path, dict[str, str]] = {}
        for (cell_type, row, column), key in self._cell_keys.items():
            if document_type is not None and cell_type.casefold() != document_type.casefold():
                continue
            path, field_key = key
            item = self._document_tables[cell_type.casefold()].item(row, column)
            if item is None or not item.flags() & Qt.ItemIsEditable:
                continue
            value = item.text()
            if value != self._original_values.get(key, ""):
                changes.setdefault(path, {})[field_key] = value
        return changes

    def _open_pdf_for_metadata_row(self, table: MetadataTable, row: int, _column: int) -> None:
        document_item = table.item(row, 0)
        if document_item is None:
            return
        path_text = document_item.data(Qt.UserRole)
        if path_text:
            self._show_pdf_preview(Path(str(path_text)).with_suffix(".pdf"))

    @staticmethod
    def _document_type_label(document_type: str | None) -> str:
        if document_type == "delivery":
            return "DOCX-verifikat"
        if document_type:
            return f"{document_type.upper()}-dokument"
        return "dokument"

    def _metadata_is_loading(self) -> bool:
        """Sant (och meddelar) om tabellerna ännu inte visar aktuell markering."""
        if self._metadata_load_pending is None:
            return False
        self.status.setText("Metadata läses fortfarande in – försök igen om ett ögonblick.")
        return True

    def save_metadata(self, document_type: str | None = None) -> None:
        if self._metadata_is_loading():
            return
        changes = self._collect_changes(document_type)
        document_label = self._document_type_label(document_type)

        if not changes:
            QMessageBox.information(
                self,
                "Inga ändringar",
                "Det finns inga metadataändringar att spara för det valda filformatet.",
            )
            return

        try:
            updated = self._run_background_task(
                "Uppdaterar dokumentmetadata…",
                lambda: metadata_controller.save_changes(changes),
            )
        except (MetadataError, KeyError, ValueError) as exc:
            QMessageBox.critical(self, "Kunde inte spara metadata", str(exc))
            return

        self.refresh_table()
        self._refresh_pdf_status_items()

        QMessageBox.information(
            self,
            "Sparat",
            f"{len(updated)} {document_label} fick metadata uppdaterad.",
        )

    def save_metadata_and_create_pdfs(self, document_type: str | None = None) -> None:
        if self._metadata_is_loading():
            return
        paths = self._checked_documents_of_type(document_type)
        if not paths:
            QMessageBox.information(self, "Inga dokument", "Markera först minst ett dokument.")
            return

        changes = self._collect_changes(document_type)
        document_label = self._document_type_label(document_type)
        try:
            result = self._run_background_task(
                f"Microsoft Word/Office uppdaterar metadata och skapar PDF för {len(paths)} {document_label}…",
                lambda: metadata_controller.save_changes_and_create_pdfs(paths, changes),
            )
        except Exception as exc:  # Office-automationen kan höja flera olika feltyper.
            QMessageBox.critical(self, "Kunde inte skapa PDF", str(exc))
            return

        self.refresh_folder()
        QMessageBox.information(
            self,
            "Klart",
            f"{len(result.updated_documents)} dokument uppdaterades.\n"
            f"{len(result.created_pdfs)} PDF-filer skapades.",
        )
