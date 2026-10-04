"""Tester för Rust-bryggans process-återanvändning (dwg/rust_bridge.py).

Worker-/batchlogiken testas mot små Python-skript som talar samma protokoll
som `rust_dwg_extractor` (`--serve`: `###READY`/`###LEN`, batch:
`###FILE:i`/`###END`). Det ger deterministiska fall för timeout, krasch,
äldre binär och återstart utan att kräva LibreDWG. Ett avslutande test kör
den riktiga binären och jämför worker- mot batchläget.
"""

from __future__ import annotations

import os
import shutil
import sys
import threading
from pathlib import Path

import pytest

from dwg import rust_bridge

REPO_ROOT = Path(__file__).resolve().parents[1]
DWG_FIXTURES = sorted((REPO_ROOT / "Examples").rglob("*.dwg"))

FAKE_WORKER = r'''
import sys, time
from pathlib import Path
mode, log = sys.argv[1], Path(sys.argv[2])
with log.open("a", encoding="utf-8") as handle:
    handle.write("worker\n")
out = sys.stdout.buffer
if mode == "old":
    out.write(b"###FILE:0\n###BLOCK_NOT_FOUND\n###END\n")
    out.flush()
    sys.exit(0)
out.write(b"###READY 1\n")
out.flush()
for raw in sys.stdin.buffer:
    _block, _, path = raw.decode("utf-8").rstrip("\r\n").partition("\t")
    name = Path(path).stem
    if "hang" in name:
        time.sleep(60)
    if "crash" in name:
        sys.exit(3)
    payload = f"###FILE:0\nSlm\t{name}\t0\n###END\n".encode("utf-8")
    out.write(b"###LEN %d\n" % len(payload))
    out.write(payload)
    out.flush()
'''

FAKE_BATCH = r'''
import sys, time
from pathlib import Path
log = Path(sys.argv[1])
paths = sys.argv[4:]
with log.open("a", encoding="utf-8") as handle:
    handle.write(f"batch {len(paths)}\n")
out = sys.stdout.buffer
for index, path in enumerate(paths):
    name = Path(path).stem
    if "hang" in name:
        time.sleep(60)
    if "crash" in name:
        sys.stderr.write("boom")
        sys.exit(3)
    out.write(f"###FILE:{index}\nSlm\t{name}\t0\n###END\n".encode("utf-8"))
    out.flush()
'''


@pytest.fixture
def fake_extractor(tmp_path, monkeypatch):
    worker_script = tmp_path / "fake_worker.py"
    worker_script.write_text(FAKE_WORKER, encoding="utf-8")
    batch_script = tmp_path / "fake_batch.py"
    batch_script.write_text(FAKE_BATCH, encoding="utf-8")
    log = tmp_path / "spawn.log"
    exe = tmp_path / "fake_extractor.exe"
    exe.write_bytes(b"")
    state = {"mode": "serve"}

    pool = rust_bridge._RustWorkerPool()
    monkeypatch.setattr(rust_bridge, "_POOL", pool)
    monkeypatch.delenv(rust_bridge._WORKER_ENV_SWITCH, raising=False)
    monkeypatch.setattr(rust_bridge, "locate_rust_extractor", lambda: exe)
    monkeypatch.setattr(
        rust_bridge,
        "_worker_command",
        lambda _exe: [sys.executable, str(worker_script), state["mode"], str(log)],
    )
    monkeypatch.setattr(
        rust_bridge,
        "_batch_command",
        lambda _exe, block, paths: [sys.executable, str(batch_script), str(log), "--block", block, *map(str, paths)],
    )

    def spawns(kind: str) -> int:
        if not log.exists():
            return 0
        return sum(1 for line in log.read_text(encoding="utf-8").splitlines() if line.startswith(kind))

    def drawings(*names: str) -> list[Path]:
        paths = []
        for name in names:
            path = tmp_path / f"{name}.dwg"
            path.touch()
            paths.append(path)
        return paths

    yield state, spawns, drawings, pool, exe
    pool.close_all()


def _values(results):
    return {
        path.stem: (result.fields[0].value if result.fields else None, result.error)
        for path, result in results.items()
    }


