"""Huvudfönster för den aktiva applikationen.

Portering av det PySide6-baserade huvudfönstret i
``.old/python_v5/app_v5_desktop.py`` (och dess bas ``app_v4_desktop.py``)
till den nya strukturen. Funktionellt en identisk kopia: samma
katalogträd, samma DOCX/XLSX-multiredigeringstabell (kopiera/klistra in),
samma separata DWG-tabell med automatisk
bakgrundsinläsning via Rust/LibreDWG och skrivning via accoreconsole.
Granskningsvattenstämpeln för DOCX/XLSX är borttagen ur UI:t
(användarbeslut 2026-10-02, utanför TB): vattenstämpel hanteras bara i
PDF-panelen.

Skillnaden mot baslinjen är strukturell, inte funktionell: baslinjens
tre nedärvda fönsterklasser (``app_v3_desktop.SharePointFileBrowser`` →
``app_v4_desktop.SharePointMetadataEditor`` → ``app_v5_desktop.DwgAwareMetadataEditor``)
är här samlade i en enda ``MainWindow``-klass som pratar med de nya,
redan porterade och testade modulerna
(``app.controllers.metadata_controller``, ``app.controllers.dwg_controller``,
``app.controllers.delivery_controller``, ``backend.file_discovery``)
i stället för att importera ``src.version_3/4/5`` direkt. Sedan 2026-10-02
ligger beteendet i mixin-moduler (``tree_panel``, ``document_panel``,
``dwg_panel``, ``pdf_panel``); den här modulen äger uppbyggnaden av
gränssnittet och tillståndet i ``__init__``.

En skillnad i säkerhet, inte i användarflöde: DWG-skrivningen går nu via
``dwg.writers`` (backup + read-back-verifiering, se docs/plan.md Fas 3)
i stället för baslinjens direkta accoreconsole-skrivning utan
säkerhetsnät.

Kravkälla: docs/kravsparning.md, TB-sektion AFC.1/EAA/EAB/EAC/CBE/D/J/L.
"""

from __future__ import annotations

import queue
import sys
from pathlib import Path

try:
    from PySide6.QtCore import QBuffer, QSettings, Qt, QTimer
    from PySide6.QtGui import QCloseEvent
    from PySide6.QtPdf import QPdfDocument
    from PySide6.QtWidgets import (
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QMessageBox,
        QPushButton,
        QSplitter,
        QTabWidget,
        QTableWidget,
        QTreeWidget,
        QVBoxLayout,
        QWidget,
    )
except ModuleNotFoundError as exc:
    if exc.name != "PySide6":
        raise
    print(
        "PySide6 saknas i aktuell Python-miljö.\n"
        "Kör detta från projektroten:\n"
        r".\.venv\Scripts\python.exe -m pip install -r requirements.txt"
        "\n\nStarta sedan appen med:\n"
        r".\.venv\Scripts\python.exe -m app.main",
        file=sys.stderr,
    )
    raise SystemExit(1) from exc

from app.controllers import metadata_controller, dwg_controller, pdf_controller
from app.ui import theme
from app.ui.autocad_settings import AutocadSettingsMixin
from app.ui.document_panel import DocumentPanelMixin
from app.ui.dwg_panel import DwgPanelMixin
from app.ui.metadata_table import (  # noqa: F401 - re-export (tester, bakåtkompatibilitet)
    METADATA_TABLE_GRID_COLOR,
    METADATA_TABLE_SELECTION_COLOR,
    METADATA_TABLE_STYLESHEET,
    METADATA_TABLE_ZEBRA_COLOR,
    MetadataTable,
    _IdentifierColumnDelegate,
)
from app.ui.pdf_panel import PdfPanelMixin
from app.ui.pdf_preview_view import ZoomablePdfView
from app.ui.tree_panel import TreePanelMixin
from dwg import DwgAttributeResult


def _section_heading(text: str, object_name: str) -> QLabel:
    """Rubrik för någon av huvudkolumnerna FILHANTERARE/METADATAHANTERARE/FÖRHANDSVISNING."""
    heading = QLabel(text)
    heading.setObjectName(object_name)
    font = heading.font()
    font.setBold(True)
    font.setPointSizeF(font.pointSizeF() * 1.15)
    heading.setFont(font)
    theme.set_role(heading, "sectionHeading")
    return heading


