"""Tester för app/ui/theme.py (tema L-2, användarbeslut 2026-10-02)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QCheckBox, QLabel, QPushButton, QStyle, QStyleOptionButton, QWidget

from app.ui import theme


@pytest.fixture
def themed_app():
    """Tillämpar temat och återställer appens globala stil efteråt."""
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet("")
    previous = (app.style().name(), QPalette(app.palette()), app.font())
    theme.apply_theme(app)
    yield app
    style_name, palette, font = previous
    app.setStyleSheet("")
    app.setStyle(style_name)
    app.setPalette(palette)
    app.setFont(font)
    app.processEvents()


def test_theme_assets_exist_and_are_referenced_in_stylesheet():
    stylesheet = theme.build_stylesheet()
    for name in ("check.svg", "chevron_down.svg", "chevron_right.svg", "indeterminate.svg"):
        assert (theme.ASSETS_DIR / name).is_file()
        assert theme.asset_url(name) in stylesheet
    # Statusfärger per cell får aldrig skrivas över av en item-bakgrund.
    assert "::item {" not in stylesheet.replace("QTreeView::item {", "")


def test_status_colors_are_distinct_from_zebra_and_selection():
    neutral = {theme.ZEBRA.lower(), theme.SELECTION.lower(), theme.CARD.lower()}
    assert theme.STATUS_READONLY.lower() not in neutral
    assert theme.STATUS_WARNING.lower() not in neutral


def test_apply_theme_sets_fusion_palette_fonts_and_stylesheet(themed_app):
    assert themed_app.styleSheet() == theme.build_stylesheet()
    # Med stilmall returnerar style() en proxy utan namn; läs basstilen utan den.
    themed_app.setStyleSheet("")
    assert themed_app.style().name().lower() == "fusion"
    themed_app.setStyleSheet(theme.build_stylesheet())
    assert themed_app.palette().color(QPalette.Highlight) == QColor(theme.ACCENT)
    assert themed_app.palette().color(QPalette.Window) == QColor(theme.WINDOW)
    assert themed_app.font().families()[: len(theme.FONT_FAMILIES)] == list(theme.FONT_FAMILIES)
    assert "Garamond" in theme.DISPLAY_FONT


def test_apply_theme_fails_loudly_when_assets_are_missing(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(theme, "ASSETS_DIR", tmp_path)
    with pytest.raises(FileNotFoundError):
        theme.apply_theme(app)


def test_roles_render_card_primary_button_and_checked_box(themed_app):
    root = QWidget()
    root.resize(320, 160)
    card = theme.set_role(QWidget(root), "card")
    card.setGeometry(10, 10, 140, 60)
    assert card.testAttribute(Qt.WA_StyledBackground)
    primary = theme.set_role(QPushButton("Spara", root), "primary")
    primary.setGeometry(170, 10, 120, 32)
    checkbox = QCheckBox("Vald", root)
    checkbox.setChecked(True)
    checkbox.setGeometry(170, 60, 120, 24)
    heading = theme.set_role(QLabel("RUBRIK", root), "sectionHeading")
    heading.setGeometry(10, 90, 200, 40)
    root.show()
    themed_app.processEvents()

    image = root.grab().toImage()
    assert QColor(image.pixel(card.geometry().center())).name() == theme.CARD
    assert QColor(image.pixel(primary.geometry().left() + 6, primary.geometry().center().y())).name() == theme.ACCENT

    option = QStyleOptionButton()
    option.initFrom(checkbox)
    indicator = checkbox.style().subElementRect(QStyle.SE_CheckBoxIndicator, option, checkbox)
    corner = checkbox.mapTo(root, indicator.topLeft()) + indicator.center() - indicator.topLeft()
    # Ikryssad ruta fylls med accentfärg (vit bock i mitten, därför 3 px in).
    sample = QColor(image.pixel(corner.x() - indicator.width() // 2 + 3, corner.y()))
    assert sample.name() == theme.ACCENT
    root.close()
