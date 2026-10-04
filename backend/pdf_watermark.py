"""Vattenstämpel i PDF-filer (standardtext KONTROLLÄRENDE).

Motsvarar DOCX-vattenstämpeln i ``backend/file_status.py`` (diagonal,
halvgenomskinlig, grå text över hela sidan), men för PDF. Standardtexten är
``KONTROLLÄRENDE``; användaren kan välja annan text i PDF-metadatapanelen
(användarbeslut). TB omfattar i dag bara DOCX-vattenstämpel; PDF-stämpeln
är ett nytt krav som rapporterats enligt AFC.27 (se docs/kravsparning.md,
krav PDF-1).

Kontrakt och invarianter:

* Stämpeln läggs som en egen form-XObject per sida och två egna
  innehållsströmmar som omsluter sidans ursprungliga innehåll
  (``q`` … ``Q q /DSAKontrollarende Do Q``). Båda strömmarna och formen bär
  nyckeln ``/DSAKontrollarende``. Borttagning tar bort exakt dessa
  objekt; sidans eget innehåll läses och skrivs aldrig om.
* Stämpeltexten sparas i formens ``/DSAWatermarkText`` så att den kan
  läsas tillbaka utan textextraktion. Byte av text = borttagning + ny
  stämpel på samma sida i samma skrivning.
* Skrivning sker som PDF-inkrementell uppdatering (pypdf
  ``incremental=True``): originalfilens bytes behålls oförändrade och
  ändringarna läggs till sist.
* Säkerhetsflöde per fil: skriv till temporär fil i samma mapp → läs
  tillbaka och kontrollera sidantal och stämpelstatus på varje sida →
  ersätt originalet atomiskt (``os.replace``) bara om det inte ändrats
  under tiden. Misslyckas något är originalet orört.
* Krypterade och digitalt signerade PDF:er ändras aldrig (en ändring
  skulle bryta signaturen).

``pypdf._add_object`` är privat i pypdf men det enda sättet att lägga till
nya indirekta objekt i en inkrementell skrivning; pypdf är låst till
version 6 i pyproject.toml.
"""

from __future__ import annotations

import io
import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from pypdf.errors import PdfReadError
from pypdf.generic import (
    ArrayObject,
    BooleanObject,
    DictionaryObject,
    FloatObject,
    IndirectObject,
    NameObject,
    StreamObject,
    TextStringObject,
)

WATERMARK_TEXT = "KONTROLLÄRENDE"

_MARKER = NameObject("/DSAKontrollarende")
_XOBJECT_NAME = NameObject("/DSAKontrollarende")
_TEXT_KEY = NameObject("/DSAWatermarkText")
MAX_TEXT_LENGTH = 40

# Helvetica-bredder (Adobe AFM, enhet 1/1000 em) för de tecken stämpeltexter
# får innehålla. Helvetica är en av PDF:ens 14 standardtypsnitt och behöver
# inte bäddas in; WinAnsiEncoding täcker ÅÄÖ. Tecken utanför tabellen
# avvisas av ``validate_watermark_text`` i stället för att mätas fel.
_HELVETICA_WIDTHS = {
    " ": 278, "A": 667, "B": 667, "C": 722, "D": 722, "E": 667, "F": 611, "G": 778,
    "H": 722, "I": 278, "J": 500, "K": 667, "L": 556, "M": 833, "N": 722, "O": 778,
    "P": 667, "Q": 778, "R": 722, "S": 667, "T": 611, "U": 722, "V": 667, "W": 944,
    "X": 667, "Y": 667, "Z": 611, "Å": 667, "Ä": 667, "Ö": 778, "É": 667, "Ü": 722,
    "a": 556, "b": 556, "c": 500, "d": 556, "e": 556, "f": 278, "g": 556, "h": 556,
    "i": 222, "j": 222, "k": 500, "l": 222, "m": 833, "n": 556, "o": 556, "p": 556,
    "q": 556, "r": 333, "s": 500, "t": 278, "u": 556, "v": 500, "w": 722, "x": 500,
    "y": 500, "z": 500, "å": 556, "ä": 556, "ö": 556, "é": 556, "ü": 556,
    "0": 556, "1": 556, "2": 556, "3": 556, "4": 556, "5": 556, "6": 556, "7": 556,
    "8": 556, "9": 556, "-": 333, ".": 278, ",": 278, ":": 278, "/": 278, "(": 333,
    ")": 333, "&": 667, "!": 278, "?": 556, "_": 556, "+": 584,
}
_HELVETICA_CAP_HEIGHT = 0.718
# Samma utseende som DOCX-stämpeln: silvergrå, halvgenomskinlig.
_GRAY_LEVEL = 0.6
_OPACITY = 0.5
# Andel av sidans diagonal som texten upptar; lägre än 1 så att glyfernas
# höjd inte klipps i hörnen.
_LENGTH_OF_DIAGONAL = 0.7


