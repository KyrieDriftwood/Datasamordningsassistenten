"""Tester för accoreconsole-baserad DWG-läsning (dwg/readers.py).

Körs live mot en riktig AutoCAD/accoreconsole-installation (hittades på
denna dev-maskin) och jämförs direkt mot baslinjens
`src/version_5.py:extract_dwg_attributes`. Ett enskilt accoreconsole-
anrop tar ~7,5 sekunder (modul-laddning), så dessa tester är långsamma
men körs live snarare än mockade, för att verkligen bevisa paritet.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".old" / "python_v5"))

from dwg import locate_accoreconsole  # noqa: E402
from dwg.readers import (  # noqa: E402
    _build_extract_lisp,
    extract_dwg_attributes,
    extract_many_dwg_attributes,
    extract_many_dwg_attributes_for_block,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES_DIR = REPO_ROOT / "Examples"
DWG_FIXTURES = sorted(EXAMPLES_DIR.rglob("*.dwg")) if EXAMPLES_DIR.is_dir() else []

try:
    locate_accoreconsole()
    ACCORECONSOLE_AVAILABLE = True
except Exception:
    ACCORECONSOLE_AVAILABLE = False

requires_accoreconsole = pytest.mark.skipif(
    not ACCORECONSOLE_AVAILABLE, reason="Ingen accoreconsole.exe hittades på denna maskin"
)
requires_fixtures = pytest.mark.skipif(not DWG_FIXTURES, reason="Ingen DWG-fixture hittades i Examples/")


def _baseline_extract_dwg_attributes(path):
    from src.version_5 import extract_dwg_attributes as baseline_extract

    return baseline_extract(path)


@requires_accoreconsole
@requires_fixtures
@pytest.mark.parametrize("fixture_path", DWG_FIXTURES, ids=lambda p: p.name)
def test_extract_dwg_attributes_matches_baseline(fixture_path):
    try:
        baseline_result = _baseline_extract_dwg_attributes(fixture_path)
    except Exception as baseline_exc:  # baslinjens egen DwgAttributeError-klass, inte vår
        with pytest.raises(Exception) as new_exc:
            extract_dwg_attributes(fixture_path)
        assert str(new_exc.value) == str(baseline_exc)
        return

    new_result = extract_dwg_attributes(fixture_path)
    assert new_result == baseline_result


@requires_accoreconsole
@requires_fixtures
def test_extract_many_dwg_attributes_matches_rust_bridge_result():
    """Accoreconsole-läsvägen ska hitta samma sak (eller samma
    "hittade inget block"-fel) som Rust-bryggan för samma filer, då
    båda läser samma TRVJ_NAMNRUTA-block ur samma DWG-filer.
    """
    from dwg.rust_bridge import extract_many_dwg_attributes_fast

    accoreconsole_results = extract_many_dwg_attributes(DWG_FIXTURES)
    rust_results = extract_many_dwg_attributes_fast(DWG_FIXTURES)

    assert set(accoreconsole_results.keys()) == set(rust_results.keys())
    for path, accoreconsole_result in accoreconsole_results.items():
        rust_result = rust_results[path]
        if accoreconsole_result.error is not None or rust_result.error is not None:
            assert (accoreconsole_result.error is not None) == (rust_result.error is not None)
        else:
            assert accoreconsole_result.fields == rust_result.fields


def test_named_block_extraction_uses_autocad_and_keeps_dynamic_tags(tmp_path, monkeypatch):
    from dwg import TRVJ_MODEL_BLOCK_NAME

    drawing = tmp_path / "model.dwg"
    drawing.touch()
    scripts = []
    monkeypatch.setattr("dwg.readers.run_accoreconsole_script", lambda path, script: scripts.append(script))
    monkeypatch.setattr(
        "dwg.readers.read_ansi_output",
        lambda path: "###FILE:0\nKOORDSYSPLAN\tSWEREF 99 16 30\t0\n###END\n",
    )

    result = extract_many_dwg_attributes_for_block((drawing,), TRVJ_MODEL_BLOCK_NAME)[drawing.resolve()]
    lisp = _build_extract_lisp((drawing,), tmp_path / "attributes.txt", block_name=TRVJ_MODEL_BLOCK_NAME)

    assert result.error is None
    assert [(field.key, field.value, field.editable) for field in result.fields] == [
        ("KOORDSYSPLAN", "SWEREF 99 16 30", True)
    ]
    assert TRVJ_MODEL_BLOCK_NAME in scripts[0]
    assert TRVJ_MODEL_BLOCK_NAME in lisp