def _card_panel(object_name: str, title: str, help_text: str | None = None) -> tuple[QWidget, QVBoxLayout]:
    """Metadatapanel som vitt kort med rubrik och valfri grå hjälptext (tema L-2)."""
    panel = QWidget()
    panel.setObjectName(object_name)
    theme.set_role(panel, "card")
    layout = QVBoxLayout(panel)
    layout.setContentsMargins(theme.CARD_PADDING, theme.CARD_PADDING, theme.CARD_PADDING, theme.CARD_PADDING)
    layout.setSpacing(theme.SPACING)
    layout.addWidget(theme.set_role(QLabel(title), "panelTitle"))
    if help_text:
        layout.addWidget(theme.set_role(QLabel(help_text), "muted"))
    return panel, layout


def _cap_panel_height(panel: QWidget) -> None:
    """Begränsar en metadatapanel till sitt innehålls höjd (se MetadataTable)."""
    layout = panel.layout()
    layout.invalidate()
    panel.setMaximumHeight(layout.sizeHint().height())


PANEL_STACK_FILLER_NAME = "panelStackFiller"


def _stacked_panel_splitter(name: str, panels: tuple[QWidget, ...]) -> QSplitter:
    """Vertikal splitter där panelerna staplas uppifrån med jämnt mellanrum.

    Panelerna har maxhöjd (``_cap_panel_height``) och kan inte ta emot
    restyta. Utan utfyllnad sprider QSplitter den mellan panelerna, vilket ger
    ojämna glapp och kan kollapsa en panel till 0. Den osynliga utfyllnaden
    sist tar all restyta; dess handtag är avstängt så den inte kan dras.
    Invarians: utfyllnaden är alltid sista widget – ``count() - 1`` paneler.
    """
    splitter = QSplitter(Qt.Vertical)
    splitter.setObjectName(name)
    splitter.setChildrenCollapsible(False)
    for panel in panels:
        splitter.addWidget(panel)
        splitter.setStretchFactor(splitter.indexOf(panel), 0)
    filler = QWidget()
    filler.setObjectName(PANEL_STACK_FILLER_NAME)
    splitter.addWidget(filler)
    filler_index = splitter.indexOf(filler)
    splitter.setStretchFactor(filler_index, 1)
    splitter.handle(filler_index).setEnabled(False)
    return splitter


