"""Metadatarepresentation för DWG-filer (namnruta/attribut som metadata).

Motsvarar TRVJ_NAMNRUTA-hanteringen i .old/python_v5/src/version_5.py,
sedd som metadata snarare än som ren filoperation.

Kravkälla: docs/kravsparning.md, modul metadata/dwg.

Status: skelett (Steg 3). Portering sker i Steg 4, Fas 3, i samspel
med dwg/readers.py och dwg/writers.py.
"""

from __future__ import annotations
