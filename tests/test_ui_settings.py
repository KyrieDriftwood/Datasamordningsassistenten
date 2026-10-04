"""Tester för beständiga användarinställningar i huvudfönstrets layout."""

from __future__ import annotations

import os
import shutil
import time
from threading import Event, get_ident
from pathlib import Path
from zipfile import ZipFile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSettings, QTimer, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QMessageBox, QTableWidget, QTableWidgetItem, QWidget

from app.controllers import dwg_controller
from app.controllers import metadata_controller
from app.ui import main_window as main_window_module
from app.ui.main_window import MainWindow
from dwg import DwgAttributeResult, DwgUpdateResult
from dwg.pdf_sync import record_verified_pdf
from metadata import MetadataField
from metadata.grid import MetadataColumn, MetadataGrid
from backend.file_status import has_review_watermark, update_docx_status


@pytest.fixture
def qt_app(tmp_path, monkeypatch):
    """Använder isolerade INI-inställningar och en headless Qt-instans."""
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(
        QSettings.Format.IniFormat,
        QSettings.Scope.UserScope,
        str(tmp_path),
    )
    app = QApplication.instance() or QApplication([])
    # QSettings(org, app) använder alltid NativeFormat (registret) i Qt 6 och
    # ignorerar setDefaultFormat; tvinga INI så att tester aldrig läser eller
    # skriver användarens riktiga inställningar.
    class _IniSettings(QSettings):
        def __init__(self, organization, application, parent=None):
            super().__init__(
                QSettings.Format.IniFormat,
                QSettings.Scope.UserScope,
                organization,
                application,
                parent,
            )

    monkeypatch.setattr(main_window_module, "QSettings", _IniSettings)
    yield app
    app.processEvents()


def test_splitter_sizes_are_restored_between_window_sessions(qt_app, tmp_path):
    """Panelindelningen ska överleva stängning och ny instans av fönstret."""
    first = MainWindow()
    first.root_input.setText(str(tmp_path))
    first.resize(1200, 800)
    first.show()
    qt_app.processEvents()

    first.main_splitter.setSizes([390, 810, 0])
    first.documents_splitter.setSizes([300, 200, 150])
    first.metadata_tabs.setCurrentIndex(1)
    qt_app.processEvents()
    first.drawings_splitter.setSizes([400, 250])
    expected_main_sizes = first.main_splitter.sizes()
    expected_document_sizes = first.documents_splitter.sizes()
    expected_drawing_sizes = first.drawings_splitter.sizes()
    first.close()
    qt_app.processEvents()

    second = MainWindow()
    second.root_input.setText(str(tmp_path))
    second.resize(1200, 800)
    second.show()
    qt_app.processEvents()

    assert second.main_splitter.sizes() == expected_main_sizes
    assert second.metadata_tabs.currentIndex() == 1
    assert second.drawings_splitter.sizes() == expected_drawing_sizes
    second.metadata_tabs.setCurrentIndex(0)
    qt_app.processEvents()
    assert second.documents_splitter.sizes() == expected_document_sizes

    second.close()
    qt_app.processEvents()


def test_last_project_root_and_file_tree_are_restored_on_restart(qt_app, tmp_path):
    project_root = tmp_path / "senaste_projekt"
    nested_folder = project_root / "Ritningar"
    nested_folder.mkdir(parents=True)
    document = nested_folder / "underlag.docx"
    document.touch()

    first = MainWindow()
    first.root_input.setText(str(project_root))
    first.refresh_folder()
    first.close()
    qt_app.processEvents()

    second = MainWindow()
    assert Path(second.root_input.text()) == project_root.resolve()
    qt_app.processEvents()

    root_item = second.tree.topLevelItem(0)
    assert Path(str(root_item.data(0, Qt.UserRole))) == project_root.resolve()
    assert root_item.childCount() == 1
    assert Path(str(root_item.child(0).data(0, Qt.UserRole))) == nested_folder.resolve()
    # Lat inläsning: undermappen listas först när den expanderas.
    assert root_item.child(0).childCount() == 0
    root_item.child(0).setExpanded(True)
    assert root_item.child(0).childCount() == 1
    assert document.name in root_item.child(0).child(0).text(0)

    second.close()
    qt_app.processEvents()


def test_checking_unexpanded_folder_selects_all_nested_documents(qt_app, tmp_path, monkeypatch):
    deep = tmp_path / "A" / "B"
    deep.mkdir(parents=True)
    first = (tmp_path / "A" / "ett.docx").resolve()
    second = (deep / "två.docx").resolve()
    first.touch()
    second.touch()
    window = MainWindow()
    monkeypatch.setattr(window, "refresh_table", lambda: None)
    window.root_input.setText(str(tmp_path))
    window.refresh_folder()

    folder_item = window.tree.topLevelItem(0).child(0)
    assert folder_item.childCount() == 0
    folder_item.setCheckState(0, Qt.Checked)

    assert sorted(window._checked_documents()) == sorted([first, second])
    assert window._updating_tree is False

    window.close()
    qt_app.processEvents()


def test_tree_check_loads_metadata_in_background_and_ignores_stale_results(qt_app, tmp_path, monkeypatch):
    document = (tmp_path / "a.xlsx").resolve()
    document.touch()
    window = MainWindow()
    window.root_input.setText(str(tmp_path))
    window.refresh_folder()
    release = Event()
    loaded: list[list[Path]] = []

    def slow_load(paths):
        release.wait(5)
        loaded.append(list(paths))
        return set(), {"docx": [], "delivery": [], "xlsx": list(paths)}, {}

    monkeypatch.setattr(window, "_load_metadata_grids", slow_load)
    item = window.tree.topLevelItem(0).child(0)
    item.setCheckState(0, Qt.Checked)
    assert window._metadata_load_pending is not None
    assert not window.save_xlsx_button.isEnabled()
    window.save_metadata("xlsx")  # får inte spara mot gamla rutnät
    assert "läses fortfarande" in window.status.text()

    item.setCheckState(0, Qt.Unchecked)  # ny generation gör den första inaktuell
    release.set()
    deadline = time.monotonic() + 5
    while (window._metadata_load_pending is not None or len(loaded) < 2) and time.monotonic() < deadline:
        qt_app.processEvents()
        time.sleep(0.01)

    assert window._metadata_load_pending is None
    assert [document] in loaded
    # Den inaktuella (ikryssade) generationen får inte tillämpas.
    assert window.xlsx_table.rowCount() == 0
    assert not window.save_xlsx_button.isEnabled()
    window.close()
    qt_app.processEvents()


