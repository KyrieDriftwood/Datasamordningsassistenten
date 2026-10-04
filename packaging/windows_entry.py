"""Windows entry point (G/Y, user decision 2026-10-04, outside TB).

The packaged self-test uses no project files, Office or AutoCAD.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def self_test(report: Path) -> None:
    from PySide6.QtPdf import QPdfDocument
    from PySide6.QtWidgets import QApplication

    from app.ui.main_window import MainWindow
    from app.ui.theme import ASSETS_DIR, apply_theme
    from dwg.pdf_plotter import locate_pdf_backend
    from dwg.rust_bridge import locate_rust_extractor

    app = QApplication.instance() or QApplication([])
    apply_theme(app)
    pdf_document = QPdfDocument(app)
    icons = sorted(path.name for path in ASSETS_DIR.glob("*.svg"))
    if not icons:
        raise RuntimeError("Packaged theme icons are missing.")
    reader = locate_rust_extractor()
    backend = locate_pdf_backend()
    for executable in (reader, backend):
        # No arguments must reach Rust's input-validation error (exit 2),
        # rather than a Windows loader failure caused by a missing DLL.
        result = subprocess.run(
            [str(executable)],
            capture_output=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 2 or not result.stderr:
            raise RuntimeError(
                f"Native runtime failed to load: {executable.name}, exit {result.returncode}"
            )
    report.write_text(
        json.dumps(
            {
                "frozen": bool(getattr(sys, "frozen", False)),
                "window_class": MainWindow.__name__,
                "pdf_status": pdf_document.status().name,
                "icons": icons,
                "dwg_reader": str(reader),
                "dwg_pdf_backend": str(backend),
            },
            indent=2,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--self-test":
        self_test(Path(sys.argv[2]).resolve())
    else:
        from app.main import main

        main()
