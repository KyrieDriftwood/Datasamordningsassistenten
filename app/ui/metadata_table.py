"""Metadatatabellen med Ctrl+C/Ctrl+V och innehållsanpassad storlek.

Utbruten ur ``app/ui/main_window.py`` 2026-10-02 (utanför TB); main_window
re-exporterar namnen för bakåtkompatibilitet.
"""

from __future__ import annotations


from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFont, QKeySequence
from PySide6.QtWidgets import QApplication, QHeaderView, QStyledItemDelegate, QTableWidget, QWidget

from app.ui import theme


# Tabellestetik (L-2, användarbeslut utanför TB): svag zebra + ljust grid enligt
# Hellmuths "Ultimate Guide to Designing Data Tables", färger från app/ui/theme.py.
# Invarians: zebratonen måste ligga långt ifrån statusfärgerna som sätts per
# cell (Qt.lightGray = ej redigerbar, Qt.yellow = läsfel), och markeringen är
# ljus så att statusfärgen syns även på markerad rad. Sätt aldrig
# ``background`` på ``::item`` – det skulle skriva över cellernas statusbakgrund
# (BackgroundRole).
METADATA_TABLE_ZEBRA_COLOR = theme.ZEBRA
METADATA_TABLE_GRID_COLOR = theme.GRID
METADATA_TABLE_SELECTION_COLOR = theme.SELECTION
METADATA_TABLE_STYLESHEET = f"""
QTableWidget {{
    background-color: {theme.CARD};
    alternate-background-color: {METADATA_TABLE_ZEBRA_COLOR};
    gridline-color: {METADATA_TABLE_GRID_COLOR};
    selection-background-color: {METADATA_TABLE_SELECTION_COLOR};
    selection-color: {theme.TEXT};
    border: 1px solid {theme.CARD_STROKE};
    border-radius: {theme.RADIUS}px;
}}
QTableWidget[empty="true"] {{
    background-color: {theme.STATUS_READONLY};
    alternate-background-color: {theme.STATUS_READONLY};
}}
QTableWidget::item {{
    padding: 3px 8px;
}}
QTableWidget QHeaderView::section {{
    background-color: {theme.CARD};
    color: {theme.TEXT_MUTED};
    padding: 6px 8px;
    border: none;
    border-right: 1px solid {METADATA_TABLE_GRID_COLOR};
    border-bottom: 1px solid {theme.HEADER_STROKE};
}}
"""


class _IdentifierColumnDelegate(QStyledItemDelegate):
    """Fetstil för radens identifierare (kolumn 0: dokument-/filnamn)."""

    def initStyleOption(self, option, index) -> None:  # noqa: N802 - Qt override
        super().initStyleOption(option, index)
        option.font.setBold(True)


