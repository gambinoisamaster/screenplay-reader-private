"""Parser test on a PDF laid out like Final Draft output (Courier 12, standard
indents). Guards the two things the audio depends on: wrapped dialogue lines
merge into one element, and blank-line-separated paragraphs do not."""

import pytest

from screenplay_reader.parser import parse_screenplay

reportlab = pytest.importorskip("reportlab")
from reportlab.lib.pagesizes import letter  # noqa: E402
from reportlab.pdfgen import canvas  # noqa: E402

LEFT, DIALOGUE, PAREN, CHARACTER = 108, 180, 216, 260  # x positions in points
LINE = 12  # Courier 12 single spacing

SCRIPT = [
    (LEFT, "INT. HOTEL BEL-AIR - GRACE KELLY SUITE - SITTING ROOM - DAY"),
    None,
    (LEFT, "Kyle inspects the attire in front of a grand standing mirror. He re-"),
    (LEFT, "ties his tie. He looks a bit unsure. Time is running out--"),
    (LEFT, "fast."),
    None,
    (CHARACTER, "PATRICK"),
    (PAREN, "(to stylists)"),
    (DIALOGUE, "Excellent work, guys. This is"),
    (DIALOGUE, "exactly what we're going for. It's"),
    (DIALOGUE, "bold."),
    None,
    (CHARACTER, "KYLE"),
    (DIALOGUE, "For real? Not too out there?"),
    None,
    (CHARACTER, "ETHAN"),
    (DIALOGUE, "Me?"),
    (PAREN, "(off Kyle's look)"),
    (DIALOGUE, "I dunno, man.  Not really the"),
    (DIALOGUE, "fashion type."),
]


@pytest.fixture
def pdf(tmp_path):
    path = tmp_path / "script.pdf"
    c = canvas.Canvas(str(path), pagesize=letter)
    c.setFont("Courier", 12)
    y = 720
    for row in SCRIPT:
        if row is not None:
            x, text = row
            c.drawString(x, y, text)
        y -= LINE
    c.save()
    return str(path)


def test_wrapped_dialogue_merges_into_one_element(pdf):
    sp = parse_screenplay(pdf)
    dialogue = [e for e in sp.elements if e.kind == "dialogue"]
    texts = [e.text for e in dialogue]
    assert "Excellent work, guys. This is exactly what we're going for. It's bold." in texts
    assert "I dunno, man. Not really the fashion type." in texts or "I dunno, man.  Not really the fashion type." in texts
    assert all("\n" not in t for t in texts)


def test_wrapped_action_merges_and_blank_lines_split(pdf):
    sp = parse_screenplay(pdf)
    kinds = [e.kind for e in sp.elements]
    assert kinds[0] == "scene"
    action = [e for e in sp.elements if e.kind == "action"]
    assert len(action) == 1
    # "re-" + "ties" rejoins as one word; "out--" + "fast." keeps the dash as punctuation.
    assert action[0].text == (
        "Kyle inspects the attire in front of a grand standing mirror. He re-ties his tie. "
        "He looks a bit unsure. Time is running out-- fast."
    )


def test_speakers_and_parentheticals(pdf):
    sp = parse_screenplay(pdf)
    assert sp.characters == ["PATRICK", "KYLE", "ETHAN"]
    parens = [e.text for e in sp.elements if e.kind == "parenthetical"]
    assert parens == ["(to stylists)", "(off Kyle's look)"]
    # Ethan's "Me?" and "I dunno..." are separated by a parenthetical -> two elements
    ethan = [e.text for e in sp.elements if e.kind == "dialogue" and e.character == "ETHAN"]
    assert len(ethan) == 2


def test_speakers_survive_when_dialogue_is_more_common_than_action(tmp_path):
    path = tmp_path / "short_scene.pdf"
    c = canvas.Canvas(str(path), pagesize=letter)
    c.setFont("Courier", 12)
    rows = [
        (108, "INT. JAIL - COMMISSARY - LATER"),
        (108, "Kyle sits at a table."),
        (252, "KYLE"),
        (180, "I’m not talking without my lawyer."),
        (180, "You heard me."),
        (252, "ETHAN"),
        (180, "Relax. I’m not a cop."),
        (180, "I just want to talk."),
        (108, "A guard approaches."),
        (252, "CORRECTIONAL OFFICER"),
        (180, "Break it up."),
    ]
    y = 720
    for x, text in rows:
        c.drawString(x, y, text)
        y -= LINE
    c.save()

    sp = parse_screenplay(str(path))

    assert sp.characters == ["KYLE", "ETHAN", "CORRECTIONAL OFFICER"]


