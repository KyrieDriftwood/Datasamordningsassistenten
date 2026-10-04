"""Validering innan skrivning (metadata och DWG-attribut).

Motsvarar den tvåfasvalidering som redan finns i baslinjen för DWG
(version_5.py: kontroll före skrivning via accoreconsole) samt
fältvalidering för metadata (version_3.py/version_4.py).

Kravkälla: docs/kravsparning.md, modul backend/validation, TB-krav
om "validering innan skrivning" och att originalfiler aldrig skrivs
direkt utan säkerhetskontroll (CBE-3).

Metadatafältvalideringen (okända/låsta fält) är porterad här och
används av ``metadata.update_metadata``. DWG-valideringen (read-back-
verifiering efter skrivning, se CBE-8/J-10) porteras separat i Steg 4,
Fas 3, eftersom den hör ihop med det ännu oimplementerade
``dwg``-paketet (accoreconsole-bryggan).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from metadata import MetadataField


def validate_metadata_changes(fields_by_key: dict[str, "MetadataField"], changes: dict[str, str]) -> None:
    """Validerar föreslagna metadataändringar mot ett dokuments kända fält.

    Direkt port av valideringsstegen i ``version_3.py:update_metadata``
    (innan någon fil rörs): en ändring mot en okänd nyckel ger
    ``KeyError``, en ändring mot ett beräknat (icke-redigerbart) fält
    ger ``ValueError``. Kastar ingenting om ``changes`` är tomt eller
    om alla nycklar är kända och redigerbara.
    """
    unknown_keys = sorted(set(changes) - fields_by_key.keys())
    if unknown_keys:
        raise KeyError(f"Okända metadatafält: {', '.join(unknown_keys)}")
    locked_keys = sorted(key for key in changes if not fields_by_key[key].editable)
    if locked_keys:
        raise ValueError(f"Beräknade metadatafält kan inte ändras: {', '.join(locked_keys)}")

