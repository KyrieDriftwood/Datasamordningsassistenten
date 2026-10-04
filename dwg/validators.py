"""Validering av DWG-attribut före och efter skrivning.

Före skrivning: motsvarar tvåfasvalideringen i
.old/python_v5/src/version_5.py (kontroll innan accoreconsole anropas).

Efter skrivning läses filen tillbaka från disk och jämförs mot avsedda
värden. dwg/writers.py kontrollerar först med Rust/LibreDWG
(`fast_verify_passes`, ~50 ms per fil) och använder AutoCAD-läsning via
dwg/readers.py (`check_written_fields`) för allt som Rust inte entydigt
kan bekräfta. Modellblocket kräver vid AutoCAD-verifiering även att
Rust-läsaren stämmer, eftersom UI-tabellen använder den. Avvikelse leder
till rollback via dwg/backup.py (TB CBE: "Den skrivna filen ska läsas
tillbaka och kontrolleras").

Kravkälla: docs/kravsparning.md, modul dwg/validators, TB-sektion CBE/J.

Status: implementerad för standard- och modellnamnrutor.
"""

from __future__ import annotations

from pathlib import Path

from dwg import DwgAttributeError, DwgAttributeResult, DwgError, TRVJ_BLOCK_NAME, TRVJ_MODEL_BLOCK_NAME


class DwgWriteVerificationError(DwgError):
    """Höjs när read-back-verifieringen efter en DWG-skrivning misslyckas.

    Täcker det verifierade gapet i J-10/Z-11: baslinjen läser aldrig
    tillbaka den faktiskt skrivna filen för att bekräfta att de avsedda
    attributen verkligen ändrades, och att övriga attribut inte
    oavsiktligt påverkades. Se dwg/writers.py för orkestreringen av
    backup → skrivning → verifiering → ev. återställning.
    """


def _write_mismatches(
    changes: dict[str, str],
    values_before: dict[str, str],
    values_after: dict[str, str],
) -> list[str]:
    mismatches: list[str] = []
    for tag, expected_value in changes.items():
        actual_value = values_after.get(tag)
        if actual_value != expected_value:
            mismatches.append(f"{tag}: förväntade {expected_value!r}, fick {actual_value!r}")

    for tag, original_value in values_before.items():
        if tag in changes:
            continue
        actual_value = values_after.get(tag)
        if actual_value != original_value:
            mismatches.append(
                f"{tag}: oavsiktligt ändrat från {original_value!r} till {actual_value!r} (skulle inte ändras)"
            )
    return mismatches


def fast_verify_passes(
    changes: dict[str, str],
    rust_before: DwgAttributeResult | None,
    rust_after: DwgAttributeResult | None,
) -> bool:
    """Snabb read-back-kontroll med Rust/LibreDWG före och efter skrivning.

    Returnerar True endast när den skrivna filen på disk bevisligen har
    exakt de avsedda värdena och inga andra attribut har ändrats. Allt
    osäkert ger False, och då verifierar anroparen med AutoCAD i stället:
    läsfel, saknade taggar och värden som är lika med sin egen tagg (känt
    MText-eko i LibreDWG, där det verkliga värdet inte går att se).
    """
    if rust_before is None or rust_after is None:
        return False
    if rust_before.error is not None or rust_after.error is not None:
        return False
    values_before = {field.key: field.value for field in rust_before.fields}
    values_after = {field.key: field.value for field in rust_after.fields}
    if not values_after or values_before.keys() - values_after.keys():
        return False
    for values in (values_before, values_after):
        if any(value == tag for tag, value in values.items()):
            return False
    return not _write_mismatches(changes, values_before, values_after)


