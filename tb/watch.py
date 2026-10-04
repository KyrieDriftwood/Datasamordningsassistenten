"""Filbevakning av TB-handlingen.

Kravkälla: TB-sektion BBB (watchdog för fil- och katalogövervakning) och
AFC.27 (löpande rapportering av ändringar).

Bevakningen sker på **katalogen**, inte på filen direkt. Både Word och
LibreOffice sparar ODT-filer genom att skriva en temporärfil och sedan
byta namn på den över originalet. En bevakare som är bunden till
filhandtaget tappar då målet efter första sparningen.

Sparningen ger dessutom flera händelser tätt inpå varandra (created,
moved, modified). Bevakaren avvaktar därför en kort stund efter sista
händelsen innan den synkroniserar, så att en enda sparning ger en enda
synkronisering — och läser filen först när den gått att öppna, eftersom
Word kortvarigt håller den låst.
"""

from __future__ import annotations

import time
from pathlib import Path

from tb import ODT_PATH, TbError
from tb.sync import sync

#: Tid att vänta efter sista filhändelsen innan synkronisering (sekunder).
DEBOUNCE_SECONDS = 1.5

#: Hur länge vi försöker öppna filen innan vi ger upp en omgång.
_OPEN_RETRY_SECONDS = 10.0


def _wait_until_readable(path: Path, timeout: float = _OPEN_RETRY_SECONDS) -> bool:
    """Väntar tills filen går att öppna för läsning.

    Word/LibreOffice håller filen låst en kort stund under sparningen.
    Returnerar False om den fortfarande är låst när tiden gått ut.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with path.open("rb"):
                return True
        except OSError:
            time.sleep(0.25)
    return False


def watch(*, odt_path: Path = ODT_PATH, debounce: float = DEBOUNCE_SECONDS) -> int:
    """Bevakar ODT-filen och synkroniserar vid varje innehållsändring.

    Blockerar tills användaren avbryter med Ctrl+C. Returnerar en
    exit-kod avsedd för ``python -m tb.sync --watch``.
    """
    try:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer
    except ImportError:  # pragma: no cover - beror på miljön, inte på logiken
        print("FEL: watchdog är inte installerat. Kör: pip install watchdog")
        return 2

    target = Path(odt_path).resolve()
    if not target.is_file():
        print(f"FEL: hittar inte {target}")
        return 2

    pending: list[float] = []

    class _Handler(FileSystemEventHandler):
        def on_any_event(self, event) -> None:
            if event.is_directory:
                return
            paths = [getattr(event, "src_path", None), getattr(event, "dest_path", None)]
            if any(p and Path(p).name == target.name for p in paths):
                pending.append(time.monotonic())

    observer = Observer()
    observer.schedule(_Handler(), str(target.parent), recursive=False)
    observer.start()

    print(f"Bevakar {target.name} (Ctrl+C för att avsluta).")
    print("Synkroniserar en gång direkt för att fastställa utgångsläget...")
    _run_once(target)

    try:
        while True:
            time.sleep(0.25)
            if pending and time.monotonic() - pending[-1] >= debounce:
                pending.clear()
                if _wait_until_readable(target):
                    _run_once(target)
                else:
                    print(f"{target.name} är fortfarande låst - hoppar över denna omgång.")
    except KeyboardInterrupt:
        print("\nAvslutar bevakningen.")
    finally:
        observer.stop()
        observer.join()
    return 0


def _run_once(target: Path) -> None:
    """Kör en synkronisering och skriver ut resultatet."""
    stamp = time.strftime("%H:%M:%S")
    try:
        result = sync(odt_path=target)
    except TbError as exc:
        print(f"[{stamp}] FEL: {exc}")
        return
    for line in result.describe().splitlines():
        print(f"[{stamp}] {line}")
