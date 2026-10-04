"""Sektionsvis jämförelse mellan två speglingar av TB.

Kravkälla: TB-sektion AFC.27 (kodagenten ska rapportera avvikelser och
förändringar i handlingen).

Jämförelsen sker på TB-kod, inte på radnummer. Det gör diffen stabil när
sektioner flyttas i dokumentet: en sektion som bara bytt plats räknas som
oförändrad, medan en sektion vars text ändrats rapporteras med exakt vad
som lagts till och tagits bort.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field

from tb import Section, TbDocument


@dataclass(frozen=True)
class SectionChange:
    """En ändring i en enskild TB-sektion."""

    code: str
    kind: str  # "added" | "removed" | "modified" | "moved"
    title: str
    before: Section | None = None
    after: Section | None = None

    def unified_diff(self) -> str:
        """Radvis diff av sektionens brödtext, som Markdown-vänlig text."""
        before_lines = (self.before.body.splitlines() if self.before else [])
        after_lines = (self.after.body.splitlines() if self.after else [])
        lines = difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile=f"{self.code} (tidigare)",
            tofile=f"{self.code} (ny)",
            lineterm="",
            n=1,
        )
        return "\n".join(lines)


@dataclass
class TbDiff:
    """Resultatet av en jämförelse mellan två versioner av handlingen."""

    changes: list[SectionChange] = field(default_factory=list)
    duplicate_codes: list[str] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return any(change.kind != "moved" for change in self.changes)

    def by_kind(self, kind: str) -> list[SectionChange]:
        return [change for change in self.changes if change.kind == kind]

    def summary(self) -> str:
        """En rad som sammanfattar diffen, för terminal och loggning."""
        if not self.changes:
            return "Inga ändringar i TB."
        counts = {
            "tillagda": len(self.by_kind("added")),
            "borttagna": len(self.by_kind("removed")),
            "ändrade": len(self.by_kind("modified")),
            "flyttade": len(self.by_kind("moved")),
        }
        return ", ".join(f"{count} {label}" for label, count in counts.items() if count)


def diff_documents(previous: TbDocument, current: TbDocument) -> TbDiff:
    """Jämför två tolkade TB-dokument sektion för sektion.

    Sektioner matchas på TB-kod. En sektion vars innehåll är identiskt
    men som bytt ordningsnummer i dokumentet rapporteras som ``moved``,
    vilket inte räknas som en innehållsändring (`TbDiff.has_changes`).
    """
    before_map = previous.section_map()
    after_map = current.section_map()
    before_order = {section.code: index for index, section in enumerate(previous.sections)}
    after_order = {section.code: index for index, section in enumerate(current.sections)}

    result = TbDiff(duplicate_codes=current.duplicate_codes())

    for section in current.sections:
        if section.code in before_map:
            continue
        if any(change.code == section.code for change in result.changes):
            continue
        result.changes.append(
            SectionChange(code=section.code, kind="added", title=section.title, after=section)
        )

    for section in previous.sections:
        if section.code in after_map:
            continue
        if any(change.code == section.code for change in result.changes):
            continue
        result.changes.append(
            SectionChange(code=section.code, kind="removed", title=section.title, before=section)
        )

    for code, after in after_map.items():
        before = before_map.get(code)
        if before is None:
            continue
        if before.fingerprint() != after.fingerprint():
            result.changes.append(
                SectionChange(
                    code=code,
                    kind="modified",
                    title=after.title,
                    before=before,
                    after=after,
                )
            )
        elif before_order.get(code) != after_order.get(code):
            result.changes.append(
                SectionChange(code=code, kind="moved", title=after.title, before=before, after=after)
            )

    order = {"added": 0, "modified": 1, "removed": 2, "moved": 3}
    result.changes.sort(key=lambda change: (order[change.kind], change.code))
    return result
