"""Tester för dwg/validators.py: read-back-verifiering efter skrivning.

Enhetstester med MetadataField-tupler och monkeypatchade AutoCAD- och
Rust-läsare - kräver varken accoreconsole eller DWG-fixturer.
"""

from __future__ import annotations

from pathlib import Path

import dwg.readers
import dwg.rust_bridge
import pytest

from dwg import DwgAttributeResult, TRVJ_MODEL_BLOCK_NAME
from dwg.validators import DwgWriteVerificationError, verify_write
from metadata import MetadataField


def _field(key: str, value: str, *, location: str = "TRVJ_NAMNRUTA") -> MetadataField:
    return MetadataField(key=key, label=key, value=value, editable=True, location=location)


def test_verify_write_passes_when_changed_and_unchanged_values_match(monkeypatch):
    fields_before = (_field("Slm", "gammalt varde"), _field("Skala", "1:100"))
    fields_after = (_field("Slm", "nytt varde"), _field("Skala", "1:100"))
    monkeypatch.setattr(
        dwg.readers,
        "extract_many_dwg_attributes_for_block",
        lambda paths, block_name: {
            Path(path).resolve(): DwgAttributeResult(Path(path).resolve(), fields_after) for path in paths
        },
    )

    verify_write("dummy.dwg", {"Slm": "nytt varde"}, fields_before)


def test_verify_write_raises_when_changed_value_not_applied(monkeypatch):
    fields_before = (_field("Slm", "gammalt varde"),)
    fields_after = (_field("Slm", "gammalt varde"),)  # skrivningen "tog" inte
    monkeypatch.setattr(
        dwg.readers,
        "extract_many_dwg_attributes_for_block",
        lambda paths, block_name: {
            Path(path).resolve(): DwgAttributeResult(Path(path).resolve(), fields_after) for path in paths
        },
    )

    with pytest.raises(DwgWriteVerificationError, match="Slm"):
        verify_write("dummy.dwg", {"Slm": "nytt varde"}, fields_before)


def test_verify_write_raises_when_unrelated_attribute_unexpectedly_changed(monkeypatch):
    fields_before = (_field("Slm", "varde"), _field("Skala", "1:100"))
    fields_after = (_field("Slm", "varde"), _field("Skala", "1:200"))  # oavsiktlig sidoeffekt
    monkeypatch.setattr(
        dwg.readers,
        "extract_many_dwg_attributes_for_block",
        lambda paths, block_name: {
            Path(path).resolve(): DwgAttributeResult(Path(path).resolve(), fields_after) for path in paths
        },
    )

    with pytest.raises(DwgWriteVerificationError, match="Skala"):
        verify_write("dummy.dwg", {"Slm": "varde"}, fields_before)


def test_verify_write_raises_when_read_back_itself_fails(monkeypatch):
    from dwg import DwgAttributeError

    def raise_error(path):
        raise DwgAttributeError("kunde inte läsa filen")

    monkeypatch.setattr(
        dwg.readers,
        "extract_many_dwg_attributes_for_block",
        lambda paths, block_name: (_ for _ in ()).throw(DwgAttributeError("kunde inte läsa filen")),
    )

    with pytest.raises(DwgWriteVerificationError):
        verify_write("dummy.dwg", {"Slm": "varde"}, ())


def test_model_block_verification_requires_same_value_as_standard_block(monkeypatch):
    fields_before = (_field("SKAPAD_AV", "Patricia Friberg", location=TRVJ_MODEL_BLOCK_NAME),)
    fields_after = (_field("SKAPAD_AV", "Patricia Friberg", location=TRVJ_MODEL_BLOCK_NAME),)
    calls = []

    def extract_named(paths, block_name):
        resolved_paths = tuple(Path(path).resolve() for path in paths)
        calls.append((resolved_paths, block_name))
        return {path: DwgAttributeResult(path, fields_after) for path in resolved_paths}

    monkeypatch.setattr(dwg.readers, "extract_many_dwg_attributes_for_block", extract_named)
    monkeypatch.setattr(dwg.rust_bridge, "extract_many_dwg_attributes_for_block", extract_named)

    verify_write(
        "dummy.dwg",
        {"SKAPAD_AV": "Patricia Friberg"},
        fields_before,
        block_name=TRVJ_MODEL_BLOCK_NAME,
    )

    assert calls == [
        ((Path("dummy.dwg").resolve(),), TRVJ_MODEL_BLOCK_NAME),
        ((Path("dummy.dwg").resolve(),), TRVJ_MODEL_BLOCK_NAME),
    ]