def test_worker_is_reused_across_files_and_calls(fake_extractor):
    _state, spawns, drawings, _pool, _exe = fake_extractor
    paths = drawings("a", "b", "c", "d")

    first = rust_bridge.extract_many_dwg_attributes_fast(paths, max_workers=1)
    second = rust_bridge.extract_many_dwg_attributes_fast(paths[:2], max_workers=1)

    assert _values(first) == {name: (name, None) for name in "abcd"}
    assert _values(second) == {name: (name, None) for name in "ab"}
    assert spawns("worker") == 1
    assert spawns("batch") == 0


def test_worker_timeout_only_fails_that_file_and_respawns(fake_extractor):
    _state, spawns, drawings, _pool, _exe = fake_extractor
    paths = drawings("a", "hang_b", "c")

    results = _values(rust_bridge.extract_many_dwg_attributes_fast(paths, max_workers=1, timeout=1))

    assert results["a"] == ("a", None)
    assert results["c"] == ("c", None)
    assert results["hang_b"][0] is None
    assert "svarade inte inom 1 sekunder för hang_b.dwg" in results["hang_b"][1]
    assert spawns("worker") == 2


def test_worker_crash_only_fails_that_file_and_respawns(fake_extractor):
    _state, spawns, drawings, _pool, _exe = fake_extractor
    paths = drawings("a", "crash_b", "c")

    results = _values(rust_bridge.extract_many_dwg_attributes_fast(paths, max_workers=1))

    assert results["a"] == ("a", None)
    assert results["c"] == ("c", None)
    assert "avslutades med felkod 3 för crash_b.dwg" in results["crash_b"][1]
    assert spawns("worker") == 2


def test_old_binary_without_serve_falls_back_to_batch(fake_extractor):
    state, spawns, drawings, pool, exe = fake_extractor
    state["mode"] = "old"
    paths = drawings("a", "b", "c")

    first = rust_bridge.extract_many_dwg_attributes_fast(paths, max_workers=1)
    workers_after_first = spawns("worker")
    second = rust_bridge.extract_many_dwg_attributes_fast(paths, max_workers=1)

    assert _values(first) == {name: (name, None) for name in "abc"}
    assert _values(second) == _values(first)
    assert pool.is_unsupported(pool.key_for(exe))
    assert workers_after_first == 1
    assert spawns("worker") == 1
    assert spawns("batch") >= 2


