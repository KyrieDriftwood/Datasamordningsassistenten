"""Tester för dwg/writers.py: skrivning med backup och read-back-verifiering.

Två sorters tester:

1. Live-parity mot en riktig accoreconsole-installation, med kopior av
   de riktiga DWG-fixturerna i Examples/ (kopior i tmp_path, så att
   testerna aldrig skriver i det egna repo:t). Båda fixturerna saknar
   TRVJ_NAMNRUTA-blocket (verifierat i test_dwg_rust_bridge.py och
   test_dwg_readers.py), så det här ger bara parity för
   "block saknas"-felvägen - INTE ett fullständigt lyckat skriv+
   verifiera-varv, eftersom ingen tillgänglig fixture har blocket.
   Det är en dokumenterad begränsning, inte en gissning: se
   docs/plan.md, Fas 3 (skrivdelen).
2. Rena enhetstester (monkeypatchade) för backup/verifiera/återställ-
   orkestreringen, som inte kräver accoreconsole alls.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".old" / "python_v5"))

import dwg.backup
import dwg.readers
import dwg.rust_bridge
import dwg.validators
import dwg.writers

from dwg import DwgAttributeError, DwgAttributeResult, DwgUpdateResult
from dwg.backup import backup_path_for, has_backup
from dwg.writers import (
    _build_update_lisp,
    _parse_update_output,
    update_dwg_attributes,
    update_many_dwg_attributes,
    update_many_dwg_model_attributes,
)
from metadata import MetadataField

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES_DIR = REPO_ROOT / "Examples"
DWG_FIXTURES = sorted(EXAMPLES_DIR.rglob("*.dwg")) if EXAMPLES_DIR.is_dir() else []

try:
    from dwg import locate_accoreconsole

    locate_accoreconsole()
    ACCORECONSOLE_AVAILABLE = True
except Exception:
    ACCORECONSOLE_AVAILABLE = False

requires_accoreconsole = pytest.mark.skipif(
    not ACCORECONSOLE_AVAILABLE, reason="Ingen accoreconsole.exe hittades på denna maskin"
)
requires_fixtures = pytest.mark.skipif(not DWG_FIXTURES, reason="Ingen DWG-fixture hittades i Examples/")


def _baseline_update_dwg_attributes(path, changes):
    from src.version_5 import update_dwg_attributes as baseline_update

    return baseline_update(path, changes)


def test_update_lisp_catches_and_records_autolisp_errors(tmp_path):
    script = _build_update_lisp(
        ((tmp_path / "drawing.dwg", {"DATUM": "2026-10-01"}),),
        tmp_path / "result.txt",
        block_name="TRVJ_NAMNRUTA_MODELL",
    )

    assert "vl-catch-all-apply 'DatasamordningUpdateOne" in script
    assert "vl-catch-all-error-message update-result" in script
    assert '"###ERROR:"' in script
    assert '###END" DatasamordningOutf' in script


def test_update_lisp_serializes_multiple_field_changes_as_autolisp_list(tmp_path):
    script = _build_update_lisp(
        (
            (
                (tmp_path / "drawing.dwg"),
                {"Slm": "Exempelö", "Datum": "2026-10-01", "Skala": "1:1000"},
            ),
        ),
        tmp_path / "result.txt",
    )

    assert '(list (cons "Slm" "Exempelö") (cons "Datum" "2026-10-01") (cons "Skala" "1:1000"))' in script
    assert '(list (cons "Slm" "Exempelö"),' not in script


def test_parse_update_output_includes_autolisp_error_detail(tmp_path):
    path = tmp_path / "drawing.dwg"
    result = _parse_update_output(
        "###FILE:0\n###ERROR:bad argument type: consp nil\n###END\n",
        (path,),
        block_name="TRVJ_NAMNRUTA_MODELL",
    )[path]

    assert result.error is not None
    assert "bad argument type: consp nil" in result.error
    assert "ändrats delvis" in result.error


def test_parse_update_output_includes_unrecognized_result_for_diagnostics(tmp_path):
    path = tmp_path / "drawing.dwg"
    result = _parse_update_output(
        "###FILE:0\nAutoCAD command failed\n",
        (path,),
        block_name="TRVJ_NAMNRUTA_MODELL",
    )[path]

    assert result.error is not None
    assert "AutoCAD command failed" in result.error


@requires_accoreconsole
@requires_fixtures
@pytest.mark.parametrize("fixture_path", DWG_FIXTURES, ids=lambda p: p.name)
def test_update_dwg_attributes_missing_block_matches_baseline_and_still_backs_up(tmp_path, fixture_path):
    project_root = tmp_path / "projekt"
    project_root.mkdir()
    copy_path = project_root / fixture_path.name
    shutil.copy2(fixture_path, copy_path)
    original_bytes = copy_path.read_bytes()

    changes = {"Slm": "test-varde"}

    with pytest.raises(Exception) as baseline_exc:
        _baseline_update_dwg_attributes(copy_path, dict(changes))

    with pytest.raises(DwgAttributeError) as new_exc:
        update_dwg_attributes(copy_path, dict(changes), project_root=project_root)

    assert str(new_exc.value) == str(baseline_exc.value)
    # Skrivningen ska ha misslyckats (blocket saknas) - filen ska vara
    # oförändrad, men en backup-kopia ska ändå ha skapats innan försöket.
    assert copy_path.read_bytes() == original_bytes
    assert has_backup(project_root, copy_path)
    assert backup_path_for(project_root, copy_path).read_bytes() == original_bytes


def _field(key: str, value: str, location: str = "TRVJ_NAMNRUTA") -> MetadataField:
    return MetadataField(key=key, label=key, value=value, editable=True, location=location)


class _FakeReader:
    """Fejkad läsare som svarar med `before` vid första anropet och `after`
    därefter. `None` betyder att läsaren ger felresultat (oläsbar fil)."""

    def __init__(self, before=(), after=()):
        self.before = before
        self.after = after
        self.calls: list[tuple[tuple[Path, ...], str]] = []

    def _answer(self, paths, block_name, fields):
        self.calls.append((tuple(paths), block_name))
        if callable(fields):
            return {path: fields(path) for path in paths}
        if fields is None:
            return {path: DwgAttributeResult(path=path, fields=(), error="oläsbar") for path in paths}
        return {path: DwgAttributeResult(path=path, fields=tuple(fields)) for path in paths}

    def rust(self, paths, block_name):
        return self._answer(paths, block_name, self.before if not self.calls else self.after)

    def autocad(self, paths, block_name, **kwargs):
        return self._answer(paths, block_name, self.after)


def _fake_write(fields_before=(), side_effect=None):
    calls = []

    def fake_run_update_batch(changes_by_path, block_name):
        calls.append((tuple(path for path, _ in changes_by_path), block_name))
        for path, _ in changes_by_path:
            if side_effect is not None:
                side_effect(path)
        return {
            path: DwgUpdateResult(path=path, error=None, fields_before=fields_before)
            for path, _ in changes_by_path
        }

    fake_run_update_batch.calls = calls
    return fake_run_update_batch


def _project_with_files(tmp_path, *names):
    project_root = tmp_path / "projekt"
    project_root.mkdir()
    paths = []
    for name in names:
        path = project_root / name
        path.write_bytes(b"original-" + name.encode())
        paths.append(path.resolve())
    return project_root, paths


def test_update_lisp_reports_autocad_before_values_in_same_session(tmp_path):
    script = _build_update_lisp(((tmp_path / "drawing.dwg", {"Datum": "x"}),), tmp_path / "result.txt")

    assert "###BEFORE" in script


def test_parse_update_output_collects_before_values_on_success(tmp_path):
    path = tmp_path / "drawing.dwg"
    result = _parse_update_output(
        "###FILE:0\n###BEFORE\tDatum\t2026-01-01\t0\n###BEFORE\tAndr\tA\t0\n###OK\n###END\n",
        (path,),
    )[path]

    assert result.error is None
    assert {f.key: f.value for f in result.fields_before} == {"Datum": "2026-01-01", "Andr": "A"}


def test_parse_update_output_ignores_before_values_on_failure(tmp_path):
    path = tmp_path / "drawing.dwg"
    result = _parse_update_output(
        "###FILE:0\n###BEFORE\tDatum\t2026-01-01\t0\n###MISSING_TAG:Besk_2\n###END\n",
        (path,),
    )[path]

    assert result.error is not None
    assert "Besk_2" in result.error
    assert "###BEFORE" not in result.error
    assert result.fields_before is None


def test_rust_fast_path_skips_autocad_read_back(tmp_path, monkeypatch):
    project_root, (dwg_path,) = _project_with_files(tmp_path, "X.dwg")
    reader = _FakeReader(before=(_field("Slm", "gammalt"), _field("Datum", "d")),
                         after=(_field("Slm", "nytt"), _field("Datum", "d")))
    write = _fake_write(
        fields_before=(_field("Slm", "gammalt"),),
        side_effect=lambda path: path.write_bytes(b"skriven-lokalt"),
    )
    monkeypatch.setattr(dwg.rust_bridge, "extract_many_dwg_attributes_for_block", reader.rust)
    monkeypatch.setattr(dwg.readers, "extract_many_dwg_attributes_for_block",
                        lambda *a, **k: pytest.fail("AutoCAD ska inte läsa när Rust bekräftar"))
    monkeypatch.setattr(dwg.writers, "_run_update_batch", write)

    results = update_many_dwg_attributes({dwg_path: {"Slm": "nytt"}}, project_root=project_root)

    assert results[dwg_path].error is None
    assert has_backup(project_root, dwg_path)
    assert [paths for paths, _ in reader.calls] == [(dwg_path,), (dwg_path,)]
    assert write.calls == [((dwg_path,), "TRVJ_NAMNRUTA")]


def test_unconfirmed_files_share_one_autocad_read_back(tmp_path, monkeypatch):
    project_root, (ok_path, echo_path, broken_path) = _project_with_files(tmp_path, "A.dwg", "B.dwg", "C.dwg")
    after_by_name = {
        "A.dwg": ((_field("Slm", "nytt"),), None),
        "B.dwg": ((_field("Slm", "Slm"),), None),
        "C.dwg": ((), "LibreDWG kunde inte läsa"),
    }
    rust = _FakeReader(
        before=(_field("Slm", "gammalt"),),
        after=lambda path: DwgAttributeResult(path, *after_by_name[path.name]),
    )
    autocad = _FakeReader(after=(_field("Slm", "nytt"),))
    monkeypatch.setattr(dwg.rust_bridge, "extract_many_dwg_attributes_for_block", rust.rust)
    monkeypatch.setattr(dwg.readers, "extract_many_dwg_attributes_for_block", autocad.autocad)
    monkeypatch.setattr(dwg.writers, "_run_update_batch", _fake_write(fields_before=(_field("Slm", "gammalt"),)))

    results = update_many_dwg_attributes(
        {path: {"Slm": "nytt"} for path in (ok_path, echo_path, broken_path)},
        project_root=project_root,
    )

    assert all(result.error is None for result in results.values())
    assert len(autocad.calls) == 1
    assert {path.name for path in autocad.calls[0][0]} == {"B.dwg", "C.dwg"}


def test_update_many_dwg_attributes_restores_backup_when_verification_fails(tmp_path, monkeypatch):
    project_root, (dwg_path,) = _project_with_files(tmp_path, "X.dwg")
    reader = _FakeReader(before=(_field("Slm", "gammalt"),), after=(_field("Slm", "fel-varde"),))
    # Simulerar en "lyckad" accoreconsole-skrivning som visar sig ha skrivit
    # fel värde och samtidigt ändrat filen på disk.
    write = _fake_write(
        fields_before=(_field("Slm", "gammalt"),),
        side_effect=lambda path: path.write_bytes(b"korrupt-efter-skrivning"),
    )
    monkeypatch.setattr(dwg.rust_bridge, "extract_many_dwg_attributes_for_block", reader.rust)
    monkeypatch.setattr(dwg.readers, "extract_many_dwg_attributes_for_block", reader.autocad)
    monkeypatch.setattr(dwg.writers, "_run_update_batch", write)

    results = update_many_dwg_attributes({dwg_path: {"Slm": "nytt-varde"}}, project_root=project_root)

    assert results[dwg_path].error is not None
    assert "fel-varde" in results[dwg_path].error
    assert "återställts" in results[dwg_path].error
    assert dwg_path.read_bytes() == b"original-X.dwg"


def test_restores_backup_when_autocad_before_values_are_missing(tmp_path, monkeypatch):
    project_root, (dwg_path,) = _project_with_files(tmp_path, "X.dwg")
    reader = _FakeReader(before=None, after=(_field("Slm", "nytt"),))
    write = _fake_write(fields_before=None, side_effect=lambda path: path.write_bytes(b"skriven"))
    monkeypatch.setattr(dwg.rust_bridge, "extract_many_dwg_attributes_for_block", reader.rust)
    monkeypatch.setattr(dwg.readers, "extract_many_dwg_attributes_for_block", reader.autocad)
    monkeypatch.setattr(dwg.writers, "_run_update_batch", write)

    results = update_many_dwg_attributes({dwg_path: {"Slm": "nytt"}}, project_root=project_root)

    assert "kunde inte verifieras" in results[dwg_path].error
    assert dwg_path.read_bytes() == b"original-X.dwg"


def test_autocad_fallback_passes_when_rust_is_unavailable(tmp_path, monkeypatch):
    project_root, (dwg_path,) = _project_with_files(tmp_path, "X.dwg")

    def rust_down(paths, block_name):
        raise dwg.DwgError("Rust-binären saknas")

    autocad = _FakeReader(after=(_field("Slm", "nytt"), _field("Datum", "d")))
    monkeypatch.setattr(dwg.rust_bridge, "extract_many_dwg_attributes_for_block", rust_down)
    monkeypatch.setattr(dwg.readers, "extract_many_dwg_attributes_for_block", autocad.autocad)
    monkeypatch.setattr(
        dwg.writers, "_run_update_batch", _fake_write(fields_before=(_field("Slm", "gammalt"), _field("Datum", "d")))
    )

    results = update_many_dwg_attributes({dwg_path: {"Slm": "nytt"}}, project_root=project_root)

    assert results[dwg_path].error is None
    assert len(autocad.calls) == 1


def test_update_many_dwg_attributes_rejects_locked_file_before_backup_or_write(tmp_path, monkeypatch):
    project_root, (dwg_path,) = _project_with_files(tmp_path, "X.dwg")
    calls = []

    def fake_locked(path):
        calls.append(("lock-check", path))
        raise dwg.backup.BackupError("DWG-filen är låst av en annan process eller användare")

    monkeypatch.setattr(dwg.writers, "ensure_not_locked", fake_locked)
    monkeypatch.setattr(
        dwg.rust_bridge,
        "extract_many_dwg_attributes_for_block",
        lambda *args, **kwargs: pytest.fail("Låst fil ska inte läsas"),
    )
    monkeypatch.setattr(dwg.writers, "create_backup", lambda *args, **kwargs: pytest.fail("Backup ska inte skapas"))
    monkeypatch.setattr(dwg.writers, "_run_update_batch", lambda *args, **kwargs: pytest.fail("Ingen skrivning"))

    results = update_many_dwg_attributes({dwg_path: {"Slm": "nytt-varde"}}, project_root=project_root)

    assert calls == [("lock-check", dwg_path)]
    assert "låst" in results[dwg_path].error
    assert not has_backup(project_root, dwg_path)


def test_update_many_restores_backup_when_autocad_write_result_is_ambiguous(tmp_path, monkeypatch):
    project_root, (dwg_path,) = _project_with_files(tmp_path, "X.dwg")
    reader = _FakeReader(before=(_field("DATUM", "före"),))

    def fake_run_update_batch(changes_by_path, block_name):
        for path, _ in changes_by_path:
            path.write_bytes(b"partial-update")
        return {
            path: DwgUpdateResult(
                path,
                error="AutoCAD avbröt DWG-skrivningen: bad argument type: consp nil "
                "DWG-filen kan ha ändrats delvis.",
            )
            for path, _ in changes_by_path
        }

    monkeypatch.setattr(dwg.rust_bridge, "extract_many_dwg_attributes_for_block", reader.rust)
    monkeypatch.setattr(dwg.writers, "_run_update_batch", fake_run_update_batch)

    results = update_many_dwg_model_attributes({dwg_path: {"DATUM": "efter"}}, project_root=project_root)

    assert results[dwg_path].error is not None
    assert "återställts" in results[dwg_path].error
    assert dwg_path.read_bytes() == b"original-X.dwg"


def test_update_many_model_attributes_targets_model_block_with_backup(tmp_path, monkeypatch):
    from dwg import TRVJ_MODEL_BLOCK_NAME

    project_root, (dwg_path,) = _project_with_files(tmp_path, "X.dwg")
    reader = _FakeReader(
        before=(_field("Modellnamn", "före", "Modellnamn"),),
        after=(_field("Modellnamn", "efter", "Modellnamn"),),
    )
    write = _fake_write(fields_before=(_field("Modellnamn", "före", "Modellnamn"),))
    monkeypatch.setattr(dwg.rust_bridge, "extract_many_dwg_attributes_for_block", reader.rust)
    monkeypatch.setattr(dwg.readers, "extract_many_dwg_attributes_for_block",
                        lambda *a, **k: pytest.fail("AutoCAD ska inte läsa när Rust bekräftar"))
    monkeypatch.setattr(dwg.writers, "_run_update_batch", write)

    results = update_many_dwg_model_attributes({dwg_path: {"Modellnamn": "efter"}}, project_root=project_root)

    assert results[dwg_path].error is None
    assert has_backup(project_root, dwg_path)
    assert [block for _, block in reader.calls] == [TRVJ_MODEL_BLOCK_NAME, TRVJ_MODEL_BLOCK_NAME]
    assert [block for _, block in write.calls] == [TRVJ_MODEL_BLOCK_NAME]


def test_model_mtext_echo_falls_back_to_autocad_and_checks_rust_agreement(tmp_path, monkeypatch):
    project_root, (dwg_path,) = _project_with_files(tmp_path, "X.dwg")
    rust = _FakeReader(
        before=(_field("Modellnamn", "Modellnamn", "Modellnamn"),),
        after=(_field("Modellnamn", "Modellnamn", "Modellnamn"), _field("Datum", "rust-avviker", "Datum")),
    )
    autocad = _FakeReader(after=(_field("Modellnamn", "efter", "Modellnamn"), _field("Datum", "d", "Datum")))
    write = _fake_write(
        fields_before=(_field("Modellnamn", "före", "Modellnamn"), _field("Datum", "d", "Datum")),
        side_effect=lambda path: path.write_bytes(b"skriven"),
    )
    monkeypatch.setattr(dwg.rust_bridge, "extract_many_dwg_attributes_for_block", rust.rust)
    monkeypatch.setattr(dwg.readers, "extract_many_dwg_attributes_for_block", autocad.autocad)
    monkeypatch.setattr(dwg.writers, "_run_update_batch", write)

    results = update_many_dwg_model_attributes({dwg_path: {"Modellnamn": "efter"}}, project_root=project_root)

    # AutoCAD bekräftar skrivningen, men Rust ser ett annat Datum - samma
    # överensstämmelsekontroll som tidigare ska fälla och återställa.
    assert len(autocad.calls) == 1
    assert results[dwg_path].error is not None
    assert dwg_path.read_bytes() == b"original-X.dwg"