def test_tree_update_guard_is_restored_after_exception(qt_app):
    window = MainWindow()
    with pytest.raises(RuntimeError):
        with window._tree_update():
            with window._tree_update():
                pass
            assert window._updating_tree is True
            raise RuntimeError("fel")
    assert window._updating_tree is False
    window.close()
    qt_app.processEvents()


def test_document_tables_have_no_review_watermark_column(qt_app, tmp_path):
    """Vattenstämpel hanteras bara i PDF-panelen (användarbeslut 2026-10-02)."""
    document = (tmp_path / "review.docx").resolve()
    grid = MetadataGrid((), (MetadataColumn(document, "docx", {}),))
    window = MainWindow()

    window._populate_metadata_table("docx", window.docx_table, grid)
    assert window.docx_table.columnCount() == 1
    assert window.docx_table.horizontalHeaderItem(0).text() == "Dokumentnamn"
    assert window.docx_table.item(0, 0).data(Qt.CheckStateRole) is None

    window.close()
    qt_app.processEvents()


def test_saving_metadata_leaves_existing_docx_watermark_untouched(tmp_path):
    document = tmp_path / "review-state.docx"
    with ZipFile(document, "w") as archive:
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:p/><w:sectPr/></w:body></w:document>",
        )
        archive.writestr(
            "word/header1.xml",
            '<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>',
        )
    update_docx_status(document, "för granskning")
    before = document.read_bytes()

    assert metadata_controller.save_changes({}) == ()
    assert document.read_bytes() == before
    assert has_review_watermark(document)


def test_standard_and_model_title_block_tables_have_separate_panels(qt_app, tmp_path):
    drawing = tmp_path / "model.dwg"
    drawing.touch()
    window = MainWindow()
    window.root_input.setText(str(tmp_path))
    window.show()
    qt_app.processEvents()

    window._model_attribute_results[drawing.resolve()] = DwgAttributeResult(
        drawing.resolve(),
        (MetadataField("Modellnamn", "Modellnamn", "DEMO 00+100", True, "Modellnamn"),),
    )
    window._refresh_model_table((drawing.resolve(),))

    tabs = window.metadata_tabs
    assert [tabs.tabText(index) for index in range(tabs.count())] == [
        "Dokument",
        "Ritningar och modeller",
        "ACC-leverans",
    ]
    assert tabs.widget(0) is window.documents_splitter
    assert tabs.widget(1) is window.drawings_splitter
    # Sista widgeten är den osynliga utfyllnaden som tar restytan.
    assert window.documents_splitter.count() == 5
    assert window.drawings_splitter.count() == 3
    assert window.documents_splitter.widget(4).objectName() == "panelStackFiller"
    assert window.drawings_splitter.widget(2).objectName() == "panelStackFiller"
    docx_panel = window.documents_splitter.widget(0)
    delivery_panel = window.documents_splitter.widget(1)
    xlsx_panel = window.documents_splitter.widget(2)
    pdf_panel = window.documents_splitter.widget(3)
    assert delivery_panel.objectName() == "deliveryListMetadataPanel"
    assert delivery_panel.findChild(type(window.delivery_table), "deliveryListMetadataTable") is window.delivery_table
    assert delivery_panel.isHidden()
    standard_panel = window.drawings_splitter.widget(0)
    model_panel = window.drawings_splitter.widget(1)
    acc_panel = tabs.widget(2)
    assert pdf_panel.objectName() == "pdfMetadataPanel"
    assert pdf_panel.findChild(type(window.pdf_table), "pdfMetadataTable") is window.pdf_table
    assert docx_panel.objectName() == "docxMetadataPanel"
    assert xlsx_panel.objectName() == "xlsxMetadataPanel"
    assert standard_panel.objectName() == "dwgStandardMetadataPanel"
    assert model_panel.objectName() == "dwgModelMetadataPanel"
    assert acc_panel.objectName() == "accMetadataPanel"
    assert docx_panel.findChild(type(window.docx_table), "docxMetadataTable") is window.docx_table
    assert xlsx_panel.findChild(type(window.xlsx_table), "xlsxMetadataTable") is window.xlsx_table
    assert standard_panel.findChild(type(window.dwg_table), "dwgStandardMetadataTable") is window.dwg_table
    assert model_panel.findChild(type(window.model_table), "dwgModelMetadataTable") is window.model_table
    assert acc_panel.findChild(type(window.acc_table), "accMetadataTable") is window.acc_table
    assert docx_panel.findChild(type(window.save_docx_button), "saveDocxMetadataButton") is window.save_docx_button
    assert (
        docx_panel.findChild(type(window.save_docx_pdf_button), "saveDocxAndCreatePdfButton")
        is window.save_docx_pdf_button
    )
    assert xlsx_panel.findChild(type(window.save_xlsx_button), "saveXlsxMetadataButton") is window.save_xlsx_button
    assert (
        xlsx_panel.findChild(type(window.save_xlsx_pdf_button), "saveXlsxAndCreatePdfButton")
        is window.save_xlsx_pdf_button
    )
    assert (
        standard_panel.findChild(type(window.update_dwg_button), "updateDwgMetadataButton")
        is window.update_dwg_button
    )
    assert standard_panel.findChild(type(window.update_plot_dwg_button), "updateAndPlotDwgButton") is (
        window.update_plot_dwg_button
    )
    assert (
        model_panel.findChild(type(window.update_model_metadata_button), "updateDwgModelMetadataButton")
        is window.update_model_metadata_button
    )
    assert window.docx_table is not window.xlsx_table
    assert window.dwg_table is not window.model_table
    assert window.model_table.horizontalHeaderItem(0).text() == "Filnamn"
    assert window.model_table.horizontalHeaderItem(1).text() == "Modellnamn"
    assert window.model_table.item(0, 1).text() == "DEMO 00+100"
    assert window.model_table.item(0, 1).flags() & Qt.ItemIsEditable
    assert window.model_table.editTriggers() & QTableWidget.EditTrigger.DoubleClicked
    assert window.update_model_metadata_button.isEnabled()
    assert window.update_model_metadata_button.text() == "Uppdatera DWG-modellmetadata"
    window.model_table.item(0, 1).setText("DEMO 00+200")
    assert window._collect_dwg_model_attribute_changes() == {
        drawing.resolve(): {"Modellnamn": "DEMO 00+200"}
    }
    assert window.update_plot_dwg_button.isEnabled() is False

    window.close()
    qt_app.processEvents()