def test_rebuilt_binary_is_not_treated_as_unsupported(fake_extractor):
    state, spawns, drawings, pool, exe = fake_extractor
    state["mode"] = "old"
    rust_bridge.extract_many_dwg_attributes_fast(drawings("a"), max_workers=1)
    state["mode"] = "serve"
    stat = exe.stat()
    os.utime(exe, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

    results = rust_bridge.extract_many_dwg_attributes_fast(drawings("b"), max_workers=1)

    assert _values(results) == {"b": ("b", None)}
    assert spawns("worker") == 2


def test_disabled_workers_use_batches_with_progress_on_caller_thread(fake_extractor, monkeypatch):
    _state, spawns, drawings, _pool, _exe = fake_extractor
    monkeypatch.setenv(rust_bridge._WORKER_ENV_SWITCH, "0")
    paths = drawings(*(f"f{index}" for index in range(6)))
    progress = []
    caller = threading.get_ident()

    results = rust_bridge.extract_many_dwg_attributes_fast(
        paths,
        max_workers=2,
        progress_callback=lambda done, total: progress.append((done, total, threading.get_ident())),
    )

    assert _values(results) == {f"f{index}": (f"f{index}", None) for index in range(6)}
    assert [(done, total) for done, total, _ in progress] == [(index, 6) for index in range(1, 7)]
    assert {thread for _, _, thread in progress} == {caller}
    assert spawns("worker") == 0
    assert spawns("batch") == 2


def test_batch_crash_reports_unfinished_files(fake_extractor, monkeypatch):
    _state, _spawns, drawings, _pool, _exe = fake_extractor
    monkeypatch.setenv(rust_bridge._WORKER_ENV_SWITCH, "0")
    paths = drawings("a", "crash_b", "c")

    results = _values(rust_bridge.extract_many_dwg_attributes_fast(paths, max_workers=1))

    assert results["a"] == ("a", None)
    assert "avslutades med felkod 3 för crash_b.dwg. (boom)" in results["crash_b"][1]
    assert "felkod 3 för c.dwg" in results["c"][1]


def test_batch_timeout_reports_waiting_file(fake_extractor, monkeypatch):
    _state, _spawns, drawings, _pool, _exe = fake_extractor
    monkeypatch.setenv(rust_bridge._WORKER_ENV_SWITCH, "0")
    paths = drawings("a", "hang_b")

    results = _values(rust_bridge.extract_many_dwg_attributes_fast(paths, max_workers=1, timeout=1))

    assert results["a"] == ("a", None)
    assert "svarade inte inom 1 sekunder för hang_b.dwg" in results["hang_b"][1]


def test_worker_is_recycled_after_file_limit(fake_extractor, monkeypatch):
    _state, spawns, drawings, _pool, _exe = fake_extractor
    monkeypatch.setattr(rust_bridge, "_WORKER_MAX_FILES", 2)

    results = rust_bridge.extract_many_dwg_attributes_fast(drawings(*"abcde"), max_workers=1)

    assert _values(results) == {name: (name, None) for name in "abcde"}
    assert spawns("worker") == 3


def test_duplicate_paths_are_extracted_once(fake_extractor):
    _state, _spawns, drawings, _pool, _exe = fake_extractor
    path = drawings("a")[0]
    progress = []

    results = rust_bridge.extract_many_dwg_attributes_fast(
        [path, path], progress_callback=lambda done, total: progress.append((done, total))
    )

    assert _values(results) == {"a": ("a", None)}
    assert progress == [(1, 1)]


def test_block_name_with_protocol_separator_is_rejected(fake_extractor):
    _state, _spawns, drawings, _pool, _exe = fake_extractor
    with pytest.raises(ValueError):
        rust_bridge.extract_many_dwg_attributes_for_block(drawings("a"), "BAD\tBLOCK")


def test_batch_paths_respects_file_and_command_line_limits():
    paths = tuple(Path(f"C:/p/{index}.dwg") for index in range(100))
    batches = rust_bridge._batch_paths(paths, 2)
    assert [path for batch in batches for path in batch] == list(paths)
    assert max(len(batch) for batch in batches) <= rust_bridge._BATCH_MAX_FILES

    long_paths = tuple(Path("C:/" + "x" * 5000 + f"/{index}.dwg") for index in range(10))
    long_batches = rust_bridge._batch_paths(long_paths, 1)
    assert all(
        sum(len(str(path)) + 3 for path in batch) <= rust_bridge._BATCH_MAX_COMMAND_CHARS
        for batch in long_batches
    )
    assert [path for batch in long_batches for path in batch] == list(long_paths)


@pytest.mark.skipif(not rust_bridge.is_available() or not DWG_FIXTURES, reason="Kräver byggd binär och DWG-fixtures")
def test_real_worker_matches_batch_and_does_not_lock_drawings(tmp_path, monkeypatch):
    copies = []
    for index, fixture in enumerate(DWG_FIXTURES):
        copy = tmp_path / f"{index}_{fixture.name}"
        shutil.copy2(fixture, copy)
        copies.append(copy)
    pool = rust_bridge._RustWorkerPool()
    monkeypatch.setattr(rust_bridge, "_POOL", pool)
    try:
        monkeypatch.delenv(rust_bridge._WORKER_ENV_SWITCH, raising=False)
        worker_results = rust_bridge.extract_many_dwg_attributes_fast(copies, max_workers=2)
        assert not pool.is_unsupported(pool.key_for(rust_bridge.locate_rust_extractor()))
        assert pool._idle, "--serve-workern ska finnas kvar som ledig efter anropet"

        # Lediga workers får inte hålla DWG-filen öppen (skrivflödet kräver
        # exklusiv åtkomst för backup/återställning).
        for copy in copies:
            moved = copy.with_suffix(".moved")
            copy.rename(moved)
            moved.rename(copy)

        monkeypatch.setenv(rust_bridge._WORKER_ENV_SWITCH, "0")
        batch_results = rust_bridge.extract_many_dwg_attributes_fast(copies, max_workers=2)
    finally:
        pool.close_all()

    assert worker_results == batch_results
