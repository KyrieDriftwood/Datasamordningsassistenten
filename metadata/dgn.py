"""Metadatarepresentation för DGN-filer.

Verifierat gap: DGN-formatet hanteras inte alls i baslinjen
(.old/python_v5), varken för läsning, skrivning eller klassificering.
TB kräver dock stöd för DGN parallellt med DWG.

Kravkälla: docs/kravsparning.md, modul metadata/dgn.

Status: ej implementerat (medvetet gap). Omfattning och tidplan för
DGN-stöd måste beslutas explicit innan implementation påbörjas —
se docs/kravsparning.md, öppna frågor.
"""

from __future__ import annotations