def test_dwg_filename_edit_survives_table_refresh_and_is_renamed_on_disk(qt_app, tmp_path, monkeypatch):
    drawing = tmp_path / "current-name.dwg"
    drawing.write_bytes(b"DWG test content")
    resolved_drawing = drawing.resolve()
    window = MainWindow()
    window.root_input.setText(str(tmp_path))
    window._dwg_attribute_results[resolved_drawing] = DwgAttributeResult(resolved_drawing, ())
    monkeypatch.setattr(window, "_checked_dwg_paths", lambda: [resolved_drawing])
    window._refresh_dwg_table()

    window.dwg_table.item(0, 0).setText("new-name.dwg")
    window._refresh_dwg_table()

    assert window.dwg_table.item(0, 0).text() == "new-name.dwg"
    assert window._collect_dwg_renames() == {resolved_drawing: "new-name.dwg"}

    monkeypatch.setattr(window, "_apply_dwg_attribute_changes", lambda **_kwargs: (0, []))
    monkeypatch.setattr(window, "refresh_folder", lambda: None)
    monkeypatch.setattr(QMessageBox, "information", lambda *_args: None)

    renamed = window.write_dwg_changes()

    assert renamed == {resolved_drawing: tmp_path / "new-name.dwg"}
    assert (tmp_path / "new-name.dwg").read_bytes() == b"DWG test content"
    assert not drawing.exists()
    window.close()


def test_file_tree_hides_pdf_and_shows_document_sync_status(qt_app, tmp_path):
    source = tmp_path / "report.docx"
    pdf = source.with_suffix(".pdf")
    source.write_bytes(b"document")
    pdf.write_bytes(b"%PDF")
    source_stat = source.stat()
    os.utime(
        pdf,
        ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns - 10_000_000_000),
    )

    window = MainWindow()
    window.root_input.setText(str(tmp_path))
    window.refresh_folder()
    qt_app.processEvents()

    root_item = window.tree.topLevelItem(0)
    assert root_item.childCount() == 1
    source_item = root_item.child(0)
    assert source_item.text(0).endswith("[PDF inte synkad]")
    assert Path(str(source_item.data(0, Qt.UserRole))) == source.resolve()

    os.utime(
        pdf,
        ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns + 10_000_000_000),
    )
    window._refresh_pdf_status_items()
    assert source_item.text(0).endswith("[PDF synkad]")

    window.close()


def test_main_columns_have_headings_and_checked_file_selects_its_metadata_tab(qt_app):
    from PySide6.QtWidgets import QLabel, QTreeWidgetItem

    window = MainWindow()
    headings = {
        name: window.findChild(QLabel, name).text()
        for name in ("fileManagerHeading", "metadataManagerHeading", "previewHeading")
    }
    assert headings == {
        "fileManagerHeading": "FILHANTERARE",
        "metadataManagerHeading": "METADATAHANTERARE",
        "previewHeading": "FÖRHANDSVISNING",
    }
    assert window.file_manager_panel.isAncestorOf(window.tree)
    assert window.metadata_tabs.currentWidget() is window.documents_splitter

    def tree_item(kind: str, state) -> QTreeWidgetItem:
        item = QTreeWidgetItem()
        item.setData(0, Qt.UserRole + 1, kind)
        item.setCheckState(0, state)
        return item

    window._show_metadata_tab_for_tree_item(tree_item("dwg", Qt.Checked))
    assert window.metadata_tabs.currentWidget() is window.drawings_splitter
    window._show_metadata_tab_for_tree_item(tree_item("document", Qt.Unchecked))
    window._show_metadata_tab_for_tree_item(tree_item("directory", Qt.Checked))
    assert window.metadata_tabs.currentWidget() is window.drawings_splitter
    window._show_metadata_tab_for_tree_item(tree_item("document", Qt.Checked))
    assert window.metadata_tabs.currentWidget() is window.documents_splitter

    window.close()


def test_metadata_tables_share_zebra_grid_style_without_hiding_status_colors(qt_app):
    from PySide6.QtGui import QColor
    from PySide6.QtWidgets import QStyleOptionViewItem

    window = MainWindow()
    tables = [
        window.docx_table,
        window.xlsx_table,
        window.pdf_table,
        window.dwg_table,
        window.model_table,
        window.acc_table,
    ]
    for table in tables:
        assert table.alternatingRowColors()
        assert table.showGrid()
        assert table.styleSheet() == main_window_module.METADATA_TABLE_STYLESHEET
        assert table.horizontalHeader().defaultAlignment() & Qt.AlignLeft
    window.close()

    table = main_window_module.MetadataTable()
    table.resize(400, 200)
    table.setColumnCount(2)
    table.setRowCount(3)
    for row in range(3):
        table.setItem(row, 0, QTableWidgetItem(f"fil{row}.docx"))
        table.setItem(row, 1, QTableWidgetItem(""))
    table.item(1, 1).setBackground(Qt.yellow)
    table.show()
    qt_app.processEvents()

    option = QStyleOptionViewItem()
    table.itemDelegateForColumn(0).initStyleOption(option, table.model().index(0, 0))
    assert option.font.bold()
    assert table.itemDelegateForColumn(1) is None

    image = table.viewport().grab().toImage()

    def cell_center_color(row: int, column: int) -> str:
        rect = table.visualRect(table.model().index(row, column))
        return QColor(image.pixel(rect.center())).name()

    assert cell_center_color(0, 1) == "#ffffff"
    assert cell_center_color(2, 1) == "#ffffff"
    # Rad 1 är zebrarad men cellens statusfärg ska vinna över zebratonen.
    assert cell_center_color(1, 1) == QColor(Qt.yellow).name()
    zebra_rect = table.visualRect(table.model().index(1, 0))
    zebra_pixel = QColor(image.pixel(zebra_rect.right() - 2, zebra_rect.center().y())).name()
    assert zebra_pixel == main_window_module.METADATA_TABLE_ZEBRA_COLOR
    table.close()


def _filled_metadata_table(qt_app, rows: int, values: list[str] | None = None):
    table = main_window_module.MetadataTable()
    table.resize(900, 200)
    values = values or ["kort", "x"]
    table.setColumnCount(len(values))
    table.setRowCount(rows)
    for row in range(rows):
        for column, value in enumerate(values):
            table.setItem(row, column, QTableWidgetItem(value))
    table.show()
    qt_app.processEvents()
    return table


def test_metadata_table_height_follows_row_count_up_to_ten_rows(qt_app):
    three = _filled_metadata_table(qt_app, 3)
    ten = _filled_metadata_table(qt_app, 10)
    fifteen = _filled_metadata_table(qt_app, 15)

    assert three.height() < ten.height()
    assert ten.height() == fifteen.height()
    assert three.height() == three.fitted_height()
    assert three.minimumHeight() == three.maximumHeight() == three.height()
    # Alla tre rader ryms utan vertikal rullning; 15 rader rullas.
    assert three.verticalScrollBar().maximum() == 0
    assert fifteen.verticalScrollBar().maximum() > 0

    fifteen.setRowCount(2)
    qt_app.processEvents()
    assert fifteen.height() < ten.height()
    for table in (three, ten, fifteen):
        table.close()


