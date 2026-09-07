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
