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
# "CONTINUED", "(CONTINUED)", "CONTINUED:", and scene-numbered forms like
# "10  CONTINUED:  10" — but never "CONTINUOUS" (a real scene-heading word).
JUNK_RE = re.compile(
    r"^\s*\(?\s*(?:\d+[A-Za-z]?\s+)?CONTINUED\s*:?\s*(?:\d+[A-Za-z]?)?\s*\)?\s*$", re.I
)

# Character-cue delivery modes: ETHAN (V.O.) / (O.S.) / (O.C.) / (PRE-LAP).
CUE_MODE_RE = re.compile(r"\(\s*(V\s*\.?\s*O\s*\.?|O\s*\.?\s*S\s*\.?|O\s*\.?\s*C\s*\.?|PRE\s*-?\s*LAP)\s*\)", re.I)
CUE_MODE_WORDS = {"VO": "V.O.", "OS": "O.S.", "OC": "O.C.", "PRELAP": "PRE-LAP"}

# Running headers / footers to drop (only when they sit in the page margins).
_DATE_RE = re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b")
_REVISION_RE = re.compile(
    r"\b(blue|pink|yellow|green|goldenrod|salmon|buff|cherry|tan|white|gold|gray|grey|ivory)\s+"
    r"(revision|revisions|draft|pages|rev)\b",
    re.I,
)
_EPISODE_HDR_RE = re.compile(
    r"as broadcast|as aired|\bep\s*#|\bepisode\s*#|shooting draft|production draft|network draft|writer'?s draft",
    re.I,
)
_PAGE_NUM_RE = re.compile(r"^\s*\d+[A-Za-z]?\.?\s*$")

# Whole production pages to ignore.
PRODUCTION_PAGE_RE = re.compile(
    r"^\s*(cast list|location list|set list|character list|"
    r"list of (?:locations|characters|sets)|sets? and locations)\s*[:.]?\s*$",
    re.I,
)

# Title-page detection: author credit lines, and junk to skip on the cover.
_BY_INLINE_RE = re.compile(r"^\s*(?:written|screenplay|teleplay|story|created|adapted)\s+by\b[:.\s]*(.*)$", re.I)
_BY_ALONE_RE = re.compile(
    r"^\s*(?:written\s+by|screenplay\s+by|teleplay\s+by|story\s+by|by)\s*[:.]?\s*$", re.I
)
_COVER_JUNK_RE = re.compile(
    r"confidential|do not|property of|all rights|copyright|©|\bdrafts?\b|revision|\brev\.|"
    r"registered|\bwga\b|for your consideration|\bfyc\b|educational purposes|\bfade in\b|"
    r"\bcontact\b|represented by|\binc\.|\bllc\b|productions?\b|\d{1,2}/\d{1,2}/\d{2,4}",
    re.I,
)


@dataclass
class Element:
    kind: str  # scene | action | character | dialogue | parenthetical
    text: str
    character: str | None = None  # speaker, for dialogue/parenthetical
    is_beat: bool = False  # parenthetical that triggers a pause
    cue_mode: str | None = None  # V.O. / O.S. / O.C. / PRE-LAP on a character cue
    lines: list[str] = field(default_factory=list)  # original line breaks


def _cue_mode(text: str) -> str | None:
    m = CUE_MODE_RE.search(text)
    if not m:
        return None
    return CUE_MODE_WORDS.get(re.sub(r"[^A-Za-z]", "", m.group(1)).upper())


def _is_header_footer(text: str) -> bool:
    """A running header/footer line (episode slug, revision, date, page no.).
    Callers gate this to the page margins so body text is never affected."""
    if _PAGE_NUM_RE.match(text):
        return True
    if _EPISODE_HDR_RE.search(text) or _REVISION_RE.search(text) or _DATE_RE.search(text):
        return True
    # e.g. "EP#101 ... "Smoke Gets in Your Eyes"  5/16/07  2."
    return bool(re.search(r"\b\d+[A-Za-z]?\.\s*$", text)) and len(text) > 8


def _is_production_page(page_texts: list[str]) -> bool:
    return any(PRODUCTION_PAGE_RE.match(t) for t in page_texts[:4])


def _cover_intro(cover: list["Element"]) -> list["Element"]:
    """From title-page lines keep only the title and author (read aloud); drop
    drafts, dates, disclaimers, etc. Returns [] unless an author credit is
    present, which is a strong signal that this really was a title page."""
    lines = [e.text.strip() for e in cover if e.text.strip()]
    author = None
    author_idx = None
    for i, ln in enumerate(lines):
        inline = _BY_INLINE_RE.match(ln)
        if inline and inline.group(1).strip(" .,-"):
            author, author_idx = inline.group(1).strip(" .,-"), i
            break
        if _BY_ALONE_RE.match(ln) and i + 1 < len(lines):
            author, author_idx = lines[i + 1].strip(" .,-"), i + 1
            break
    if not author:
        return []
    title = None
    for i, ln in enumerate(lines):
        if i == author_idx or _BY_INLINE_RE.match(ln) or _BY_ALONE_RE.match(ln):
            continue
        if _COVER_JUNK_RE.search(ln):
            continue
        title = ln.strip('"“” ')
        break
    intro: list[Element] = []
    if title:
        title = title if title.endswith((".", "!", "?")) else title + "."
        intro.append(Element("action", title, lines=[title]))
    credit = f"Written by {author}."
    intro.append(Element("action", credit, lines=[credit]))
    return intro


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
            page_lines = page.extract_text_lines()
            page_texts = [t for t in (_collapse_doubled(l["text"].strip()) for l in page_lines) if t]
            if _is_production_page(page_texts):
                continue  # skip whole CAST LIST / LOCATION LIST pages
            page_height = page.height
            prev_bottom: float | None = None
            for line in page_lines:
                text = _collapse_doubled(line["text"].strip())
                if not text or JUNK_RE.match(text):
                    continue
                # Drop running headers/footers, but only in the page margins so
                # body text that happens to contain a date is never removed.
                in_margin = line["top"] < 62 or line["bottom"] > page_height - 52
                if in_margin and _is_header_footer(text):
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
            elements.append(
                Element("character", text, character=current_char, cue_mode=_cue_mode(text))
            )
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

    # Title page: keep only the title and author (read aloud), drop the rest.
    first_scene = next((i for i, e in enumerate(elements) if e.kind == "scene"), None)
    if first_scene is not None:
        elements = _cover_intro(elements[:first_scene]) + elements[first_scene:]

    # Only people who actually speak belong in the casting list.
    speakers = {e.character for e in elements if e.kind == "dialogue" and e.character}
    characters = [c for c in characters if c in speakers]

    return Screenplay(elements, characters)
