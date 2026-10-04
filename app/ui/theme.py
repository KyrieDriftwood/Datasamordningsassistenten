"""Gemensamt visuellt tema: professionell Windows 11-känsla (L-2, användarbeslut).

Användarbeslut 2026-10-02, utanför TB: hela appen ritas med Qt-stilen
``Fusion`` plus en stilmall som efterliknar Windows 11 (Fluent) – ljusgrå
fönsteryta, vita kort, 4 px rundning, en accentfärg och Utforskarens ljusblå
markering. Se docs/plan.md ("Tema 2026-10-02").

Kontrakt för andra moduler:

* Alla färger och mått finns som konstanter här; hårdkoda dem inte i vyer.
* Vyer markerar roller med Qt-egenskapen ``role`` (``set_role``) i stället för
  egna stilmallar: ``card``, ``primary``, ``sectionHeading``, ``panelTitle``,
  ``muted`` och ``subtle``. Stilmallen väljer på ``[role="..."]``.

Invarianser / riskgränser:

* Basstilen måste vara ``Fusion``. Den inbyggda ``windows11``-stilen faller
  tillbaka till en enklare ritning så snart en widget får en stilmall, och då
  försvann bl.a. ikryssade rutor i tabeller (verifierat 2026-10-02).
* Statusfärger sätts per cell (BackgroundRole) av main_window och ska gå före
  zebran; sätt därför aldrig ``background`` på ``::item`` i stilmallen.
* Ikonerna ligger som SVG-filer i ``theme_assets/`` och refereras med absolut
  sökväg; de måste följa med vid paketering (pyproject: package-data).
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication, QStyleFactory, QWidget

ASSETS_DIR = Path(__file__).resolve().parent / "theme_assets"

# Färger (Windows 11 ljust tema).
ACCENT = "#005fb8"
ACCENT_HOVER = "#196ebf"
ACCENT_PRESSED = "#3c82c8"
ACCENT_BORDER = "#0a5aa8"
WINDOW = "#f3f3f3"
CARD = "#ffffff"
CARD_STROKE = "#e5e5e5"
CONTROL = "#fbfbfb"
CONTROL_HOVER = "#f6f6f6"
CONTROL_PRESSED = "#f0f0f0"
CONTROL_STROKE_BOTTOM = "#d1d1d1"
INPUT_STROKE_BOTTOM = "#868686"
INDICATOR_STROKE = "#8a8a8a"
TEXT = "#1a1a1a"
TEXT_MUTED = "#5f5f5f"
TEXT_DISABLED = "#a0a0a0"
SELECTION = "#cce8ff"
SELECTION_HOVER = "#e5f3ff"
ZEBRA = "#f7f7f7"
GRID = "#e6e6e6"
HEADER_STROKE = "#d6d6d6"
# Statusbakgrund per cell. Måste skilja sig tydligt från ZEBRA och SELECTION.
STATUS_READONLY = "#e3e3e3"
STATUS_WARNING = "#fff4ce"

# Mått.
RADIUS = 4
CARD_RADIUS = 8
SPACING = 8
CARD_PADDING = 12

# Typografi (användarbeslut 2026-10-02): Helvetica Neue för brödtext och
# kontroller, Garamond för rubriker. Helvetica Neue finns sällan på Windows;
# då används Helvetica och sedan Arial (metriskt likvärdig). Typsnitten
# licensieras separat och buntas därför inte med appen.
FONT_FAMILIES = ("Helvetica Neue", "Helvetica", "Arial", "Segoe UI")
FONT_POINT_SIZE = 9
DISPLAY_FONT = '"Garamond", "EB Garamond", "Georgia", serif'


def asset_url(name: str) -> str:
    """Sökväg till en temaikon i det format Qt-stilmallar förväntar sig."""
    return (ASSETS_DIR / name).as_posix()


def set_role(widget: QWidget, role: str) -> QWidget:
    """Ger widgeten en temaroll och returnerar den (för kedjning i vyer)."""
    widget.setProperty("role", role)
    if role == "card":
        # Vanliga QWidget ritar inte stilmallens bakgrund utan detta attribut.
        widget.setAttribute(Qt.WA_StyledBackground, True)
    return widget


def build_palette() -> QPalette:
    palette = QPalette()
    roles = {
        QPalette.Window: WINDOW,
        QPalette.WindowText: TEXT,
        QPalette.Base: CARD,
        QPalette.AlternateBase: ZEBRA,
        QPalette.Text: TEXT,
        QPalette.Button: CONTROL,
        QPalette.ButtonText: TEXT,
        QPalette.Highlight: ACCENT,
        QPalette.HighlightedText: "#ffffff",
        QPalette.ToolTipBase: CARD,
        QPalette.ToolTipText: TEXT,
        QPalette.PlaceholderText: TEXT_MUTED,
        QPalette.Link: ACCENT,
        QPalette.Accent: ACCENT,
    }
    for role, color in roles.items():
        palette.setColor(role, QColor(color))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        palette.setColor(QPalette.Disabled, role, QColor(TEXT_DISABLED))
    return palette


def build_stylesheet() -> str:
    check = asset_url("check.svg")
    indeterminate = asset_url("indeterminate.svg")
    chevron_down = asset_url("chevron_down.svg")
    chevron_right = asset_url("chevron_right.svg")
    return f"""
