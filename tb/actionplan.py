"""Åtgärdsplan: från TB-diff till konkret arbetslista.

Kravkälla: TB-sektion AFC.27 (kodagenten ska informera om brister och
föreslå åtgärder) och AFC.22 (spårbarhet mellan TB-kod och modul).

Kopplingen TB-sektion -> modul hämtas ur ``docs/kravsparning.md`` i
stället för att dupliceras här. Kravspårningen är redan den etablerade
kopplingen mellan TB, baslinjefunktion och implementeringsmodul; att läsa
den i stället för att upprepa den håller en enda sanningskälla och
undviker att de två glider isär.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from tb import KRAVSPARNING_PATH, ODT_PATH
from tb.diff import SectionChange, TbDiff

#: En tabellrad i kravsparning.md med minst sex kolumner.
_ROW_RE = re.compile(r"^\|(?P<cells>.+)\|\s*$")

#: Den inledande AMA-koden i kolumnen "TB-sektion" (t.ex. "BBB (Tillhandahållen info)").
_CODE_RE = re.compile(r"^(?P<code>[A-Z]{1,4}(?:\.\d{1,3})?)")


@dataclass(frozen=True)
class RequirementRow:
    """En rad ur kravspårningen, kopplad till en TB-sektion."""

    requirement_id: str
    tb_section: str
    requirement: str
    modules: str
    status: str


def load_requirement_index(
    kravsparning_path: str | Path = KRAVSPARNING_PATH,
) -> dict[str, list[RequirementRow]]:
    """Läser kravspårningen och indexerar raderna på TB-kod.

    Returnerar en tom mappning om filen saknas — åtgärdsplanen ska kunna
    genereras även då, bara utan modulkoppling.
    """
    path = Path(kravsparning_path)
    index: dict[str, list[RequirementRow]] = {}
    if not path.is_file():
        return index

    for line in path.read_text(encoding="utf-8").splitlines():
        match = _ROW_RE.match(line.strip())
        if match is None:
            continue
        cells = [cell.strip() for cell in match.group("cells").split("|")]
        if len(cells) < 6 or set(cells[0]) <= {"-", ":"}:
            continue
        if cells[0] in {"Krav-ID", ""}:
            continue

        code_match = _CODE_RE.match(cells[1])
        if code_match is None:
            continue
        row = RequirementRow(
            requirement_id=cells[0],
            tb_section=cells[1],
            requirement=cells[2],
            modules=cells[4],
            status=cells[5],
        )
        index.setdefault(code_match.group("code"), []).append(row)
    return index


def _related_rows(code: str, index: dict[str, list[RequirementRow]]) -> list[RequirementRow]:
    """Krav som hör till en TB-kod, inklusive underliggande koder.

    En ändring i ``AFC`` berör även ``AFC.22``, och en ändring i ``CB``
    berör ``CBE``/``CBF``. Uppslaget är därför prefixbaserat, inte exakt.
    """
    rows: list[RequirementRow] = []
    for indexed_code, indexed_rows in index.items():
        if indexed_code == code or indexed_code.startswith(code):
            rows.extend(indexed_rows)
    return rows


def _modules_for(rows: list[RequirementRow]) -> list[str]:
    """Unika modulreferenser ur en uppsättning kravrader, i ordning."""
    modules: list[str] = []
    for row in rows:
        for module in row.modules.split(","):
            cleaned = module.strip()
            if cleaned and cleaned not in {"—", "-"} and cleaned not in modules:
                modules.append(cleaned)
    return modules


def _render_change(change: SectionChange, index: dict[str, list[RequirementRow]]) -> list[str]:
    """Renderar en enskild sektionsändring som ett avsnitt i planen."""
    labels = {
        "added": "Ny sektion",
        "removed": "Borttagen sektion",
        "modified": "Ändrad sektion",
        "moved": "Flyttad sektion (oförändrat innehåll)",
    }
    heading = f"{change.code} – {change.title}" if change.title else change.code
    lines = [f"### {labels[change.kind]}: {heading}", ""]

    rows = _related_rows(change.code, index)
    modules = _modules_for(rows)

    if modules:
        lines.append("**Berörda moduler:** " + ", ".join(modules))
    else:
        lines.append(
            "**Berörda moduler:** okänt — sektionen saknar rad i "
            "[kravsparning.md](kravsparning.md). Lägg till en rad där som "
            "en del av åtgärden."
        )
    lines.append("")

    if rows:
        lines.append("| Krav-ID | Krav | Modul | Status före ändringen |")
        lines.append("|---|---|---|---|")
        for row in rows:
            requirement = row.requirement.replace("\n", " ")
            if len(requirement) > 160:
                requirement = requirement[:157] + "..."
            lines.append(f"| {row.requirement_id} | {requirement} | {row.modules} | {row.status} |")
        lines.append("")

    if change.kind == "added":
        lines.extend(
            [
                "**Åtgärd:**",
                "",
                f"- [ ] Läs sektionen i den genererade speglingen och avgör om den inför ett nytt krav.",
                f"- [ ] Lägg till rad(er) för `{change.code}` i [kravsparning.md](kravsparning.md).",
                "- [ ] Bestäm modul enligt AFC.22 (modulnamnet ska hänvisa till TB-koden).",
                "- [ ] Implementera och testa, eller dokumentera varför kravet inte implementeras nu.",
                "",
                "**Sektionens innehåll:**",
                "",
                "```",
                (change.after.body if change.after else "").strip() or "(tom sektion)",
                "```",
            ]
        )
    elif change.kind == "removed":
        lines.extend(
            [
                "**Åtgärd:**",
                "",
                f"- [ ] Bekräfta med beställaren att `{change.code}` verkligen ska utgå (AFC.27).",
                "- [ ] Avgör om koden som uppfyllde kravet ska tas bort eller behållas.",
                f"- [ ] Ta bort eller markera raderna för `{change.code}` i [kravsparning.md](kravsparning.md).",
                "",
                "**Borttaget innehåll:**",
                "",
                "```",
                (change.before.body if change.before else "").strip() or "(tom sektion)",
                "```",
            ]
        )
    elif change.kind == "modified":
        lines.extend(
            [
                "**Åtgärd:**",
                "",
                "- [ ] Granska diffen nedan och avgör om den ändrar ett krav eller bara formuleringen.",
                "- [ ] Om kravet ändrats: uppdatera berörda moduler och deras tester.",
                f"- [ ] Uppdatera raderna för `{change.code}` i [kravsparning.md](kravsparning.md).",
                "- [ ] Om den nya texten motsäger befintligt beteende: rapportera det enligt AFC.27",
                "      i stället för att tyst välja en tolkning.",
                "",
                "**Diff:**",
                "",
                "```diff",
                change.unified_diff() or "(endast rubriken ändrad)",
                "```",
            ]
        )
    else:
        lines.extend(
            [
                "Innehållet är oförändrat, bara placeringen i dokumentet har ändrats.",
                "Ingen kodändring krävs.",
            ]
        )

    lines.append("")
    return lines


def render_action_plan(
    diff: TbDiff,
    *,
    kravsparning_path: str | Path = KRAVSPARNING_PATH,
    odt_name: str = ODT_PATH.name,
) -> str:
    """Bygger åtgärdsplanen som Markdown.

    Planen är avsiktligt en checklista, inte en fritextsammanfattning:
    den ska gå att beta av och kunna visa vad som återstår.
    """
    index = load_requirement_index(kravsparning_path)
    lines = [
        "<!-- GENERERAD FIL - skrivs om vid varje TB-andring. -->",
        "",
        "# Åtgärdsplan efter ändring i TB",
        "",
        f"Primärkälla: `{odt_name}`.",
        "Speglingen finns i "
        "[TB-Datasamordningsassistenten.md](TB-Datasamordningsassistenten.md).",
        "",
        f"**Sammanfattning:** {diff.summary()}",
        "",
    ]

    if diff.duplicate_codes:
        lines.extend(
            [
                "## Avvikelse i handlingen (AFC.27)",
                "",
                "Följande TB-koder förekommer mer än en gång i handlingen, vilket gör dem",
                "tvetydiga att hänvisa till. Endast den första förekomsten av varje kod",
                "spåras. Detta bör rättas i TB:",
                "",
            ]
        )
        lines.extend(f"- `{code}`" for code in diff.duplicate_codes)
        lines.append("")

    if not diff.changes:
        lines.extend(
            [
                "## Inga ändringar",
                "",
                "Handlingens innehåll är oförändrat sedan förra synkroniseringen.",
                "",
            ]
        )
        return "\n".join(lines)

    lines.extend(["## Ändringar att åtgärda", ""])
    for change in diff.changes:
        lines.extend(_render_change(change, index))

    return "\n".join(lines).rstrip() + "\n"