def test_metadata_table_columns_fit_loaded_content(qt_app):
    table = _filled_metadata_table(qt_app, 2, ["a", "DEMO000-00-000-00000-00_00-0001.docx"])
    short_width, long_width = table.columnWidth(0), table.columnWidth(1)
    assert short_width < long_width
    assert not table.horizontalHeader().stretchLastSection()

    table.item(0, 0).setText("En betydligt längre titel i första kolumnen")
    qt_app.processEvents()
    assert table.columnWidth(0) > short_width
    table.close()


def test_metadata_table_reserves_height_for_horizontal_scrollbar(qt_app):
    table = _filled_metadata_table(qt_app, 2, ["x" * 200, "y" * 200])
    narrow_height = table.height()
    table.resize(20000, table.height())
    qt_app.processEvents()
    assert narrow_height > table.height()
    table.close()


def test_empty_metadata_table_is_single_grey_row(qt_app):
    from PySide6.QtGui import QColor

    table = _filled_metadata_table(qt_app, 0, ["a", "b"])
    filled = _filled_metadata_table(qt_app, 1, ["a", "b"])

    assert table.property("empty") is True
    assert table.horizontalHeader().isHidden()
    assert table.height() == table.verticalHeader().defaultSectionSize() + 2 * table.frameWidth()
    assert table.height() < filled.height()
    image = table.viewport().grab().toImage()
    center = image.rect().center()
    assert QColor(image.pixel(center)).name() == main_window_module.theme.STATUS_READONLY

    table.setRowCount(1)
    qt_app.processEvents()
    assert table.property("empty") is False
    assert not table.horizontalHeader().isHidden()
    assert table.height() == filled.height()
    for each in (table, filled):
        each.close()


def test_metadata_panels_shrink_with_fitted_tables(qt_app):
    window = MainWindow()
    window.resize(1200, 1000)
    window.show()
    qt_app.processEvents()
    acc_panel = window.findChild(QWidget, "accMetadataPanel")
    empty_max = acc_panel.maximumHeight()

    window.acc_table.setColumnCount(1)
    window.acc_table.setRowCount(5)
    qt_app.processEvents()
    assert acc_panel.maximumHeight() > empty_max
    assert acc_panel.maximumHeight() == acc_panel.layout().sizeHint().height()
    window.close()


def _visual_headers(table) -> list[str]:
    header = table.horizontalHeader()
    return [
        table.horizontalHeaderItem(header.logicalIndex(visual)).text()
        for visual in range(table.columnCount())
    ]


def _load_rows(table, headers: list[str], rows: list[list[object]]) -> None:
    table.clear()
    table.setRowCount(len(rows))
    table.setColumnCount(len(headers))
    table.setHorizontalHeaderLabels(headers)
    for row_index, row in enumerate(rows):
        for column, value in enumerate(row):
            item = QTableWidgetItem("" if value is None or isinstance(value, bool) else value)
            if isinstance(value, bool):
                item.setCheckState(Qt.Checked if value else Qt.Unchecked)
            table.setItem(row_index, column, item)


def test_metadata_table_moves_empty_columns_last_without_changing_logical_columns(qt_app):
    table = main_window_module.MetadataTable()
    headers = ["Dokumentnamn", "Kryss", "TOM1", "TITEL", "TOM2", "DATUM"]
    _load_rows(
        table,
        headers,
        [
            ["a.docx", False, "", "Titel", "  ", ""],
            ["b.docx", True, None, "", "", "2026-10-02"],
        ],
    )
    qt_app.processEvents()

    # Kryssrutekolumnen räknas som ifylld; delvis ifyllda kolumner står kvar.
    assert _visual_headers(table) == ["Dokumentnamn", "Kryss", "TITEL", "DATUM", "TOM1", "TOM2"]
    assert table.horizontalHeaderItem(2).text() == "TOM1"
    assert table.item(0, 3).text() == "Titel"

    # Användarredigering flyttar inte kolumnen medan man arbetar ...
    table.item(0, 2).setText("nu ifylld")
    qt_app.processEvents()
    assert _visual_headers(table)[-2:] == ["TOM1", "TOM2"]

    # ... men nästa inläsning (samma storlek) sorterar om.
    _load_rows(
        table,
        headers,
        [
            ["a.docx", False, "nu ifylld", "Titel", "", ""],
            ["b.docx", True, "", "", "", ""],
        ],
    )
    qt_app.processEvents()
    assert _visual_headers(table) == headers
    table.close()


def test_metadata_table_halves_header_text_wider_than_data(qt_app):
    table = main_window_module.MetadataTable()
    _load_rows(
        table,
        ["EN MYCKET LÅNG RUBRIK FÖR KORT VÄRDE", "K"],
        [["x", "ett betydligt längre metadatavärde"]],
    )
    table.show()
    qt_app.processEvents()

    header = table.horizontalHeader()
    normal_size = header.font().pointSizeF()
    assert header.font().weight() == QFont.DemiBold
    long_header = table.horizontalHeaderItem(0)
    assert long_header.font().pointSizeF() == pytest.approx(normal_size * 0.75)
    assert table.horizontalHeaderItem(1).data(Qt.FontRole) is None

    # Kolumnbredden styrs nu av den krympta rubriken, inte av fullbreddsrubriken.
    long_header.setData(Qt.FontRole, None)
    full_width = header.sectionSizeFromContents(0).width()
    table.fit_to_contents()
    assert table.columnWidth(0) < full_width

    # När datan blir bredare än rubriken återfår rubriken full storlek.
    table.item(0, 0).setText("x" * 120)
    qt_app.processEvents()
    assert long_header.data(Qt.FontRole) is None
    table.close()


def test_metadata_table_copy_paste_follow_visual_column_order(qt_app):
    from PySide6.QtCore import QItemSelectionModel

    table = main_window_module.MetadataTable()
    _load_rows(
        table,
        ["Namn", "Kryss", "TOM", "A", "B"],
        [["r0", False, "", "a0", "b0"], ["r1", False, "", "a1", "b1"]],
    )
    qt_app.processEvents()
    assert _visual_headers(table) == ["Namn", "Kryss", "A", "B", "TOM"]

    # Visuellt sammanhängande block A..B (logiskt 3..4).
    selection = table.selectionModel()
    for row in range(2):
        for column in (3, 4):
            selection.select(table.model().index(row, column), QItemSelectionModel.Select)
    table._copy_selection()
    assert QApplication.clipboard().text() == "a0\tb0\na1\tb1"

    # Inklistring från visuell kolumn B fortsätter in i den flyttade TOM-kolumnen.
    QApplication.clipboard().setText("x\ty")
    table.setCurrentCell(0, 4)
    table._paste_selection()
    assert table.item(0, 4).text() == "x"
    assert table.item(0, 2).text() == "y"
    table.close()