QWidget {{ color: {TEXT}; }}
QToolTip {{ background: {CARD}; color: {TEXT}; border: 1px solid {CARD_STROKE}; padding: 4px 6px; }}

*[role="card"] {{ background: {CARD}; border: 1px solid {CARD_STROKE}; border-radius: {CARD_RADIUS}px; }}
QLabel[role="sectionHeading"] {{ font-family: {DISPLAY_FONT}; font-size: 15pt; font-weight: 700; padding: 2px 2px 4px 2px; }}
QLabel[role="panelTitle"] {{ font-family: {DISPLAY_FONT}; font-size: 12.5pt; font-weight: 700; }}
QLabel[role="muted"] {{ color: {TEXT_MUTED}; }}

QPushButton {{
    background: {CONTROL}; border: 1px solid {CARD_STROKE}; border-bottom-color: {CONTROL_STROKE_BOTTOM};
    border-radius: {RADIUS}px; padding: 5px 14px; min-height: 20px;
}}
QPushButton:hover {{ background: {CONTROL_HOVER}; }}
QPushButton:pressed {{ background: {CONTROL_PRESSED}; color: {TEXT_MUTED}; }}
QPushButton:focus {{ border: 1px solid {ACCENT}; }}
QPushButton[role="primary"] {{
    background: {ACCENT}; color: #ffffff; border: 1px solid {ACCENT_BORDER}; border-bottom-color: #003e7a;
}}
QPushButton[role="primary"]:hover {{ background: {ACCENT_HOVER}; }}
QPushButton[role="primary"]:pressed {{ background: {ACCENT_PRESSED}; color: #e6f0fa; }}
QPushButton:disabled, QPushButton[role="primary"]:disabled {{
    background: #f5f5f5; color: {TEXT_DISABLED}; border: 1px solid {CARD_STROKE};
}}
QPushButton[role="subtle"] {{ background: transparent; border: 1px solid transparent; padding: 3px 8px; }}
QPushButton[role="subtle"]:hover {{ background: #eaeaea; }}

QLineEdit, QComboBox {{
    background: #ffffff; border: 1px solid {CARD_STROKE}; border-bottom: 1px solid {INPUT_STROKE_BOTTOM};
    border-radius: {RADIUS}px; padding: 4px 8px; min-height: 20px;
    selection-background-color: {ACCENT}; selection-color: #ffffff;
}}
QLineEdit:hover, QComboBox:hover {{ background: #fdfdfd; }}
QLineEdit:focus, QComboBox:focus {{ border-bottom: 2px solid {ACCENT}; padding-bottom: 3px; }}
QLineEdit:disabled {{ color: {TEXT_DISABLED}; background: #f5f5f5; }}
QComboBox::drop-down {{ border: none; width: 26px; }}
QComboBox::down-arrow {{ image: url({chevron_down}); width: 10px; height: 10px; }}

QCheckBox, QRadioButton {{ spacing: 8px; }}
QCheckBox::indicator, QAbstractItemView::indicator {{
    width: 16px; height: 16px; border: 1px solid {INDICATOR_STROKE}; border-radius: {RADIUS}px; background: #f7f7f7;
}}
QCheckBox::indicator:hover, QAbstractItemView::indicator:hover {{ background: #efefef; }}
QCheckBox::indicator:checked, QAbstractItemView::indicator:checked {{
    background: {ACCENT}; border-color: {ACCENT}; image: url({check});
}}
QCheckBox::indicator:indeterminate, QAbstractItemView::indicator:indeterminate {{
    background: {ACCENT}; border-color: {ACCENT}; image: url({indeterminate});
}}
QCheckBox::indicator:disabled, QAbstractItemView::indicator:disabled {{ background: #f0f0f0; border-color: #c8c8c8; }}
QRadioButton::indicator {{
    width: 18px; height: 18px; border: 1px solid {INDICATOR_STROKE}; border-radius: 9px; background: #f7f7f7;
}}
QRadioButton::indicator:checked {{ border: 5px solid {ACCENT}; width: 8px; height: 8px; background: #ffffff; }}

QTabWidget::pane {{ border: none; }}
QTabBar::tab {{
    background: transparent; border: none; padding: 6px 12px 8px 12px; margin-right: 2px;
    color: {TEXT_MUTED}; border-radius: {RADIUS}px;
}}
QTabBar::tab:hover {{ background: #eaeaea; color: {TEXT}; }}
QTabBar::tab:selected {{
    color: {TEXT}; font-weight: 600; border-bottom: 3px solid {ACCENT};
    border-bottom-left-radius: 0; border-bottom-right-radius: 0;
}}

QTreeView {{
    background: {CARD}; border: 1px solid {CARD_STROKE}; border-radius: {CARD_RADIUS}px; padding: 4px;
    selection-background-color: {SELECTION}; selection-color: {TEXT}; show-decoration-selected: 1;
}}
QTreeView::item {{ padding: 3px 2px; border-radius: {RADIUS}px; }}
QTreeView::item:hover {{ background: {SELECTION_HOVER}; }}
QTreeView::item:selected {{ background: {SELECTION}; color: {TEXT}; }}
QTreeView::branch:has-children:!has-siblings:closed, QTreeView::branch:closed:has-children:has-siblings {{
    image: url({chevron_right});
}}
QTreeView::branch:open:has-children:!has-siblings, QTreeView::branch:open:has-children:has-siblings {{
    image: url({chevron_down});
}}

QHeaderView::section {{
    background: {CARD}; color: {TEXT_MUTED}; padding: 6px 8px;
    border: none; border-right: 1px solid {GRID}; border-bottom: 1px solid {HEADER_STROKE};
}}
/* Fetstil sätts bara på trädets rubrik. En typsnittsegenskap i stilmallen
   skulle trumfa per-rubrik-typsnitt (FontRole), som MetadataTable använder
   för att krympa rubriker som är bredare än sin data. MetadataTable sätter
   i stället fetstil via QHeaderView.setFont. */
QTreeView QHeaderView::section {{ font-weight: 600; }}
QTableCornerButton::section {{ background: {CARD}; border: none; border-bottom: 1px solid {HEADER_STROKE}; }}

QSplitter::handle {{ background: transparent; }}
QSplitter::handle:horizontal {{ width: 8px; }}
QSplitter::handle:vertical {{ height: 8px; }}
QSplitter::handle:hover {{ background: #e0e0e0; }}

QScrollBar:vertical {{ background: transparent; width: 12px; margin: 2px; }}
QScrollBar:horizontal {{ background: transparent; height: 12px; margin: 2px; }}
QScrollBar::handle {{ background: #c2c2c2; border-radius: 4px; min-height: 24px; min-width: 24px; }}
QScrollBar::handle:hover {{ background: #9e9e9e; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
"""


def build_font() -> QFont:
    font = QFont()
    font.setFamilies(list(FONT_FAMILIES))
    font.setPointSize(FONT_POINT_SIZE)
    return font


def apply_theme(app: QApplication) -> None:
    """Sätter Fusion, palett, typsnitt och stilmall på hela applikationen."""
    style = QStyleFactory.create("Fusion")
    if style is None:
        raise RuntimeError("Qt-stilen Fusion saknas; temat kan inte tillämpas.")
    missing = [name for name in ("check.svg", "chevron_down.svg", "chevron_right.svg", "indeterminate.svg")
               if not (ASSETS_DIR / name).is_file()]
    if missing:
        raise FileNotFoundError(f"Temaikoner saknas i {ASSETS_DIR}: {', '.join(missing)}")
    app.setStyle(style)
    app.setPalette(build_palette())
    app.setFont(build_font())
    app.setStyleSheet(build_stylesheet())
