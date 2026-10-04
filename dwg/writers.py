"""Skrivning av DWG-attribut via headless accoreconsole-process.

Motsvarar skrivlogiken i .old/python_v5/src/version_5.py.

Kravkälla: docs/kravsparning.md, modul dwg/writers, TB-sektion CBE/J.

Viktigt: varje skrivning går via dwg/backup.py (backup före) och
read-back-verifiering efter (dwg/validators.py), med automatisk
återställning vid avvikelse. Baslinjen saknade båda (se
docs/kravsparning.md, anmärkning CBE-3/CBE-8 och J-10).

Status: klar (Steg 4, Fas 3, skrivdelen). Portering av accoreconsole-
skrivlogiken (``_build_update_lisp``/``_parse_update_output``) är
direkt från ``version_5.py``, kompletterad med backup-före/read-back-
verifiering-efter enligt beslutet i docs/plan.md (Fas 3).

Prestanda: varje accoreconsole-start kostar ~10–15 s. Flödet startar
därför AutoCAD bara för själva skrivningen; före-värden läses med Rust
och i skrivsessionen, och read-back görs med Rust med en gemensam
AutoCAD-läsning som reserv (se `_update_many_dwg_attributes_for_block`).
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import dwg.readers as autocad_readers
import dwg.rust_bridge as rust_bridge
from dwg import (
    DEFAULT_MAX_PARALLEL_WORKERS,
    DwgAttributeError,
    DwgAttributeResult,
    DwgError,
    DwgUpdateResult,
    TRVJ_BLOCK_NAME,
    TRVJ_MODEL_BLOCK_NAME,
    lisp_string_literal,
    parse_extract_output,
    read_ansi_output,
    run_accoreconsole_script,
    to_lisp_path,
)
from dwg.backup import BackupError, create_backup, ensure_not_locked, restore_backup
from dwg.validators import DwgWriteVerificationError, check_written_fields, fast_verify_passes

DEFAULT_AUTOCAD_WRITE_WORKERS = 1
"""En skrivkö åt gången för att undvika konkurrerande AutoCAD-processer."""
_AUTOCAD_WRITE_RETRIES = 1


def _is_retryable_autocad_error(error: str) -> bool:
    return any(
        marker in error
        for marker in (
            "svarade inte inom",
            "Kunde inte starta accoreconsole",
            "avslutades utan att skriva någon utdatafil",
            "Core Console avslutades med felkod",
        )
    )


def _build_update_lisp(
    changes_by_path: tuple[tuple[Path, dict[str, str]], ...],
    output_path: Path,
    *,
    block_name: str = TRVJ_BLOCK_NAME,
) -> str:
    """Bygger ett AutoLISP-skript som uppdaterar TRVJ_NAMNRUTA-attribut i
    flera DWG-filer i en och samma accoreconsole-process. Uppdateringen
    sker i två steg per fil: först samlas alla attributentiteter in och
    önskade taggar valideras (så att en fil inte delvis sparas om en
    tagg saknas), därefter skrivs värdena och filen sparas med QSAVE.

    Direkt port av ``version_5.py:_build_update_lisp``.
    """
    lines = [
        "(defun DatasamordningUpdateOne (idx changes outf /",
        "    ss ins e adata tag val flags entity-by-tag missing-tags change entry update-result)",
        '  (write-line (strcat "###FILE:" (itoa idx)) outf)',
        "  (setq entity-by-tag (list))",
        f'  (setq ss (ssget "X" (list (cons 0 "INSERT") (cons 2 {lisp_string_literal(block_name)}))))',
        "  (if (and ss (> (sslength ss) 0))",
        "    (progn",
        "      (setq ins (ssname ss 0))",
        "      (setq e (entnext ins))",
        '      (while (and e (/= (cdr (assoc 0 (entget e))) "SEQEND"))',
        "        (setq adata (entget e))",
        "        (setq tag (cdr (assoc 2 adata)))",
        # Före-värdena skrivs i samma session som ändringen, så att ingen
        # separat AutoCAD-läsning behövs före skrivningen (se writers-flödet).
        "        (setq val (cdr (assoc 1 adata)))",
        "        (setq flags (cdr (assoc 70 adata)))",
        '        (write-line (strcat "###BEFORE\\t" tag "\\t" (if val val "") "\\t" '
        "(itoa (if flags flags 0))) outf)",
        "        (setq entity-by-tag (cons (cons tag e) entity-by-tag))",
        "        (setq e (entnext e))",
        "      )",
        "      (setq missing-tags (list))",
        "      (foreach change changes",
        "        (if (not (assoc (car change) entity-by-tag))",
        "          (setq missing-tags (cons (car change) missing-tags))",
        "        )",
        "      )",
        "      (if missing-tags",
        "        (foreach tag missing-tags",
        '          (write-line (strcat "###MISSING_TAG:" tag) outf)',
        "        )",
        "        (progn",
        "          (foreach change changes",
        "            (setq entry (assoc (car change) entity-by-tag))",
        "            (setq e (cdr entry))",
        "            (setq adata (entget e))",
        "            (entmod (subst (cons 1 (cdr change)) (assoc 1 adata) adata))",
        "            (entupd e)",
        "          )",
        '          (command "_.QSAVE")',
        '          (write-line "###OK" outf)',
        "        )",
        "      )",
        "    )",
        '    (write-line "###BLOCK_NOT_FOUND" outf)',
        "  )",
        '  (write-line "###END" outf)',
        ")",
        f'(setq DatasamordningOutf (open {lisp_string_literal(to_lisp_path(output_path))} "w"))',
    ]
    for index, (path, changes) in enumerate(changes_by_path):
        if index > 0:
            lines.append(f'(command "_.OPEN" {lisp_string_literal(to_lisp_path(path))})')
        # AutoLISP's `list` form separates expressions with whitespace.
        # Commas are not argument separators here and make multi-field
        # updates fail to load in accoreconsole.
        change_pairs = " ".join(
            f"(cons {lisp_string_literal(tag)} {lisp_string_literal(value)})" for tag, value in changes.items()
        )
        lines.extend(
            (
                f"(setq update-result (vl-catch-all-apply 'DatasamordningUpdateOne "
                f"(list {index} (list {change_pairs}) DatasamordningOutf)))",
                "(if (vl-catch-all-error-p update-result)",
                "  (progn",
                '    (write-line (strcat "###ERROR:" (vl-catch-all-error-message update-result)) DatasamordningOutf)',
                '    (write-line "###END" DatasamordningOutf)',
                "  )",
                ")",
            )
        )
    lines.append("(close DatasamordningOutf)")
    lines.append("(princ)")
    return "\n".join(lines) + "\n"


def _parse_update_output(
    text: str,
    paths: tuple[Path, ...],
    *,
    block_name: str = TRVJ_BLOCK_NAME,
) -> dict[Path, DwgUpdateResult]:
    """Parses update output for the requested title block."""
    results: dict[Path, DwgUpdateResult] = {}
    segments = text.split("###FILE:")
    for segment in segments[1:]:
        header, _, body = segment.partition("\n")
        index = int(header.strip())
        path = paths[index]
        body = body.split("###END", 1)[0]
        all_lines = [line for line in body.splitlines() if line]
        before_lines = [line.removeprefix("###BEFORE\t") for line in all_lines if line.startswith("###BEFORE\t")]
        raw_lines = [line for line in all_lines if not line.startswith("###BEFORE\t")]

        if "###OK" in raw_lines:
            fields_before = parse_extract_output(
                "###FILE:0\n" + "".join(f"{line}\n" for line in before_lines) + "###END\n",
                (path,),
                block_name=block_name,
                include_unknown_tags=block_name != TRVJ_BLOCK_NAME,
            )[path].fields
            results[path] = DwgUpdateResult(path=path, error=None, fields_before=fields_before)
            continue
        if "###BLOCK_NOT_FOUND" in raw_lines:
            results[path] = DwgUpdateResult(
                path=path,
                error=f"Hittade inget block med namnet {block_name} i ritningen.",
            )
            continue
        missing_tags = [
            line.removeprefix("###MISSING_TAG:") for line in raw_lines if line.startswith("###MISSING_TAG:")
        ]
        if missing_tags:
            results[path] = DwgUpdateResult(
                path=path,
                error="Följande attribut hittades inte i ritningen, ingen ändring sparades: "
                + ", ".join(missing_tags),
            )
            continue
        write_errors = [line.removeprefix("###ERROR:") for line in raw_lines if line.startswith("###ERROR:")]
        if write_errors:
            results[path] = DwgUpdateResult(
                path=path,
                error="AutoCAD avbröt DWG-skrivningen: "
                + "; ".join(write_errors)
                + " DWG-filen kan ha ändrats delvis.",
            )
            continue
        details = " | ".join(raw_lines) if raw_lines else "inget statusresultat skrevs"
        results[path] = DwgUpdateResult(
            path=path,
            error=f"Okänt fel: kunde inte tolka accoreconsole-resultatet ({details}).",
        )
    return results


def _run_update_batch(
    changes_by_path: tuple[tuple[Path, dict[str, str]], ...],
    block_name: str = TRVJ_BLOCK_NAME,
) -> dict[Path, DwgUpdateResult]:
    """Kör själva accoreconsole-uppdateringen (utan backup/verifiering)
    för en batch filer i samma process. Motsvarar ``run_chunk`` i
    ``version_5.py:update_many_dwg_attributes``, extraherad hit så att
    både enfils- och flerfils-flödet i denna modul kan återanvända den.
    """
    chunk_paths = tuple(path for path, _ in changes_by_path)
    with tempfile.TemporaryDirectory(prefix="datasamordning_dwg_update_") as temp_dir:
        output_path = Path(temp_dir) / "update_output.txt"
        lisp_body = _build_update_lisp(changes_by_path, output_path, block_name=block_name)
        try:
            run_accoreconsole_script(chunk_paths[0], lisp_body)
            text = read_ansi_output(output_path)
        except DwgAttributeError as exc:
            return {path: DwgUpdateResult(path=path, error=str(exc)) for path in chunk_paths}
        return _parse_update_output(text, chunk_paths, block_name=block_name)


def _restore_with_note(project_root: str | Path, path: Path) -> str:
    try:
        restore_backup(project_root, path)
    except BackupError as restore_exc:
        return f"VARNING: kunde inte återställa från backup: {restore_exc}"
    return "Originalfilen har återställts från backup-kopian."


def _read_with_rust(paths: tuple[Path, ...], block_name: str) -> dict[Path, DwgAttributeResult]:
    """Snabb LibreDWG-läsning för verifiering. Fel ger tomt/felresultat och
    leder därmed bara till AutoCAD-verifiering, aldrig till att skrivningen
    avbryts."""
    if not paths:
        return {}
    try:
        return rust_bridge.extract_many_dwg_attributes_for_block(paths, block_name)
    except (DwgError, OSError):
        return {}


def _verify_written_files(
    project_root: str | Path,
    written:  list[tuple[Path, dict[str, str], tuple | None]],
    rust_before: dict[Path, DwgAttributeResult],
    *,
    block_name: str,
    max_workers: int,
) -> dict[Path, DwgUpdateResult]:
    """Read-back-verifierar skrivna filer från disk och återställer vid fel.

    1. Rust/LibreDWG läser alla filer (snabbt). Filer där Rust entydigt
       visar exakt avsedda värden och oförändrade övriga attribut är klara.
    2. Övriga filer läses med AutoCAD i EN gemensam process och jämförs
       med AutoCADs egna före-värden från skrivsessionen. Det är samma
       kontroll som tidigare gjordes med en AutoCAD-process per fil.
    """
    rust_after = _read_with_rust(tuple(path for path, _, _ in written), block_name)
    results: dict[Path, DwgUpdateResult] = {}
    needs_autocad: list[tuple[Path, dict[str, str], tuple | None]] = []
    for item in written:
        path, changes, _autocad_before = item
        if fast_verify_passes(changes, rust_before.get(path), rust_after.get(path)):
            results[path] = DwgUpdateResult(path=path, error=None)
        else:
            needs_autocad.append(item)
    if not needs_autocad:
        return results

    autocad_after: dict[Path, DwgAttributeResult] = {}
    read_error: Exception | None = None
    try:
        autocad_after = autocad_readers.extract_many_dwg_attributes_for_block(
            tuple(path for path, _, _ in needs_autocad),
            block_name,
            max_workers=max_workers,
        )
    except DwgError as exc:
        read_error = exc

    for path, changes, autocad_before in needs_autocad:
        try:
            if autocad_before is None:
                raise DwgWriteVerificationError(
                    "Skrivningen lyckades men kunde inte verifieras (saknar attribut från innan skrivning)."
                )
            after = autocad_after.get(path)
            if after is None or after.error is not None:
                detail = after.error if after is not None else (read_error or "inget resultat")
                raise DwgWriteVerificationError(
                    f"Kunde inte läsa tillbaka {path.name} efter skrivning för verifiering: {detail}"
                )
            check_written_fields(
                path,
                changes,
                autocad_before,
                after.fields,
                block_name=block_name,
                rust_after=rust_after.get(path),
            )
        except DwgWriteVerificationError as exc:
            results[path] = DwgUpdateResult(path=path, error=f"{exc} {_restore_with_note(project_root, path)}")
        else:
            results[path] = DwgUpdateResult(path=path, error=None)
    return results


def update_dwg_attributes(path: str | Path, changes: dict[str, str], *, project_root: str | Path) -> None:
    """Uppdaterar TRVJ_NAMNRUTA-attribut i en enskild DWG-fil, med
    backup före skrivning och read-back-verifiering efter.

    `project_root` avgör var backup-kopian läggs (se dwg/backup.py).
    Höjer DwgAttributeError om blocket saknas eller om någon begärd
    attributtagg inte finns i ritningen (ingen ändring sparas i så
    fall - motsvarar baslinjens beteende). Höjer
    DwgWriteVerificationError om skrivningen lyckades enligt
    accoreconsole men read-back-verifieringen ändå upptäcker en
    avvikelse (originalet återställs automatiskt från backup i det
    fallet innan felet höjs vidare).
    """
    if not changes:
        return
    dwg_path = Path(path).expanduser().resolve()
    results = update_many_dwg_attributes({dwg_path: changes}, project_root=project_root)
    result = results[dwg_path]
    if result.error is not None:
        raise DwgAttributeError(result.error)


def update_many_dwg_attributes(
    changes_by_path: dict[Path, dict[str, str]],
    *,
    project_root: str | Path,
    max_workers: int = DEFAULT_AUTOCAD_WRITE_WORKERS,
    progress_callback: Callable[[int, int], None] | None = None,
) -> dict[Path, DwgUpdateResult]:
    """Uppdaterar TRVJ_NAMNRUTA med den gemensamma verifierade processen."""
    return _update_many_dwg_attributes_for_block(
        changes_by_path,
        project_root=project_root,
        block_name=TRVJ_BLOCK_NAME,
        max_workers=max_workers,
        progress_callback=progress_callback,
    )


def _update_many_dwg_attributes_for_block(
    changes_by_path: dict[Path, dict[str, str]],
    *,
    project_root: str | Path,
    block_name: str,
    max_workers: int = DEFAULT_AUTOCAD_WRITE_WORKERS,
    progress_callback: Callable[[int, int], None] | None = None,
) -> dict[Path, DwgUpdateResult]:
    """Kör samma backup-, AutoCAD-skriv- och verifieringsflöde för ett block.

    `progress_callback(done, total)` anropas efter färdig bearbetning av
    varje chunk, efter både skrivning och verifiering.

    Säkerhetsflödet per fil, i tur och ordning:
    1. Läs nuvarande attribut snabbt med Rust/LibreDWG (facit för den
       snabba verifieringen). AutoCADs egna före-värden skrivs ut av
       skrivskriptet i samma session, så ingen separat AutoCAD-start
       behövs före skrivningen.
    2. Skapa/skriv över backup-kopian (dwg/backup.py).
    3. Skriv de nya attributen via accoreconsole.
    4. Läs tillbaka filen från disk och verifiera att ändringen blev
       korrekt och att inga andra attribut oavsiktligt påverkades:
       först Rust, sedan en gemensam AutoCAD-läsning för filer som Rust
       inte entydigt kan bekräfta (dwg/validators.py).
    5. Om verifieringen misslyckas: återställ originalet från backupen
       och rapportera felet.

    Att låta AutoCAD arbeta på en lokal arbetskopia i stället för på
    nätverksfilen provades 2026-10-02 och gav ingen mätbar vinst
    (15,4 s mot 15,3 s på GM-Uppdrag); tiden domineras av AutoCAD-starten.
    """
    items = tuple(
        (Path(path).expanduser().resolve(), changes) for path, changes in changes_by_path.items() if changes
    )
    if not items:
        return {}

    total = len(items)
    completed = 0
    results: dict[Path, DwgUpdateResult] = {}

    # Steg 1: avvisa exklusivt låsta filer innan någon läsning, backup eller
    # AutoCAD-skrivning sker.
    writable_items: list[tuple[Path, dict[str, str]]] = []
    lock_errors: dict[Path, DwgUpdateResult] = {}
    for path, changes in items:
        try:
            ensure_not_locked(path)
        except BackupError as exc:
            lock_errors[path] = DwgUpdateResult(path=path, error=str(exc))
        else:
            writable_items.append((path, changes))

    if not writable_items:
        return lock_errors

    # Steg 2: snabb Rust-läsning av nuvarande attribut innan något skrivs,
    # som facit för den snabba read-back-verifieringen.
    rust_before_by_path = _read_with_rust(tuple(path for path, _ in writable_items), block_name)

    # Steg 3: skapa/skriv över backup-kopior för samtliga filer innan
    # någon skrivning påbörjas.
    backup_errors: dict[Path, DwgUpdateResult] = {}
    backed_up_items: list[tuple[Path, dict[str, str]]] = []
    for path, changes in writable_items:
        try:
            create_backup(project_root, path)
        except BackupError as exc:
            backup_errors[path] = DwgUpdateResult(path=path, error=f"Kunde inte skapa backup, avbryter: {exc}")
            continue
        backed_up_items.append((path, changes))
    writable_items = backed_up_items

    def run_chunk(chunk: tuple[tuple[Path, dict[str, str]], ...]) -> dict[Path, DwgUpdateResult]:
        chunk_paths = tuple(path for path, _ in chunk)
        chunk_results = _run_update_batch(chunk, block_name)
        retry_required = any(
            result.error is not None and _is_retryable_autocad_error(result.error)
            for result in chunk_results.values()
        )
        if retry_required:
            restore_succeeded = True
            for path in chunk_paths:
                try:
                    restore_backup(project_root, path)
                except BackupError as restore_exc:
                    restore_succeeded = False
                    chunk_results[path] = DwgUpdateResult(
                        path=path,
                        error=f"AutoCAD-försöket misslyckades och backup kunde inte återställas: {restore_exc}",
                    )
            if restore_succeeded:
                for _ in range(_AUTOCAD_WRITE_RETRIES):
                    retry_results = _run_update_batch(chunk, block_name)
                    chunk_results.update(retry_results)
        final_results: dict[Path, DwgUpdateResult] = {}
        written: list[tuple[Path, dict[str, str], tuple | None]] = []
        for path, changes in chunk:
            write_result = chunk_results[path]
            if write_result.error is not None:
                safe_prewrite_errors = (
                    "Hittade inget block med namnet ",
                    "Följande attribut hittades inte i ritningen, ingen ändring sparades:",
                )
                if write_result.error.startswith(safe_prewrite_errors):
                    final_results[path] = write_result
                    continue
                final_results[path] = DwgUpdateResult(
                    path=path,
                    error=f"{write_result.error} {_restore_with_note(project_root, path)}",
                )
                continue
            written.append((path, dict(changes), write_result.fields_before))
        final_results.update(
            _verify_written_files(
                project_root,
                written,
                rust_before_by_path,
                block_name=block_name,
                max_workers=max_workers,
            )
        )
        return final_results

    worker_count = max(1, min(max_workers, len(writable_items))) if writable_items else 0
    if worker_count:
        chunks: list[list[tuple[Path, dict[str, str]]]] = [[] for _ in range(worker_count)]
        for index, item in enumerate(writable_items):
            chunks[index % worker_count].append(item)
        non_empty_chunks = [tuple(chunk) for chunk in chunks if chunk]

        with ThreadPoolExecutor(max_workers=len(non_empty_chunks)) as executor:
            futures = [executor.submit(run_chunk, chunk) for chunk in non_empty_chunks]
            for future, chunk in zip(futures, non_empty_chunks, strict=True):
                chunk_results = future.result()
                results.update(chunk_results)
                completed += len(chunk)
                if progress_callback is not None:
                    progress_callback(completed, total)

    results.update(lock_errors)
    results.update(backup_errors)
    if progress_callback is not None and backup_errors:
        completed += len(backup_errors)
        progress_callback(completed, total)

    return results


def update_many_dwg_model_attributes(
    changes_by_path: dict[Path, dict[str, str]],
    *,
    project_root: str | Path,
    max_workers: int = DEFAULT_MAX_PARALLEL_WORKERS,
    progress_callback: Callable[[int, int], None] | None = None,
) -> dict[Path, DwgUpdateResult]:
    """Uppdaterar TRVJ_NAMNRUTA_MODELL via samma process som standardblocket."""
    return _update_many_dwg_attributes_for_block(
        changes_by_path,
        project_root=project_root,
        block_name=TRVJ_MODEL_BLOCK_NAME,
        max_workers=max_workers,
        progress_callback=progress_callback,
    )