def test_model_block_verification_allows_known_rust_tag_echo_for_mtext(monkeypatch):
    fields_before = (_field("DATUM", "före", location=TRVJ_MODEL_BLOCK_NAME),)
    fields_after_autocad = (_field("DATUM", "2026-10-15", location=TRVJ_MODEL_BLOCK_NAME),)
    fields_after_rust = (_field("DATUM", "DATUM", location=TRVJ_MODEL_BLOCK_NAME),)
    resolved_path = Path("dummy.dwg").resolve()
    monkeypatch.setattr(
        dwg.readers,
        "extract_many_dwg_attributes_for_block",
        lambda paths, block_name: {
            resolved_path: DwgAttributeResult(resolved_path, fields_after_autocad)
        },
    )
    monkeypatch.setattr(
        dwg.rust_bridge,
        "extract_many_dwg_attributes_for_block",
        lambda paths, block_name: {
            resolved_path: DwgAttributeResult(resolved_path, fields_after_rust)
        },
    )

    verify_write(
        resolved_path,
        {"DATUM": "2026-10-15"},
        fields_before,
        block_name=TRVJ_MODEL_BLOCK_NAME,
    )


def test_model_block_verification_rejects_different_value(monkeypatch):
    fields_before = (_field("SKAPAD_AV", "före", location=TRVJ_MODEL_BLOCK_NAME),)
    fields_after = (_field("SKAPAD_AV", "annan person", location=TRVJ_MODEL_BLOCK_NAME),)
    monkeypatch.setattr(
        dwg.readers,
        "extract_many_dwg_attributes_for_block",
        lambda paths, block_name: {
            Path(path).resolve(): DwgAttributeResult(Path(path).resolve(), fields_after) for path in paths
        },
    )

    with pytest.raises(DwgWriteVerificationError, match="annan person"):
        verify_write(
            "dummy.dwg",
            {"SKAPAD_AV": "före"},
            fields_before,
            block_name=TRVJ_MODEL_BLOCK_NAME,
        )


def test_model_block_verification_still_rejects_non_tag_rust_mismatch(monkeypatch):
    path = Path("dummy.dwg").resolve()
    fields_after_autocad = (_field("DATUM", "2026-10-15", location=TRVJ_MODEL_BLOCK_NAME),)
    fields_after_rust = (_field("DATUM", "2026-10-16", location=TRVJ_MODEL_BLOCK_NAME),)
    monkeypatch.setattr(
        dwg.readers,
        "extract_many_dwg_attributes_for_block",
        lambda paths, block_name: {path: DwgAttributeResult(path, fields_after_autocad)},
    )
    monkeypatch.setattr(
        dwg.rust_bridge,
        "extract_many_dwg_attributes_for_block",
        lambda paths, block_name: {path: DwgAttributeResult(path, fields_after_rust)},
    )

    with pytest.raises(DwgWriteVerificationError, match="Rust/LibreDWG läste '2026-10-16'"):
        verify_write(
            path,
            {"DATUM": "2026-10-15"},
            (_field("DATUM", "före", location=TRVJ_MODEL_BLOCK_NAME),),
            block_name=TRVJ_MODEL_BLOCK_NAME,
        )


def _rust(*pairs, error=None):
    from pathlib import Path as _P

    from dwg import DwgAttributeResult
    from metadata import MetadataField

    fields = tuple(MetadataField(key=k, label=k, value=v, editable=True, location="TRVJ_NAMNRUTA") for k, v in pairs)
    return DwgAttributeResult(_P("X.dwg"), fields, error=error)


def test_fast_verify_passes_only_when_rust_confirms_exact_write():
    from dwg.validators import fast_verify_passes

    before = _rust(("Datum", "a"), ("Andr", "A"))
    assert fast_verify_passes({"Datum": "b"}, before, _rust(("Datum", "b"), ("Andr", "A")))


@pytest.mark.parametrize(
    "before, after",
    [
        (None, _rust(("Datum", "b"))),
        (_rust(("Datum", "a")), None),
        (_rust(("Datum", "a")), _rust(error="oläsbar")),
        (_rust(("Datum", "a")), _rust()),
        (_rust(("Datum", "a"), ("Andr", "A")), _rust(("Datum", "b"))),
        (_rust(("Datum", "a"), ("Andr", "A")), _rust(("Datum", "b"), ("Andr", "X"))),
        (_rust(("Datum", "a")), _rust(("Datum", "fel"))),
        (_rust(("Datum", "Datum")), _rust(("Datum", "b"))),
        (_rust(("Datum", "a"), ("Andr", "Andr")), _rust(("Datum", "b"), ("Andr", "Andr"))),
    ],
    ids=["no-before", "no-after", "after-error", "after-empty", "tag-vanished",
         "other-changed", "wrong-value", "echo-before", "echo-untouched"],
)
def test_fast_verify_rejects_anything_uncertain(before, after):
    from dwg.validators import fast_verify_passes

    assert not fast_verify_passes({"Datum": "b"}, before, after)
