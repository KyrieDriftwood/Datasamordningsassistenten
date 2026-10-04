"""Python bridge for the Rust/AutoCAD DWG-to-PDF plotting backend."""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from dwg import DwgError

_BACKEND_ENV_OVERRIDE = "DATASAMORDNING_DWG_PDF_BACKEND"
_BACKEND_RELATIVE_PATH = Path("dwg_pdf_backend") / "target" / "release" / "dwg_pdf_backend.exe"
_logger = logging.getLogger(__name__)


class DwgPdfPlotError(DwgError):
    """Raised when the Rust DWG-to-PDF backend cannot be run."""


@dataclass(frozen=True)
class DwgPdfJob:
    drawing: Path
    output: Path
    expected_values: tuple[tuple[str, str], ...] = ()
    verification_issue: str | None = None


@dataclass(frozen=True)
class DwgPdfPlotResult:
    drawing: Path
    output: Path
    error: str | None = None
    warning: str | None = None


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def locate_pdf_backend() -> Path:
    override = os.environ.get(_BACKEND_ENV_OVERRIDE)
    if override:
        path = Path(override)
        if path.is_file():
            return path
        raise DwgPdfPlotError(f"{_BACKEND_ENV_OVERRIDE} pekar på en fil som inte finns: {path}")

    path = _repo_root() / _BACKEND_RELATIVE_PATH
    if path.is_file():
        return path
    raise DwgPdfPlotError(
        f"Hittade ingen DWG-PDF-backend under {path}. "
        "Bygg den med:\ncargo build --release --manifest-path dwg_pdf_backend\\Cargo.toml"
    )


def _hex_encode(value: str) -> str:
    return value.encode("utf-8").hex()


def _hex_decode(value: str) -> str:
    try:
        return bytes.fromhex(value).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise DwgPdfPlotError("Rust-backenden returnerade ett felmeddelande med ogiltig kodning.") from exc


def plot_many(
    jobs: Iterable[DwgPdfJob],
    *,
    timeout_seconds: int = 300,
    progress_callback: Callable[[int, int, DwgPdfPlotResult], None] | None = None,
) -> dict[Path, DwgPdfPlotResult]:
    resolved_jobs = tuple(
        DwgPdfJob(
            drawing=job.drawing.expanduser().resolve(),
            output=job.output.expanduser().resolve(),
            expected_values=job.expected_values,
            verification_issue=job.verification_issue,
        )
        for job in jobs
    )
    if not resolved_jobs:
        return {}

    backend = locate_pdf_backend()
    manifest = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="ascii",
        newline="\n",
        prefix="datasamordning-dwg-pdf-",
        suffix=".tsv",
        delete=False,
    )
    manifest_path = Path(manifest.name)
    try:
        with manifest:
            for job in resolved_jobs:
                fields = (
                    _hex_encode(str(job.drawing)),
                    _hex_encode(str(job.output)),
                    _hex_encode(job.verification_issue or ""),
                    *(
                        f"{_hex_encode(label)}:{_hex_encode(value)}"
                        for label, value in job.expected_values
                    ),
                )
                manifest.write("\t".join(fields) + "\n")

        results: dict[Path, DwgPdfPlotResult] = {}
        done = 0
        with tempfile.TemporaryFile(mode="w+b") as stderr_file:
            process = subprocess.Popen(
                [str(backend), "plot", str(manifest_path), str(max(1, timeout_seconds))],
                stdout=subprocess.PIPE,
                stderr=stderr_file,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            assert process.stdout is not None
            try:
                for line in process.stdout:
                    parts = line.rstrip("\r\n").split("\t", 3)
                    if len(parts) != 4 or parts[0] != "###RESULT":
                        raise DwgPdfPlotError(f"Rust-backenden returnerade ett oväntat svar: {line.strip()}")
                    try:
                        index = int(parts[1])
                    except ValueError as exc:
                        raise DwgPdfPlotError("Rust-backenden returnerade ett ogiltigt jobbindex.") from exc
                    if index < 0 or index >= len(resolved_jobs) or resolved_jobs[index].drawing in results:
                        raise DwgPdfPlotError("Rust-backenden returnerade ett duplicerat eller ogiltigt jobbindex.")
                    job = resolved_jobs[index]
                    status = parts[2]
                    if status == "OK" and not parts[3]:
                        result = DwgPdfPlotResult(job.drawing, job.output)
                    elif status == "WARN" and parts[3]:
                        result = DwgPdfPlotResult(
                            job.drawing,
                            job.output,
                            warning=_hex_decode(parts[3]),
                        )
                    elif status == "ERROR" and parts[3]:
                        result = DwgPdfPlotResult(job.drawing, job.output, _hex_decode(parts[3]))
                    else:
                        raise DwgPdfPlotError("Rust-backenden returnerade en ogiltig jobbstatus.")
                    results[job.drawing] = result
                    done += 1
                    if progress_callback is not None:
                        progress_callback(done, len(resolved_jobs), result)
            except Exception:
                process.kill()
                process.wait()
                raise
            return_code = process.wait()
            stderr_file.seek(0)
            stderr = stderr_file.read().decode("utf-8", errors="replace").strip()
            if return_code != 0:
                detail = f": {stderr}" if stderr else ""
                raise DwgPdfPlotError(f"Rust-backenden avslutades med felkod {return_code}{detail}")
            if done != len(resolved_jobs):
                raise DwgPdfPlotError(
                    f"Rust-backenden rapporterade {done} av {len(resolved_jobs)} plotjobb som slutförda."
                )
            return results
    except OSError as exc:
        raise DwgPdfPlotError(f"Kunde inte starta DWG-PDF-backenden: {exc}") from exc
    finally:
        try:
            manifest_path.unlink(missing_ok=True)
        except OSError:
            _logger.warning("Kunde inte rensa tillfälligt DWG-PDF-manifest: %s", manifest_path, exc_info=True)
