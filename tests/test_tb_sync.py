"""Tester för TB-synkroniseringen (tb/).

Kravkälla: TB-sektion AFC.22/AFC.27.

Testerna delas i tre grupper:

1. Rubrikigenkänningen, med verkliga rader ur handlingen — både sådana
   som ska bli rubriker och sådana som absolut inte får bli det.
2. Extraktion och round-trip mot den riktiga ODT-filen, som säkerställer
   att speglingen kan tolkas tillbaka utan förlust. Utan det skulle varje
   efterföljande ändring rapportera samtliga sektioner som ändrade.
3. Synkroniseringsflödet mot syntetiska ODT-filer, inklusive det
   centrala kravet att en omsparning utan textändring inte ger någon
   åtgärdsplan.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from tb import ODT_PATH, Section, TbDocument, TbError
from tb.actionplan import load_requirement_index, render_action_plan
from tb.diff import diff_documents
from tb.extract import heading_level, parse_heading, parse_odt, render_markdown
from tb.sync import _parse_previous, main, sync

_CONTENT_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<office:document-content
  xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
  xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"
  xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0">
  <office:body><office:text>{body}</office:text></office:body>
</office:document-content>
"""


def _make_odt(path: Path, paragraphs: list[str]) -> Path:
    """Bygger en minimal men giltig ODT-fil av en lista stycken."""
    body = "".join(f"<text:p>{p}</text:p>" for p in paragraphs)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        archive.writestr("content.xml", _CONTENT_TEMPLATE.format(body=body))
    return path


# --------------------------------------------------------------------------
# 1. Rubrikigenkänning
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("A –Administrativa föreskrifter", ("A", "Administrativa föreskrifter")),
        ("AFA– Allmän orientering", ("AFA", "Allmän orientering")),
        ("AFC.1Omfattning", ("AFC.1", "Omfattning")),
        ("AFC.2 Utförande", ("AFC.2", "Utförande")),
        ("AFC.22 Kvalitets och miljökrav", ("AFC.22", "Kvalitets och miljökrav")),
        ("BBFörarbeten", ("BB", "Förarbeten")),
        ("BBA – Programeringsspråk", ("BBA", "Programeringsspråk")),
        ("CBE – DWG-backend", ("CBE", "DWG-backend")),
        ("CBF – Asynkron och parallell bearbetning", ("CBF", "Asynkron och parallell bearbetning")),
        ("D–Frontend (Marköverbyggnader, anläggningskompletteringar m m)", ("D", "Frontend (Marköverbyggnader, anläggningskompletteringar m m)")),
    ],
)
def test_parse_heading_accepts_real_tb_headings(line, expected):
    assert parse_heading(line) == expected


@pytest.mark.parametrize(
    "line",
    [
        # Brödtext som inleds med en versal förkortning - får aldrig bli rubrik.
        "DWG-data hämtas från blocket TRVJ_NAMNRUTA och dess attribut.",
        "XLSX-data hämtas från definierade arbetsblad och celler.",
        "DOCX-data hämtas bland annat från Word Content Controls och synliga metadatafält.",
        # Vanliga meningar och listpunkter.
        "I detta projekt används projektportalen ACC",
        "Applikationen ska byggas och köras i en Windowsmiljö.",
        "Python 3.11 eller senare,",
        "PySide6,",
        "NiceGUI",
        "Rust 2021 edition för snabb DWG-hantering via LibreDWG.",
        "bindgen vid byggnation av DWG-modulen.",
        "För att bygga krävs",
        "",
    ],
)
def test_parse_heading_rejects_body_text(line):
    assert parse_heading(line) is None


def test_parse_heading_rejects_code_without_title():
    assert parse_heading("ACC") is None


@pytest.mark.parametrize(
    ("code", "level"),
    [("A", 2), ("BB", 3), ("BBA", 4), ("AFC.1", 5), ("EACD", 5), ("AFC.22", 5)],
)
def test_heading_level_follows_code_depth(code, level):
    assert heading_level(code) == level


# --------------------------------------------------------------------------
# 2. Extraktion och round-trip mot den riktiga handlingen
# --------------------------------------------------------------------------


@pytest.mark.skipif(not ODT_PATH.is_file(), reason="TB-handlingen finns inte i denna checkout")
def test_real_odt_yields_expected_section_codes():
    document = parse_odt(ODT_PATH)
    codes = {section.code for section in document.sections}

    assert codes == {"CBE"}, f"TB-sektionerna måste uppdateras efter aktuell ODT: {sorted(codes)}"

    # Förkortningar i brödtexten får inte ha blivit sektioner.
    for forbidden in {"DWG", "XLSX", "DOCX", "TRVJ"}:
        assert forbidden not in codes, f"{forbidden} feltolkades som sektionskod"


