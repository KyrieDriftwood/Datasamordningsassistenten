"""Snabb, läsande extraktion av DWG-attribut (Rust/LibreDWG-bryggan).

Motsvarar den kliniska logiken i .old/python_v5/src/version_5_rust.py,
men den körbara Rust-komponenten är inte längre kvar i `.old/` —
källkod finns i den aktiva [rust_dwg_extractor/](../rust_dwg_extractor)
i projektroten, eftersom detta är levande funktionalitet, inte
historik. Binären är ombyggd från källa där (GNU-toolchain, se
rust_dwg_extractor/README.md) och verifierad mot exempel-DWG samt mot
baslinjens src/version_5_rust.py.

Prestanda: i stället för en process per fil återanvänds långlivade
`--serve`-workers (protokoll i README). Timeout, krasch eller ogiltigt svar
kasserar bara den berörda workern och ger fel för just den filen. Batchläget
(flera filer per process) används för äldre binärer och när
DATASAMORDNING_RUST_DWG_WORKER=0.

Kravkälla: docs/kravsparning.md, modul dwg/rust_bridge.

Känd, dokumenterad avvikelse i baslinjen (gäller tills vidare, se
Kravpolicy i docs/plan.md): attributläsningen triggas automatiskt vid
kryssruta i UI, inte via en explicit knapp som TB-sektion D
föreskriver. Baslinjens egen kodkommentar förklarar att detta gjordes
avsiktligt för att undvika en krasch (access violation) som uppstod
med det strikta "endast explicit knapp"-flödet.
"""

from __future__ import annotations

import atexit
import math
import os
import queue
import subprocess
import threading
from collections.abc import Callable, Iterable
from pathlib import Path

from dwg import DwgAttributeError, DwgAttributeResult, DwgError, TRVJ_BLOCK_NAME, parse_extract_output

_RUST_EXTRACTOR_ENV_OVERRIDE = "DATASAMORDNING_RUST_DWG_EXTRACTOR"
_DEFAULT_EXTRACTOR_RELATIVE_PATH = Path("rust_dwg_extractor") / "target" / "release" / "rust_dwg_extractor.exe"
_EXTRACTION_TIMEOUT_SECONDS = 30
_DEFAULT_MAX_PARALLEL_WORKERS = 8

# Långlivade `--serve`-workers sparar process- och DLL-start per fil. De kan
# stängas av (t.ex. vid felsökning) med DATASAMORDNING_RUST_DWG_WORKER=0;
# då används batchläget (flera filer per process) i stället.
_WORKER_ENV_SWITCH = "DATASAMORDNING_RUST_DWG_WORKER"
_WORKER_READY_TIMEOUT_SECONDS = 15
# LibreDWG-strängar läcker medvetet i Rust-processen (se main.rs); en worker
# återstartas därför efter ett begränsat antal filer så minnet hålls nere.
_WORKER_MAX_FILES = 250
_MAX_IDLE_WORKERS_PER_EXE = 8
_BATCH_MAX_FILES = 32
# Windows CreateProcess tillåter 32 767 tecken; marginal för exe och flaggor.
_BATCH_MAX_COMMAND_CHARS = 24_000
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class RustExtractorNotFoundError(DwgError):
    """Kastas när rust_dwg_extractor.exe inte kan hittas eller inte är byggd."""


def _repo_root() -> Path:
    """Projektroten (där ``rust_dwg_extractor/`` ligger). Skiljer sig
    från baslinjens ``version_5_rust.py:_repo_root`` genom att räkna
    upp från denna moduls plats (``dwg/rust_bridge.py``, ett steg upp
    i paketstrukturen jämfört med baslinjens ``src/version_5_rust.py``).
    """
    return Path(__file__).resolve().parent.parent


def locate_rust_extractor() -> Path:
    """Hittar rust_dwg_extractor.exe.

    Direkt port av ``version_5_rust.py:locate_rust_extractor``.
    Sökvägen kan override:as med miljövariabeln
    DATASAMORDNING_RUST_DWG_EXTRACTOR, annars förväntas den ligga på sin
    vanliga build-plats under `rust_dwg_extractor/target/release/`.
    """
    override = os.environ.get(_RUST_EXTRACTOR_ENV_OVERRIDE)
    if override:
        override_path = Path(override)
        if override_path.is_file():
            return override_path
        raise RustExtractorNotFoundError(
            f"Miljövariabeln {_RUST_EXTRACTOR_ENV_OVERRIDE} pekar på en fil som inte finns: {override_path}"
        )

    default_path = _repo_root() / _DEFAULT_EXTRACTOR_RELATIVE_PATH
    if default_path.is_file():
        return default_path

    raise RustExtractorNotFoundError(
        f"Hittade ingen byggd rust_dwg_extractor.exe under {default_path}. "
        "Bygg den med:\ncd rust_dwg_extractor\ncargo build --release"
    )


