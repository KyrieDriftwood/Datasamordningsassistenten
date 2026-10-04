"""PDF-export av DOCX/XLSX via installerad Microsoft Word/Excel.

Direkt port av ``.old/python_v5/src/version_2.py``. Word exporterar
dokumentet som det renderas; Excel exporterar bara de två första
arbetsbladen och respekterar varje blads konfigurerade utskriftsområde
(samma begränsning som baslinjen har).

Kravkälla: docs/kravsparning.md, TB-sektion AFA (leveransförberedelse/
PDF-export), använd av metadata/grid.py:update_metadata_and_create_pdfs
och app/controllers/delivery_controller.py.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Callable, Iterable
from pathlib import Path

from backend.office_automation import (
    EXCEL_IMAGE,
    WORD_IMAGE,
    BatchOutcome,
    OfficeScriptHostMissing,
    OfficeScriptTimeout,
    batch_timeout,
    parse_batch_output,
    run_office_script,
    vbscript_literal,
)

SUPPORTED_EXTENSIONS = {".docx", ".xlsx"}
PDF_HEADER = b"%PDF-"

PdfExporter = Callable[[Path, Path], None]


class PdfExportError(RuntimeError):
    """Höjs när Microsoft Office inte kan skapa en giltig PDF."""


def _vbscript_literal(value: Path) -> str:
    return vbscript_literal(value)


# Egen ``On Error Resume Next`` i varje funktion: annars hoppar ett fel ur
# funktionen och huvudskriptet fortsätter utan felmeddelande.
_WORD_EXPORT_FUNCTION = """
Function ExportDocument(source, output)
    On Error Resume Next
    Dim document, message
    Err.Clear
    Set document = application.Documents.Open(source, False, True)
    If Err.Number <> 0 Then
        ExportDocument = "Dokumentet kunde inte öppnas: " & Err.Description
        Exit Function
    End If
    Err.Clear
    document.ExportAsFixedFormat output, 17
    If Err.Number <> 0 Then message = "Word kunde inte exportera PDF: " & Err.Description
    Err.Clear
    document.Close False
    ExportDocument = Replace(Replace(message, vbCr, " "), vbLf, " ")
End Function
"""

_EXCEL_EXPORT_FUNCTION = """
Function ExportDocument(source, output)
    On Error Resume Next
    Dim workbook, message
    Err.Clear
    Set workbook = application.Workbooks.Open(source, 0, True)
    If Err.Number <> 0 Then
        ExportDocument = "Arbetsboken kunde inte öppnas: " & Err.Description
        Exit Function
    End If
    If workbook.Worksheets.Count < 2 Then
        workbook.Close False
        ExportDocument = "Arbetsboken måste innehålla minst två arbetsblad."
        Exit Function
    End If
    Err.Clear
    workbook.Worksheets(Array(1, 2)).Select
    application.ActiveSheet.ExportAsFixedFormat 0, output
    If Err.Number <> 0 Then message = "Excel kunde inte exportera PDF: " & Err.Description
    Err.Clear
    workbook.Close False
    ExportDocument = Replace(Replace(message, vbCr, " "), vbLf, " ")
End Function
"""


def _office_batch_export_script(suffix: str, jobs: list[tuple[Path, Path]]) -> str:
    """Ett skript och en Office-instans för alla filer av samma typ.

    Stannar vid första fel; protokollet ``OK|i``/``FAIL|i|meddelande``
    tolkas av ``backend.office_automation.parse_batch_output``.
    """
    calls = "".join(
        f"""
failure = ExportDocument({_vbscript_literal(source)}, {_vbscript_literal(output)})
If Len(failure) > 0 Then
    application.Quit
    WScript.Echo "FAIL|{index}|" & failure
    WScript.Quit 1
End If
WScript.Echo "OK|{index}"
"""
        for index, (source, output) in enumerate(jobs)
    )
    if suffix == ".docx":
        return f"""