def check_written_fields(
    path: str | Path,
    changes: dict[str, str],
    fields_before: tuple,
    fields_after: tuple,
    *,
    block_name: str = TRVJ_BLOCK_NAME,
    rust_after: DwgAttributeResult | None = None,
) -> None:
    """Jämför AutoCAD-lästa attribut före/efter skrivning.

    För modellblocket krävs dessutom att Rust/LibreDWG-läsningen
    (`rust_after`) stämmer med AutoCAD, eftersom UI-tabellen använder
    Rust-läsaren. Höjer DwgWriteVerificationError vid avvikelse.
    """
    dwg_path = Path(path)
    values_after = {field.key: field.value for field in fields_after}
    values_before = {field.key: field.value for field in fields_before}

    mismatches = _write_mismatches(changes, values_before, values_after)
    if mismatches:
        raise DwgWriteVerificationError(
            f"Read-back-verifieringen av {dwg_path.name} misslyckades:\n" + "\n".join(mismatches)
        )

    if block_name != TRVJ_MODEL_BLOCK_NAME:
        return
    if rust_after is None or rust_after.error is not None:
        detail = rust_after.error if rust_after is not None else "inget resultat"
        raise DwgWriteVerificationError(
            f"Kunde inte verifiera modellnamnrutan med Rust/LibreDWG efter skrivning i {dwg_path.name}: {detail}"
        )

    values_after_rust = {field.key: field.value for field in rust_after.fields}
    tags_to_compare = values_after.keys() | values_after_rust.keys()
    rust_mismatches = [
        f"{tag}: AutoCAD läste {values_after.get(tag)!r}, "
        f"Rust/LibreDWG läste {values_after_rust.get(tag)!r}"
        for tag in sorted(tags_to_compare)
        if values_after.get(tag) != values_after_rust.get(tag)
        and values_after_rust.get(tag) != tag
    ]
    if rust_mismatches:
        raise DwgWriteVerificationError(
            f"AutoCAD och Rust/LibreDWG läser inte samma modellvärden i {dwg_path.name}:\n"
            + "\n".join(rust_mismatches)
        )


def verify_write(
    path: str | Path,
    changes: dict[str, str],
    fields_before: tuple,
    *,
    block_name: str = TRVJ_BLOCK_NAME,
) -> None:
    """Läser tillbaka en nyss skriven DWG-fil med AutoCAD och verifierar den.

    Kontrollerar dels att varje ändrat attribut i `changes` nu har
    exakt det avsedda värdet, dels att alla attribut som INTE ingick i
    `changes` fortfarande har samma värde som i `fields_before` (för att
    upptäcka oavsiktliga sidoeffekter av skrivningen). `fields_before`
    ska vara resultatet av en läsning tagen precis före skrivningen.

    Höjer DwgWriteVerificationError med en läsbar sammanfattning av
    varje avvikelse om verifieringen misslyckas. Modellblocket läses
    dessutom tillbaka med Rust/LibreDWG och jämförs med AutoCAD-resultatet,
    eftersom UI-tabellen använder Rust-läsaren.

    dwg/writers.py använder i stället `fast_verify_passes` och, vid behov,
    en gemensam AutoCAD-läsning för flera filer via `check_written_fields`.
    """
    dwg_path = Path(path).expanduser().resolve()
    try:
        from dwg.readers import extract_many_dwg_attributes_for_block

        result = extract_many_dwg_attributes_for_block((dwg_path,), block_name)[dwg_path]
        if result.error is not None:
            raise DwgAttributeError(result.error)
        fields_after = result.fields
    except DwgAttributeError as exc:
        raise DwgWriteVerificationError(
            f"Kunde inte läsa tillbaka {dwg_path.name} efter skrivning för verifiering: {exc}"
        ) from exc

    # Jämförelsen sker före Rust-läsningen, så att ett AutoCAD-fel
    # rapporteras som read-back-fel (samma ordning som tidigare).
    check_written_fields(dwg_path, changes, fields_before, fields_after)
    if block_name != TRVJ_MODEL_BLOCK_NAME:
        return
    try:
        from dwg.rust_bridge import extract_many_dwg_attributes_for_block as rust_extract

        rust_after = rust_extract((dwg_path,), block_name)[dwg_path]
    except DwgError as exc:
        raise DwgWriteVerificationError(
            f"Kunde inte verifiera modellnamnrutan med Rust/LibreDWG efter skrivning "
            f"i {dwg_path.name}: {exc}"
        ) from exc
    check_written_fields(
        dwg_path,
        changes,
        fields_before,
        fields_after,
        block_name=block_name,
        rust_after=rust_after,
    )
