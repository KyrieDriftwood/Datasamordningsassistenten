"""Startpunkt för den aktiva applikationen.

Detta är den nya, TB-styrda motsvarigheten till baslinjens
``app_v5_desktop.py`` i .old/python_v5/app_v5_desktop.py.

Status: klar (Steg 4, Fas 4). Startar PySide6-fönstret i
``app/ui/main_window.py`` (som i sin tur pratar med
``app/controllers/*``) i stället för den frusna baslinjekopian.

Den frusna baslinjekopian finns kvar orörd som referens/fallback:
    .old/python_v5/.venv/Scripts/python.exe .old/python_v5/app_v5_desktop.py
"""

from __future__ import annotations

import sys


def main() -> None:
    """Startar applikationen: skapar QApplication, huvudfönstret och
    kör händelseloopen."""
    from PySide6.QtWidgets import QApplication

    from app.ui.main_window import MainWindow
    from app.ui.theme import apply_theme

    app = QApplication.instance() or QApplication(sys.argv)
    apply_theme(app)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