def test_metadata_panels_are_theme_cards_with_one_primary_action(qt_app):
    from PySide6.QtWidgets import QPushButton, QWidget

    window = MainWindow()
    for name in (
        "docxMetadataPanel",
        "xlsxMetadataPanel",
        "pdfMetadataPanel",
        "dwgStandardMetadataPanel",
        "dwgModelMetadataPanel",
        "accMetadataPanel",
    ):
        panel = window.findChild(QWidget, name)
        assert panel.property("role") == "card", name
        primaries = [b for b in panel.findChildren(QPushButton) if b.property("role") == "primary"]
        assert len(primaries) == (0 if name == "accMetadataPanel" else 1), name
    assert window.findChild(QWidget, "fileManagerHeading").property("role") == "sectionHeading"
    window.close()


def test_document_change_collection_can_be_scoped_to_one_format(qt_app, tmp_path):
    docx = tmp_path / "document.docx"
    xlsx = tmp_path / "spreadsheet.xlsx"
    window = MainWindow()
    for document_type, path, table in (
        ("DOCX", docx, window.docx_table),
        ("XLSX", xlsx, window.xlsx_table),
    ):
        table.setRowCount(1)
        table.setColumnCount(3)
        item = QTableWidgetItem("updated")
        table.setItem(0, 2, item)
        key = (path.resolve(), "field")
        window._cell_keys[(document_type, 0, 2)] = key
        window._original_values[key] = "original"

    docx_changes = window._collect_changes("docx")
    xlsx_changes = window._collect_changes("xlsx")

    assert list(docx_changes) == [docx.resolve()]
    assert list(xlsx_changes) == [xlsx.resolve()]

    window.close()
    qt_app.processEvents()


def test_document_action_buttons_use_their_format_and_pdf_workflow(qt_app, monkeypatch):
    window = MainWindow()
    window.save_docx_button.setEnabled(True)
    window.save_docx_pdf_button.setEnabled(True)
    window.save_xlsx_button.setEnabled(True)
    window.save_xlsx_pdf_button.setEnabled(True)
    actions = []
    monkeypatch.setattr(window, "save_metadata", lambda document_type: actions.append(("save", document_type)))
    monkeypatch.setattr(
        window,
        "save_metadata_and_create_pdfs",
        lambda document_type: actions.append(("pdf", document_type)),
    )

    window.save_docx_button.click()
    window.save_docx_pdf_button.click()
    window.save_xlsx_button.click()
    window.save_xlsx_pdf_button.click()

    assert actions == [
        ("save", "docx"),
        ("pdf", "docx"),
        ("save", "xlsx"),
        ("pdf", "xlsx"),
    ]

    window.close()
    qt_app.processEvents()


def test_background_task_keeps_ui_event_loop_responsive(qt_app):
    window = MainWindow()
    started = Event()
    release = Event()
    main_thread_id = get_ident()

    def work():
        started.set()
        assert release.wait(timeout=2)
        return get_ident()

    QTimer.singleShot(30, release.set)
    worker_thread_id = window._run_background_task("Testar dokumentåtgärd…", work)

    assert started.is_set()
    assert worker_thread_id != main_thread_id

    window.close()
    qt_app.processEvents()


def test_background_task_returns_worker_errors_to_ui_thread(qt_app):
    window = MainWindow()
    error = RuntimeError("Office automation failed")

    def work():
        raise error

    with pytest.raises(RuntimeError, match="Office automation failed"):
        window._run_background_task("Testar felhantering…", work)

    window.close()
    qt_app.processEvents()


def test_model_metadata_action_writes_only_model_title_block(qt_app, tmp_path, monkeypatch):
    window = MainWindow()
    model_path = tmp_path / "model.dwg"
    calls = []

    monkeypatch.setattr(window, "_collect_dwg_attribute_changes", lambda: {model_path: {"STANDARD": "x"}})
    monkeypatch.setattr(window, "_collect_dwg_model_attribute_changes", lambda: {model_path: {"MODEL": "y"}})
    monkeypatch.setattr(window, "_run_dwg_task", lambda _title, _count, work: work(lambda _done, _total: None))
    monkeypatch.setattr(window, "_refresh_model_table", lambda _paths: None)
    monkeypatch.setattr(window, "_refresh_pdf_status_items", lambda: None)

    def write_model(changes, **_kwargs):
        calls.append(("model", changes))
        return {model_path: DwgUpdateResult(path=model_path, error=None)}

    def write_standard(*_args, **_kwargs):
        calls.append(("standard", {}))
        return {}

    monkeypatch.setattr(dwg_controller, "write_model_attribute_changes", write_model)
    monkeypatch.setattr(dwg_controller, "write_attribute_changes", write_standard)
    monkeypatch.setattr(window, "_collect_dwg_renames", lambda: {})

    updated_count, errors = window._apply_dwg_attribute_changes(include_standard=False, include_model=True)

    assert calls == [("model", {model_path: {"MODEL": "y"}})]
    assert updated_count == 1
    assert errors == []

    window.close()
    qt_app.processEvents()


def test_combined_dwg_action_updates_before_plotting_and_keeps_renamed_selection(qt_app, tmp_path, monkeypatch):
    drawing = tmp_path / "before.dwg"
    renamed_drawing = tmp_path / "after.dwg"
    drawing.touch()
    window = MainWindow()
    window.root_input.setText(str(tmp_path))
    window.refresh_folder()
    qt_app.processEvents()

    def find_item(item):
        if item.data(0, Qt.UserRole + 1) == "dwg":
            return item
        for index in range(item.childCount()):
            found = find_item(item.child(index))
            if found is not None:
                return found
        return None

    root_item = window.tree.topLevelItem(0)
    dwg_item = find_item(root_item)
    assert dwg_item is not None
    dwg_item.setCheckState(0, Qt.Checked)
    qt_app.processEvents()

    window._dwg_row_paths[0] = drawing.resolve()
    window._dwg_original_names[0] = drawing.name
    window.dwg_table.setRowCount(1)
    window.dwg_table.setColumnCount(1)
    window.dwg_table.setItem(0, 0, QTableWidgetItem(renamed_drawing.name))

    events = []

    def write_changes():
        events.append("update")
        return {drawing.resolve(): renamed_drawing.resolve()}

    def plot_dwgs(paths):
        events.append(("plot", paths))

    monkeypatch.setattr(window, "write_dwg_changes", write_changes)
    monkeypatch.setattr(window, "_plot_dwgs", plot_dwgs)

    assert window.update_plot_dwg_button.isEnabled()
    window.update_and_plot_dwgs()

    assert events == ["update", ("plot", [renamed_drawing.resolve()])]

    window.close()
    qt_app.processEvents()


