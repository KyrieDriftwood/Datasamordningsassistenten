"""Katalogträd: lat inläsning, kryss och PDF-synkstatus per fil.

Mixin till ``app.ui.main_window.MainWindow`` (utbruten 2026-10-02,
granskningspunkt "dela upp main_window.py", utanför TB). Metoderna förutsätter
widgetar och tillstånd som skapas i ``MainWindow.__init__``/``_build_ui``.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox, QTreeWidgetItem

from backend.file_discovery import EntryKind, classify_entry, list_directory_entries
from dwg.pdf_sync import load_status_records, status_for

# Katalognod i filträdet: True när barnen listats (lat inläsning).
_TREE_LOADED_ROLE = Qt.UserRole + 2


class TreePanelMixin:
    def choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Välj projektmapp", self.root_input.text())
        if not folder:
            return
        self.root_input.setText(folder)
        self.refresh_folder()

    def refresh_folder(self) -> None:
        # Byte av mapp/omdöpta filer gör gamla cachade DWG-sökvägar obsoleta.
        self._dwg_attribute_results.clear()
        self._dwg_attribute_loading.clear()
        self._model_attribute_results.clear()
        self._model_attribute_loading.clear()

        folder = Path(self.root_input.text().strip() or ".").expanduser().resolve()
        if not folder.is_dir():
            QMessageBox.warning(self, "Katalog saknas", f"Katalogen finns inte: {folder}")
            return
        self._pdf_status_root = folder
        try:
            self._pdf_status_records = load_status_records(folder)
        except ValueError as exc:
            self._pdf_status_records = {}
            QMessageBox.warning(self, "PDF-synkstatus kunde inte läsas", str(exc))
        try:
            self._populate_explorer(folder)
        except OSError as exc:
            QMessageBox.critical(self, "Kunde inte läsa katalog", str(exc))
            return
        self.refresh_table()
        self._reload_pdf_table()

    def _project_root(self) -> Path:
        return Path(self.root_input.text().strip() or ".").expanduser().resolve()

    def _populate_explorer(self, root: Path) -> None:
        with self._tree_update():
            self.tree.clear()
            root_item = self._create_tree_item(root)
            self.tree.addTopLevelItem(root_item)
            self._load_directory_children(root_item)
            root_item.setExpanded(True)

    @contextmanager
    def _tree_update(self):
        """Stänger av itemChanged-hanteringen; återställs även vid undantag.

        Reentrant: inre anrop återställer föregående värde, inte ``False``.
        """
        previous = self._updating_tree
        self._updating_tree = True
        try:
            yield
        finally:
            self._updating_tree = previous

    def _create_tree_item(self, path: Path) -> QTreeWidgetItem:
        kind = classify_entry(path)

        if kind is EntryKind.DIRECTORY:
            # Lat inläsning: barnen listas först vid expandering eller kryss
            # (``_load_directory_children``). Att lista hela projektet i
            # UI-tråden tog flera sekunder på nätverksenhet.
            item = QTreeWidgetItem([f"📁 {path.name or path}"])
            item.setData(0, Qt.UserRole, str(path))
            item.setData(0, Qt.UserRole + 1, "directory")
            item.setData(0, _TREE_LOADED_ROLE, False)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(0, Qt.Unchecked)
            item.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator)
            return item

        if kind is EntryKind.DWG:
            state, pdf_path = status_for(self._project_root(), path.resolve(), self._pdf_status_records)
            label = "PDF synkad" if state == "synced" else "PDF inte synkad"
            item = QTreeWidgetItem([f"📐 {path.name}  [{label}]"])
            item.setData(0, Qt.UserRole, str(path.resolve()))
            item.setData(0, Qt.UserRole + 1, "dwg")
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(0, Qt.Unchecked)
            item.setToolTip(0, f"DWG: {path.resolve()}\n{label}: {pdf_path}")
            return item

        # EntryKind.DOCUMENT (DOCX/XLSX) och EntryKind.OTHER (t.ex. DGN,
        # se docs/kravsparning.md om DGN-avgränsningen) visas båda som
        # vanliga dokumentrader.
        if kind is EntryKind.DOCUMENT:
            pdf_path = path.with_suffix(".pdf")
            label = "PDF synkad" if self._document_pdf_is_synced(path, pdf_path) else "PDF inte synkad"
            item = QTreeWidgetItem([f"📄 {path.name}  [{label}]"])
            item.setToolTip(0, f"Källfil: {path.resolve()}\n{label}: {pdf_path.resolve()}")
        else:
            item = QTreeWidgetItem([f"📄 {path.name}"])
        item.setData(0, Qt.UserRole, str(path.resolve()))
        item.setData(0, Qt.UserRole + 1, "document")
        item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
        item.setCheckState(0, Qt.Unchecked)
        return item

    @staticmethod
    def _document_pdf_is_synced(source: Path, pdf_path: Path) -> bool:
        """A document PDF is stale whenever its source was modified later."""
        try:
            return pdf_path.stat().st_mtime_ns >= source.stat().st_mtime_ns
        except FileNotFoundError:
            return False

    def _load_directory_children(self, item: QTreeWidgetItem) -> None:
        """Listar en katalognivå en gång. Barn ärver förälderns kryss."""
        if item.data(0, Qt.UserRole + 1) != "directory" or item.data(0, _TREE_LOADED_ROLE):
            return
        with self._tree_update():
            item.setData(0, _TREE_LOADED_ROLE, True)
            state = item.checkState(0)
            for child_path in list_directory_entries(Path(str(item.data(0, Qt.UserRole)))):
                child = self._create_tree_item(child_path)
                if state == Qt.Checked:
                    child.setCheckState(0, state)
                item.addChild(child)
            if item.childCount() == 0:
                item.setChildIndicatorPolicy(QTreeWidgetItem.DontShowIndicatorWhenChildless)

    def _load_directory_subtree(self, item: QTreeWidgetItem) -> None:
        self._load_directory_children(item)
        for index in range(item.childCount()):
            child = item.child(index)
            if child.data(0, Qt.UserRole + 1) == "directory":
                self._load_directory_subtree(child)

    def _on_tree_item_expanded(self, item: QTreeWidgetItem) -> None:
        if item.data(0, _TREE_LOADED_ROLE):
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self._load_directory_children(item)
        except OSError as exc:
            QMessageBox.warning(self, "Kunde inte läsa katalog", str(exc))
        finally:
            QApplication.restoreOverrideCursor()

    def _on_tree_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if self._updating_tree:
            return
        if item.data(0, Qt.UserRole + 1) == "directory" and item.checkState(0) == Qt.Checked:
            # Ett kryss på en ej expanderad mapp ska omfatta alla filer under
            # den, precis som när hela trädet lästes in direkt.
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                self._load_directory_subtree(item)
            except OSError as exc:
                QMessageBox.warning(self, "Kunde inte läsa katalog", str(exc))
            finally:
                QApplication.restoreOverrideCursor()
        with self._tree_update():
            self._set_children_check_state(item, item.checkState(0))
        self._show_metadata_tab_for_tree_item(item)
        self._refresh_table_in_background()

    def _show_metadata_tab_for_tree_item(self, item: QTreeWidgetItem) -> None:
        """Visar underfliken för en fil som just kryssades i filträdet.

        Mappar och urkryssning byter aldrig flik, så användarens val står kvar.
        """
        if item.checkState(0) != Qt.Checked:
            return
        tab_for_kind = {"document": self.documents_splitter, "dwg": self.drawings_splitter}
        target = tab_for_kind.get(item.data(0, Qt.UserRole + 1))
        if target is not None:
            self.metadata_tabs.setCurrentWidget(target)

    def _set_children_check_state(self, item: QTreeWidgetItem, state) -> None:
        for index in range(item.childCount()):
            child = item.child(index)
            if child.data(0, Qt.UserRole + 1) != "pdf":
                child.setCheckState(0, state)
            self._set_children_check_state(child, state)

    def _checked_documents(self) -> list[Path]:
        documents: list[Path] = []

        def collect(item: QTreeWidgetItem) -> None:
            kind = item.data(0, Qt.UserRole + 1)
            path_text = item.data(0, Qt.UserRole)
            if kind == "document" and path_text and item.checkState(0) == Qt.Checked:
                documents.append(Path(str(path_text)))
            for index in range(item.childCount()):
                collect(item.child(index))

        for index in range(self.tree.topLevelItemCount()):
            collect(self.tree.topLevelItem(index))
        return documents

    def _checked_dwg_paths(self) -> list[Path]:
        paths: list[Path] = []

        def collect(item: QTreeWidgetItem) -> None:
            kind = item.data(0, Qt.UserRole + 1)
            path_text = item.data(0, Qt.UserRole)
            if kind == "dwg" and path_text and item.checkState(0) == Qt.Checked:
                paths.append(Path(str(path_text)))
            for index in range(item.childCount()):
                collect(item.child(index))

        for index in range(self.tree.topLevelItemCount()):
            collect(self.tree.topLevelItem(index))
        return paths

    def _refresh_pdf_status_items(self) -> None:
        root = self._pdf_status_root
        if root is None:
            return

        def update(item: QTreeWidgetItem) -> None:
            if item.data(0, Qt.UserRole + 1) == "dwg":
                drawing = Path(str(item.data(0, Qt.UserRole)))
                state, pdf_path = status_for(root, drawing, self._pdf_status_records)
                label = "PDF synkad" if state == "synced" else "PDF inte synkad"
                item.setText(0, f"📐 {drawing.name}  [{label}]")
                item.setToolTip(0, f"DWG: {drawing}\n{label}: {pdf_path}")
            elif item.data(0, Qt.UserRole + 1) == "document":
                source = Path(str(item.data(0, Qt.UserRole)))
                pdf_path = source.with_suffix(".pdf")
                label = "PDF synkad" if self._document_pdf_is_synced(source, pdf_path) else "PDF inte synkad"
                item.setText(0, f"📄 {source.name}  [{label}]")
                item.setToolTip(0, f"Källfil: {source}\n{label}: {pdf_path}")
            for index in range(item.childCount()):
                update(item.child(index))

        with self._tree_update():
            for index in range(self.tree.topLevelItemCount()):
                update(self.tree.topLevelItem(index))
