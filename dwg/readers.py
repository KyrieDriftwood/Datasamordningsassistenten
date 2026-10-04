"""Läsning av DWG-attribut (TRVJ_NAMNRUTA m.fl.) via accoreconsole.

Motsvarar läsvägen i .old/python_v5/src/version_5.py via
accoreconsole/AutoLISP. Den snabbare, skrivskyddade Rust-baserade
läsvägen (version_5_rust.py) ligger i dwg/rust_bridge.py och
återanvänder samma delade typer/parser från dwg/__init__.py.

Kravkälla: docs/kravsparning.md, modul dwg/readers.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dwg import (
    DEFAULT_MAX_PARALLEL_WORKERS,
    DwgAttributeError,
    DwgAttributeResult,
    TRVJ_BLOCK_NAME,
    chunk_paths,
    lisp_string_literal,
    parse_extract_output,
    read_ansi_output,
    run_accoreconsole_script,
    to_lisp_path,
)


def _build_extract_lisp(
    paths: tuple[Path, ...],
    output_path: Path,
    *,
    block_name: str = TRVJ_BLOCK_NAME,
) -> str:
    """Bygger ett AutoLISP-skript som extraherar attribut ur
    flera DWG-filer i en och samma accoreconsole-process (batchning), för
    att bara betala modul-laddningskostnaden en gång per batch.

    Direkt port av ``version_5.py:_build_extract_lisp``.
    """
    lines = [
        "(defun DatasamordningExtractOne (idx outf / ss ins e adata tag val flags)",
        '  (write-line (strcat "###FILE:" (itoa idx)) outf)',
        f'  (setq ss (ssget "X" (list (cons 0 "INSERT") (cons 2 {lisp_string_literal(block_name)}))))',
        "  (if (and ss (> (sslength ss) 0))",
        "    (progn",
        "      (setq ins (ssname ss 0))",
        "      (setq e (entnext ins))",
        '      (while (and e (/= (cdr (assoc 0 (entget e))) "SEQEND"))',
        "        (setq adata (entget e))",
        "        (setq tag (cdr (assoc 2 adata)))",
        "        (setq val (cdr (assoc 1 adata)))",
        "        (setq flags (cdr (assoc 70 adata)))",
        '        (write-line (strcat tag "\\t" (if val val "") "\\t" (itoa (if flags flags 0))) outf)',
        "        (setq e (entnext e))",
        "      )",
        "    )",
        '    (write-line "###BLOCK_NOT_FOUND" outf)',
        "  )",
        '  (write-line "###END" outf)',
        ")",
        f'(setq DatasamordningOutf (open {lisp_string_literal(to_lisp_path(output_path))} "w"))',
    ]
    for index, path in enumerate(paths):
        if index > 0:
            lines.append(f'(command "_.OPEN" {lisp_string_literal(to_lisp_path(path))})')
        lines.append(f"(DatasamordningExtractOne {index} DatasamordningOutf)")
    lines.append("(close DatasamordningOutf)")
    lines.append("(princ)")
    return "\n".join(lines) + "\n"


def extract_dwg_attributes(path: str | Path) -> tuple:
    """Extraherar TRVJ_NAMNRUTA-attribut från en enskild DWG-fil.

    Direkt port av ``version_5.py:extract_dwg_attributes``. Höjer
    DwgAttributeError om blocket inte hittas eller om accoreconsole
    inte kan köras. Använd extract_many_dwg_attributes() för batchar -
    ett enskilt accoreconsole-anrop tar ~7,5 sekunder på grund av
    modul-laddning.
    """
    dwg_path = Path(path).expanduser().resolve()
    results = extract_many_dwg_attributes((dwg_path,))
    result = results[dwg_path]
    if result.error is not None:
        raise DwgAttributeError(result.error)
    return result.fields


def extract_many_dwg_attributes(
    paths: Iterable[str | Path],
    *,
    max_workers: int = DEFAULT_MAX_PARALLEL_WORKERS,
    progress_callback: Callable[[int, int], None] | None = None,
) -> dict[Path, DwgAttributeResult]:
    """Extraherar TRVJ_NAMNRUTA-attribut för flera DWG-filer.

    Direkt port av ``version_5.py:extract_many_dwg_attributes``. Filerna
    delas upp i grupper (chunks) som körs i samma accoreconsole-process
    (via OPEN mellan filer) för att slippa upprepad modul-laddning, och
    grupperna körs parallellt (upp till max_workers) för att korta ned
    den totala väntetiden. progress_callback(done, total) anropas efter
    varje färdig fil.
    """
    return extract_many_dwg_attributes_for_block(
        paths,
        TRVJ_BLOCK_NAME,
        max_workers=max_workers,
        progress_callback=progress_callback,
    )


def extract_many_dwg_attributes_for_block(
    paths: Iterable[str | Path],
    block_name: str,
    *,
    max_workers: int = DEFAULT_MAX_PARALLEL_WORKERS,
    progress_callback: Callable[[int, int], None] | None = None,
) -> dict[Path, DwgAttributeResult]:
    """Extraherar ett namngivet block via AutoCAD/accoreconsole.

    Använder samma blockurval som den namngivna skrivrutinen och behåller
    dynamiska taggar för andra block än standardnamnrutan.
    """
    if not block_name or "\x00" in block_name:
        raise ValueError("Blocknamnet måste vara en icke-tom sträng utan null-byte.")

    resolved_paths = tuple(Path(path).expanduser().resolve() for path in paths)
    if not resolved_paths:
        return {}

    total = len(resolved_paths)
    completed = 0
    results: dict[Path, DwgAttributeResult] = {}

    def run_chunk(chunk: tuple[Path, ...]) -> dict[Path, DwgAttributeResult]:
        with tempfile.TemporaryDirectory(prefix="datasamordning_dwg_out_") as temp_dir:
            output_path = Path(temp_dir) / "extract_output.txt"
            lisp_body = _build_extract_lisp(chunk, output_path, block_name=block_name)
            try:
                run_accoreconsole_script(chunk[0], lisp_body)
                text = read_ansi_output(output_path)
            except DwgAttributeError as exc:
                return {path: DwgAttributeResult(path=path, fields=(), error=str(exc)) for path in chunk}
            return parse_extract_output(
                text,
                chunk,
                block_name=block_name,
                include_unknown_tags=block_name != TRVJ_BLOCK_NAME,
            )

    chunks = chunk_paths(resolved_paths, max_workers)
    with ThreadPoolExecutor(max_workers=len(chunks)) as executor:
        futures = [executor.submit(run_chunk, chunk) for chunk in chunks]
        for future, chunk in zip(futures, chunks, strict=True):
            chunk_results = future.result()
            results.update(chunk_results)
            completed += len(chunk)
            if progress_callback is not None:
                progress_callback(completed, total)

    return results