def test_combined_dwg_action_stops_if_update_fails(qt_app, tmp_path, monkeypatch):
    drawing = tmp_path / "drawing.dwg"
    drawing.touch()
    window = MainWindow()
    window.root_input.setText(str(tmp_path))
    window.refresh_folder()
    qt_app.processEvents()

    dwg_item = window.tree.topLevelItem(0).child(0)
    dwg_item.setCheckState(0, Qt.Checked)
    qt_app.processEvents()

    window._dwg_row_paths[0] = drawing.resolve()
    window._dwg_original_names[0] = drawing.name
    window.dwg_table.setRowCount(1)
    window.dwg_table.setColumnCount(1)
    window.dwg_table.setItem(0, 0, QTableWidgetItem("renamed.dwg"))
    monkeypatch.setattr(window, "write_dwg_changes", lambda: None)
    plotted = []
    monkeypatch.setattr(window, "_plot_dwgs", lambda paths: plotted.extend(paths))

    window.update_and_plot_dwgs()

    assert plotted == []

    window.close()
    qt_app.processEvents()


def test_combined_dwg_action_requires_model_metadata_to_be_updated_separately(qt_app, tmp_path, monkeypatch):
    drawing = tmp_path / "drawing.dwg"
    drawing.touch()
    window = MainWindow()
    window.root_input.setText(str(tmp_path))
    window.refresh_folder()
    qt_app.processEvents()

    dwg_item = window.tree.topLevelItem(0).child(0)
    dwg_item.setCheckState(0, Qt.Checked)
    qt_app.processEvents()
    monkeypatch.setattr(window, "_collect_dwg_model_attribute_changes", lambda: {drawing: {"MODEL": "value"}})
    monkeypatch.setattr(window, "write_dwg_changes", lambda: pytest.fail("DWG model changes are a separate action"))
    monkeypatch.setattr(window, "_plot_dwgs", lambda _paths: pytest.fail("Plotting must wait for model update"))
    messages = []
    monkeypatch.setattr(QMessageBox, "information", lambda *_args: messages.append("model update required"))

    window.update_and_plot_dwgs()

    assert messages == ["model update required"]

    window.close()
    qt_app.processEvents()


def test_combined_dwg_action_plots_when_no_update_is_needed(qt_app, tmp_path, monkeypatch):
    drawing = tmp_path / "drawing.dwg"
    drawing.touch()
    window = MainWindow()
    window.root_input.setText(str(tmp_path))
    window.refresh_folder()
    qt_app.processEvents()

    dwg_item = window.tree.topLevelItem(0).child(0)
    dwg_item.setCheckState(0, Qt.Checked)
    qt_app.processEvents()

    plotted = []
    monkeypatch.setattr(
        window,
        "write_dwg_changes",
        lambda: pytest.fail("DWG-skrivning ska hoppas över när inga ändringar finns"),
    )
    monkeypatch.setattr(window, "_plot_dwgs", lambda paths: plotted.extend(paths))

    assert window.update_plot_dwg_button.text() == "Uppdatera metadata och skapa PDF"
    window.update_and_plot_dwgs()

    assert plotted == [drawing.resolve()]

    window.close()
    qt_app.processEvents()


@pytest.fixture
def source_pdf(tmp_path):
    """Synthetic PDF: public tests must not require project documents."""
    from pypdf import PdfWriter

    path = tmp_path / "example.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    with path.open("wb") as stream:
        writer.write(stream)
    return path


def test_metadata_cell_opens_associated_pdf_and_close_button_hides_reader(qt_app, tmp_path, source_pdf):
    document = tmp_path / "source.docx"
    document.touch()
    pdf = document.with_suffix(".pdf")
    shutil.copyfile(source_pdf, pdf)

    window = MainWindow()
    window.resize(1400, 850)
    window.show()
    qt_app.processEvents()
    window.docx_table.setRowCount(1)
    window.docx_table.setColumnCount(3)
    filename = QTableWidgetItem(document.name)
    filename.setData(Qt.UserRole, str(document))
    window.docx_table.setItem(0, 0, filename)
    window.docx_table.setItem(0, 2, QTableWidgetItem("metadata value"))

    window.docx_table.cellClicked.emit(0, 0)
    qt_app.processEvents()

    assert window.main_splitter.count() == 3
    assert window.main_splitter.widget(0) is window.file_manager_panel
    assert window.main_splitter.widget(1) is not window.pdf_preview_panel
    assert window.main_splitter.widget(2) is window.pdf_preview_panel
    assert window._pdf_preview_path == pdf
    assert window.pdf_preview_panel.isVisible()
    assert window.pdf_document.status().name == "Ready"
    assert window.main_splitter.sizes()[2] >= 300
    assert window.main_splitter.widget(2) is window.pdf_preview_panel

    window.pdf_preview_close_button.click()
    qt_app.processEvents()

    assert not window.pdf_preview_panel.isVisible()
    assert window._pdf_preview_path is None
    window.close()
    qt_app.processEvents()


def test_clicking_any_dwg_cell_opens_its_pdf_in_rightmost_column(qt_app, tmp_path, source_pdf):
    drawing = tmp_path / "plan.dwg"
    drawing.touch()
    shutil.copyfile(source_pdf, drawing.with_suffix(".pdf"))

    window = MainWindow()
    window.resize(1400, 850)
    window.show()
    qt_app.processEvents()
    window.dwg_table.setRowCount(1)
    window.dwg_table.setColumnCount(2)
    window.dwg_table.setItem(0, 0, QTableWidgetItem(drawing.name))
    window._dwg_row_paths[0] = drawing

    window.dwg_table.cellClicked.emit(0, 0)
    qt_app.processEvents()

    assert window._pdf_preview_path == drawing.with_suffix(".pdf")
    assert window.pdf_preview_panel.isVisible()
    assert window.pdf_document.status().name == "Ready"
    assert window.main_splitter.widget(2) is window.pdf_preview_panel
    assert window.main_splitter.sizes()[2] >= 300

    window.close()
    qt_app.processEvents()