@pytest.mark.skipif(not ODT_PATH.is_file(), reason="TB-handlingen finns inte i denna checkout")
def test_markdown_round_trip_is_lossless():
    """Speglingen måste kunna tolkas tillbaka till samma sektioner.

    Om detta går sönder rapporteras varje sektion som ändrad vid nästa
    verkliga TB-ändring, och åtgärdsplanen blir oanvändbar.
    """
    document = parse_odt(ODT_PATH)
    reparsed = _parse_previous(render_markdown(document))

    assert len(reparsed.sections) == len(document.sections)
    assert not diff_documents(reparsed, document).has_changes


@pytest.mark.skipif(not ODT_PATH.is_file(), reason="TB-handlingen finns inte i denna checkout")
def test_real_odt_duplicate_codes_are_reported():
    """Den aktuella handlingen innehåller inga dubbla sektionskoder."""
    document = parse_odt(ODT_PATH)
    assert document.duplicate_codes() == []


def test_parse_odt_rejects_missing_file(tmp_path):
    with pytest.raises(TbError):
        parse_odt(tmp_path / "finns-inte.odt")


def test_parse_odt_rejects_non_odt(tmp_path):
    broken = tmp_path / "trasig.odt"
    broken.write_bytes(b"inte en zip")
    with pytest.raises(TbError):
        parse_odt(broken)


# --------------------------------------------------------------------------
# 3. Diff
# --------------------------------------------------------------------------


def _doc(*sections: tuple[str, str, str]) -> TbDocument:
    return TbDocument(
        sections=[Section(code=c, title=t, level=2, body=b) for c, t, b in sections]
    )


def test_diff_detects_added_removed_and_modified():
    before = _doc(("CBE", "DWG-backend", "gammal text"), ("CBF", "Asynkron", "oförändrad"))
    after = _doc(
        ("CBE", "DWG-backend", "ny text"),
        ("CBF", "Asynkron", "oförändrad"),
        ("CBG", "Nytt krav", "helt nytt"),
    )

    result = diff_documents(before, after)

    assert [c.code for c in result.by_kind("modified")] == ["CBE"]
    assert [c.code for c in result.by_kind("added")] == ["CBG"]
    assert result.by_kind("removed") == []
    assert result.has_changes


def test_diff_treats_pure_reorder_as_moved_not_changed():
    before = _doc(("CBE", "A", "x"), ("CBF", "B", "y"))
    after = _doc(("CBF", "B", "y"), ("CBE", "A", "x"))

    result = diff_documents(before, after)

    assert {c.code for c in result.by_kind("moved")} == {"CBE", "CBF"}
    assert not result.has_changes


def test_diff_of_identical_documents_is_empty():
    document = _doc(("CBE", "DWG-backend", "text"))
    assert diff_documents(document, document).changes == []


def test_modified_change_exposes_unified_diff():
    before = _doc(("CBE", "DWG", "rad ett\nrad två"))
    after = _doc(("CBE", "DWG", "rad ett\nrad tre"))

    change = diff_documents(before, after).by_kind("modified")[0]
    diff_text = change.unified_diff()

    assert "-rad två" in diff_text
    assert "+rad tre" in diff_text


# --------------------------------------------------------------------------
# 4. Åtgärdsplan
# --------------------------------------------------------------------------


def test_requirement_index_maps_tb_section_to_module(tmp_path):
    kravsparning = tmp_path / "kravsparning.md"
    kravsparning.write_text(
        "| Krav-ID | TB-sektion | Krav | Ursprungsfunktion | Modul | Status |\n"
        "|---|---|---|---|---|---|\n"
        "| C-9 | CBE | DWG via Rust | v5.3 | `dwg/rust_bridge` | Uppfyllt |\n",
        encoding="utf-8",
    )

    index = load_requirement_index(kravsparning)

    assert index["CBE"][0].requirement_id == "C-9"
    assert index["CBE"][0].modules == "`dwg/rust_bridge`"


def test_requirement_index_is_empty_when_file_missing(tmp_path):
    assert load_requirement_index(tmp_path / "saknas.md") == {}