class PdfWatermarkError(Exception):
    """Höjs när en PDF inte kan läsas eller stämplas säkert."""


@dataclass(frozen=True)
class PdfWatermarkResult:
    path: Path
    changed: bool
    error: str | None = None


def _resolve(value):
    return value.get_object() if isinstance(value, IndirectObject) else value


def _content_refs(page: DictionaryObject) -> list:
    raw = page.raw_get("/Contents") if "/Contents" in page else None
    if raw is None:
        return []
    resolved = _resolve(raw)
    if isinstance(resolved, ArrayObject):
        return list(resolved)
    return [raw]


def _is_marker_stream(ref) -> bool:
    stream = _resolve(ref)
    return isinstance(stream, DictionaryObject) and stream.get(_MARKER) == BooleanObject(True)


def _page_has_watermark(page: DictionaryObject) -> bool:
    return any(_is_marker_stream(ref) for ref in _content_refs(page))


def _page_watermark_text(page: DictionaryObject) -> str | None:
    """Stämpeltexten på sidan, eller None om sidan saknar stämpel."""
    if not _page_has_watermark(page):
        return None
    resources = _resolve(page.get("/Resources"))
    xobjects = _resolve(resources.get("/XObject")) if isinstance(resources, DictionaryObject) else None
    form = _resolve(xobjects.get(_XOBJECT_NAME)) if isinstance(xobjects, DictionaryObject) else None
    if isinstance(form, DictionaryObject) and _TEXT_KEY in form:
        return str(form[_TEXT_KEY])
    # Stämplar från första versionen saknade textnyckeln och var alltid KONTROLLÄRENDE.
    return WATERMARK_TEXT


def validate_watermark_text(text: str) -> str:
    """Returnerar texten utan omgivande blanksteg eller höjer PdfWatermarkError."""
    cleaned = " ".join(text.split())
    if not cleaned:
        raise PdfWatermarkError("Vattenstämpelns text får inte vara tom.")
    if len(cleaned) > MAX_TEXT_LENGTH:
        raise PdfWatermarkError(f"Vattenstämpelns text får vara högst {MAX_TEXT_LENGTH} tecken.")
    unsupported = sorted({char for char in cleaned if char not in _HELVETICA_WIDTHS})
    if unsupported:
        raise PdfWatermarkError(
            "Vattenstämpelns text innehåller tecken som inte stöds: " + " ".join(unsupported)
        )
    return cleaned


def _inherited_resources(page: DictionaryObject) -> DictionaryObject:
    node = page
    while node is not None:
        if "/Resources" in node:
            return _resolve(node["/Resources"])
        node = _resolve(node.get("/Parent"))
    return DictionaryObject()


def _check_readable(reader: PdfReader, path: Path) -> None:
    if reader.is_encrypted:
        raise PdfWatermarkError(f"{path.name} är krypterad och kan inte stämplas.")
    acro_form = _resolve(reader.trailer["/Root"].get("/AcroForm"))
    if isinstance(acro_form, DictionaryObject) and int(_resolve(acro_form.get("/SigFlags", 0))) & 1:
        raise PdfWatermarkError(
            f"{path.name} är digitalt signerad; en vattenstämpel skulle bryta signaturen."
        )


def _open_reader(source, path: Path) -> PdfReader:
    try:
        return PdfReader(source)
    except (PdfReadError, OSError, ValueError) as exc:
        raise PdfWatermarkError(f"{path.name} kunde inte läsas som PDF: {exc}") from exc


def read_watermark_text(path: str | Path) -> str | None:
    """Texten på första stämplade sidan, eller None om PDF:en saknar stämpel."""
    pdf_path = Path(path)
    reader = _open_reader(pdf_path, pdf_path)
    if reader.is_encrypted:
        raise PdfWatermarkError(f"{pdf_path.name} är krypterad och kan inte läsas.")
    try:
        for page in reader.pages:
            text = _page_watermark_text(page)
            if text is not None:
                return text
    except (PdfReadError, KeyError, ValueError) as exc:
        raise PdfWatermarkError(f"{pdf_path.name} kunde inte läsas som PDF: {exc}") from exc
    return None


def has_control_watermark(path: str | Path) -> bool:
    """True om någon sida i PDF:en bär en vattenstämpel från appen."""
    return read_watermark_text(path) is not None


