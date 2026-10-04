"""Backup-kopior av DWG-filer inför skrivning, med återställning.

Verifierat gap i baslinjen: endast tempfile.TemporaryDirectory
används (auto-städas efter körning) — ingen ihållande, återställnings-
bar backup-kopia skapas eller underhålls (se docs/kravsparning.md,
anmärkning CBE-3/CBE-8).

Backup-mappens placering (beslutad med användaren, uppdaterad efter
CBE-granskning): en **dold** `.datasamordning_backup/`-undermapp i
projektroten, som speglar DWG-filens relativa sökväg under projektroten
(t.ex. ``<projektrot>/.datasamordning_backup/Ritningar/X.dwg`` för en
fil på ``<projektrot>/Ritningar/X.dwg``). Mappen sätts dessutom med
Windows dolt-attribut, så att den är dold både enligt punkt-konventionen
och enligt filsystemet — TB:s CBE kräver uttryckligen "en dold . mapp".
Endast den senaste backupen sparas per fil — den skrivs över varje
gång, precis som TB kräver ("alltid en stycken kopia ... som skrivs
över varje gång"), ingen historik/versionering.

Kravkälla: docs/kravsparning.md, modul dwg/backup, TB-sektion CBE-8/
J-10/Z-11/Z-12.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import shutil
import time
from pathlib import Path

from dwg import DwgError

BACKUP_DIR_NAME = ".datasamordning_backup"
"""Namn på den dolda backup-mappen i projektroten (TB, CBE)."""

_FILE_ATTRIBUTE_HIDDEN = 0x02
_RESTORE_RETRY_ATTEMPTS = 10
_RESTORE_RETRY_DELAY_SECONDS = 0.5


class BackupError(DwgError):
    """Höjs när en backup-kopia inte kan skapas eller återställas."""


def ensure_not_locked(dwg_path: str | Path) -> None:
    """Säkerställer att DWG-filen inte är exklusivt låst av en annan process.

    På Windows begärs läsåtkomst utan delning. AutoCAD och andra
    program som håller filen öppen exklusivt svarar då med WinError 32/33.
    Kontrollen görs före attributläsning och backup så att en låst originalfil
    aldrig kopieras eller skickas till skrivflödet.
    """
    path = Path(dwg_path).expanduser().resolve()
    if os.name != "nt":
        try:
            descriptor = os.open(path, os.O_RDWR)
        except OSError as exc:
            raise BackupError(f"Kan inte öppna DWG-filen för skrivning: {path}: {exc}") from exc
        else:
            os.close(descriptor)
        return

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    handle = create_file(str(path), 0x80000000, 0, None, 3, 0, None)
    if handle == wintypes.HANDLE(-1).value:
        error_code = ctypes.get_last_error()
        if error_code in (32, 33):
            raise BackupError(
                f"DWG-filen är låst av en annan process eller användare: {path}. "
                "Stäng filen i AutoCAD eller be den andra användaren stänga den och försök igen."
            )
        raise BackupError(f"Kan inte kontrollera om DWG-filen är skrivbar: {path} (WinError {error_code}).")
    kernel32.CloseHandle(handle)


def _mark_hidden(directory: Path) -> None:
    """Sätter Windows dolt-attribut på `directory`.

    Punkt-prefixet gör mappen dold enligt Unix-konvention, men på
    Windows krävs dessutom filsystemets dolt-attribut för att den ska
    döljas i Utforskaren. Misslyckas anropet (t.ex. på en nätverksdelning
    som inte stödjer attributet) är det inte ett fel som får avbryta
    backupen — kopian är det som skyddar användarens data, inte
    attributet.
    """
    try:
        ctypes.windll.kernel32.SetFileAttributesW(str(directory), _FILE_ATTRIBUTE_HIDDEN)
    except (AttributeError, OSError):
        pass


def backup_path_for(project_root: str | Path, dwg_path: str | Path) -> Path:
    """Beräknar var backup-kopian för en given DWG-fil ska ligga.

    Om `dwg_path` ligger under `project_root` speglas dess relativa
    sökväg under `<project_root>/.datasamordning_backup/`. Om filen
    (mot förmodan) ligger utanför projektroten används filnamnet direkt
    under backup-mappen som en dokumenterad, medveten reträttlösning —
    detta ska inte inträffa i normalt bruk eftersom `dwg/writers.py`
    alltid anropas med filer som redan valts inifrån en `ProjectContext`.
    """
    root = Path(project_root).expanduser().resolve()
    path = Path(dwg_path).expanduser().resolve()
    backup_root = root / BACKUP_DIR_NAME
    try:
        relative = path.relative_to(root)
    except ValueError:
        return backup_root / path.name
    return backup_root / relative


def create_backup(project_root: str | Path, dwg_path: str | Path) -> Path:
    """Skapar (eller skriver över) en backup-kopia av `dwg_path`.

    Höjer BackupError om originalfilen inte finns. Returnerar
    sökvägen till den skapade backup-kopian.
    """
    source = Path(dwg_path).expanduser().resolve()
    if not source.is_file():
        raise BackupError(f"Kan inte säkerhetskopiera - filen finns inte: {source}")

    destination = backup_path_for(project_root, source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _mark_hidden(Path(project_root).expanduser().resolve() / BACKUP_DIR_NAME)
    try:
        shutil.copy2(source, destination)
    except OSError as exc:
        raise BackupError(f"Kunde inte skriva backup-kopia till {destination}: {exc}") from exc
    return destination


def has_backup(project_root: str | Path, dwg_path: str | Path) -> bool:
    """True om en backup-kopia redan finns för `dwg_path`."""
    return backup_path_for(project_root, dwg_path).is_file()


def restore_backup(project_root: str | Path, dwg_path: str | Path) -> Path:
    """Återställer `dwg_path` från dess backup-kopia.

    Används när read-back-verifieringen efter en DWG-skrivning
    misslyckas (dwg/validators.py), för att uppfylla TB:s krav att
    originalfilen ska kunna återställas vid fel (Z-12). Höjer
    BackupError om ingen backup-kopia finns att återställa från.
    """
    target = Path(dwg_path).expanduser().resolve()
    source = backup_path_for(project_root, target)
    if not source.is_file():
        raise BackupError(f"Ingen backup-kopia finns att återställa från: {source}")
    for attempt in range(_RESTORE_RETRY_ATTEMPTS):
        try:
            shutil.copy2(source, target)
            return target
        except OSError as exc:
            is_file_lock = isinstance(exc, PermissionError) or getattr(exc, "winerror", None) == 32
            if not is_file_lock or attempt == _RESTORE_RETRY_ATTEMPTS - 1:
                raise BackupError(f"Kunde inte återställa {target} från backup: {exc}") from exc
            time.sleep(_RESTORE_RETRY_DELAY_SECONDS)

    raise AssertionError("restore_backup avslutades utan resultat")