def test_action_plan_lists_modules_for_changed_section(tmp_path):
    kravsparning = tmp_path / "kravsparning.md"
    kravsparning.write_text(
        "| Krav-ID | TB-sektion | Krav | Ursprungsfunktion | Modul | Status |\n"
        "|---|---|---|---|---|---|\n"
        "| C-9 | CBE | DWG via Rust | v5.3 | `dwg/rust_bridge`, `dwg/writers` | Uppfyllt |\n",
        encoding="utf-8",
    )
    diff = diff_documents(
        _doc(("CBE", "DWG-backend", "gammalt")),
        _doc(("CBE", "DWG-backend", "nytt")),
    )

    plan = render_action_plan(diff, kravsparning_path=kravsparning)

    assert "Ändrad sektion: CBE" in plan
    assert "`dwg/rust_bridge`" in plan
    assert "`dwg/writers`" in plan
    assert "-gammalt" in plan and "+nytt" in plan


def test_action_plan_flags_section_without_requirement_row(tmp_path):
    kravsparning = tmp_path / "kravsparning.md"
    kravsparning.write_text("ingen tabell här\n", encoding="utf-8")
    diff = diff_documents(TbDocument(), _doc(("QQQ", "Nytt", "text")))

    plan = render_action_plan(diff, kravsparning_path=kravsparning)

    assert "Ny sektion: QQQ" in plan
    assert "okänt" in plan


def test_action_plan_reports_duplicate_codes():
    document = _doc(("BBB", "Ett", "a"), ("BBB", "Två", "b"))
    diff = diff_documents(TbDocument(), document)

    plan = render_action_plan(diff)

    assert "AFC.27" in plan
    assert "`BBB`" in plan


# --------------------------------------------------------------------------
# 5. Synkroniseringsflödet
# --------------------------------------------------------------------------


@pytest.fixture()
def workspace(tmp_path):
    """En isolerad arbetsyta med ODT, spegling, plan och tillståndsfil."""
    odt = _make_odt(tmp_path / "TB.odt", ["CBE – DWG-backend", "Ursprunglig text."])
    return {
        "odt_path": odt,
        "markdown_path": tmp_path / "TB.md",
        "action_plan_path": tmp_path / "plan.md",
        "kravsparning_path": tmp_path / "kravsparning.md",
        "state_path": tmp_path / "state.json",
    }


def test_first_sync_creates_mirror_and_plan(workspace):
    result = sync(**workspace)

    assert result.changed
    assert result.markdown_was_missing
    assert workspace["markdown_path"].is_file()
    assert workspace["action_plan_path"].is_file()
    assert "CBE" in workspace["markdown_path"].read_text(encoding="utf-8")


def test_second_sync_without_changes_is_silent(workspace):
    sync(**workspace)
    workspace["action_plan_path"].unlink()

    result = sync(**workspace)

    assert not result.changed
    assert not workspace["action_plan_path"].exists(), "åtgärdsplan skrevs trots att inget ändrats"


def test_resaving_odt_without_text_change_produces_no_plan(workspace):
    """Kärnkravet: ODT är primärkälla, men bara *innehåll* räknas.

    Word och LibreOffice ger filen nya bytes vid varje sparning. Speglingen
    ska ändå vara oförändrad så länge texten är det.
    """
    sync(**workspace)
    mirror_before = workspace["markdown_path"].read_text(encoding="utf-8")
    workspace["action_plan_path"].unlink()

    # Samma text, ny fil - alltså nya bytes och ny tidsstämpel.
    _make_odt(workspace["odt_path"], ["CBE – DWG-backend", "Ursprunglig text."])
    result = sync(**workspace)

    assert not result.changed
    assert workspace["markdown_path"].read_text(encoding="utf-8") == mirror_before
    assert not workspace["action_plan_path"].exists()


def test_changed_odt_triggers_plan_with_diff(workspace):
    sync(**workspace)

    _make_odt(workspace["odt_path"], ["CBE – DWG-backend", "Reviderad text."])
    result = sync(**workspace)

    assert result.changed
    assert [c.code for c in result.diff.by_kind("modified")] == ["CBE"]
    plan = workspace["action_plan_path"].read_text(encoding="utf-8")
    assert "-Ursprunglig text." in plan
    assert "+Reviderad text." in plan


def test_new_section_in_odt_is_reported_as_added(workspace):
    sync(**workspace)

    _make_odt(
        workspace["odt_path"],
        ["CBE – DWG-backend", "Ursprunglig text.", "CBG – Nytt krav", "Något nytt."],
    )
    result = sync(**workspace)

    assert [c.code for c in result.diff.by_kind("added")] == ["CBG"]
    assert "Ny sektion: CBG" in workspace["action_plan_path"].read_text(encoding="utf-8")


