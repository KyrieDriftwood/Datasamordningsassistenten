"""PDF-förhandsvisning med autopassning och zoom med mushjulet.

Beteende (användarbeslut 2026-10-02):

* En ny PDF visas alltid anpassad så att hela sidan syns i panelen
  (``FitInView``), och följer med när panelen ändrar storlek.
* Mushjulet zoomar mot muspekaren, som i CAD-program. Shift+hjul
  rullar i sidled och Ctrl+hjul rullar uppåt/nedåt.
* Vänster musknapp + dra panorerar. Dubbelklick återgår till anpassad vy.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent, QWheelEvent
from PySide6.QtPdfWidgets import QPdfView

MIN_ZOOM = 0.05
MAX_ZOOM = 20.0
# Zoomsteg per hjulsteg (120 vinkelenheter = ett "klick").
_ZOOM_STEP = 1.2


class ZoomablePdfView(QPdfView):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setPageMode(QPdfView.PageMode.MultiPage)
        self._pan_origin: QPoint | None = None
        self.fit_to_view()

    def fit_to_view(self) -> None:
        self.setZoomMode(QPdfView.ZoomMode.FitInView)

    def effective_zoom_factor(self) -> float:
        """Faktisk zoomfaktor, även i FitInView-läget.

        ``zoomFactor()`` uppdateras inte av Qt i anpassningslägena, så
        faktorn räknas om på samma sätt som QPdfView gör: sidans storlek i
        pixlar (punkter × skärm-dpi / 72) skalad in i viewporten minus
        dokumentmarginalerna.
        """
        if self.zoomMode() == QPdfView.ZoomMode.Custom:
            return self.zoomFactor()
        document = self.document()
        if document is None or document.pageCount() == 0:
            return self.zoomFactor()
        page = self.pageNavigator().currentPage() if self.pageNavigator() else 0
        point_size = document.pagePointSize(max(0, page))
        resolution = self.logicalDpiX() / 72.0
        page_width = point_size.width() * resolution
        page_height = point_size.height() * resolution
        if page_width <= 0 or page_height <= 0:
            return self.zoomFactor()
        margins = self.documentMargins()
        available_width = self.viewport().width() - margins.left() - margins.right()
        available_height = self.viewport().height() - margins.top() - margins.bottom()
        if self.zoomMode() == QPdfView.ZoomMode.FitToWidth:
            return max(available_width / page_width, MIN_ZOOM)
        return max(min(available_width / page_width, available_height / page_height), MIN_ZOOM)

    def zoom_at(self, factor: float, anchor: QPointF) -> None:
        """Zoomar med ``factor`` och håller punkten under ``anchor`` stilla."""
        old_zoom = self.effective_zoom_factor()
        new_zoom = min(max(old_zoom * factor, MIN_ZOOM), MAX_ZOOM)
        if new_zoom == old_zoom:
            return
        horizontal = self.horizontalScrollBar()
        vertical = self.verticalScrollBar()
        content_x = horizontal.value() + anchor.x()
        content_y = vertical.value() + anchor.y()
        scale = new_zoom / old_zoom
        self.setZoomMode(QPdfView.ZoomMode.Custom)
        self.setZoomFactor(new_zoom)
        horizontal.setValue(round(content_x * scale - anchor.x()))
        vertical.setValue(round(content_y * scale - anchor.y()))

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802 - Qt override
        modifiers = event.modifiers()
        if modifiers & (Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.ControlModifier):
            if modifiers & Qt.KeyboardModifier.ShiftModifier:
                bar = self.horizontalScrollBar()
                delta = event.angleDelta().y() or event.angleDelta().x()
            else:
                bar = self.verticalScrollBar()
                delta = event.angleDelta().y()
            bar.setValue(bar.value() - round(delta / 120 * bar.singleStep() * 3))
            event.accept()
            return
        steps = event.angleDelta().y() / 120
        if steps == 0 or self.document() is None:
            event.ignore()
            return
        self.zoom_at(_ZOOM_STEP**steps, event.position())
        event.accept()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.MouseButton.LeftButton:
            self._pan_origin = event.position().toPoint()
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        if self._pan_origin is not None:
            position = event.position().toPoint()
            delta = position - self._pan_origin
            self._pan_origin = position
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - delta.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - delta.y())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.MouseButton.LeftButton and self._pan_origin is not None:
            self._pan_origin = None
            self.viewport().unsetCursor()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.MouseButton.LeftButton:
            self.fit_to_view()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)