def _pdf(tmp_path, rows, name="script.pdf"):
    path = tmp_path / name
    c = canvas.Canvas(str(path), pagesize=letter)
    c.setFont("Courier", 12)
    y = 720
    for row in rows:
        if row is not None:
            x, text = row
            c.drawString(x, y, text)
        y -= LINE
    c.save()
    return str(path)


def test_cue_extensions_and_revision_marks_never_make_a_new_character(tmp_path):
    rows = [
        (LEFT, "1 INT. EASTTOWN POLICE STATION - MORNING 1"),
        None,
        (CHARACTER, "JAN (ON THE PHONE) *"),
        (DIALOGUE, "Hello?"),
        None,
        (CHARACTER, "JAN"),
        (DIALOGUE, "It's me."),
        None,
        (CHARACTER, "JAN (ON THE PHONE) (CONT'D) *"),
        (DIALOGUE, "Still here."),
        None,
        (CHARACTER, "CORPORAL MASTERSON *"),
        (DIALOGUE, "Morning."),
        None,
        (CHARACTER, "CORPORAL MASTERSON (CONT’D) *"),
        (DIALOGUE, "Again."),
        None,
        (CHARACTER, "MAN’S VOICE (O.C.)"),
        (DIALOGUE, "Mare?"),
        None,
        (CHARACTER, "MAN'S VOICE"),
        (DIALOGUE, "Mare!"),
    ]
    sp = parse_screenplay(_pdf(tmp_path, rows))
    assert sp.characters == ["JAN", "CORPORAL MASTERSON", "MAN'S VOICE"]
    speakers = {e.character for e in sp.elements if e.kind == "dialogue"}
    assert speakers == {"JAN", "CORPORAL MASTERSON", "MAN'S VOICE"}
    # The V.O./O.C. mode survives the cleanup; the asterisk doesn't reach the page.
    cues = [e for e in sp.elements if e.kind == "character"]
    assert cues[-2].cue_mode == "O.C."
    assert not any("*" in e.text for e in sp.elements)


def test_page_markers_and_omitted_scenes_are_skipped(tmp_path):
    rows = [
        (LEFT, "2 INT. CARROLL HOME - KITCHEN - MORNING 2"),
        None,
        (LEFT, "Betty pours coffee."),
        None,
        (CHARACTER, "BETTY"),
        (DIALOGUE, "You want some?"),
        (DIALOGUE, "It's fresh."),
        None,
        (LEFT, "2 CONTINUED: (2) 2"),
        None,
        (LEFT, "3 OMITTED 3"),
        None,
        (LEFT, "3A OMITTED 3A"),
        None,
        (LEFT, "4-10 OMITTED 4-10"),
        None,
        (LEFT, "11 EXT. EASTTOWN POLICE STATION - MORNING 11"),
        None,
        (LEFT, "Cars pull in. It's a CONTINUOUS stream."),
    ]
    sp = parse_screenplay(_pdf(tmp_path, rows))
    texts = [e.text for e in sp.elements]
    assert not any("CONTINUED" in t or "OMITTED" in t for t in texts)
    assert "Cars pull in. It's a CONTINUOUS stream." in texts


def test_scene_numbers_with_letters_are_scene_headings(tmp_path):
    rows = [
        (LEFT, "2 INT/EXT. CARROLL HOME - ENTRY/KITCHEN/BATHROOM - MORNING 2"),
        None,
        (LEFT, "Mare knocks."),
        None,
        (CHARACTER, "MARE"),
        (DIALOGUE, "Mrs. Carroll? It's Mare."),
        (DIALOGUE, "I'm outside."),
        None,
        (LEFT, "A3 EXT. CARROLL HOME - BACKYARD - MOMENTS LATER - MORNING A3"),
        None,
        (LEFT, "Snow on the ground."),
        None,
        (LEFT, "B3 INT/EXT. CARROLL HOME - KITCHEN - MORNING B3 *"),
        None,
        (LEFT, "Betty waits."),
    ]
    sp = parse_screenplay(_pdf(tmp_path, rows))
    scenes = [e for e in sp.elements if e.kind == "scene"]
    assert [s.scene_number for s in scenes] == ["2", "A3", "B3"]
    assert not any(s.scene_number_added for s in scenes)


def test_unnumbered_scripts_get_scene_numbers(pdf):
    sp = parse_screenplay(pdf)
    scenes = [e for e in sp.elements if e.kind == "scene"]
    assert [(s.scene_number, s.scene_number_added) for s in scenes] == [("1", True)]