def _escape_pdf_string(raw: bytes) -> bytes:
    return raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def _watermark_content(box: tuple[float, float, float, float], rotation: int, text: str) -> bytes:
    """Ritinstruktioner för en diagonal, centrerad stämpel på sidan.

    Vinkeln räknas i visningsläget (efter sidans /Rotate) så att texten
    alltid läses nedifrån vänster mot uppe till höger, som i Word.
    """
    llx, lly, urx, ury = box
    width, height = urx - llx, ury - lly
    shown_width, shown_height = (height, width) if rotation in (90, 270) else (width, height)
    angle = math.atan2(shown_height, shown_width) + math.radians(rotation)
    try:
        text_units = sum(_HELVETICA_WIDTHS[char] for char in text) / 1000
    except KeyError as exc:
        raise PdfWatermarkError(f"Tecknet {exc.args[0]!r} stöds inte i PDF-vattenstämpeln.") from None
    diagonal = math.hypot(shown_width, shown_height)
    font_size = min(_LENGTH_OF_DIAGONAL * diagonal / text_units, 0.18 * min(shown_width, shown_height))
    text_width = text_units * font_size
    cap_height = _HELVETICA_CAP_HEIGHT * font_size
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    center_x, center_y = llx + width / 2, lly + height / 2
    start_x = center_x - text_width / 2 * cos_a + cap_height / 2 * sin_a
    start_y = center_y - text_width / 2 * sin_a - cap_height / 2 * cos_a
    encoded = _escape_pdf_string(text.encode("cp1252"))
    return (
        f"q /DSAGs gs {_GRAY_LEVEL} g BT /DSAFont {font_size:.3f} Tf "
        f"{cos_a:.6f} {sin_a:.6f} {-sin_a:.6f} {cos_a:.6f} {start_x:.3f} {start_y:.3f} Tm ("
    ).encode("ascii") + encoded + b") Tj ET Q\n"


def _add_to_page(writer: PdfWriter, page, text: str) -> None:
    box = tuple(float(value) for value in page.cropbox)
    rotation = int(page.get("/Rotate", 0) or 0) % 360

    form = StreamObject()
    form.update(
        {
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Form"),
            NameObject("/BBox"): ArrayObject(FloatObject(value) for value in box),
            NameObject("/Resources"): DictionaryObject(
                {
                    NameObject("/Font"): DictionaryObject(
                        {
                            NameObject("/DSAFont"): DictionaryObject(
                                {
                                    NameObject("/Type"): NameObject("/Font"),
                                    NameObject("/Subtype"): NameObject("/Type1"),
                                    NameObject("/BaseFont"): NameObject("/Helvetica"),
                                    NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
                                }
                            )
                        }
                    ),
                    NameObject("/ExtGState"): DictionaryObject(
                        {
                            NameObject("/DSAGs"): DictionaryObject(
                                {
                                    NameObject("/Type"): NameObject("/ExtGState"),
                                    NameObject("/ca"): FloatObject(_OPACITY),
                                }
                            )
                        }
                    ),
                }
            ),
            _MARKER: BooleanObject(True),
            _TEXT_KEY: TextStringObject(text),
        }
    )
    form.set_data(_watermark_content(box, rotation, text))
    form_ref = writer._add_object(form)

    def marker_stream(data: bytes) -> IndirectObject:
        stream = StreamObject()
        stream[_MARKER] = BooleanObject(True)
        stream.set_data(data)
        return writer._add_object(stream)

    # Sidans resurser kopieras grunt till sidan själv, eftersom flera sidor
    # (med olika format) kan dela samma resursordbok men behöver var sin form.
    resources = DictionaryObject(_inherited_resources(page))
    xobjects = DictionaryObject(_resolve(resources.get("/XObject")) or {})
    xobjects[_XOBJECT_NAME] = form_ref
    resources[NameObject("/XObject")] = xobjects
    page[NameObject("/Resources")] = resources

    contents = _content_refs(page)
    page[NameObject("/Contents")] = ArrayObject(
        [marker_stream(b"q\n"), *contents, marker_stream(b"\nQ q /DSAKontrollarende Do Q\n")]
    )


def _remove_from_page(page) -> None:
    page[NameObject("/Contents")] = ArrayObject(ref for ref in _content_refs(page) if not _is_marker_stream(ref))
    if "/Resources" not in page:
        return
    resources = DictionaryObject(_resolve(page["/Resources"]))
    xobjects = _resolve(resources.get("/XObject"))
    if isinstance(xobjects, DictionaryObject) and _XOBJECT_NAME in xobjects:
        xobjects = DictionaryObject(xobjects)
        del xobjects[_XOBJECT_NAME]
        resources[NameObject("/XObject")] = xobjects
        page[NameObject("/Resources")] = resources


