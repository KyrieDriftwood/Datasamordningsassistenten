"""Projektkontext: aktiv projektmapp, katalogavgränsning.

Motsvarar det inledande projektval som i baslinjen sker direkt i UI-koden
(``app_v3_desktop.py``/``app_v4_desktop.py``/``app_v5_desktop.py``: en
katalogväljare sätter trädets rot), samt CLI-ingången i
``.old/python_v5/src/version_1.py:main`` (``--path``-argumentet).

Baslinjen har ingen fristående, testbar "ProjectContext"-klass — det
här är den delen av Fas 2 (Steg 4 i docs/plan.md) som ger UI-lagret
ett rent objekt att bygga mot, i stället för att som idag läsa/skriva
en `Path` direkt i widget-koden.

Kravkälla: docs/kravsparning.md, modul backend/project_context,
TB-sektion AFC.1 ("Välja/läsa projektrot").
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from backend.file_discovery import list_directory_entries


class ProjectContextError(Exception):
    """Höjs när en projektrot inte kan användas (saknas, är ingen mapp)."""


@dataclass(frozen=True, slots=True)
class ProjectContext:
    """En vald, validerad projektrot.

    Motsvarar den katalog som i baslinjen väljs via en filmapp-dialog och
    sedan används som rot för explorer-trädet (`_create_tree_item`).
    """

    root: Path

    @classmethod
    def from_path(cls, path: str | Path) -> "ProjectContext":
        """Skapar och validerar en projektkontext från en given sökväg.

        Höjer ``ProjectContextError`` om sökvägen inte finns eller inte är
        en katalog — samma villkor som `find_documents`/`find_dwg_files`
        (backend/file_discovery.py) redan kräver av sin ``base_path``.
        """
        resolved = Path(path).expanduser().resolve()
        if not resolved.exists():
            raise ProjectContextError(f"Projektmappen finns inte: {resolved}")
        if not resolved.is_dir():
            raise ProjectContextError(f"Projektroten är inte en mapp: {resolved}")
        return cls(root=resolved)

    def list_top_level_entries(self) -> list[Path]:
        """Listar synliga poster direkt under projektroten.

        Motsvarar den första nivån av baslinjens explorer-träd
        (``_create_tree_item`` anropad på rot-noden).
        """
        return list_directory_entries(self.root)
