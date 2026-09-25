"""Parse a PDF screenplay into a list of typed elements.

Classification is by horizontal position (x0), which is how screenplay
formatting encodes meaning. Positions are inferred per-document from the
two most common indents (action and dialogue), so scripts with slightly
different margins still parse.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

import pdfplumber

# Parentheticals that mean "pause here" rather than delivery direction.
BEAT_RE = re.compile(r"^\(\s*(a\s+)?(long\s+)?(beat|pause|silence)s?\.?\s*\)$", re.I)
SCENE_RE = re.compile(r"^\d*\s*(INT|EXT|I/E|INT/EXT)[\s./]", re.I)
# Trailing cue annotations: (CONT'D), (V.O.), (O.S.) — possibly stacked.
CUE_PAREN_RE = re.compile(r"(\s*\([^)]*\))+\s*$")
JUNK_RE = re.compile(r"^\(?\s*CONTINUED:?\s*\)?$", re.I)


@dataclass
class Element:
    kind: str  # scene | action | character | dialogue | parenthetical
    text: str
    character: str | None = None  # speaker, for dialogue/parenthetical
    is_beat: bool = False  # parenthetical that triggers a pause
    lines: list[str] = field(default_factory=list)  # original line breaks


@dataclass
class Screenplay:
    elements: list[Element]
    characters: list[str]  # speaking characters, in order of first appearance


def _collapse_doubled(text: str) -> str:
    """Fix bold-overprint extraction artifacts like 'TTOOMM ((CCOONNTT'D))'.

    Collapses per word, so a mix of doubled and normal words survives.
    """

    def collapse_word(word: str) -> str:
        if len(word) >= 4 and len(word) % 2 == 0:
            pairs = [word[i : i + 2] for i in range(0, len(word), 2)]
            if all(p[0] == p[1] for p in pairs):
                return "".join(p[0] for p in pairs)
        return word

    return " ".join(collapse_word(w) for w in text.split())


def _join_wrapped(prev: str, text: str) -> str:
    """Rejoin a line wrapped mid-word: 're-' + 'twisting' -> 're-twisting'.

    A line ending in a hyphen followed by a lowercase continuation is a word
    broken at the margin; a hyphen before a capital ('Bel-' / 'Air') or a
    dash used as punctuation ('hanging on--') keeps its space.
    """
    if prev.endswith("-") and not prev.endswith("--") and text[:1].islower():
        return prev + text
    return prev + " " + text


def _clean_cue(text: str) -> str:
    """'ELIZABETH (CONT'D)' -> 'ELIZABETH'."""
    return CUE_PAREN_RE.sub("", text).strip()


def _looks_like_cue(text: str) -> bool:
    name = _clean_cue(text)
    return bool(name) and name == name.upper() and any(c.isalpha() for c in name) and len(name) < 40


def parse_screenplay(pdf_path: str) -> Screenplay:
    raw_lines: list[tuple[float, str, bool]] = []  # (x0, text, starts_new_paragraph)
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            prev_bottom: float | None = None
            for line in page.extract_text_lines():
                text = _collapse_doubled(line["text"].strip())
                if not text or JUNK_RE.match(text):
                    continue
                # A vertical gap larger than ~1.5 line heights means a blank
                # line separated the paragraphs in the original script.
                new_para = prev_bottom is None or (line["top"] - prev_bottom) > 8
                prev_bottom = line["bottom"]
                raw_lines.append((line["x0"], text, new_para))

    # Character cues are usually the rightmost all-caps lines. Use them to
    # keep a frequent dialogue indent from being mistaken for the action indent
    # when a short scene contains many more dialogue lines than action lines.
    hist = Counter(round(x) for x, _, _ in raw_lines)
    cue_xs = [
        round(x) for x, text, _ in raw_lines
        if not SCENE_RE.match(text) and _looks_like_cue(text)
    ]
    cue_x = max(cue_xs) if cue_xs else None
    if cue_x is not None:
        body_xs = [x for x in hist if x < cue_x]
        dialogue_x = max(body_xs, key=lambda x: hist[x], default=None)
        action_x = min(body_xs, default=None)
    else:
        top_two = sorted(x for x, _ in hist.most_common(2))
        action_x, dialogue_x = (top_two + [None, None])[:2]
    if action_x is None or dialogue_x is None or action_x == dialogue_x:
        return Screenplay([], [])

    elements: list[Element] = []
    characters: list[str] = []
    current_char: str | None = None

    def classify(x: float, text: str) -> str:
        if x > dialogue_x + 200:
            return "junk"  # page numbers, (CONTINUED) footers, transitions
        if SCENE_RE.match(text):
            return "scene"
        if x < (action_x + dialogue_x) / 2:
            return "action"
        if x < dialogue_x + 15:
            return "dialogue"
        if text.startswith("(") or not _looks_like_cue(text):
            return "parenthetical"
        return "character"

    for x, text, new_para in raw_lines:
        kind = classify(x, text)
        if kind == "junk":
            continue
        if kind == "character":
            current_char = _clean_cue(text)
            if current_char not in characters:
                characters.append(current_char)
            elements.append(Element("character", text, character=current_char))
        elif kind == "dialogue":
            prev = elements[-1] if elements else None
            merge = (
                prev
                and prev.kind == "dialogue"
                and prev.character == current_char
                and not new_para
            )
            if merge:
                prev.text = _join_wrapped(prev.text, text)
                prev.lines.append(text)
            else:
                elements.append(Element("dialogue", text, character=current_char, lines=[text]))
        elif kind == "parenthetical":
            elements.append(
                Element("parenthetical", text, character=current_char, is_beat=bool(BEAT_RE.match(text)))
            )
        else:  # scene or action
            current_char = None
            prev = elements[-1] if elements else None
            if kind == "action" and prev and prev.kind == "action" and not new_para:
                prev.text = _join_wrapped(prev.text, text)
                prev.lines.append(text)
            else:
                elements.append(Element(kind, text, lines=[text]))

    # Drop title-page material: everything before the first scene heading.
    for i, e in enumerate(elements):
        if e.kind == "scene":
            elements = elements[i:]
            break

    # Only people who actually speak belong in the casting list.
    speakers = {e.character for e in elements if e.kind == "dialogue" and e.character}
    characters = [c for c in characters if c in speakers]

    return Screenplay(elements, characters)