def _reserve_existing_object_numbers(writer: PdfWriter, reader: PdfReader) -> None:
    """Hindrar att nya objekt återanvänder objektnummer som redan finns i filen.

    pypdf 6 numrerar nya objekt efter det högsta objekt som står i
    xref-tabellen, men en tidigare inkrementell uppdatering (t.ex. vår egen
    stämpel) lägger sin xref-ström på ett nummer som inte står där. Utan
    utfyllnad krockar nästa stämpel med den xref-strömmen och läsare
    hittar fel objekt. ``None``-platser skrivs aldrig ut av pypdf.
    """
    size = int(reader.trailer.get("/Size", 0))
    missing = size - 1 - len(writer._objects)
    if missing > 0:
        writer._objects.extend([None] * missing)


def set_control_watermark(path: str | Path, enabled: bool, *, text: str = WATERMARK_TEXT) -> bool:
    """Lägger till (``enabled``) eller tar bort stämpeln på alla sidor.

    Med ``enabled`` får varje sida exakt ``text``; sidor med annan
    stämpeltext byts ut. Returnerar True om filen ändrades. Höjer PdfWatermarkError om filen inte
    kan läsas, inte får ändras eller om read-back-kontrollen misslyckas;
    originalet är då orört.
    """
    pdf_path = Path(path).expanduser().resolve()
    wanted = validate_watermark_text(text) if enabled else None
    try:
        stat_before = pdf_path.stat()
        original = pdf_path.read_bytes()
    except OSError as exc:
        raise PdfWatermarkError(f"{pdf_path.name} kunde inte läsas: {exc}") from exc

    reader = _open_reader(io.BytesIO(original), pdf_path)
    _check_readable(reader, pdf_path)
    try:
        page_states = [_page_watermark_text(page) for page in reader.pages]
    except (PdfReadError, KeyError, ValueError) as exc:
        raise PdfWatermarkError(f"{pdf_path.name} kunde inte läsas som PDF: {exc}") from exc
    if all(state == wanted for state in page_states):
        return False

    try:
        writer = PdfWriter(io.BytesIO(original), incremental=True)
        _reserve_existing_object_numbers(writer, reader)
        for page, current in zip(writer.pages, page_states, strict=True):
            if current == wanted:
                continue
            if current is not None:
                _remove_from_page(page)
            if wanted is not None:
                _add_to_page(writer, page, wanted)
        output = io.BytesIO()
        writer.write(output)
    except PdfWatermarkError:
        raise
    except Exception as exc:  # noqa: BLE001 - pypdf höjer många feltyper för trasiga PDF:er
        raise PdfWatermarkError(f"{pdf_path.name} kunde inte stämplas: {exc}") from exc
    updated = output.getvalue()

    # Read-back: den nya filen ska vara läsbar, ha samma sidantal och rätt
    # stämpelstatus på varje sida innan originalet ersätts.
    try:
        check = PdfReader(io.BytesIO(updated))
        verified = len(check.pages) == len(page_states) and all(
            _page_watermark_text(page) == wanted for page in check.pages
        )
    except Exception as exc:  # noqa: BLE001
        raise PdfWatermarkError(f"Read-back av {pdf_path.name} misslyckades: {exc}") from exc
    if not verified:
        raise PdfWatermarkError(f"Read-back av {pdf_path.name} visade fel stämpelstatus; filen ändrades inte.")

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=pdf_path.parent, prefix=f".{pdf_path.stem}.", suffix=".tmp", delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(updated)
        current = pdf_path.stat()
        if (current.st_size, current.st_mtime_ns) != (stat_before.st_size, stat_before.st_mtime_ns):
            raise PdfWatermarkError(f"{pdf_path.name} ändrades av någon annan under stämplingen; försök igen.")
        os.replace(temporary_path, pdf_path)
        temporary_path = None
    except OSError as exc:
        raise PdfWatermarkError(
            f"{pdf_path.name} kunde inte skrivas (är filen öppen i en PDF-läsare?): {exc}"
        ) from exc
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return True


def apply_control_watermark_state(
    desired: dict[Path, bool], *, text: str = WATERMARK_TEXT
) -> list[PdfWatermarkResult]:
    """Sätter stämpelstatus per fil; ett fel stoppar inte övriga filer."""
    results: list[PdfWatermarkResult] = []
    for path, enabled in desired.items():
        try:
            changed = set_control_watermark(path, enabled, text=text)
        except PdfWatermarkError as exc:
            results.append(PdfWatermarkResult(Path(path), False, str(exc)))
        else:
            results.append(PdfWatermarkResult(Path(path), changed))
    return results


__all__ = [
    "WATERMARK_TEXT",
    "PdfWatermarkError",
    "PdfWatermarkResult",
    "apply_control_watermark_state",
    "MAX_TEXT_LENGTH",
    "has_control_watermark",
    "read_watermark_text",
    "validate_watermark_text",
    "set_control_watermark",
]
