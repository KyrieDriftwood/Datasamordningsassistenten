"""TB-synkronisering: ODT som primärkälla, Markdown som spegling.

Kravkälla: TB-sektion AFC.22 (spårbarhet, token-effektivt underhåll) och
AFC.27 (kodagenten ska löpande rapportera avvikelser i handlingen).

Styrprincip för hela paketet:

1. ``TB - Datasamordningsassistenten.odt`` är **primärkälla**. Den läses,
   den skrivs aldrig.
2. ``docs/TB-Datasamordningsassistenten.md`` är en **genererad spegling**
   av ODT:ns innehåll och får inte redigeras för hand.
3. Markdown-filen är en ren funktion av ODT:ns *innehåll*, inte av dess
   filtidsstämpel eller binära bytes. En omsparning av ODT som inte ändrar
   texten ger därför ingen diff och ingen åtgärdsplan — bara verkliga
   ändringar i handlingen leder till arbete.
4. När innehållet ändras jämförs den nya speglingen mot den föregående,
   och en åtgärdsplan skrivs till ``docs/TB-atgardsplan.md`` som kopplar
   varje ändrad TB-sektion till de moduler som berörs.

Moduler:

- ``tb.extract``    -- ODT -> strukturerat dokument -> Markdown
- ``tb.diff``       -- sektionsvis jämförelse mellan två speglingar
- ``tb.actionplan`` -- diff + kravspårning -> åtgärdsplan
- ``tb.sync``       -- orkestrering och kommandoradsgränssnitt
- ``tb.watch``      -- filbevakning via watchdog (TB-sektion BBB)
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

#: Projektroten (katalogen som innehåller ODT-filen och docs/).
PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: Primärkällan. Läses aldrig destruktivt, skrivs aldrig av detta paket.
ODT_PATH = PROJECT_ROOT / "TB - Datasamordningsassistenten.odt"

#: Den genererade speglingen av primärkällan.
MARKDOWN_PATH = PROJECT_ROOT / "docs" / "TB-Datasamordningsassistenten.md"

#: Den genererade åtgärdsplanen som skrivs när innehållet ändrats.
ACTION_PLAN_PATH = PROJECT_ROOT / "docs" / "TB-atgardsplan.md"

#: Kravspårningen, som används för att koppla TB-sektion till modul.
KRAVSPARNING_PATH = PROJECT_ROOT / "docs" / "kravsparning.md"

#: Synkroniseringens tillstånd (hashar och tidsstämplar). Dold fil.
STATE_PATH = PROJECT_ROOT / "docs" / ".tb-sync-state.json"

#: Varningsrubrik som skrivs överst i den genererade Markdown-filen.
GENERATED_HEADER = (
    "<!-- GENERERAD FIL - REDIGERA INTE. -->\n"
    "<!-- Primarkalla: TB - Datasamordningsassistenten.odt -->\n"
    "<!-- Uppdateras med: python -m tb.sync -->\n"
)


class TbError(Exception):
    """Basfel för TB-synkroniseringen."""


@dataclass(frozen=True)
class Section:
    """En TB-sektion, identifierad av sin AMA-kod.

    `code` är sektionskoden så som den står i handlingen (``AFC.22``,
    ``CBE``, ``BBA`` ...). `title` är rubriktexten efter koden. `body` är
    sektionens brödtext som Markdown, utan rubrikraden.
    """

    code: str
    title: str
    level: int
    body: str = ""

    @property
    def heading(self) -> str:
        """Rubrikraden som den renderas i Markdown."""
        text = f"{self.code} – {self.title}" if self.title else self.code
        return f"{'#' * self.level} {text}"

    def fingerprint(self) -> str:
        """Innehållshash för sektionen, används för att upptäcka ändringar."""
        payload = f"{self.code}\x00{self.title}\x00{self.body}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class TbDocument:
    """Hela den tolkade handlingen: en preambel plus en lista sektioner."""

    preamble: str = ""
    sections: list[Section] = field(default_factory=list)

    def section_map(self) -> dict[str, Section]:
        """Sektioner uppslagna på kod.

        Om samma kod förekommer flera gånger i handlingen behålls den
        första och dubbletten rapporteras av `duplicate_codes()` — det är
        en brist i handlingen som ska rapporteras enligt AFC.27, inte
        tystas.
        """
        result: dict[str, Section] = {}
        for section in self.sections:
            result.setdefault(section.code, section)
        return result

    def duplicate_codes(self) -> list[str]:
        """Sektionskoder som förekommer mer än en gång (AFC.27-avvikelse)."""
        seen: set[str] = set()
        duplicates: list[str] = []
        for section in self.sections:
            if section.code in seen and section.code not in duplicates:
                duplicates.append(section.code)
            seen.add(section.code)
        return duplicates


def sha256_text(text: str) -> str:
    """SHA-256 över en textsträng, normaliserad till UTF-8."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: str | Path) -> str:
    """SHA-256 över en fils bytes."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()