On Error Resume Next
Dim application, failure
Set application = CreateObject("Word.Application")
If Err.Number <> 0 Then
    WScript.Echo "Microsoft Word kunde inte startas: " & Err.Description
    WScript.Quit 1
End If
application.Visible = False
application.DisplayAlerts = 0
Err.Clear
application.ActivePrinter = "Microsoft Print to PDF"
If Err.Number <> 0 Then Err.Clear
{_WORD_EXPORT_FUNCTION}
{calls}
application.Quit
"""

    return f"""
On Error Resume Next
Dim application, failure
Set application = CreateObject("Excel.Application")
If Err.Number <> 0 Then
    WScript.Echo "Microsoft Excel kunde inte startas: " & Err.Description
    WScript.Quit 1
End If
application.Visible = False
application.DisplayAlerts = False
{_EXCEL_EXPORT_FUNCTION}
{calls}
application.Quit
"""


def _office_export_script(source: Path, output: Path) -> str:
    return _office_batch_export_script(source.suffix.lower(), [(source, output)])


def _export_batch_with_office(suffix: str, jobs: list[tuple[Path, Path]]) -> BatchOutcome:
    """Exporterar alla ``jobs`` (samma filtyp) i en Word- eller Excel-instans."""
    if not jobs:
        return BatchOutcome(0)
    image_name = WORD_IMAGE if suffix == ".docx" else EXCEL_IMAGE
    try:
        result = run_office_script(
            _office_batch_export_script(suffix, jobs),
            image_name=image_name,
            timeout=batch_timeout(len(jobs)),
        )
    except OfficeScriptHostMissing as exc:
        raise PdfExportError(str(exc)) from exc
    except OfficeScriptTimeout as exc:
        names = ", ".join(source.name for source, _output in jobs)
        raise PdfExportError(f"PDF-exporten tog för lång tid för {names}. {exc}") from exc
    return parse_batch_output(result, len(jobs))


def _export_with_office(source: Path, output: Path) -> None:
    outcome = _export_batch_with_office(source.suffix.lower(), [(source, output)])
    if outcome.failed_index is not None:
        raise PdfExportError(f"PDF-exporten misslyckades för {source.name}: {outcome.message}")

def _validate_pdf(path: Path) -> None:
    if not path.is_file():
        raise PdfExportError(f"Office skapade ingen PDF-fil: {path}")
    with path.open("rb") as pdf_file:
        if pdf_file.read(len(PDF_HEADER)) != PDF_HEADER:
            raise PdfExportError(f"Office skapade en ogiltig PDF-fil: {path}")


def _publish_pdf(temporary_output: Path, output: Path) -> None:
    try:
        os.replace(temporary_output, output)
        return
    except PermissionError as replace_error:
        backup_path: Path | None = None
        try:
            if output.exists():
                backup_directory = tempfile.mkdtemp(prefix="docx_pdf_backup_")
                backup_path = Path(backup_directory) / output.name
                shutil.copy2(output, backup_path)

            try:
                with temporary_output.open("rb") as source, output.open("wb") as destination:
                    shutil.copyfileobj(source, destination)
                    destination.flush()
                _validate_pdf(output)
                if output.stat().st_size != temporary_output.stat().st_size:
                    raise PdfExportError(f"PDF-filen kopierades ofullständigt till {output}.")
                temporary_output.unlink()
            except Exception as publish_error:
                if backup_path is not None:
                    try:
                        shutil.copy2(backup_path, output)
                    except OSError as restore_error:
                        raise PdfExportError(
                            f"PDF kunde inte publiceras till {output}; den tidigare PDF-filen kunde "
                            f"inte återställas från {backup_path}: {restore_error}"
                        ) from publish_error
                elif output.exists():
                    output.unlink()
                raise PdfExportError(
                    f"PDF kunde inte publiceras till {output}. Kontrollera att filen inte är öppen "
                    f"och att du har skrivrättighet i mappen: {publish_error}"
                ) from publish_error
        except PdfExportError:
            raise
        except OSError as fallback_error:
            raise PdfExportError(
                f"PDF skapades men kunde inte ersätta målfilen {output}. Kontrollera att filen inte "
                f"är öppen och att du har skrivrättighet i nätverksmappen: {fallback_error}"
            ) from replace_error
        finally:
            if backup_path is not None:
                shutil.rmtree(backup_path.parent, ignore_errors=True)


def plot_document(
    path: str | Path,
    output_path: str | Path | None = None,
    *,
    exporter: PdfExporter | None = None,
) -> Path:
    """Exporterar en DOCX/XLSX till PDF utan att ändra källfilen.

    Direkt port av ``version_2.py:plot_document``.
    """
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Filen finns inte: {source}")
    if source.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"Filtypen stöds inte för PDF-export: {source.suffix}")

    output = Path(output_path).expanduser().resolve() if output_path is not None else source.with_suffix(".pdf")
    output.parent.mkdir(parents=True, exist_ok=True)

    fd, temporary_name = tempfile.mkstemp(suffix=".pdf", dir=str(output.parent))
    os.close(fd)
    temporary_output = Path(temporary_name)
    temporary_output.unlink()

    export = exporter or _export_with_office
    try:
        export(source, temporary_output)
        _validate_pdf(temporary_output)
        _publish_pdf(temporary_output, output)
    except Exception:
        temporary_output.unlink(missing_ok=True)
        raise

    return output


def plot_documents(
    paths: Iterable[str | Path],
    output_dir: str | Path | None = None,
    *,
    exporter: PdfExporter | None = None,
) -> list[Path]:
    """Exporterar flera dokument. Port av ``version_2.py:plot_documents``.

    Med standardexportören körs alla DOCX i en Word-instans och alla XLSX i
    en Excel-instans (i stället för en Office-start per fil). Varje PDF
    valideras och publiceras sedan i indataordning; vid första fel tas
    återstående temporärfiler bort och felet höjs. Redan publicerade PDF:er
    står kvar — samma delresultat som baslinjens fil-för-fil-loop.
    """
    destination = Path(output_dir).expanduser().resolve() if output_dir is not None else None
    sources = [Path(path).expanduser().resolve() for path in paths]
    if exporter is not None:
        outputs: list[Path] = []
        for source in sources:
            output = destination / f"{source.stem}.pdf" if destination is not None else None
            outputs.append(plot_document(source, output, exporter=exporter))
        return outputs

    for source in sources:
        if not source.is_file():
            raise FileNotFoundError(f"Filen finns inte: {source}")
        if source.suffix.lower() not in SUPPORTED_EXTENSIONS:
            raise ValueError(f"Filtypen stöds inte för PDF-export: {source.suffix}")

    planned: list[tuple[Path, Path, Path]] = []
    try:
        for source in sources:
            output = destination / f"{source.stem}.pdf" if destination is not None else source.with_suffix(".pdf")
            output.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary_name = tempfile.mkstemp(suffix=".pdf", dir=str(output.parent))
            os.close(fd)
            temporary_output = Path(temporary_name)
            temporary_output.unlink()
            planned.append((source, output, temporary_output))

        failures: dict[int, str] = {}
        for suffix in sorted(SUPPORTED_EXTENSIONS):
            positions = [index for index, (source, _o, _t) in enumerate(planned) if source.suffix.lower() == suffix]
            jobs = [(planned[index][0], planned[index][2]) for index in positions]
            if not jobs:
                continue
            outcome = _export_batch_with_office(suffix, jobs)
            for job, position in enumerate(positions):
                if job >= outcome.completed:
                    failures[position] = outcome.message or "Okänt Office-fel"

        outputs = []
        for position, (source, output, temporary_output) in enumerate(planned):
            if position in failures:
                raise PdfExportError(f"PDF-exporten misslyckades för {source.name}: {failures[position]}")
            _validate_pdf(temporary_output)
            _publish_pdf(temporary_output, output)
            outputs.append(output)
        return outputs
    finally:
        for _source, _output, temporary_output in planned:
            temporary_output.unlink(missing_ok=True)