def test_check_mode_never_writes(workspace):
    result = sync(**workspace, write=False)

    assert result.changed
    assert not workspace["markdown_path"].exists()
    assert not workspace["action_plan_path"].exists()
    assert not workspace["state_path"].exists()


def test_manual_edit_of_mirror_is_detected_and_overwritten(workspace):
    sync(**workspace)
    workspace["markdown_path"].write_text("handredigerat skräp\n", encoding="utf-8")

    result = sync(**workspace)

    assert result.markdown_was_edited
    assert "CBE" in workspace["markdown_path"].read_text(encoding="utf-8")


def test_odt_is_never_written_by_sync(workspace):
    before = workspace["odt_path"].read_bytes()
    sync(**workspace)
    assert workspace["odt_path"].read_bytes() == before, "primärkällan får aldrig ändras"


# --------------------------------------------------------------------------
# Diffrapport och kommandoradsgränssnitt (/TB)
# --------------------------------------------------------------------------


def test_diff_report_is_empty_message_when_unchanged(workspace):
    sync(**workspace)
    result = sync(**workspace)

    assert result.render_diff_report() == "Ingen skillnad mot föregående version."


def test_diff_report_shows_added_and_removed_lines(workspace):
    sync(**workspace)
    _make_odt(workspace["odt_path"], ["CBE – DWG-backend", "Reviderad text."])

    report = sync(**workspace).render_diff_report()

    assert "CBE – DWG-backend [modified]" in report
    assert "-Ursprunglig text." in report
    assert "+Reviderad text." in report


def test_diff_report_renders_new_section_body(workspace):
    sync(**workspace)
    _make_odt(
        workspace["odt_path"],
        ["CBE – DWG-backend", "Ursprunglig text.", "CBF – Parallellitet", "Helt ny text."],
    )

    report = sync(**workspace).render_diff_report()

    assert "CBF – Parallellitet [added]" in report
    assert "Helt ny text." in report


def test_diff_report_states_removal_with_previous_wording(workspace):
    sync(**workspace)
    _make_odt(workspace["odt_path"], ["CBF – Parallellitet", "Annan text."])

    report = sync(**workspace).render_diff_report()

    assert "CBE – DWG-backend [removed]" in report
    assert "Ursprunglig text." in report


def test_cli_diff_flag_prints_report(workspace, capsys, monkeypatch):
    """``--check --diff`` ska visa diffen utan att röra speglingen."""
    sync(**workspace)
    _make_odt(workspace["odt_path"], ["CBE – DWG-backend", "Reviderad text."])
    mirror_before = workspace["markdown_path"].read_text(encoding="utf-8")

    monkeypatch.setattr("tb.sync.MARKDOWN_PATH", workspace["markdown_path"])
    monkeypatch.setattr("tb.sync.ACTION_PLAN_PATH", workspace["action_plan_path"])
    monkeypatch.setattr("tb.sync.KRAVSPARNING_PATH", workspace["kravsparning_path"])
    monkeypatch.setattr("tb.sync.STATE_PATH", workspace["state_path"])

    exit_code = main(["--check", "--diff", "--odt", str(workspace["odt_path"])])

    assert exit_code == 1, "--check ska signalera att speglingen är inaktuell"
    assert "+Reviderad text." in capsys.readouterr().out
    assert workspace["markdown_path"].read_text(encoding="utf-8") == mirror_before


def test_cli_diff_prints_action_plan_in_same_run(workspace, capsys, monkeypatch):
    sync(**workspace)
    _make_odt(workspace["odt_path"], ["CBE – DWG-backend", "Reviderad text."])

    monkeypatch.setattr("tb.sync.MARKDOWN_PATH", workspace["markdown_path"])
    monkeypatch.setattr("tb.sync.ACTION_PLAN_PATH", workspace["action_plan_path"])
    monkeypatch.setattr("tb.sync.KRAVSPARNING_PATH", workspace["kravsparning_path"])
    monkeypatch.setattr("tb.sync.STATE_PATH", workspace["state_path"])

    exit_code = main(["--diff", "--odt", str(workspace["odt_path"])])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "+Reviderad text." in output
    assert "ÅTGÄRDSPLAN" in output
    assert "Granska diffen nedan" in output
    assert workspace["markdown_path"].read_text(encoding="utf-8").find("Reviderad text.") >= 0


def test_cli_rejects_diff_combined_with_watch():
    with pytest.raises(SystemExit):
        main(["--diff", "--watch"])
