"""Tester för dwg-paketet: delade typer/parser (dwg/__init__.py) och
Rust/LibreDWG-bryggan (dwg/rust_bridge.py).

Körs mot de riktiga DWG-fixturerna i Examples/ och jämförs direkt mot
baslinjens `src/version_5_rust.py`. accoreconsole-baserad läsning
(dwg/readers.py) testas inte här eftersom det kräver en riktig
AutoCAD-installation — se en separat, manuell verifieringsanteckning
i docs/plan.md om/när det blir aktuellt.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".old" / "python_v5"))

from dwg import DwgAttributeError, parse_extract_output  # noqa: E402
from dwg.rust_bridge import (  # noqa: E402
    extract_dwg_attributes_fast,
    extract_many_dwg_attributes_fast,
    extract_many_dwg_attributes_for_block,
    is_available,
    locate_rust_extractor,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES_DIR = REPO_ROOT / "Examples"
DWG_FIXTURES = sorted(EXAMPLES_DIR.rglob("*.dwg")) if EXAMPLES_DIR.is_dir() else []


def _baseline_extract_dwg_attributes_fast(path):
    from src.version_5_rust import extract_dwg_attributes_fast as baseline_extract

    return baseline_extract(path)


def test_rust_extractor_binary_is_available():
    if not is_available():
        pytest.skip("Native Rust runtime is not included in the public source export")
    assert is_available()
    assert locate_rust_extractor().is_file()


@pytest.mark.skipif(not DWG_FIXTURES, reason="Ingen DWG-fixture hittades i Examples/")
@pytest.mark.parametrize("fixture_path", DWG_FIXTURES, ids=lambda p: p.name)
def test_extract_dwg_attributes_fast_matches_baseline(fixture_path):
    try:
        baseline_result = _baseline_extract_dwg_attributes_fast(fixture_path)
    except Exception as baseline_exc:  # baslinjens egen DwgAttributeError-klass, inte vår
        with pytest.raises(DwgAttributeError) as new_exc:
            extract_dwg_attributes_fast(fixture_path)
        assert str(new_exc.value) == str(baseline_exc)
        return

    new_result = extract_dwg_attributes_fast(fixture_path)
    assert new_result == baseline_result


@pytest.mark.skipif(not DWG_FIXTURES, reason="Ingen DWG-fixture hittades i Examples/")
def test_extract_many_dwg_attributes_fast_handles_batch(tmp_path):
    results = extract_many_dwg_attributes_fast(DWG_FIXTURES)
    assert set(results.keys()) == {p.resolve() for p in DWG_FIXTURES}
    for result in results.values():
        # Varje riktigt fixturresultat ska antingen ha fält, eller ett
        # tydligt "hittade inget block"-fel (aldrig ett tomt resultat
        # utan förklaring).
        assert result.fields or result.error


def test_parse_extract_output_handles_missing_block():
    text = "###FILE:0\n###BLOCK_NOT_FOUND\n###END\n"
    fixture_path = Path("dummy.dwg")
    results = parse_extract_output(text, (fixture_path,))
    assert results[fixture_path].error is not None
    assert results[fixture_path].fields == ()


def test_parse_extract_output_parses_fields_and_editability():
    text = "###FILE:0\nSlm\tvarde\t0\nSkala\tlast\t2\n###END\n"
    fixture_path = Path("dummy.dwg")
    results = parse_extract_output(text, (fixture_path,))
    result = results[fixture_path]
    assert result.error is None
    fields_by_key = {field.key: field for field in result.fields}
    assert fields_by_key["Slm"].value == "varde"
    assert fields_by_key["Slm"].editable is True
    assert fields_by_key["Skala"].value == "last"
    assert fields_by_key["Skala"].editable is False


def test_parse_extract_output_accepts_dynamic_model_block_tags():
    text = "###FILE:0\nModellnamn\tDEMO 00+100\t0\nObjektkod\tABC-123\t2\n###END\n"
    fixture_path = Path("model.dwg")
    results = parse_extract_output(
        text,
        (fixture_path,),
        block_name="TRVJ_NAMNRUTA_MODELL",
        include_unknown_tags=True,
    )

    fields_by_key = {field.key: field for field in results[fixture_path].fields}
    assert fields_by_key["Modellnamn"].value == "DEMO 00+100"
    assert fields_by_key["Modellnamn"].editable is True
    assert fields_by_key["Objektkod"].editable is False


def test_named_block_bridge_passes_selected_block_to_extractor(tmp_path, monkeypatch):
    from dwg import DwgAttributeResult
    from dwg import rust_bridge

    path = tmp_path / "model.dwg"
    path.touch()
    called = []
    monkeypatch.setattr(rust_bridge, "locate_rust_extractor", lambda: Path("extractor.exe"))

    def fake_extract(_exe, drawing, _timeout, block_name):
        called.append((drawing, block_name))
        return DwgAttributeResult(drawing, ())

    monkeypatch.setattr(rust_bridge, "_run_single_file", fake_extract)
    results = extract_many_dwg_attributes_for_block([path], "TRVJ_NAMNRUTA_MODELL")

    assert called == [(path.resolve(), "TRVJ_NAMNRUTA_MODELL")]
    assert results[path.resolve()].error is None
