"""ODT -> strukturerat dokument -> Markdown.

Kravkälla: TB-sektion AFC.22 (spårbarhet via TB-kod).

Handlingen är skriven utan Words rubrikformatmallar — varje stycke har
styckeformatet ``Normal``, även rubrikerna. Rubriknivå kan därför inte
läsas ur ODF-stilarna, utan måste härledas ur AMA-koden som inleder
rubrikraden (``A``, ``AFA``, ``AFC.22``, ``CBE`` ...). Det är samtidigt
precis den spårbarhet AFC.22 efterfrågar, så koden blir sektionens
identitet genom hela kedjan: extraktion -> diff -> åtgärdsplan.

Den svåra avgränsningen är att skilja en rubrik som ``BBFörarbeten``
(kod ``BB`` hopskriven med titeln ``Förarbeten``) från brödtext som
``DWG-data hämtas från blocket TRVJ_NAMNRUTA och dess attribut.``, där
inledningen också ser ut som en versal kod. Reglerna i
`parse_heading()` är därför medvetet restriktiva och täcks av tester med
verkliga rader ur handlingen.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from tb import GENERATED_HEADER, Section, TbDocument, TbError

_NS = {
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
    "table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
    "draw": "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0",
    "xlink": "http://www.w3.org/1999/xlink",
}


def _q(prefix: str, tag: str) -> str:
    return f"{{{_NS[prefix]}}}{tag}"


#: Avgränsare mellan kod och titel: tankstreck med valfria blanksteg,
#: bindestreck omgivet av blanksteg, eller enbart blanksteg. Rent
#: bindestreck utan blanksteg räknas INTE, eftersom det är så
#: sammansättningar som ``DWG-data`` och ``XLSX-data`` ser ut i brödtexten.
_SEPARATOR_RE = re.compile(r"^(?P<sep>\s*[–—]\s*|\s+-\s+|\s+)")

#: Den inledande versalsekvens som kan vara en AMA-kod.
_CODE_PREFIX_RE = re.compile(r"^[A-Z]{1,4}")

#: Sifferdelen i en kod som ``AFC.22``.
_CODE_NUMBER_RE = re.compile(r"^\.\d{1,3}")

#: Titeln i en hopskriven rubrik (``BBFörarbeten``) eller efter enbart
#: blanksteg (``AFC.2 Utförande``) måste se ut som ett riktigt ord: en
#: versal följd av minst en gemen. Det utesluter ``G-data ...`` som
#: annars skulle tolkas som kod ``DW`` + titel ``G-data ...``.
_TITLE_WORD_RE = re.compile(r"^[A-ZÅÄÖ][a-zåäöéü]")

#: En rubrik är kort och avslutas inte som en mening eller listpunkt.
#: Gränsen är satt så att handlingens längsta verkliga rubrik ryms
#: (``B – (Förarbeten, hjälparbeten, ... röjning m m)``, 91 tecken).
#: Skyddet mot brödtext ligger primärt i skiljeteckensregeln och
#: ordregeln nedan, inte i längden.
_MAX_HEADING_LENGTH = 130


def _try_split(code: str, remainder: str) -> tuple[str, str] | None:
    """Prövar en given kod mot resten av raden.

    Returnerar ``(kod, titel)`` om avgränsaren och titeln uppfyller
    reglerna, annars None.
    """
    match = _SEPARATOR_RE.match(remainder)
    if match is None:
        separator, title = "", remainder
    else:
        separator, title = match.group("sep"), remainder[match.end() :]

    title = title.strip()
    if not title:
        return None

    has_dash = any(character in separator for character in "–—-")
    if not has_dash and not _TITLE_WORD_RE.match(title):
        return None
    return code, title


def parse_heading(text: str) -> tuple[str, str] | None:
    """Tolkar en rad som en TB-rubrik, eller returnerar None.

    Returnerar ``(kod, titel)`` om raden är en sektionsrubrik. Reglerna:

    - raden är högst `_MAX_HEADING_LENGTH` tecken,
    - raden avslutas inte med ``.``, ``,``, ``:`` eller ``;`` (då är det
      brödtext eller en listpunkt),
    - koden är 1-4 versaler med valfritt ``.N``-suffix,
    - avgränsaren mellan kod och titel är tankstreck, bindestreck med
      blanksteg, blanksteg, eller ingenting,
    - om avgränsaren är blanksteg eller ingenting måste titeln inledas
      med en versal följd av en gemen.

    Kodlängden prövas explicit från längsta till kortaste i stället för
    att förlita sig på regexets backtracking, eftersom ett regex som
    avslutas med ``.*`` alltid lyckas och därför aldrig backar. Det är
    den prövningen som gör att ``BBFörarbeten`` tolkas som ``BB`` +
    ``Förarbeten`` (``BBF`` + ``örarbeten`` förkastas av ordregeln),
    medan ``DWG-data hämtas ...`` förkastas i samtliga varianter.
    """
    line = text.strip()
    if not line or len(line) > _MAX_HEADING_LENGTH:
        return None
    if line[-1] in ".,:;":
        return None

    prefix = _CODE_PREFIX_RE.match(line)
    if prefix is None:
        return None

    for length in range(len(prefix.group()), 0, -1):
        letters = line[:length]
        rest = line[length:]

        number = _CODE_NUMBER_RE.match(rest)
        if number is not None:
            parsed = _try_split(letters + number.group(), rest[number.end() :])
            if parsed is not None:
                return parsed

        parsed = _try_split(letters, rest)
        if parsed is not None:
            return parsed
    return None


def heading_level(code: str) -> int:
    """Härleder Markdown-rubriknivå ur AMA-kodens djup.

    ``A`` -> nivå 2, ``BB`` -> 3, ``BBA`` -> 4, ``AFC.22`` -> 5. Nivå 1
    reserveras för dokumentets egen titel, så att den genererade filen
    har exakt en H1.
    """
    letters, _, number = code.partition(".")
    level = 1 + len(letters)
    if number:
        level += 1
    return min(level, 6)


def _element_text(element: ElementTree.Element) -> str:
    """Plockar ut all text ur ett ODF-element, inklusive mellanslagstaggar.

    ODF kodar upprepade mellanslag som ``<text:s text:c="n"/>``, tabbar
    som ``<text:tab/>`` och radbrytningar som ``<text:line-break/>``.
    Utan denna hantering försvinner indrag och ord skrivs ihop.
    """
    parts: list[str] = []

    def walk(node: ElementTree.Element) -> None:
        if node.tag == _q("text", "s"):
            count = node.get(_q("text", "c"))
            parts.append(" " * (int(count) if count and count.isdigit() else 1))
        elif node.tag == _q("text", "tab"):
            parts.append("\t")
        elif node.tag == _q("text", "line-break"):
            parts.append("\n")
        elif node.tag == _q("draw", "image"):
            href = node.get(_q("xlink", "href")) or "bild"
            parts.append(f"![{Path(href).name}]({href})")

        if node.text:
            parts.append(node.text)
        for child in node:
            walk(child)
            if child.tail:
                parts.append(child.tail)

    walk(element)
    return "".join(parts)


def _normalise(text: str) -> str:
    """Normaliserar blanksteg utan att slå ihop avsiktliga radbrytningar."""
    lines = [re.sub(r"[ \t\u00a0]+", " ", line).strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line)


def _escape_cell(text: str) -> str:
    return _normalise(text).replace("\n", "<br>").replace("|", "\\|")


def _render_table(table: ElementTree.Element) -> list[str]:
    """Renderar en ODF-tabell som en Markdown-tabell.

    Första raden tolkas som rubrikrad. Tabeller utan rader hoppas över.
    """
    rows: list[list[str]] = []
    for row in table.iter(_q("table", "table-row")):
        cells: list[str] = []
        for cell in row.findall(_q("table", "table-cell")):
            repeat = cell.get(_q("table", "number-columns-repeated"))
            value = _escape_cell(_element_text(cell))
            cells.extend([value] * (int(repeat) if repeat and repeat.isdigit() else 1))
        while cells and not cells[-1]:
            cells.pop()
        if cells:
            rows.append(cells)

    if not rows:
        return []

    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    header, *body = rows
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * width]
    lines.extend("| " + " | ".join(row) + " |" for row in body)
    return lines


def _iter_blocks(parent: ElementTree.Element, depth: int = 0):
    """Går igenom ODF-innehållet och ger ifrån sig Markdown-block.

    Varje block är ``(sort, text)`` där sort är ``paragraph``, ``list``
    eller ``table``. Listor hanteras rekursivt så att nästlade listor får
    rätt indrag.
    """
    for node in parent:
        if node.tag == _q("text", "p") or node.tag == _q("text", "h"):
            text = _normalise(_element_text(node))
            if text:
                yield ("paragraph", text)
        elif node.tag == _q("text", "list"):
            for item in node.findall(_q("text", "list-item")):
                for kind, text in _iter_blocks(item, depth + 1):
                    if kind == "paragraph":
                        yield ("list", "  " * (depth) + f"- {text}")
                    else:
                        yield (kind, text)
        elif node.tag == _q("table", "table"):
            lines = _render_table(node)
            if lines:
                yield ("table", "\n".join(lines))
        elif node.tag in {_q("text", "section"), _q("text", "soft-page-break")}:
            yield from _iter_blocks(node, depth)


def _join_blocks(blocks: list[tuple[str, str]]) -> str:
    """Fogar samman block till Markdown.

    Sammanhängande listpunkter separeras med en enkel radbrytning så att
    de blir en lista; alla andra övergångar får en tom rad emellan.
    """
    parts: list[str] = []
    for index, (kind, text) in enumerate(blocks):
        if index:
            previous_kind = blocks[index - 1][0]
            parts.append("\n" if kind == "list" and previous_kind == "list" else "\n\n")
        parts.append(text)
    return "".join(parts).strip()


def parse_odt(odt_path: str | Path) -> TbDocument:
    """Läser ODT-filen och returnerar den som ett strukturerat dokument.

    Primärkällan öppnas skrivskyddat. Höjer TbError om filen saknas eller
    inte är en läsbar ODF-fil.
    """
    path = Path(odt_path)
    if not path.is_file():
        raise TbError(f"TB-handlingen hittades inte: {path}")

    try:
        with zipfile.ZipFile(path) as archive:
            content = archive.read("content.xml")
    except (zipfile.BadZipFile, KeyError) as exc:
        raise TbError(f"Kunde inte läsa ODT-innehållet i {path.name}: {exc}") from exc

    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as exc:
        raise TbError(f"Ogiltig XML i {path.name}: {exc}") from exc

    body = root.find(f"{_q('office', 'body')}/{_q('office', 'text')}")
    if body is None:
        raise TbError(f"Hittade ingen brödtext i {path.name}")

    document = TbDocument()
    preamble: list[tuple[str, str]] = []
    current_code: str | None = None
    current_title = ""
    current_level = 2
    current_body: list[tuple[str, str]] = []

    def flush() -> None:
        nonlocal current_body
        if current_code is None:
            return
        document.sections.append(
            Section(
                code=current_code,
                title=current_title,
                level=current_level,
                body=_join_blocks(current_body),
            )
        )
        current_body = []

    for kind, text in _iter_blocks(body):
        parsed = parse_heading(text) if kind == "paragraph" else None
        if parsed is not None:
            flush()
            current_code, current_title = parsed
            current_level = heading_level(current_code)
            continue

        if current_code is None:
            preamble.append((kind, text))
        else:
            current_body.append((kind, text))

    flush()
    document.preamble = _join_blocks(preamble)
    return document


def render_markdown(document: TbDocument, *, title: str = "TB - Datasamordningsassistenten") -> str:
    """Renderar ett tolkat dokument som Markdown.

    Resultatet innehåller medvetet ingen tidsstämpel och ingen hash av
    ODT-filens bytes — endast innehållet. Det gör att en omsparning av
    ODT utan textändring inte ger någon diff, vilket är hela poängen med
    att jämföra mot den föregående speglingen.
    """
    parts = [GENERATED_HEADER, f"# {title}\n"]
    if document.preamble:
        parts.append(document.preamble + "\n")
    for section in document.sections:
        parts.append(section.heading + "\n")
        if section.body:
            parts.append(section.body + "\n")
    return "\n".join(parts).rstrip() + "\n"


def odt_to_markdown(odt_path: str | Path) -> tuple[TbDocument, str]:
    """Bekvämlighetsfunktion: läser ODT och returnerar dokument + Markdown."""
    document = parse_odt(odt_path)
    return document, render_markdown(document)