class MetadataTable(QTableWidget):
    """Tabell med Ctrl+C/Ctrl+V-stöd för blockmarkerad kopiering/inklistring.

    Direkt port av ``app_v4_desktop.py:MetadataTable``. Äger även den
    gemensamma tabellestetiken så att alla metadataflikar ser likadana ut.

    Innehållsanpassad storlek (L-2, användarbeslut 2026-10-02, utanför TB):
    kolumnerna blir exakt så breda som sitt innehåll (data och rubrik) och
    tabellens höjd följer antalet rader, högst ``MAX_VISIBLE_ROWS``; fler rader
    rullas. Anpassningen körs automatiskt (debouncad) vid varje modelländring
    och signalerar ``fitted`` så att omgivande panel kan krympa med tabellen.
    Användaren kan fortfarande dra kolumnbredder; nästa dataändring anpassar om.
    Utan inlästa rader är tabellen en enda grå rad utan rubrikrad
    (egenskapen ``empty`` styr färgen i METADATA_TABLE_STYLESHEET).
    """

    MAX_VISIBLE_ROWS = 10
    fitted = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAlternatingRowColors(True)
        self.setShowGrid(True)
        self.setStyleSheet(METADATA_TABLE_STYLESHEET)
        # Delegaten överlever clear()/setColumnCount() eftersom den är knuten
        # till kolumnindex, inte till items.
        self._identifier_delegate = _IdentifierColumnDelegate(self)
        self.setItemDelegateForColumn(0, self._identifier_delegate)
        header = self.horizontalHeader()
        # Rubriker ska linjera med kolumndatan, som är vänsterställd text.
        header.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        # Interactive + explicit anpassning i stället för ResizeToContents:
        # samma bredd, men användaren kan dra och det räknas inte om per ritning.
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setStretchLastSection(False)
        header_font = QFont(header.font())
        header_font.setWeight(QFont.DemiBold)
        header.setFont(header_font)
        self._adjusting_header_fonts = False
        self.verticalHeader().setDefaultSectionSize(
            max(self.verticalHeader().defaultSectionSize(), self.fontMetrics().height() + 12)
        )

        self._fit_timer = QTimer(self)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.setInterval(0)
        self._fit_timer.timeout.connect(self.fit_to_contents)
        self._reorder_pending = False
        model = self.model()
        # Strukturella ändringar = tabellen har lästs in på nytt -> sortera om
        # kolumnerna. Rena celländringar (användarredigering, inklistring)
        # anpassar bara storleken, så att en kolumn inte hoppar under redigering.
        for signal in (
            model.rowsInserted,
            model.rowsRemoved,
            model.columnsInserted,
            model.columnsRemoved,
            model.modelReset,
            model.layoutChanged,
            model.headerDataChanged,
        ):
            signal.connect(self._schedule_reorder_and_fit)
        model.dataChanged.connect(self._schedule_fit)
        self.fit_to_contents()

    def _schedule_fit(self, *_args) -> None:
        self._fit_timer.start()

    def _schedule_reorder_and_fit(self, *_args) -> None:
        if self._adjusting_header_fonts:
            return
        self._reorder_pending = True
        self._fit_timer.start()

    def fit_to_contents(self) -> None:
        """Anpassar kolumnbredder efter innehåll och höjden efter radantal (max 10)."""
        self._fit_timer.stop()
        if self._reorder_pending:
            self._reorder_pending = False
            self.move_empty_columns_last()
        self._apply_empty_state()
        self._shrink_wide_header_labels()
        self.resizeColumnsToContents()
        self._apply_fitted_height()
        self.fitted.emit()

    HEADER_SHRINK_FACTOR = 0.75

    def _shrink_wide_header_labels(self) -> None:
        """Krymper rubrikens textstorlek till 75 % där rubriken är bredare än kolumnens data.

        Användarbeslut 2026-10-02, utanför TB: kolumnbredden ska styras av
        metadatan, inte av rubriken. Jämförelsen görs alltid med rubrikens
        normalstorlek, så en kolumn får tillbaka full rubrik när datan växer.
        Typsnittet sätts per rubrik (FontRole); därför får stilmallarna inte
        ange typsnitt för metadatatabellens rubriker.
        """
        header = self.horizontalHeader()
        small_font = QFont(header.font())
        if small_font.pointSizeF() > 0:
            small_font.setPointSizeF(small_font.pointSizeF() * self.HEADER_SHRINK_FACTOR)
        else:
            small_font.setPixelSize(max(1, round(small_font.pixelSize() * self.HEADER_SHRINK_FACTOR)))
        # setFont/setData på rubrikobjekt ger headerDataChanged; spärren
        # hindrar att det tolkas som ny inläsning (omsortering + ny fit-loop).
        self._adjusting_header_fonts = True
        try:
            for column in range(self.columnCount()):
                item = self.horizontalHeaderItem(column)
                if item is None:
                    continue
                if item.data(Qt.FontRole) is not None:
                    item.setData(Qt.FontRole, None)
                if self.rowCount() == 0:
                    continue
                if header.sectionSizeFromContents(column).width() > self.sizeHintForColumn(column):
                    item.setFont(small_font)
        finally:
            self._adjusting_header_fonts = False

    def column_is_empty(self, column: int) -> bool:
        """Sant om ingen rad har text eller kryssruta i kolumnen."""
        if self.rowCount() == 0:
            return False
        for row in range(self.rowCount()):
            item = self.item(row, column)
            if item is None:
                continue
            if item.text().strip() or item.data(Qt.CheckStateRole) is not None:
                return False
        return True

    def move_empty_columns_last(self) -> None:
        """Flyttar helt tomma metadatakolumner längst till höger (användarbeslut, utanför TB).

        Flytten är visuell (``QHeaderView.moveSection``): logiska kolumnindex,
        som all läs-/sparlogik i MainWindow är nycklad på, ändras inte. Kolumn 0
        (identifierare) står alltid först; inbördes ordning behålls i båda grupperna.
        """
        header = self.horizontalHeader()
        columns = range(1, self.columnCount())
        order = [0] + sorted(columns, key=lambda column: (self.column_is_empty(column), column))
        if self.columnCount() == 0:
            order = []
        for target_visual, logical in enumerate(order):
            current_visual = header.visualIndex(logical)
            if current_visual != target_visual:
                header.moveSection(current_visual, target_visual)

    def is_empty(self) -> bool:
        return self.rowCount() == 0

    def _apply_empty_state(self) -> None:
        empty = self.is_empty()
        if self.property("empty") == empty:
            return
        self.setProperty("empty", empty)
        self.horizontalHeader().setVisible(not empty)
        # Egenskapsselektorer i stylesheet utvärderas bara vid polish.
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def fitted_height(self) -> int:
        """Höjd för rubrikrad + 1–``MAX_VISIBLE_ROWS`` rader; tom tabell = en rad."""
        if self.is_empty():
            return self.verticalHeader().defaultSectionSize() + 2 * self.frameWidth()
        visible_rows = min(self.rowCount(), self.MAX_VISIBLE_ROWS)
        rows_height = sum(self.rowHeight(row) for row in range(visible_rows))
        header_height = self.horizontalHeader().sizeHint().height()
        height = rows_height + header_height + 2 * self.frameWidth()
        if self._needs_horizontal_scrollbar():
            height += self.horizontalScrollBar().sizeHint().height()
        return height

    def _needs_horizontal_scrollbar(self) -> bool:
        available = self.width() - 2 * self.frameWidth()
        vertical_header = self.verticalHeader()
        if not vertical_header.isHidden():
            available -= vertical_header.sizeHint().width()
        if self.rowCount() > self.MAX_VISIBLE_ROWS:
            available -= self.verticalScrollBar().sizeHint().width()
        return self.horizontalHeader().length() > available

    def _apply_fitted_height(self) -> None:
        height = self.fitted_height()
        if self.minimumHeight() != height or self.maximumHeight() != height:
            self.setFixedHeight(height)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        # Bredden avgör om horisontell rullist behövs och därmed höjden.
        if event.size().width() != event.oldSize().width():
            self._apply_fitted_height()

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.matches(QKeySequence.Copy):
            self._copy_selection()
            return
        if event.matches(QKeySequence.Paste):
            self._paste_selection()
            return
        super().keyPressEvent(event)

    def _copy_selection(self) -> None:
        # Visuell ordning: tomma kolumner kan vara flyttade (move_empty_columns_last),
        # så kopierat block ska motsvara det användaren ser, inte logiska index.
        indexes = self.selectedIndexes()
        if not indexes:
            return
        header = self.horizontalHeader()
        rows = [index.row() for index in indexes]
        visual_columns = [header.visualIndex(index.column()) for index in indexes]
        lines: list[str] = []
        for row in range(min(rows), max(rows) + 1):
            values: list[str] = []
            for visual in range(min(visual_columns), max(visual_columns) + 1):
                item = self.item(row, header.logicalIndex(visual))
                values.append(item.text() if item is not None else "")
            lines.append("\t".join(values))
        QApplication.clipboard().setText("\n".join(lines))

    def _paste_selection(self) -> None:
        current = self.currentItem()
        if current is None:
            return
        header = self.horizontalHeader()
        start_row = current.row()
        start_visual = header.visualIndex(current.column())
        text = QApplication.clipboard().text()
        for row_offset, line in enumerate(text.splitlines()):
            target_row = start_row + row_offset
            if target_row >= self.rowCount():
                break
            for column_offset, value in enumerate(line.split("\t")):
                target_visual = start_visual + column_offset
                if target_visual >= self.columnCount():
                    continue
                target_column = header.logicalIndex(target_visual)
                if target_column == 0:
                    continue
                item = self.item(target_row, target_column)
                if item is None or not item.flags() & Qt.ItemIsEditable:
                    continue
                item.setText(value)