def test_pdf_can_be_replaced_while_open_in_preview(qt_app, tmp_path, source_pdf):
    pdf = tmp_path / "live.pdf"
    replacement = tmp_path / "live-replacement.pdf"
    shutil.copyfile(source_pdf, pdf)

    window = MainWindow()
    window.resize(1400, 850)
    window.show()
    qt_app.processEvents()
    window._show_pdf_preview(pdf)
    qt_app.processEvents()

    assert window.pdf_document.status().name == "Ready"
    assert window._pdf_preview_buffer is not None
    replacement.write_bytes(source_pdf.read_bytes())
    replacement.replace(pdf)

    assert pdf.is_file()
    assert window.pdf_document.status().name == "Ready"

    window.close()
    qt_app.processEvents()


def test_clicking_synced_dwg_in_file_tree_only_shows_sync_status(qt_app, tmp_path, source_pdf):
    drawing = tmp_path / "synced.dwg"
    drawing.touch()
    pdf = drawing.with_suffix(".pdf")
    shutil.copyfile(source_pdf, pdf)
    record_verified_pdf(tmp_path, drawing, pdf)

    window = MainWindow()
    window.root_input.setText(str(tmp_path))
    window.refresh_folder()
    window.resize(1400, 850)
    window.show()
    qt_app.processEvents()

    dwg_item = window.tree.topLevelItem(0).child(0)
    assert "PDF synkad" in dwg_item.text(0)
    window.tree.itemClicked.emit(dwg_item, 0)
    qt_app.processEvents()

    assert window._pdf_preview_path is None
    assert not window.pdf_preview_panel.isVisible()

    window.close()
    qt_app.processEvents()


def test_clicking_unsynced_dwg_in_file_tree_does_not_open_pdf_preview(qt_app, tmp_path):
    drawing = tmp_path / "unsynced.dwg"
    drawing.touch()
    drawing.with_suffix(".pdf").write_bytes(b"%PDF-")
    window = MainWindow()
    window.root_input.setText(str(tmp_path))
    window.refresh_folder()
    window.show()
    qt_app.processEvents()

    dwg_item = window.tree.topLevelItem(0).child(0)
    assert "PDF inte synkad" in dwg_item.text(0)
    window.tree.itemClicked.emit(dwg_item, 0)
    qt_app.processEvents()

    assert window._pdf_preview_path is None
    assert not window.pdf_preview_panel.isVisible()

    window.close()
    qt_app.processEvents()


def test_clicking_row_with_missing_pdf_keeps_preview_column_visible(qt_app, tmp_path):
    document = tmp_path / "source.docx"
    document.touch()
    window = MainWindow()
    window.resize(1400, 850)
    window.show()
    qt_app.processEvents()
    window.docx_table.setRowCount(1)
    window.docx_table.setColumnCount(1)
    filename = QTableWidgetItem(document.name)
    filename.setData(Qt.UserRole, str(document))
    window.docx_table.setItem(0, 0, filename)

    window.docx_table.cellClicked.emit(0, 0)
    qt_app.processEvents()

    assert window._pdf_preview_path == document.with_suffix(".pdf")
    assert window.pdf_preview_panel.isVisible()
    assert "PDF saknas" in window.pdf_preview_status.text()
    assert window.main_splitter.widget(2) is window.pdf_preview_panel

    window.close()
    qt_app.processEvents()


def test_model_title_block_does_not_open_associated_pdf_reader(qt_app, tmp_path):
    drawing = tmp_path / "model.dwg"
    drawing.touch()
    drawing.with_suffix(".pdf").write_bytes(b"not required to load")
    window = MainWindow()
    window.show()
    qt_app.processEvents()
    window.model_table.setRowCount(1)
    window.model_table.setColumnCount(2)
    window.model_table.setItem(0, 1, QTableWidgetItem("Model value"))

    window.model_table.cellClicked.emit(0, 1)
    qt_app.processEvents()

    assert window._pdf_preview_path is None
    assert not window.pdf_preview_panel.isVisible()
    window.close()
    qt_app.processEvents()


def _wait_for_pdf_table(qt_app, window, timeout: float = 10.0) -> None:
    import time

    deadline = time.monotonic() + timeout
    while window._pdf_load_pending is not None and time.monotonic() < deadline:
        qt_app.processEvents()
        time.sleep(0.01)
    assert window._pdf_load_pending is None


def test_pdf_panel_lists_pdfs_and_toggles_control_watermark(qt_app, tmp_path, monkeypatch):
    from pypdf import PdfWriter

    from backend.pdf_watermark import has_control_watermark, read_watermark_text, set_control_watermark

    project_root = tmp_path.resolve()
    (project_root / "Ritningar").mkdir()
    plain = project_root / "Ritningar" / "plan.pdf"
    stamped = project_root / "stamplad.pdf"
    for pdf in (plain, stamped):
        writer = PdfWriter()
        writer.add_blank_page(595, 842)
        writer.write(pdf)
    set_control_watermark(stamped, True)
    (project_root / "trasig.pdf").write_bytes(b"inte en pdf")

    # Modala dialoger skulle låsa testkörningen; fånga dem från start.
    warnings: list[str] = []
    errors: list[str] = []
    monkeypatch.setattr(QMessageBox, "warning", lambda _parent, _title, text: warnings.append(text))
    monkeypatch.setattr(QMessageBox, "critical", lambda _parent, _title, text: errors.append(text))

    window = MainWindow()
    window.root_input.setText(str(project_root))
    window.refresh_folder()
    _wait_for_pdf_table(qt_app, window)

    table = window.pdf_table
    assert table.horizontalHeaderItem(1).text() == "Vattenstämpel"
    assert window.pdf_watermark_text_input.text() == "KONTROLLÄRENDE"
    rows = {table.item(row, 0).text(): row for row in range(table.rowCount())}
    assert set(rows) == {"plan.pdf", "stamplad.pdf", "trasig.pdf"}
    assert table.item(rows["stamplad.pdf"], 1).text() == "KONTROLLÄRENDE"
    assert table.item(rows["plan.pdf"], 2).text() == "Ritningar"
    assert table.item(rows["plan.pdf"], 1).checkState() == Qt.Unchecked
    assert table.item(rows["stamplad.pdf"], 1).checkState() == Qt.Checked
    assert not table.item(rows["trasig.pdf"], 1).flags() & Qt.ItemIsUserCheckable
    assert not window.update_pdf_watermark_button.isEnabled()

    table.item(rows["plan.pdf"], 1).setCheckState(Qt.Checked)
    table.item(rows["stamplad.pdf"], 1).setCheckState(Qt.Unchecked)
    assert window.update_pdf_watermark_button.isEnabled()
    assert window._desired_pdf_watermarks() == {plain: True, stamped: False}

    messages: list[str] = []
    monkeypatch.setattr(window, "_run_background_task", lambda _title, work: work())
    monkeypatch.setattr(QMessageBox, "information", lambda _parent, _title, text: messages.append(text))
    window.update_pdf_watermarks()
    _wait_for_pdf_table(qt_app, window)

    assert has_control_watermark(plain)
    assert not has_control_watermark(stamped)
    assert messages == ["2 PDF-fil(er) fick uppdaterad vattenstämpel."]
    assert table.item(rows["plan.pdf"], 1).checkState() == Qt.Checked
    assert not window.update_pdf_watermark_button.isEnabled()

    table.cellClicked.emit(rows["plan.pdf"], 0)
    qt_app.processEvents()
    assert window._pdf_preview_path == plain

    # Egen text: ändrad textruta stämplar inte om befintliga PDF:er i tysthet,
    # men "kryssa ur och i" byter texten.
    window.pdf_watermark_text_input.setText("  Utkast   rev B ")
    assert window._desired_pdf_watermarks() == {}
    assert not window.update_pdf_watermark_button.isEnabled()
    table.item(rows["plan.pdf"], 1).setCheckState(Qt.Unchecked)
    table.item(rows["plan.pdf"], 1).setCheckState(Qt.Checked)
    table.item(rows["stamplad.pdf"], 1).setCheckState(Qt.Checked)
    assert window._desired_pdf_watermarks() == {plain: True, stamped: True}
    window.update_pdf_watermarks()
    _wait_for_pdf_table(qt_app, window)
    assert read_watermark_text(plain) == "Utkast rev B"
    assert read_watermark_text(stamped) == "Utkast rev B"
    assert table.item(rows["plan.pdf"], 1).text() == "Utkast rev B"
    assert main_window_module.QSettings(
        MainWindow._SETTINGS_ORGANIZATION, MainWindow._SETTINGS_APPLICATION
    ).value(MainWindow._PDF_WATERMARK_TEXT_KEY) == "Utkast rev B"
    assert warnings == [] and errors == []

    window.pdf_watermark_text_input.setText("Ogiltig €")
    table.item(rows["plan.pdf"], 1).setCheckState(Qt.Unchecked)
    table.item(rows["plan.pdf"], 1).setCheckState(Qt.Checked)
    window.update_pdf_watermarks()
    assert warnings and "inte stöds" in warnings[0]
    assert read_watermark_text(plain) == "Utkast rev B"

    window.close()
    qt_app.processEvents()