class MainWindow(AutocadSettingsMixin, TreePanelMixin, DocumentPanelMixin, DwgPanelMixin, PdfPanelMixin, QWidget):
    """Huvudfönster: katalogträd, DOCX/XLSX-multiredigering och DWG-attribut.

    Funktionellt en sammanslagning av baslinjens
    ``SharePointMetadataEditor`` (app_v4_desktop.py) och
    ``DwgAwareMetadataEditor`` (app_v5_desktop.py) i en enda klass, byggd
    mot de nya kontrollermodulerna i stället för ``src.version_*``.
    """

    _SETTINGS_ORGANIZATION = "Datasamordningsassistenten"
    _SETTINGS_APPLICATION = "Datasamordningsassistenten"
    _LAST_PROJECT_ROOT_KEY = "project/last_root"
    _PDF_WATERMARK_TEXT_KEY = "pdf/watermark_text"
    _MAIN_SPLITTER_KEY = "layout/main_splitter"
    # Metadatapanelerna ligger i underflikar sedan fliksystemet infördes; den
    # gamla gemensamma nyckeln (metadata_panels_splitter_v3) läses inte längre.
    _DOCUMENTS_SPLITTER_KEY = "layout/documents_splitter_v2"
    _DRAWINGS_SPLITTER_KEY = "layout/drawings_splitter_v2"
    _METADATA_TAB_KEY = "layout/metadata_tab"

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Datasamordningsassistenten")
        self.resize(1500, 850)
        self._settings = QSettings(
            self._SETTINGS_ORGANIZATION,
            self._SETTINGS_APPLICATION,
        )

        self._updating_tree = False
        self._grids_by_type: dict[str, metadata_controller.MetadataGrid] = {}
        self._cell_keys: dict[tuple[str, int, int], tuple[Path, str]] = {}
        self._original_values: dict[tuple[Path, str], str] = {}
        self._pdf_preview_path: Path | None = None
        self._pdf_preview_buffer: QBuffer | None = None
        self._pdf_status_records: dict[str, dict[str, object]] = {}
        self._pdf_status_root: Path | None = None

        self._dwg_row_paths: dict[int, Path] = {}
        self._dwg_original_names: dict[int, str] = {}
        self._dwg_attribute_cell_keys: dict[tuple[int, int], tuple[Path, str]] = {}
        self._dwg_attribute_original_values: dict[tuple[Path, str], str] = {}
        # Cache av senast inlästa namnrute-attribut per DWG-fil, fylld
        # automatiskt i bakgrunden (se _start_lazy_dwg_attribute_load).
        # Samma motivering som baslinjen (app_v5_desktop.py): synkron,
        # nästlad händelseloop direkt från itemChanged-signalen visade
        # sig kunna krascha (access violation) vid snabb, upprepad
        # kryssning, så läsning sker alltid via Rust/LibreDWG i en
        # bakgrundstråd som pollas med en QTimer.
        self._dwg_attribute_results: dict[Path, DwgAttributeResult] = {}
        self._dwg_attribute_loading: set[Path] = set()
        self._last_dwg_load_summary: str | None = None
        self._model_attribute_results: dict[Path, DwgAttributeResult] = {}
        self._model_attribute_loading: set[Path] = set()
        self._model_attribute_cell_keys: dict[tuple[int, int], tuple[Path, str]] = {}
        self._model_attribute_original_values: dict[tuple[Path, str], str] = {}
        # PDF-metadatapanelen (valbar vattenstämpel). Listning och stämpeltext läses
        # i en bakgrundstråd (projektmappen ligger ofta på en långsam nätverksdisk);
        # en generationsräknare gör att resultat från en äldre laddning ignoreras.
        self._pdf_row_paths: dict[int, Path] = {}
        self._pdf_original_watermarks: dict[Path, str | None] = {}
        # Rader där användaren klickat i kryssrutan sedan senaste laddning;
        # behövs för att "kryssa ur och i" ska byta text på en stämplad PDF.
        self._pdf_touched_rows: set[int] = set()
        self._pdf_load_generation = 0
        self._pdf_load_pending: tuple[int, queue.Queue] | None = None
        self._pdf_load_timer = QTimer(self)
        self._pdf_load_timer.setInterval(100)
        self._pdf_load_timer.timeout.connect(self._poll_pdf_table_load)
        # Dokumentmetadata vid kryss i filträdet läses i bakgrunden med samma
        # mönster. ``refresh_table()`` är fortsatt synkron (spara-flöden, tester).
        self._metadata_load_generation = 0
        self._metadata_load_pending: tuple[int, list[Path], queue.Queue] | None = None
        self._metadata_load_timer = QTimer(self)
        self._metadata_load_timer.setInterval(50)
        self._metadata_load_timer.timeout.connect(self._poll_metadata_load)

        self._build_ui()
        self._restore_autocad_settings()
        self.root_input.setText(self._last_project_root())
        self._restore_splitter_states()
        # Trädet byggs rekursivt över hela projektmappen (samma beteende som
        # baslinjen). För stora mappar tar det tid, så laddningen skjuts upp
        # till efter att fönstret ritats ut — annars ser appen ut att hänga
        # vid start utan att något visas.
        QTimer.singleShot(0, self.refresh_folder)

    # ------------------------------------------------------------------
    # Uppbyggnad av gränssnittet
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        self.root_input = QLineEdit(str(Path.cwd()))
        self.browse_button = QPushButton("Välj mapp")
        self.browse_button.clicked.connect(self.choose_folder)
        self.refresh_button = QPushButton("Läs in")
        self.refresh_button.clicked.connect(self.refresh_folder)
        theme.set_role(self.refresh_button, "primary")

        top = QHBoxLayout()
        top.addWidget(QLabel("Projektmapp"))
        top.addWidget(self.root_input, 1)
        top.addWidget(self.browse_button)
        top.addWidget(self.refresh_button)
        self.autocad_button = QPushButton("AutoCAD…")
        self.autocad_button.setObjectName("autocadSettingsButton")
        self.autocad_button.clicked.connect(self.choose_autocad_installation)
        top.addWidget(self.autocad_button)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabel("Lokal filläsare")
        self.tree.itemChanged.connect(self._on_tree_item_changed)
        self.tree.itemExpanded.connect(self._on_tree_item_expanded)

        self.docx_table = MetadataTable()
        self.docx_table.setObjectName("docxMetadataTable")
        self.docx_table.cellClicked.connect(
            lambda row, column: self._open_pdf_for_metadata_row(self.docx_table, row, column)
        )
        self.table = self.docx_table

        self.xlsx_table = MetadataTable()
        self.xlsx_table.setObjectName("xlsxMetadataTable")
        self.xlsx_table.cellClicked.connect(
            lambda row, column: self._open_pdf_for_metadata_row(self.xlsx_table, row, column)
        )
        self._document_tables = {"docx": self.docx_table, "xlsx": self.xlsx_table}
        # Leveransförteckningar (DOCX i mallen med Leveranspaket m.fl.) får en
        # egen tabell och eget sparflöde; se metadata.grid.DELIVERY_LIST_CATEGORIES.
        self.delivery_table = MetadataTable()
        self.delivery_table.setObjectName("deliveryListMetadataTable")
        self.delivery_table.cellClicked.connect(
            lambda row, column: self._open_pdf_for_metadata_row(self.delivery_table, row, column)
        )
        self._document_tables["delivery"] = self.delivery_table
        self._delivery_list_paths: set[Path] = set()
        self.delivery_status = theme.set_role(QLabel("Inga DOCX-verifikat markerade."), "muted")
        self.docx_status = theme.set_role(QLabel("Markera DOCX-filer i den lokala filläsaren."), "muted")
        self.xlsx_status = theme.set_role(QLabel("Markera XLSX-filer i den lokala filläsaren."), "muted")

        self.status = theme.set_role(QLabel("Markera filer i explorer-trädet för multiredigering."), "muted")
        self.dwg_table = MetadataTable()
        self.dwg_table.setObjectName("dwgStandardMetadataTable")
        self.dwg_table.cellClicked.connect(
            lambda row, column: self._open_pdf_for_dwg_row(row, column)
        )
        self.dwg_status = theme.set_role(QLabel("Markera DWG-filer i explorer-trädet."), "muted")

        self.model_table = MetadataTable()
        self.model_table.setObjectName("dwgModelMetadataTable")
        self.model_status = theme.set_role(QLabel("Modellnamnruta: markera DWG-filer i explorer-trädet."), "muted")

        self.update_dwg_button = QPushButton("Uppdatera metadata")
        self.update_dwg_button.setObjectName("updateDwgMetadataButton")
        theme.set_role(self.update_dwg_button, "primary")
        self.update_dwg_button.setToolTip(
            "Skriver och verifierar ändrade namnrute-attribut och filnamnsbyten för markerade DWG-filer."
        )
        self.update_dwg_button.clicked.connect(self.write_dwg_changes)
        self.update_dwg_button.setEnabled(False)

        self.update_plot_dwg_button = QPushButton("Uppdatera metadata och skapa PDF")
        self.update_plot_dwg_button.setObjectName("updateAndPlotDwgButton")
        self.update_plot_dwg_button.setToolTip(
            "Skriver och verifierar ändrade namnrute-attribut och filnamnsbyten för markerade DWG-filer, "
            "och plottar sedan samma ritningar till flersidiga PDF-filer via AutoCAD Core Console.\n"
            "En backup-kopia sparas i den dolda mappen <projektmapp>/.datasamordning_backup/ innan skrivning,\n"
            "och skrivningen läses tillbaka och verifieras efteråt (återställs automatiskt om verifieringen misslyckas).\n"
            "Läsning sker automatiskt i bakgrunden (Rust/LibreDWG, parallellt) så fort DWG-filer kryssas i."
        )
        self.update_plot_dwg_button.clicked.connect(self.update_and_plot_dwgs)
        if not dwg_controller.is_rust_dwg_extractor_available():
            self.update_plot_dwg_button.setToolTip(
                self.update_plot_dwg_button.toolTip()
                + "\n\nOBS: rust_dwg_extractor.exe hittades inte – automatisk läsning av DWG-attribut "
                "kommer misslyckas (visas i statusraden). Bygg den med:\n"
                "cd rust_dwg_extractor && cargo build --release"
            )

        right_actions = QHBoxLayout()
        right_actions.addWidget(self.status, 1)

        docx_panel, docx_panel_layout = _card_panel(
            "docxMetadataPanel", "DOCX", "Ctrl+C/Ctrl+V stöds."
        )
        docx_panel_layout.addWidget(self.docx_table, 1)
        docx_panel_layout.addWidget(self.docx_status)
        docx_actions = QHBoxLayout()
        docx_actions.addStretch(1)
        self.save_docx_button = QPushButton("Uppdatera metadata")
        self.save_docx_button.setObjectName("saveDocxMetadataButton")
        theme.set_role(self.save_docx_button, "primary")
        self.save_docx_button.clicked.connect(lambda: self.save_metadata("docx"))
        self.save_docx_button.setEnabled(False)
        docx_actions.addWidget(self.save_docx_button)
        self.save_docx_pdf_button = QPushButton("Uppdatera metadata och skapa PDF")
        self.save_docx_pdf_button.setObjectName("saveDocxAndCreatePdfButton")
        self.save_docx_pdf_button.clicked.connect(
            lambda: self.save_metadata_and_create_pdfs("docx")
        )
        self.save_docx_pdf_button.setEnabled(False)
        docx_actions.addWidget(self.save_docx_pdf_button)
        docx_panel_layout.addLayout(docx_actions)

        delivery_panel, delivery_panel_layout = _card_panel(
            "deliveryListMetadataPanel",
            "DOCX-Verifikat",
            "DOCX-filer i verifikatmallen (Leveranspaket m.fl.). Ctrl+C/Ctrl+V stöds.",
        )
        delivery_panel_layout.addWidget(self.delivery_table, 1)
        delivery_panel_layout.addWidget(self.delivery_status)
        delivery_actions = QHBoxLayout()
        delivery_actions.addStretch(1)
        self.save_delivery_button = QPushButton("Uppdatera metadata")
        self.save_delivery_button.setObjectName("saveDeliveryListMetadataButton")
        theme.set_role(self.save_delivery_button, "primary")
        self.save_delivery_button.clicked.connect(lambda: self.save_metadata("delivery"))
        self.save_delivery_button.setEnabled(False)
        delivery_actions.addWidget(self.save_delivery_button)
        self.save_delivery_pdf_button = QPushButton("Uppdatera metadata och skapa PDF")
        self.save_delivery_pdf_button.setObjectName("saveDeliveryListAndCreatePdfButton")
        self.save_delivery_pdf_button.clicked.connect(lambda: self.save_metadata_and_create_pdfs("delivery"))
        self.save_delivery_pdf_button.setEnabled(False)
        delivery_actions.addWidget(self.save_delivery_pdf_button)
        delivery_panel_layout.addLayout(delivery_actions)
        # Rutan visas bara när minst en markerad DOCX följer mallen.
        delivery_panel.setVisible(False)
        self.delivery_panel = delivery_panel

        xlsx_panel, xlsx_panel_layout = _card_panel("xlsxMetadataPanel", "XLSX", "Ctrl+C/Ctrl+V stöds.")
        xlsx_panel_layout.addWidget(self.xlsx_table, 1)
        xlsx_panel_layout.addWidget(self.xlsx_status)
        xlsx_actions = QHBoxLayout()
        xlsx_actions.addStretch(1)
        self.save_xlsx_button = QPushButton("Uppdatera metadata")
        self.save_xlsx_button.setObjectName("saveXlsxMetadataButton")
        theme.set_role(self.save_xlsx_button, "primary")
        self.save_xlsx_button.clicked.connect(lambda: self.save_metadata("xlsx"))
        self.save_xlsx_button.setEnabled(False)
        xlsx_actions.addWidget(self.save_xlsx_button)
        self.save_xlsx_pdf_button = QPushButton("Uppdatera metadata och skapa PDF")
        self.save_xlsx_pdf_button.setObjectName("saveXlsxAndCreatePdfButton")
        self.save_xlsx_pdf_button.clicked.connect(
            lambda: self.save_metadata_and_create_pdfs("xlsx")
        )
        self.save_xlsx_pdf_button.setEnabled(False)
        xlsx_actions.addWidget(self.save_xlsx_pdf_button)
        xlsx_panel_layout.addLayout(xlsx_actions)

        self.pdf_table = MetadataTable()
        self.pdf_table.setObjectName("pdfMetadataTable")
        self.pdf_table.cellClicked.connect(self._open_pdf_for_pdf_row)
        self.pdf_table.itemChanged.connect(self._on_pdf_watermark_item_changed)
        self.pdf_status = theme.set_role(QLabel("Välj en projektmapp för att lista PDF-filer."), "muted")
        pdf_panel, pdf_panel_layout = _card_panel(
            "pdfMetadataPanel", "PDF", "Välj vattenstämpel och kryssa per PDF i projektmappen."
        )
        watermark_choice = QHBoxLayout()
        watermark_choice.addWidget(QLabel("Vattenstämpel:"))
        # Fritext; senaste text sparas i QSettings.
        self.pdf_watermark_text_input = QLineEdit()
        self.pdf_watermark_text_input.setObjectName("pdfWatermarkTextInput")
        self.pdf_watermark_text_input.setMaxLength(pdf_controller.MAX_TEXT_LENGTH)
        self.pdf_watermark_text_input.setText(
            self._settings.value(self._PDF_WATERMARK_TEXT_KEY, pdf_controller.WATERMARK_TEXT, type=str)
            or pdf_controller.WATERMARK_TEXT
        )
        self.pdf_watermark_text_input.setToolTip(
            "Text som läggs på de PDF:er som kryssas i "
            f"(högst {pdf_controller.MAX_TEXT_LENGTH} tecken: bokstäver inkl. ÅÄÖ, siffror och - . , : / ( ) & ! ? _ +)."
        )
        self.pdf_watermark_text_input.textChanged.connect(lambda _text: self._update_pdf_watermark_button())
        watermark_choice.addWidget(self.pdf_watermark_text_input, 1)
        pdf_panel_layout.addLayout(watermark_choice)
        pdf_panel_layout.addWidget(self.pdf_table, 1)
        pdf_panel_layout.addWidget(self.pdf_status)
        pdf_actions = QHBoxLayout()
        pdf_actions.addStretch(1)
        self.update_pdf_watermark_button = QPushButton("Uppdatera vattenstämpel")
        self.update_pdf_watermark_button.setObjectName("updatePdfWatermarkButton")
        theme.set_role(self.update_pdf_watermark_button, "primary")
        self.update_pdf_watermark_button.setToolTip(
            "Lägger den valda vattenstämpeln på PDF:er som kryssats i och tar bort stämpeln från PDF:er "
            "som kryssats ur. För att byta text på en redan stämplad PDF: kryssa ur och i igen.\n"
            "Filen läses tillbaka och kontrolleras innan originalet ersätts. Krypterade och digitalt "
            "signerade PDF:er ändras inte. En ny plottning/PDF-export skriver över PDF:en och därmed stämpeln."
        )
        self.update_pdf_watermark_button.clicked.connect(self.update_pdf_watermarks)
        self.update_pdf_watermark_button.setEnabled(False)
        pdf_actions.addWidget(self.update_pdf_watermark_button)
        pdf_panel_layout.addLayout(pdf_actions)

        dwg_panel, dwg_panel_layout = _card_panel(
            "dwgStandardMetadataPanel", "Standardnamnruta", "TRVJ_NAMNRUTA i markerade DWG-filer."
        )
        dwg_panel_layout.addWidget(self.dwg_table, 1)
        dwg_panel_layout.addWidget(self.dwg_status)
        dwg_actions = QHBoxLayout()
        dwg_actions.addStretch(1)
        dwg_actions.addWidget(self.update_dwg_button)
        dwg_actions.addWidget(self.update_plot_dwg_button)
        dwg_panel_layout.addLayout(dwg_actions)

        model_panel, model_panel_layout = _card_panel(
            "dwgModelMetadataPanel", "Modellnamnruta", "TRVJ_NAMNRUTA_MODELL i markerade DWG-filer."
        )
        model_panel_layout.addWidget(self.model_table, 1)
        model_panel_layout.addWidget(self.model_status)
        self.update_model_metadata_button = QPushButton("Uppdatera DWG-modellmetadata")
        self.update_model_metadata_button.setObjectName("updateDwgModelMetadataButton")
        theme.set_role(self.update_model_metadata_button, "primary")
        self.update_model_metadata_button.clicked.connect(self.update_dwg_model_metadata)
        self.update_model_metadata_button.setEnabled(False)
        model_actions = QHBoxLayout()
        model_actions.addStretch(1)
        model_actions.addWidget(self.update_model_metadata_button)
        model_panel_layout.addLayout(model_actions)

        self.acc_table = MetadataTable()
        self.acc_table.setObjectName("accMetadataTable")
        self.acc_table.setEditTriggers(QTableWidget.NoEditTriggers)
        acc_panel, acc_panel_layout = _card_panel(
            "accMetadataPanel", "ACC-metadata", "ACC-filläsare och metadataflöde är planerade men inte implementerade."
        )
        acc_panel_layout.addWidget(self.acc_table, 1)

        # Panelerna krymper med sin innehållsanpassade tabell (max 10 rader),
        # så splittern staplar dem uppifrån i stället för att fylla ut höjden.
        for panel, table in (
            (docx_panel, self.docx_table),
            (delivery_panel, self.delivery_table),
            (xlsx_panel, self.xlsx_table),
            (pdf_panel, self.pdf_table),
            (dwg_panel, self.dwg_table),
            (model_panel, self.model_table),
            (acc_panel, self.acc_table),
        ):
            table.fitted.connect(lambda panel=panel: _cap_panel_height(panel))
            _cap_panel_height(panel)

        self.pdf_preview_panel = QWidget()
        self.pdf_preview_panel.setObjectName("pdfPreviewPanel")
        pdf_preview_layout = QVBoxLayout(self.pdf_preview_panel)
        pdf_preview_layout.setContentsMargins(0, 0, 0, 0)
        pdf_preview_header = QHBoxLayout()
        pdf_preview_layout.addWidget(_section_heading("FÖRHANDSVISNING", "previewHeading"))
        self.pdf_preview_title = QLabel("PDF-förhandsvisning")
        pdf_preview_header.addWidget(self.pdf_preview_title, 1)
        self.pdf_preview_close_button = theme.set_role(QPushButton("×"), "subtle")
        self.pdf_preview_close_button.setAccessibleName("Stäng PDF-läsaren")
        self.pdf_preview_close_button.clicked.connect(self._close_pdf_preview)
        pdf_preview_header.addWidget(self.pdf_preview_close_button)
        pdf_preview_layout.addLayout(pdf_preview_header)
        self.pdf_preview_status = theme.set_role(
            QLabel("Klicka på ett metadatafält för att visa tillhörande PDF."), "muted"
        )
        pdf_preview_layout.addWidget(self.pdf_preview_status)
        self.pdf_document = QPdfDocument(self)
        self.pdf_document.statusChanged.connect(self._on_pdf_document_status_changed)
        self.pdf_view = ZoomablePdfView()
        self.pdf_view.setToolTip("Mushjul: zooma. Dra: panorera. Dubbelklick: anpassa till panelen.")
        self.pdf_view.setDocument(self.pdf_document)
        pdf_preview_layout.addWidget(self.pdf_view, 1)
        self.pdf_preview_panel.setMinimumWidth(300)

        # METADATAHANTERARE: en underflik per filfamilj i stället för en lång
        # lista av paneler. Panelobjekten och deras objectName är oförändrade,
        # så logik och tester som hittar tabeller via panelerna fungerar som
        # förut; bara placeringen skiljer sig.
        self.documents_splitter = _stacked_panel_splitter(
            "documentsSplitter", (docx_panel, delivery_panel, xlsx_panel, pdf_panel)
        )
        self.drawings_splitter = _stacked_panel_splitter(
            "drawingsSplitter", (dwg_panel, model_panel)
        )

        self.metadata_tabs = QTabWidget()
        self.metadata_tabs.setObjectName("metadataTabs")
        self.metadata_tabs.setDocumentMode(True)
        self.metadata_tabs.addTab(self.documents_splitter, "Dokument")
        self.metadata_tabs.addTab(self.drawings_splitter, "Ritningar och modeller")
        self.metadata_tabs.addTab(acc_panel, "ACC-leverans")
        self.metadata_tabs.setTabToolTip(0, "DOCX-, XLSX- och PDF-hantering")
        self.metadata_tabs.setTabToolTip(1, "DWG – standard- och modellnamnruta")
        self.metadata_tabs.setTabToolTip(2, "ACC-leverans – planerad, inte implementerad")

        right = QWidget()
        right.setObjectName("metadataManagerPanel")
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(_section_heading("METADATAHANTERARE", "metadataManagerHeading"))
        right_layout.addWidget(self.metadata_tabs, 1)
        right_layout.addLayout(right_actions)

        self.file_manager_panel = QWidget()
        self.file_manager_panel.setObjectName("fileManagerPanel")
        file_manager_layout = QVBoxLayout(self.file_manager_panel)
        file_manager_layout.setContentsMargins(0, 0, 0, 0)
        file_manager_layout.addWidget(_section_heading("FILHANTERARE", "fileManagerHeading"))
        file_manager_layout.addWidget(self.tree, 1)

        self.main_splitter = QSplitter(Qt.Horizontal)
        self.main_splitter.addWidget(self.file_manager_panel)
        self.main_splitter.addWidget(right)
        self.main_splitter.addWidget(self.pdf_preview_panel)
        self.pdf_preview_panel.hide()
        self.main_splitter.setStretchFactor(0, 28)
        self.main_splitter.setStretchFactor(1, 72)
        self.main_splitter.setStretchFactor(2, 1)

        main = QVBoxLayout(self)
        main.addLayout(top)
        main.addWidget(self.main_splitter, 1)

    def _restore_splitter_states(self) -> None:
        """Återställer användarens panelstorlekar från föregående session."""
        main_state = self._settings.value(self._MAIN_SPLITTER_KEY)
        if main_state is not None:
            self.main_splitter.restoreState(main_state)

        for key, splitter in (
            (self._DOCUMENTS_SPLITTER_KEY, self.documents_splitter),
            (self._DRAWINGS_SPLITTER_KEY, self.drawings_splitter),
        ):
            state = self._settings.value(key)
            if state is not None:
                splitter.restoreState(state)

        tab_index = self._settings.value(self._METADATA_TAB_KEY, 0, type=int)
        if 0 <= tab_index < self.metadata_tabs.count():
            self.metadata_tabs.setCurrentIndex(tab_index)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt override
        """Sparar projektmapp och panelindelning när fönstret stängs."""
        project_root = Path(self.root_input.text().strip()).expanduser()
        if project_root.is_dir():
            self._settings.setValue(
                self._LAST_PROJECT_ROOT_KEY,
                str(project_root.resolve()),
            )
        self._settings.setValue(
            self._MAIN_SPLITTER_KEY,
            self.main_splitter.saveState(),
        )
        self._settings.setValue(
            self._DOCUMENTS_SPLITTER_KEY,
            self.documents_splitter.saveState(),
        )
        self._settings.setValue(
            self._DRAWINGS_SPLITTER_KEY,
            self.drawings_splitter.saveState(),
        )
        self._settings.setValue(self._METADATA_TAB_KEY, self.metadata_tabs.currentIndex())
        self._settings.sync()
        if self._settings.status() != QSettings.Status.NoError:
            QMessageBox.warning(
                self,
                "Inställningar kunde inte sparas",
                "Projektmapp eller panelstorlekar kunde inte sparas. Kontrollera att "
                "användarens inställningsmapp är skrivbar.",
            )
        event.accept()

    def _last_project_root(self) -> str:
        """Återställer senaste befintliga projektmapp, annars aktuell mapp."""
        saved_root = self._settings.value(self._LAST_PROJECT_ROOT_KEY, "", type=str).strip()
        if saved_root and Path(saved_root).expanduser().is_dir():
            return str(Path(saved_root).expanduser().resolve())
        return str(Path.cwd())
