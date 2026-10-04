"""Orkestrering av TB-synkroniseringen, med kommandoradsgränssnitt.

Kravkälla: TB-sektion AFC.22 och AFC.27.

Flödet vid varje körning:

1. Läs primärkällan (ODT) och rendera den till Markdown i minnet.
2. Jämför mot den **föregående** speglingen på disk. Den committade
   Markdown-filen är alltså själva baslinjen — inget separat
   skuggexemplar behövs, och baslinjen kan granskas i versionshistoriken.
3. Om innehållet är oförändrat: skriv ingenting. En omsparning av ODT
   utan textändring är därmed helt tyst.
4. Om innehållet ändrats: skriv den nya speglingen, skriv åtgärdsplanen
   och rapportera sammanfattningen.

Kommandon::

    python -m tb.sync            # synkronisera nu
    python -m tb.sync --diff     # synkronisera och skriv ut hela diffen
    python -m tb.sync --check    # avgör om speglingen är aktuell, skriv inget
    python -m tb.sync --watch    # bevaka ODT och synkronisera vid ändring

``--check --diff`` förhandsgranskar en ändring utan att röra speglingen;
baslinjen ligger då kvar så att samma diff kan visas igen.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from tb import (
    ACTION_PLAN_PATH,
    KRAVSPARNING_PATH,
    MARKDOWN_PATH,
    ODT_PATH,
    STATE_PATH,
    TbError,
    sha256_file,
    sha256_text,
)
from tb.actionplan import render_action_plan
from tb.diff import TbDiff, diff_documents
from tb.extract import odt_to_markdown


@dataclass
class SyncResult:
    """Resultatet av en synkronisering."""

    changed: bool
    diff: TbDiff
    markdown_path: Path
    action_plan_path: Path | None
    markdown_was_missing: bool = False
    markdown_was_edited: bool = False

    def describe(self) -> str:
        """Läsbar sammanfattning för terminal och för vidarerapportering."""
        lines: list[str] = []
        if self.markdown_was_missing:
            lines.append(f"Ingen tidigare spegling fanns - skapade {self.markdown_path.name}.")
        elif self.markdown_was_edited:
            lines.append(
                f"VARNING: {self.markdown_path.name} hade redigerats för hand. "
                "Den är en genererad fil och har skrivits över från ODT-filen."
            )

        if self.changed:
            lines.append(f"TB har ändrats: {self.diff.summary()}")
            for change in self.diff.changes:
                label = {
                    "added": "ny",
                    "removed": "borttagen",
                    "modified": "ändrad",
                    "moved": "flyttad",
                }[change.kind]
                lines.append(f"  - {change.code}: {label}")
            if self.action_plan_path is not None:
                lines.append(f"Åtgärdsplan skriven till {self.action_plan_path}.")
        else:
            lines.append("TB är oförändrad - ingen åtgärd krävs.")

        if self.diff.duplicate_codes:
            lines.append(
                "AFC.27 - dubblerade TB-koder i handlingen: "
                + ", ".join(self.diff.duplicate_codes)
            )
        return "\n".join(lines)

    def render_diff_report(self) -> str:
        """Fullständig, sektionsvis diff avsedd att läsas direkt i terminalen.

        Till skillnad från åtgärdsplanen skrivs den här texten aldrig till
        disk. Den finns för att ``/TB`` ska kunna redovisa exakt vad som
        skiljer utan att någon behöver öppna en fil.
        """
        if not self.diff.changes:
            return "Ingen skillnad mot föregående version."

        blocks: list[str] = []
        for change in self.diff.changes:
            heading = f"{change.code} – {change.title} [{change.kind}]"
            blocks.append(heading)
            blocks.append("-" * len(heading))
            if change.kind == "moved":
                blocks.append("Endast flyttad i dokumentet. Innehållet är oförändrat.")
            elif change.kind == "added":
                body = (change.after.body if change.after else "").strip()
                blocks.append(body or "(sektionen har ingen brödtext)")
            elif change.kind == "removed":
                body = (change.before.body if change.before else "").strip()
                blocks.append("Borttagen. Tidigare lydelse:")
                blocks.append(body or "(sektionen hade ingen brödtext)")
            else:
                diff_text = change.unified_diff().strip()
                blocks.append(diff_text or "(endast rubriken ändrad)")
            blocks.append("")
        return "\n".join(blocks).rstrip()


def _read_state(state_path: Path) -> dict:
    if not state_path.is_file():
        return {}
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _write_state(state_path: Path, state: dict) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _parse_previous(markdown_text: str):
    """Tolkar en tidigare spegling tillbaka till sektioner.

    Speglingen jämförs sektionsvis, så den föregående Markdown-filen
    måste tolkas tillbaka till samma struktur som ODT-extraktionen
    producerar. Eftersom renderingen är deterministisk räcker det att
    läsa rubrikerna och texten mellan dem.
    """
    from tb import Section, TbDocument

    document = TbDocument()
    current: dict | None = None
    body: list[str] = []
    preamble: list[str] = []

    def flush() -> None:
        nonlocal body
        if current is not None:
            document.sections.append(
                Section(
                    code=current["code"],
                    title=current["title"],
                    level=current["level"],
                    body="\n".join(body).strip(),
                )
            )
        body = []

    for line in markdown_text.splitlines():
        if line.startswith("<!--"):
            continue
        if line.startswith("#"):
            hashes = len(line) - len(line.lstrip("#"))
            heading_text = line[hashes:].strip()
            if hashes == 1:
                continue  # dokumentets titel
            flush()
            code, _, title = heading_text.partition(" – ")
            current = {"code": code.strip(), "title": title.strip(), "level": hashes}
            continue
        if current is None:
            preamble.append(line)
        else:
            body.append(line)

    flush()
    document.preamble = "\n".join(preamble).strip()
    return document


def sync(
    *,
    odt_path: Path | None = None,
    markdown_path: Path | None = None,
    action_plan_path: Path | None = None,
    kravsparning_path: Path | None = None,
    state_path: Path | None = None,
    write: bool = True,
) -> SyncResult:
    """Synkroniserar speglingen mot primärkällan.

    Med ``write=False`` görs ingen skrivning alls; resultatet talar ändå
    om huruvida speglingen är aktuell. Det används av ``--check``.

    Sökvägarna löses ut ur modulkonstanterna först när funktionen körs,
    inte när den definieras. Annars skulle standardvärdena bindas vid
    import och gå att förbise bara genom att skicka in dem explicit.
    """
    odt_path = ODT_PATH if odt_path is None else odt_path
    markdown_path = MARKDOWN_PATH if markdown_path is None else markdown_path
    action_plan_path = ACTION_PLAN_PATH if action_plan_path is None else action_plan_path
    kravsparning_path = KRAVSPARNING_PATH if kravsparning_path is None else kravsparning_path
    state_path = STATE_PATH if state_path is None else state_path

    document, markdown = odt_to_markdown(odt_path)

    markdown_was_missing = not markdown_path.is_file()
    previous_text = "" if markdown_was_missing else markdown_path.read_text(encoding="utf-8")

    state = _read_state(state_path)
    markdown_was_edited = (
        not markdown_was_missing
        and "markdown_sha256" in state
        and state["markdown_sha256"] != sha256_text(previous_text)
    )

    previous_document = _parse_previous(previous_text) if previous_text else type(document)()
    diff = diff_documents(previous_document, document)

    content_changed = sha256_text(previous_text) != sha256_text(markdown)

    if not write:
        return SyncResult(
            changed=content_changed,
            diff=diff,
            markdown_path=markdown_path,
            action_plan_path=None,
            markdown_was_missing=markdown_was_missing,
            markdown_was_edited=markdown_was_edited,
        )

    written_plan: Path | None = None
    if content_changed:
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        markdown_path.write_text(markdown, encoding="utf-8")
        plan = render_action_plan(diff, kravsparning_path=kravsparning_path, odt_name=odt_path.name)
        action_plan_path.parent.mkdir(parents=True, exist_ok=True)
        action_plan_path.write_text(plan, encoding="utf-8")
        written_plan = action_plan_path

    _write_state(
        state_path,
        {
            "odt_sha256": sha256_file(odt_path),
            "markdown_sha256": sha256_text(markdown),
            "section_count": len(document.sections),
            "last_sync_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "last_change_utc": (
                datetime.now(timezone.utc).isoformat(timespec="seconds")
                if content_changed
                else state.get("last_change_utc")
            ),
        },
    )

    return SyncResult(
        changed=content_changed,
        diff=diff,
        markdown_path=markdown_path,
        action_plan_path=written_plan,
        markdown_was_missing=markdown_was_missing,
        markdown_was_edited=markdown_was_edited,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tb.sync",
        description=(
            "Speglar TB-handlingen (ODT, primärkälla) till Markdown och skriver "
            "en åtgärdsplan när innehållet ändrats."
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Avgör om speglingen är aktuell utan att skriva. Avslutar med 1 om den är inaktuell.",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Bevaka ODT-filen och synkronisera automatiskt vid varje omsparning.",
    )
    parser.add_argument(
        "--diff",
        action="store_true",
        help="Skriv ut full diff och åtgärdsplan samtidigt som speglingen synkroniseras.",
    )
    parser.add_argument("--odt", type=Path, default=ODT_PATH, help="Sökväg till ODT-filen.")
    arguments = parser.parse_args(argv)

    if arguments.check and arguments.watch:
        parser.error("--check och --watch kan inte kombineras.")
    if arguments.diff and arguments.watch:
        parser.error("--diff och --watch kan inte kombineras.")

    try:
        if arguments.watch:
            from tb.watch import watch

            return watch(odt_path=arguments.odt)

        result = sync(odt_path=arguments.odt, write=not arguments.check)
    except TbError as exc:
        print(f"FEL: {exc}", file=sys.stderr)
        return 2

    print(result.describe())
    if arguments.diff:
        print()
        print(result.render_diff_report())
        if result.action_plan_path is not None:
            print("\nÅTGÄRDSPLAN\n")
            print(result.action_plan_path.read_text(encoding="utf-8"))
    if arguments.check and result.changed:
        print(
            "Speglingen är inaktuell. Kör 'python -m tb.sync' för att uppdatera den.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