def test_pdf_preview_fits_page_and_wheel_zooms_towards_cursor(qt_app, tmp_path):
    from PySide6.QtCore import QPoint, QPointF
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtPdfWidgets import QPdfView
    from pypdf import PdfWriter

    pdf = tmp_path / "ritning.pdf"
    writer = PdfWriter()
    writer.add_blank_page(2384, 1684)
    writer.write(pdf)

    window = MainWindow()
    window.resize(1400, 900)
    window.show()
    window._show_pdf_preview(pdf)
    for _ in range(20):
        qt_app.processEvents()
    view = window.pdf_view
    assert view.zoomMode() == QPdfView.ZoomMode.FitInView
    fitted = view.effective_zoom_factor()
    assert 0 < fitted < 1

    anchor = QPointF(view.viewport().width() / 2, view.viewport().height() / 2)
    event = QWheelEvent(
        anchor, view.viewport().mapToGlobal(anchor), QPoint(), QPoint(0, 120),
        Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False,
    )
    view.wheelEvent(event)
    qt_app.processEvents()
    assert view.zoomMode() == QPdfView.ZoomMode.Custom
    assert view.zoomFactor() == pytest.approx(fitted * 1.2)
    assert view.horizontalScrollBar().maximum() > 0

    view.zoom_at(1 / 1.2, anchor)
    assert view.zoomFactor() == pytest.approx(fitted)

    window._show_pdf_preview(pdf)
    assert view.zoomMode() == QPdfView.ZoomMode.FitInView

    window.close()
    qt_app.processEvents()


def test_delivery_list_documents_get_own_panel_and_save_flow(qt_app, tmp_path, monkeypatch):
    from tests.test_delivery_list import _delivery_list, _write_docx

    delivery = _delivery_list(tmp_path / "lev.docx").resolve()
    standard = _write_docx(tmp_path / "std.docx", [("PROJECT_NAME", "Projekt", "Ostlänken")]).resolve()
    window = MainWindow()
    window.show()
    monkeypatch.setattr(window, "_checked_documents", lambda: [standard, delivery])
    window.refresh_table()
    for _ in range(5):
        qt_app.processEvents()

    assert window.delivery_panel.isVisible()
    assert window.docx_table.rowCount() == 1
    assert window.docx_table.item(0, 0).text() == "std.docx"
    assert window.delivery_table.rowCount() == 1
    assert window.delivery_table.item(0, 0).text() == "lev.docx"
    assert window.save_delivery_button.isEnabled()

    # Sparflödena hålls isär: DOCX-sparningen får aldrig röra leveransförteckningen.
    assert window._checked_documents_of_type("docx") == [standard]
    assert window._checked_documents_of_type("delivery") == [delivery]

    header = window.delivery_table.horizontalHeader()
    labels = {
        window.delivery_table.horizontalHeaderItem(column).text(): column
        for column in range(window.delivery_table.columnCount())
    }
    window.delivery_table.item(0, labels["LEVERANSPAKET"]).setText("1.15.4")
    assert window._collect_changes("delivery") == {delivery: {"Leveranspaket": "1.15.4"}}
    assert window._collect_changes("docx") == {}
    assert "För granskning" not in labels
    assert header.visualIndex(labels["LEVERANSPAKET"]) < header.visualIndex(labels["BYGGHERRE"])

    monkeypatch.setattr(window, "_checked_documents", lambda: [standard])
    window.refresh_table()
    qt_app.processEvents()
    assert not window.delivery_panel.isVisible()
    assert not window.save_delivery_button.isEnabled()
    window.close()


def test_metadata_panels_stack_from_top_with_equal_gaps(qt_app):
    """Rutorna ska staplas uppifrån med samma mellanrum i båda flikarna."""
    window = MainWindow()
    window.resize(1600, 1000)
    window.show()
    for splitter in (window.documents_splitter, window.drawings_splitter):
        window.metadata_tabs.setCurrentWidget(splitter)
        for _ in range(5):
            qt_app.processEvents()
        visible = [
            splitter.widget(index)
            for index in range(splitter.count() - 1)
            if not splitter.widget(index).isHidden()
        ]
        assert visible[0].geometry().top() == 0
        for upper, lower in zip(visible, visible[1:]):
            assert upper.height() > 0
            assert lower.geometry().top() - upper.geometry().bottom() - 1 == splitter.handleWidth()
    window.close()