def test_parenthetical_wrapped_over_lines_is_one_direction(tmp_path):
    rows = [
        (LEFT, "INT. KITCHEN - DAY"),
        None,
        (LEFT, "Betty sticks a magnet on the fridge."),
        None,
        (CHARACTER, "BETTY"),
        (DIALOGUE, "There. That's the number for"),
        (DIALOGUE, "the animal people."),
        (PAREN, "(pushes everything else"),
        (PAREN, "aside so the magnet is"),
        (PAREN, "front and center)"),
        (DIALOGUE, "So you call them next time"),
        (DIALOGUE, "instead of wakin' me up."),
    ]
    sp = parse_screenplay(_pdf(tmp_path, rows))
    parens = [e for e in sp.elements if e.kind == "parenthetical"]
    assert [p.text for p in parens] == ["(pushes everything else aside so the magnet is front and center)"]
    assert len(parens[0].lines) == 3


DUAL_LEFT_CUE, DUAL_RIGHT_CUE = 200, 420
DUAL_LEFT, DUAL_RIGHT = 108, 333


def test_dual_dialogue_reads_the_left_speaker_then_the_right(tmp_path):
    path = tmp_path / "dual.pdf"
    c = canvas.Canvas(str(path), pagesize=letter)
    c.setFont("Courier", 12)
    rows = [
        [(LEFT, "INT. KITCHEN - DAY")],
        None,
        [(LEFT, "They argue.")],
        None,
        [(CHARACTER, "MARE")],
        [(DIALOGUE, "You never listen.")],
        [(DIALOGUE, "Not once.")],
        None,
        [(DUAL_LEFT_CUE, "MARE"), (DUAL_RIGHT_CUE, "BETTY CARROLL")],
        [(DUAL_LEFT, "Mrs. Carroll filled me in."), (DUAL_RIGHT, "Of course I told her what")],
        [(DUAL_RIGHT, "happened.")],
        None,
        [(CHARACTER, "BETTY CARROLL")],
        [(DIALOGUE, "And guess what?")],
        [(DIALOGUE, "It was solved.")],
    ]
    y = 720
    for row in rows:
        for x, text in row or []:
            c.drawString(x, y, text)
        y -= LINE
    c.save()

    sp = parse_screenplay(str(path))
    spoken = [(e.kind, e.character, e.text) for e in sp.elements if e.kind in ("character", "dialogue")]
    i = spoken.index(("dialogue", "MARE", "Mrs. Carroll filled me in."))
    assert spoken[i - 1] == ("character", "MARE", "MARE")
    assert spoken[i + 1] == ("character", "BETTY CARROLL", "BETTY CARROLL")
    assert spoken[i + 2] == ("dialogue", "BETTY CARROLL", "Of course I told her what happened.")
    assert not any("MARE BETTY" in text for _, _, text in spoken)


def test_two_columns_of_capitals_are_not_dual_dialogue(tmp_path):
    path = tmp_path / "locations.pdf"
    c = canvas.Canvas(str(path), pagesize=letter)
    c.setFont("Courier", 12)
    rows = [
        [(DUAL_LEFT_CUE, "INTERIORS"), (DUAL_RIGHT_CUE, "EXTERIORS")],
        [(DUAL_LEFT_CUE, "KITCHEN"), (DUAL_RIGHT_CUE, "BACKYARD")],
        None,
        [(LEFT, "INT. KITCHEN - DAY")],
        None,
        [(LEFT, "Betty pours coffee.")],
        None,
        [(CHARACTER, "BETTY")],
        [(DIALOGUE, "You want some?")],
        [(DIALOGUE, "It's fresh.")],
    ]
    y = 720
    for row in rows:
        for x, text in row or []:
            c.drawString(x, y, text)
        y -= LINE
    c.save()
    sp = parse_screenplay(str(path))
    assert sp.characters == ["BETTY"]


def test_title_comes_from_the_title_page_else_the_file_name(tmp_path, pdf):
    path = tmp_path / "the_last_ride-final.pdf"
    c = canvas.Canvas(str(path), pagesize=letter)
    c.setFont("Courier", 12)
    c.drawString(250, 500, "THE LAST RIDE")
    c.drawString(270, 470, "Written by")
    c.drawString(260, 455, "Sam Jones")
    c.showPage()
    c.setFont("Courier", 12)
    y = 720
    for row in SCRIPT:
        if row is not None:
            c.drawString(row[0], y, row[1])
        y -= LINE
    c.save()
    assert parse_screenplay(str(path)).title == "THE LAST RIDE"
    # No title page: the file's name stands in.
    assert parse_screenplay(pdf).title == "script"