def is_available() -> bool:
    """True om rust_dwg_extractor.exe kan hittas (utan att kasta undantag).

    Direkt port av ``version_5_rust.py:is_available``.
    """
    try:
        locate_rust_extractor()
    except RustExtractorNotFoundError:
        return False
    return True


def _workers_enabled() -> bool:
    return os.environ.get(_WORKER_ENV_SWITCH, "1").strip().lower() not in {"0", "false", "no", "off"}


def _worker_command(exe_path: Path) -> list[str]:
    return [str(exe_path), "--serve"]


def _batch_command(exe_path: Path, block_name: str, paths: tuple[Path, ...]) -> list[str]:
    return [str(exe_path), "--block", block_name, *(str(path) for path in paths)]


def _timeout_error(path: Path, timeout: float) -> DwgAttributeResult:
    return DwgAttributeResult(
        path=path,
        fields=(),
        error=f"rust_dwg_extractor svarade inte inom {timeout} sekunder för {path.name}.",
    )


def _exit_error(path: Path, returncode: int | None, detail: str = "") -> DwgAttributeResult:
    suffix = f" ({detail})" if detail else ""
    return DwgAttributeResult(
        path=path,
        fields=(),
        error=f"rust_dwg_extractor avslutades med felkod {returncode} för {path.name}.{suffix}",
    )


def _protocol_error(path: Path) -> DwgAttributeResult:
    return DwgAttributeResult(
        path=path,
        fields=(),
        error=f"rust_dwg_extractor returnerade ett ogiltigt svar för {path.name}.",
    )


def _start_error(path: Path, exc: OSError) -> DwgAttributeResult:
    return DwgAttributeResult(path=path, fields=(), error=f"Kunde inte starta rust_dwg_extractor: {exc}")


def _parse_segment(text: str, paths: tuple[Path, ...], block_name: str) -> dict[Path, DwgAttributeResult]:
    """Tolkar ett eller flera `###FILE`-segment; ogiltigt protokoll ger {}."""
    try:
        return parse_extract_output(
            text,
            paths,
            block_name=block_name,
            include_unknown_tags=block_name != TRVJ_BLOCK_NAME,
        )
    except (ValueError, IndexError):
        return {}


class _WorkerUnavailable(Exception):
    """Binären startar inte i `--serve`-läge (t.ex. äldre bygge)."""


class _WorkerTimeout(Exception):
    pass


class _WorkerExited(Exception):
    def __init__(self, returncode: int | None) -> None:
        super().__init__(returncode)
        self.returncode = returncode


class _WorkerProtocolError(Exception):
    pass


class _RustWorker:
    """En `rust_dwg_extractor --serve`-process som betjänar en fil i taget.

    Kontrakt med main.rs: första raden är `###READY <version>`, därefter ett
    `###LEN <n>`-huvud plus exakt n bytes per begäran. En läsartråd lägger
    färdiga ramar i en kö så att anroparen kan vänta med timeout. En worker
    används av högst en tråd åt gången (poolen garanterar det) och kasseras
    efter timeout/fel, så att en kvarvarande ram aldrig kan paras ihop med
    nästa begäran.
    """

    def __init__(self, exe_path: Path, key: tuple[str, int]) -> None:
        self.key = key
        self.files_served = 0
        self._frames: queue.Queue[tuple[str, bytes]] = queue.Queue()
        self._process = subprocess.Popen(
            _worker_command(exe_path),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=_CREATE_NO_WINDOW,
        )
        threading.Thread(target=self._read_frames, name="rust-dwg-worker-reader", daemon=True).start()
        try:
            kind, _payload = self._frames.get(timeout=_WORKER_READY_TIMEOUT_SECONDS)
        except queue.Empty:
            kind = "timeout"
        if kind != "ready":
            self.close(graceful=False)
            raise _WorkerUnavailable(kind)

    def _read_frames(self) -> None:
        stdout = self._process.stdout
        assert stdout is not None
        try:
            first = stdout.readline()
            if not first.startswith(b"###READY "):
                self._frames.put(("bad", first))
                return
            self._frames.put(("ready", first))
            while True:
                header = stdout.readline()
                if not header:
                    return
                if not header.startswith(b"###LEN "):
                    self._frames.put(("bad", header))
                    return
                try:
                    size = int(header[len(b"###LEN ") :].strip())
                except ValueError:
                    self._frames.put(("bad", header))
                    return
                payload = stdout.read(size)
                if len(payload) != size:
                    return
                self._frames.put(("frame", payload))
        except (OSError, ValueError):
            return
        finally:
            self._frames.put(("eof", b""))

    def is_alive(self) -> bool:
        return self._process.poll() is None

    def _returncode(self) -> int | None:
        try:
            return self._process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            return None

    def extract(self, path: Path, block_name: str, timeout: float) -> DwgAttributeResult:
        request = f"{block_name}\t{path}\n".encode("utf-8")
        stdin = self._process.stdin
        assert stdin is not None
        try:
            stdin.write(request)
            stdin.flush()
        except OSError as exc:
            raise _WorkerExited(self._returncode()) from exc
        try:
            kind, payload = self._frames.get(timeout=timeout)
        except queue.Empty as exc:
            raise _WorkerTimeout from exc
        if kind == "eof":
            raise _WorkerExited(self._returncode())
        if kind != "frame":
            raise _WorkerProtocolError
        self.files_served += 1
        result = _parse_segment(payload.decode("utf-8", errors="replace"), (path,), block_name).get(path)
        if result is None:
            raise _WorkerProtocolError
        return result

    def close(self, *, graceful: bool) -> None:
        process = self._process
        if graceful and process.poll() is None:
            try:
                if process.stdin is not None:
                    process.stdin.close()
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass
        if process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass


