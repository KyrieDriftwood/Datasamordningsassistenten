"""Metadataschema (fältnamn, typer, obligatoriska fält).

Ska spegla TB-sektionerna EAA/EAB/EAC(D) fält för fält. Baslinjens
v4-schema (.old/python_v5/src/version_4.py) är hårdkodat och stämmer
inte fullt ut mot TB — se docs/kravsparning.md, öppen fråga om
fält-för-fält-avstämning.

Kravkälla: docs/kravsparning.md, modul config/schema.

Status: ej implementerat (medvetet gap). Schemat låses först efter
att avstämningen mot TB är gjord och godkänd (Steg 4, Fas 2).
"""

from __future__ import annotations
