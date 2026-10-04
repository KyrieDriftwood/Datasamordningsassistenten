"""Background execution helpers for the Qt UI.

The application deliberately uses ``threading.Thread`` plus Qt polling.
Keeping that contract in one small module prevents the main window from
owning the worker lifecycle and the presentation details simultaneously.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from typing import TypeVar

from PySide6.QtCore import QEventLoop, QTimer, Qt
from PySide6.QtWidgets import QProgressDialog, QWidget

T = TypeVar("T")


def run_dwg_task(parent: QWidget, title: str, total_files: int, work: Callable[[Callable[[int, int], None]], T]) -> T:
    if total_files <= 0:
        return work(lambda _done, _total: None)

    dialog = QProgressDialog(title, None, 0, 100, parent)
    dialog.setWindowModality(Qt.WindowModality.WindowModal)
    dialog.setWindowTitle("Bearbetar DWG-filer")
    dialog.setMinimumDuration(0)
    dialog.setAutoClose(True)
    dialog.setAutoReset(True)
    dialog.setMinimumWidth(480)
    font = dialog.font()
    font.setPointSize(font.pointSize() + 4)
    font.setBold(True)
    dialog.setFont(font)
    dialog.setLabelText(f"{title}\n0/{total_files} filer klara (0 %)")
    dialog.setValue(0)
    dialog.show()

    events: queue.Queue[tuple[str, object, object]] = queue.Queue()
    outcome: dict[str, object] = {}

    def worker() -> None:
        try:
            result = work(lambda done, total: events.put(("progress", done, total)))
        except Exception as exc:
            events.put(("error", exc, None))
        else:
            events.put(("result", result, None))

    thread = threading.Thread(target=worker, daemon=True)
    loop = QEventLoop()
    timer = QTimer(parent)
    timer.setInterval(80)

    def poll() -> None:
        while True:
            try:
                kind, first, second = events.get_nowait()
            except queue.Empty:
                break
            if kind == "progress":
                done, total = first, second
                percent = round(done / total * 100) if total else 100
                dialog.setLabelText(f"{title}\n{done}/{total} filer klara ({percent} %)")
                dialog.setValue(percent)
            else:
                outcome[kind] = first
                timer.stop()
                loop.quit()

    timer.timeout.connect(poll)
    timer.start()
    thread.start()
    loop.exec()
    thread.join()
    dialog.close()

    if "error" in outcome:
        raise outcome["error"]
    return outcome["result"]  # type: ignore[return-value]


def run_background_task(parent: QWidget, title: str, work: Callable[[], T]) -> T:
    dialog = QProgressDialog(title, None, 0, 0, parent)
    dialog.setWindowModality(Qt.WindowModality.WindowModal)
    dialog.setWindowTitle("Bearbetar dokument")
    dialog.setMinimumDuration(0)
    dialog.setAutoClose(False)
    dialog.setAutoReset(False)
    dialog.setMinimumWidth(480)
    dialog.show()

    events: queue.Queue[tuple[str, object]] = queue.Queue()
    outcome: dict[str, object] = {}

    def worker() -> None:
        try:
            events.put(("result", work()))
        except Exception as exc:
            events.put(("error", exc))

    thread = threading.Thread(target=worker, daemon=True)
    loop = QEventLoop()
    timer = QTimer(parent)
    timer.setInterval(80)

    def poll() -> None:
        try:
            kind, value = events.get_nowait()
        except queue.Empty:
            return
        outcome[kind] = value
        timer.stop()
        loop.quit()

    timer.timeout.connect(poll)
    timer.start()
    thread.start()
    loop.exec()
    thread.join()
    dialog.close()

    if "error" in outcome:
        raise outcome["error"]
    return outcome["result"]  # type: ignore[return-value]