class _RustWorkerPool:
    """Trådsäker pool av lediga workers, nycklad på (exe-sökväg, mtime).

    Mtime i nyckeln gör att en ombyggd binär aldrig betjänas av en gammal
    process. Lediga workers håller exe-filen låst; de stängs vid
    programavslut (atexit) eller via `close_all()` före en ombyggnad.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._idle: dict[tuple[str, int], list[_RustWorker]] = {}
        self._unsupported: set[tuple[str, int]] = set()

    @staticmethod
    def key_for(exe_path: Path) -> tuple[str, int]:
        try:
            mtime = exe_path.stat().st_mtime_ns
        except OSError:
            mtime = 0
        return (str(exe_path), mtime)

    def is_unsupported(self, key: tuple[str, int]) -> bool:
        with self._lock:
            return key in self._unsupported

    def mark_unsupported(self, key: tuple[str, int]) -> None:
        with self._lock:
            self._unsupported.add(key)

    def acquire(self, exe_path: Path, key: tuple[str, int]) -> _RustWorker:
        stale: list[_RustWorker] = []
        reused: _RustWorker | None = None
        with self._lock:
            for idle_key in [k for k in self._idle if k[0] == key[0] and k != key]:
                stale.extend(self._idle.pop(idle_key))
            idle = self._idle.get(key, [])
            while idle and reused is None:
                candidate = idle.pop()
                if candidate.is_alive():
                    reused = candidate
                else:
                    stale.append(candidate)
        for worker in stale:
            worker.close(graceful=False)
        if reused is not None:
            return reused
        return _RustWorker(exe_path, key)

    def release(self, worker: _RustWorker) -> None:
        if worker.files_served < _WORKER_MAX_FILES and worker.is_alive():
            with self._lock:
                idle = self._idle.setdefault(worker.key, [])
                if len(idle) < _MAX_IDLE_WORKERS_PER_EXE:
                    idle.append(worker)
                    return
        worker.close(graceful=True)

    def close_all(self) -> None:
        with self._lock:
            workers = [worker for idle in self._idle.values() for worker in idle]
            self._idle.clear()
        for worker in workers:
            worker.close(graceful=True)


_POOL = _RustWorkerPool()
atexit.register(lambda: _POOL.close_all())


def shutdown_rust_workers() -> None:
    """Stänger lediga `--serve`-workers (frigör låset på exe-filen)."""
    _POOL.close_all()


def _run_single_file(
    exe_path: Path,
    path: Path,
    timeout: int,
    block_name: str = TRVJ_BLOCK_NAME,
) -> DwgAttributeResult:
    """Extraherar en fil via en poolad worker, med batchläget som reserv."""
    key = _POOL.key_for(exe_path)
    if not _workers_enabled() or _POOL.is_unsupported(key):
        return _run_batch_collect(exe_path, (path,), timeout, block_name)[path]
    try:
        worker = _POOL.acquire(exe_path, key)
    except _WorkerUnavailable:
        _POOL.mark_unsupported(key)
        return _run_batch_collect(exe_path, (path,), timeout, block_name)[path]
    except OSError as exc:
        return _start_error(path, exc)

    try:
        result = worker.extract(path, block_name, timeout)
    except _WorkerTimeout:
        worker.close(graceful=False)
        return _timeout_error(path, timeout)
    except _WorkerExited as exc:
        worker.close(graceful=False)
        return _exit_error(path, exc.returncode)
    except _WorkerProtocolError:
        worker.close(graceful=False)
        return _protocol_error(path)
    except UnicodeEncodeError:
        _POOL.release(worker)
        return DwgAttributeResult(path=path, fields=(), error=f"Sökvägen kan inte kodas som UTF-8: {path}")
    except BaseException:
        worker.close(graceful=False)
        raise
    _POOL.release(worker)
    return result


def _run_batch(
    exe_path: Path,
    paths: tuple[Path, ...],
    timeout: float,
    block_name: str,
    on_result: Callable[[Path, DwgAttributeResult], None],
) -> None:
    """Kör flera filer i en process och rapporterar varje fil när dess
    `###END` anländer. Timeouten gäller per fil (nollställs per rad)."""
    import tempfile

    pending = list(dict.fromkeys(paths))
    with tempfile.TemporaryFile() as stderr_file:
        try:
            process = subprocess.Popen(
                _batch_command(exe_path, block_name, paths),
                stdout=subprocess.PIPE,
                stderr=stderr_file,
                creationflags=_CREATE_NO_WINDOW,
            )
        except OSError as exc:
            for path in pending:
                on_result(path, _start_error(path, exc))
            return

        lines: queue.Queue[bytes | None] = queue.Queue()

        def read_lines() -> None:
            assert process.stdout is not None
            try:
                for line in process.stdout:
                    lines.put(line)
            except (OSError, ValueError):
                pass
            finally:
                lines.put(None)

        threading.Thread(target=read_lines, name="rust-dwg-batch-reader", daemon=True).start()
        segment: list[bytes] = []
        timed_out = False
        try:
            while pending:
                try:
                    line = lines.get(timeout=timeout)
                except queue.Empty:
                    timed_out = True
                    break
                if line is None:
                    break
                segment.append(line)
                if not line.startswith(b"###END"):
                    continue
                text = b"".join(segment).decode("utf-8", errors="replace")
                segment = []
                for path, result in _parse_segment(text, paths, block_name).items():
                    if path in pending:
                        pending.remove(path)
                        on_result(path, result)
        finally:
            if process.poll() is None and (timed_out or pending):
                process.kill()
            try:
                returncode = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                returncode = None
            if process.stdout is not None:
                process.stdout.close()

        if not pending:
            return
        stderr_file.seek(0)
        detail = stderr_file.read().decode("utf-8", errors="replace").strip()
        for path in pending:
            if timed_out:
                on_result(path, _timeout_error(path, timeout))
            elif returncode not in (0, None):
                on_result(path, _exit_error(path, returncode, detail))
            else:
                on_result(path, _protocol_error(path))


def _run_batch_collect(
    exe_path: Path, paths: tuple[Path, ...], timeout: float, block_name: str
) -> dict[Path, DwgAttributeResult]:
    results: dict[Path, DwgAttributeResult] = {}
    _run_batch(exe_path, paths, timeout, block_name, results.__setitem__)
    return results


def _batch_paths(paths: tuple[Path, ...], worker_count: int) -> list[tuple[Path, ...]]:
    """Delar upp filer i batcher, begränsade av antal och kommandoradslängd."""
    if not paths:
        return []
    size = max(1, min(_BATCH_MAX_FILES, math.ceil(len(paths) / max(1, worker_count))))
    batches: list[tuple[Path, ...]] = []
    current: list[Path] = []
    chars = 0
    for path in paths:
        cost = len(str(path)) + 3
        if current and (len(current) >= size or chars + cost > _BATCH_MAX_COMMAND_CHARS):
            batches.append(tuple(current))
            current = []
            chars = 0
        current.append(path)
        chars += cost
    batches.append(tuple(current))
    return batches


def extract_many_dwg_attributes_fast(
    paths: Iterable[str | Path],
    *,
    max_workers: int = _DEFAULT_MAX_PARALLEL_WORKERS,
    timeout: int = _EXTRACTION_TIMEOUT_SECONDS,
    progress_callback: Callable[[int, int], None] | None = None,
) -> dict[Path, DwgAttributeResult]:
    """Extraherar TRVJ_NAMNRUTA-attribut för flera DWG-filer via Rust/LibreDWG.

    Funktionell port av ``version_5_rust.py:extract_many_dwg_attributes_fast``.
    Upp till `max_workers` filer behandlas parallellt, via återanvända
    `--serve`-workers (eller batchprocesser om workers inte stöds).
    `progress_callback` (done, total) anropas i anroparens tråd efter
    varje färdig fil.

    Höjer RustExtractorNotFoundError om verktyget inte är byggt/hittas.
    """
    resolved_paths = tuple(Path(path).expanduser().resolve() for path in paths)
    if not resolved_paths:
        return {}

    return extract_many_dwg_attributes_for_block(
        resolved_paths,
        TRVJ_BLOCK_NAME,
        max_workers=max_workers,
        timeout=timeout,
        progress_callback=progress_callback,
    )


def extract_many_dwg_attributes_for_block(
    paths: Iterable[str | Path],
    block_name: str,
    *,
    max_workers: int = _DEFAULT_MAX_PARALLEL_WORKERS,
    timeout: int = _EXTRACTION_TIMEOUT_SECONDS,
    progress_callback: Callable[[int, int], None] | None = None,
) -> dict[Path, DwgAttributeResult]:
    """Extraherar attribut för ett namngivet DWG-block via LibreDWG."""
    resolved_paths = tuple(dict.fromkeys(Path(path).expanduser().resolve() for path in paths))
    if not resolved_paths:
        return {}
    if not block_name or any(char in block_name for char in "\x00\t\r\n"):
        raise ValueError("Blocknamnet måste vara en icke-tom sträng utan null-byte, tabb eller radbrytning.")

    exe_path = locate_rust_extractor()
    total = len(resolved_paths)
    worker_count = max(1, min(max_workers, total))
    use_workers = _workers_enabled() and not _POOL.is_unsupported(_POOL.key_for(exe_path))

    events: queue.Queue[tuple] = queue.Queue()
    work: queue.Queue[Path | tuple[Path, ...]] = queue.Queue()
    units: list[Path | tuple[Path, ...]] = (
        list(resolved_paths) if use_workers else list(_batch_paths(resolved_paths, worker_count))
    )
    for unit in units:
        work.put(unit)

    def report(path: Path, result: DwgAttributeResult) -> None:
        events.put(("result", path, result))

    def run_units() -> None:
        try:
            while True:
                try:
                    unit = work.get_nowait()
                except queue.Empty:
                    return
                if isinstance(unit, Path):
                    report(unit, _run_single_file(exe_path, unit, timeout, block_name))
                else:
                    _run_batch(exe_path, unit, timeout, block_name, report)
        except BaseException as exc:  # vidarebefordras till anroparens tråd
            events.put(("error", exc))
        finally:
            events.put(("done",))

    thread_count = max(1, min(worker_count, len(units)))
    threads = [
        threading.Thread(target=run_units, name=f"rust-dwg-extract-{index}", daemon=True)
        for index in range(thread_count)
    ]
    for thread in threads:
        thread.start()

    results: dict[Path, DwgAttributeResult] = {}
    first_error: BaseException | None = None
    done_threads = 0
    while done_threads < thread_count:
        event = events.get()
        if event[0] == "done":
            done_threads += 1
        elif event[0] == "error":
            first_error = first_error or event[1]
        else:
            _kind, path, result = event
            if path not in results:
                results[path] = result
                if progress_callback is not None:
                    progress_callback(len(results), total)
    for thread in threads:
        thread.join()
    if first_error is not None:
        raise first_error
    return results


def extract_dwg_attributes_fast(path: str | Path) -> tuple:
    """Extraherar TRVJ_NAMNRUTA-attribut från en enskild DWG-fil via Rust/LibreDWG.

    Direkt port av ``version_5_rust.py:extract_dwg_attributes_fast``.
    """
    dwg_path = Path(path).expanduser().resolve()
    results = extract_many_dwg_attributes_fast((dwg_path,))
    result = results[dwg_path]
    if result.error is not None:
        raise DwgAttributeError(result.error)
    return result.fields


def extract_many_dwg_model_attributes(
    paths: Iterable[str | Path],
    *,
    max_workers: int = _DEFAULT_MAX_PARALLEL_WORKERS,
    timeout: int = _EXTRACTION_TIMEOUT_SECONDS,
    progress_callback: Callable[[int, int], None] | None = None,
) -> dict[Path, DwgAttributeResult]:
    """Läser `TRVJ_NAMNRUTA_MODELL` separat från den ordinarie namnrutan."""
    from dwg import TRVJ_MODEL_BLOCK_NAME

    return extract_many_dwg_attributes_for_block(
        paths,
        TRVJ_MODEL_BLOCK_NAME,
        max_workers=max_workers,
        timeout=timeout,
        progress_callback=progress_callback,
    )
