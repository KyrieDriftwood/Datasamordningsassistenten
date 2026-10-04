"""Gemensam körning av Office-automatisering (VBScript via cscript.exe).

Används av DOCX-skrivningen (``metadata/docx.py``) och PDF-exporten
(``backend/pdf_export.py``). Två ansvar samlas här:

* **Batchprotokoll.** Ett skript bearbetar flera filer i *en*
  Word-/Excel-instans och skriver en rad per fil på stdout:
  ``OK|<index>`` eller ``FAIL|<index>|<meddelande>``. Skriptet stannar vid
  första felet, så anroparen vet exakt vilka filer som blev klara och kan
  publicera dem och städa resten (samma delresultat som när baslinjen
  körde en fil i taget och avbröt vid första fel).
* **Städning vid tidsgräns.** ``subprocess.run`` dödar bara cscript.exe.
  Word/Excel startas av COM (DCOM) och är inte barnprocesser till skriptet,
  så de blev kvar och höll filer låsta. Därför tas en ögonblicksbild av
  programmets processer före start; vid tidsgräns avslutas bara *nya*
  processer vars kommandorad visar att de startats via automatisering
  (``/Automation`` eller ``-Embedding``). En Word som användaren själv
  öppnat under körningen rörs alltså inte.

Användarbeslut 2026-10-02, utanför TB (granskningspunkt "en Office-instans
per batch, döda vid timeout").
"""

from __future__ import annotations

import csv
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

WORD_IMAGE = "WINWORD.EXE"
EXCEL_IMAGE = "EXCEL.EXE"
SECONDS_PER_FILE = 90.0
MINIMUM_TIMEOUT = 180.0

_CREATION_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_AUTOMATION_MARKERS = ("/automation", "-embedding")


class OfficeScriptHostMissing(RuntimeError):
    """cscript.exe (Windows Script Host) kunde inte startas."""


class OfficeScriptTimeout(RuntimeError):
    """Skriptet överskred tidsgränsen; kvarlämnade Office-processer är avslutade."""


@dataclass(frozen=True, slots=True)
class BatchOutcome:
    """Resultat av ett batchskript: antal klara filer i ordning och ev. fel."""

    completed: int
    failed_index: int | None = None
    message: str | None = None


def batch_timeout(job_count: int) -> float:
    """Tidsgräns för en batch: minst baslinjens 180 s, annars 90 s per fil."""
    return max(MINIMUM_TIMEOUT, SECONDS_PER_FILE * max(job_count, 1))


def vbscript_literal(value: str | Path) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def _automation_pids(image_name: str) -> set[int]:
    """PID:er för körande ``image_name``; tom mängd om listningen misslyckas."""
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {image_name}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            creationflags=_CREATION_FLAGS,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    pids: set[int] = set()
    for row in csv.reader(result.stdout.splitlines()):
        if len(row) >= 2 and row[0].lower() == image_name.lower():
            try:
                pids.add(int(row[1]))
            except ValueError:
                continue
    return pids


def _is_automation_instance(pid: int) -> bool:
    """Sant bara om processens kommandorad visar COM-automatisering.

    Okänd kommandorad (frågan misslyckas) räknas som *inte* automatisering,
    så att en osäker träff aldrig leder till att användarens Office dödas.
    """
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            creationflags=_CREATION_FLAGS,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    command_line = result.stdout.lower()
    return any(marker in command_line for marker in _AUTOMATION_MARKERS)


def _kill_process(pid: int) -> None:
    try:
        subprocess.run(
            ["taskkill", "/PID", str(int(pid)), "/F", "/T"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            creationflags=_CREATION_FLAGS,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def _run_cscript(script_path: Path, timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["cscript.exe", "//NoLogo", str(script_path)],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        creationflags=_CREATION_FLAGS,
    )


def kill_orphaned_office(image_name: str, before: set[int]) -> list[int]:
    """Avslutar automatiseringsinstanser av ``image_name`` som startat efter ``before``."""
    killed: list[int] = []
    for pid in sorted(_automation_pids(image_name) - before):
        if _is_automation_instance(pid):
            _kill_process(pid)
            killed.append(pid)
    return killed


def run_office_script(script: str, *, image_name: str, timeout: float) -> subprocess.CompletedProcess:
    """Kör ``script`` med cscript och städar Office-processer vid tidsgräns."""
    fd, script_name = tempfile.mkstemp(suffix=".vbs")
    os.close(fd)
    script_path = Path(script_name)
    script_path.write_text(script, encoding="utf-16")
    before = _automation_pids(image_name)
    try:
        return _run_cscript(script_path, timeout)
    except FileNotFoundError as exc:
        raise OfficeScriptHostMissing("Windows Script Host kunde inte startas.") from exc
    except subprocess.TimeoutExpired as exc:
        killed = kill_orphaned_office(image_name, before)
        suffix = f" {len(killed)} kvarlämnad(e) Office-process(er) avslutades." if killed else ""
        raise OfficeScriptTimeout(f"Office svarade inte inom {int(timeout)} s.{suffix}") from exc
    finally:
        script_path.unlink(missing_ok=True)


def parse_batch_output(result: subprocess.CompletedProcess, job_count: int) -> BatchOutcome:
    """Tolkar batchprotokollet. Fel utan ``FAIL``-rad gäller första ej klara fil."""
    completed = 0
    other_lines: list[str] = []
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("OK|"):
            completed += 1
        elif line.startswith("FAIL|"):
            _, index_text, *message = line.split("|", 2)
            try:
                index = int(index_text)
            except ValueError:
                index = completed
            return BatchOutcome(completed, index, message[0] if message else "Okänt Office-fel")
        elif line:
            other_lines.append(line)
    if result.returncode != 0 or completed < job_count:
        details = (result.stderr or "").strip() or "\n".join(other_lines) or "Okänt Office-fel"
        return BatchOutcome(completed, min(completed, max(job_count - 1, 0)), details)
    return BatchOutcome(completed)
