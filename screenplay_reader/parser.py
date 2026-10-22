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
from pathlib import Path

import pdfplumber

# Parentheticals that mean "pause here" rather than delivery direction.
BEAT_RE = re.compile(r"^\(\s*(a\s+)?(long\s+)?(beat|pause|silence)s?\.?\s*\)$", re.I)
# A scene number: "12", "12A", "A12" (scenes added after numbering is locked).
SCENE_NO = r"[A-Z]{0,2}\d+[A-Z]{0,2}"
_SCENE_RANGE = rf"{SCENE_NO}(?:\s*[-–—]\s*{SCENE_NO})?"
SCENE_RE = re.compile(rf"^(?:{SCENE_NO}\.?\s+)?(INT|EXT|I/E)[\s./]", re.I)
_SCENE_NUMBER_RE = re.compile(rf"^\s*({SCENE_NO})\.?\s+", re.I)
# Trailing cue annotations: (CONT'D), (V.O.), (O.S.) — possibly stacked.
CUE_PAREN_RE = re.compile(r"(\s*\([^)]*\))+\s*$")
# "CONTINUED", "(CONTINUED)", "CONTINUED:", and scene-numbered forms like
# "10  CONTINUED:  10" or "2 CONTINUED: (2) 2" — but never "CONTINUOUS" (a
# real scene-heading word).
JUNK_RE = re.compile(
    rf"^\s*\(?\s*(?:{SCENE_NO}\s+)?CONTINUED\s*:?\s*(?:\(\s*\d+\s*\))?\s*(?:{SCENE_NO})?\s*\)?\s*$", re.I
)
# Placeholders for cut scenes: "3 OMITTED 3", "3A OMITTED 3A", "4-10 OMITTED 4-10".
OMITTED_RE = re.compile(rf"^\s*(?:scenes?\s+)?(?:{_SCENE_RANGE}\s+)?OMIT(?:TED)?\s*(?:{_SCENE_RANGE})?\s*$", re.I)
# Revision marks: the asterisks a production draft puts beside changed lines.
_REVISION_MARK_RE = re.compile(r"^\*+\s+|\s+\*+$")
_APOSTROPHES = str.maketrans({"’": "'", "‘": "'"})

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
    scene_number: str | None = None  # "12", "A3"... on a scene heading
    scene_number_added: bool = False  # the PDF had no scene numbers; the app numbered it


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


def _cover_intro(cover: list["Element"]) -> tuple[list["Element"], str | None]:
    """From title-page lines keep only the title and author (read aloud); drop
    drafts, dates, disclaimers, etc. Returns ([], None) unless an author credit
    is present, which is a strong signal that this really was a title page;
    otherwise (intro elements, the title as written)."""
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
        return [], None
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
        spoken = title if title.endswith((".", "!", "?")) else title + "."
        intro.append(Element("action", spoken, lines=[spoken]))
    credit = f"Written by {author}."
    intro.append(Element("action", credit, lines=[credit]))
    return intro, title


@dataclass
class Screenplay:
    elements: list[Element]
    characters: list[str]  # speaking characters, in order of first appearance
    title: str = ""  # from the title page, else the PDF's metadata, else its file name


_JUNK_TITLE_RE = re.compile(r"untitled|microsoft|\.(?:docx?|fdx|pdf|rtf)\b|final draft|^document\d*$", re.I)


def _fallback_title(metadata_title: str | None, pdf_path: str) -> str:
    """A script without a title page is named by its PDF's metadata title when that's
    a real title, else by its file name ("the_holdovers-2023" -> "the holdovers 2023")."""
    meta = (metadata_title or "").strip()
    if meta and len(meta) <= 80 and any(c.isalpha() for c in meta) and not _JUNK_TITLE_RE.search(meta):
        return meta
    return re.sub(r"[_\-]+", " ", Path(pdf_path).stem).strip()


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


def character_name(text: str) -> str:
    """The character's name alone: 'JAN (ON THE PHONE) (CONT'D) *' -> 'JAN'.

    Extensions like (CONT'D), (V.O.) or (ON THE PHONE) and revision asterisks
    never make a new character, so they're all dropped. Curly apostrophes are
    straightened so "MAN’S VOICE" and "MAN'S VOICE" are one person."""
    text = text.replace("*", " ").translate(_APOSTROPHES)
    text = re.sub(r"[\s'\"“”:]+$", "", text)  # a stray quote or colon after the cue
    text = CUE_PAREN_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def _scene_number(text: str) -> str | None:
    """'A3 EXT. CARROLL HOME - MORNING A3' -> 'A3'; None for an unnumbered heading."""
    m = _SCENE_NUMBER_RE.match(text)
    return m.group(1).upper() if m else None


def _is_open_paren(text: str) -> bool:
    """A parenthetical still waiting for its ')' continues on the next line."""
    return text.count("(") > text.count(")")


def _looks_like_cue(text: str) -> bool:
    name = character_name(text)
    return bool(name) and name == name.upper() and any(c.isalpha() for c in name) and len(name) < 40


def _char_width(chars: list[dict]) -> float:
    widths = sorted(c["x1"] - c["x0"] for c in chars)
    return widths[len(widths) // 2] if widths else 6.0


def _text_of(chars: list[dict], width: float) -> str:
    """Rebuild text from positioned characters: a space wherever there's a gap."""
    if not chars:
        return ""
    text = chars[0]["text"]
    for a, b in zip(chars, chars[1:]):
        text += (" " if b["x0"] - a["x1"] > 0.3 * width else "") + b["text"]
    return text


def _segments(chars: list[dict]) -> list[list[dict]]:
    """Runs of characters separated by a wide gap (3+ character widths) — the two
    columns of dual dialogue sit on one line with such a gap between them."""
    if not chars:
        return []
    width = _char_width(chars)
    runs, run = [], [chars[0]]
    for a, b in zip(chars, chars[1:]):
        if b["x0"] - a["x1"] > 3 * width:
            runs.append(run)
            run = []
        run.append(b)
    runs.append(run)
    return runs


def _clean_line(text: str) -> str:
    return _REVISION_MARK_RE.sub("", _collapse_doubled(text.strip()))


class _DualDialogue:
    """Two characters speaking at once, printed side by side. Only one voice can
    speak at a time, so it's read as an exchange: the left column's speaker,
    then the right's."""

    def __init__(self, left_cue: str, right_cue: str, right_cue_x: float, first: tuple):
        self.cues = (left_cue, right_cue)
        self.right_cue_x = right_cue_x
        self.first = first  # the cue line as an ordinary (x0, text, new_para) line
        self.lines: list[list[dict]] = []  # the characters of each line under the cues
        self.plain: list[tuple[float, str, bool]] = []  # the same lines, as ordinary lines

    @staticmethod
    def starts(chars: list[dict], first: tuple) -> "_DualDialogue | None":
        """A line holding two character cues far apart starts dual dialogue."""
        runs = _segments(chars)
        if len(runs) != 2 or runs[1][0]["x0"] - runs[0][0]["x0"] < 120:
            return None
        width = _char_width(chars)
        left, right = (_clean_line(_text_of(run, width)) for run in runs)
        if not (_looks_like_cue(left) and _looks_like_cue(right)):
            return None
        return _DualDialogue(left, right, runs[1][0]["x0"], first)

    def add(self, chars: list[dict], plain: tuple[float, str, bool]) -> None:
        self.lines.append(chars)
        self.plain.append(plain)

    def raw_lines(self) -> list[tuple]:
        """Each speaker's cue and lines, left speaker first, as pre-classified lines.
        Two columns of capitals with no speech under them (a location list, say)
        weren't dialogue at all: those go back as ordinary lines."""
        if not any(c["text"].islower() for chars in self.lines for c in chars):
            return [(x, text, new_para, None) for x, text, new_para in (self.first, *self.plain)]
        # The right column's speech starts where the second run of a two-run line
        # starts; with no such line, about an inch left of its cue.
        starts = [runs[1][0]["x0"] for runs in map(_segments, self.lines) if len(runs) >= 2]
        split = min(starts) - 2 if starts else self.right_cue_x - 72
        columns: tuple[list[str], list[str]] = ([], [])
        for chars in self.lines:
            width = _char_width(chars)
            for side, part in enumerate(([c for c in chars if c["x0"] < split], [c for c in chars if c["x0"] >= split])):
                text = _clean_line(_text_of(part, width))
                if text:
                    columns[side].append(text)
        out = []
        for cue, lines in zip(self.cues, columns):
            out.append((None, cue, True, "character"))
            depth = 0
            for text in lines:
                role = "parenthetical" if depth > 0 or text.startswith("(") else "dialogue"
                if role == "parenthetical":
                    depth = max(0, depth + text.count("(") - text.count(")"))
                out.append((None, text, False, role))
        return out


def _indents(raw_lines: list[tuple[float, str, bool]]) -> tuple[int | None, int | None]:
    """(action x, dialogue x) of this document, learned from where its lines start.

    - Character cues: the most common indent of all-caps lines that have their
      speech right underneath, further left. Strays like "CUT TO:", an "FYC 2."
      footer or OCR noise aren't followed by speech, so they don't count.
    - Action: the leftmost indent that's really used by lines other than scene
      headings (their scene numbers can hang into the left margin); a few strays
      from a title page or an epigraph further left don't count.
    - Dialogue: the most common indent between action and the cues — not the most
      common overall, which in an action-heavy script is the action margin.
    Documents laid out some other way fall back to the original rule.
    """
    body = [(round(x), text, new_para) for x, text, new_para in raw_lines if not SCENE_RE.match(text)]
    hist = Counter(x for x, _, _ in body)
    cues = Counter(
        x for (x, text, _), (next_x, next_text, next_para) in zip(body, body[1:])
        if _looks_like_cue(text) and not next_para and next_x < x - 20 and not _looks_like_cue(next_text)
    )
    if cues:
        cue_x = cues.most_common(1)[0][0]
        floor = max(1, len(raw_lines) // 200)  # strays only matter in a full-length script
        left_of_cues = [x for x in hist if x < cue_x - 8]
        margins = sorted(x for x in left_of_cues if hist[x] >= floor) or sorted(left_of_cues)
        if margins:
            action_x = margins[0]
            dialogue_x = max((x for x in margins if x > action_x + 10), key=lambda x: hist[x], default=None)
            if dialogue_x is not None:
                return action_x, dialogue_x
    # The original rule: cues are the rightmost all-caps lines, dialogue the most
    # common indent left of them, action the leftmost.
    all_hist = Counter(round(x) for x, _, _ in raw_lines)
    cue_xs = [x for x, text, _ in body if _looks_like_cue(text)]
    if cue_xs:
        left = [x for x in all_hist if x < max(cue_xs)]
        return min(left, default=None), max(left, key=lambda x: all_hist[x], default=None)
    top_two = sorted(x for x, _ in all_hist.most_common(2))
    return tuple((top_two + [None, None])[:2])

def parse_screenplay(pdf_path: str) -> Screenplay:
    # (x0, text, starts_new_paragraph, role): role is None for a line classified by its
    # indent, or the kind already known (dual dialogue is classified as it's read).
    raw_lines: list[tuple[float | None, str, bool, str | None]] = []
    dual: _DualDialogue | None = None
    with pdfplumber.open(pdf_path) as pdf:
        metadata_title = (pdf.metadata or {}).get("Title")
        for page in pdf.pages:
            page_lines = page.extract_text_lines()
            page_texts = [t for t in (_collapse_doubled(l["text"].strip()) for l in page_lines) if t]
            if _is_production_page(page_texts):
                continue  # skip whole CAST LIST / LOCATION LIST pages
            page_height = page.height
            prev_bottom: float | None = None
            for line in page_lines:
                text = _REVISION_MARK_RE.sub("", _collapse_doubled(line["text"].strip()))
                if not text or text.strip("*") == "" or JUNK_RE.match(text) or OMITTED_RE.match(text):
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
                chars = [c for c in line.get("chars", []) if c["text"].strip()]
                if dual is not None:
                    if not new_para:
                        dual.add(chars, (line["x0"], text, new_para))  # still under the two cues
                        continue
                    raw_lines.extend(dual.raw_lines())
                    dual = None
                dual = _DualDialogue.starts(chars, (line["x0"], text, new_para))
                if dual is None:
                    raw_lines.append((line["x0"], text, new_para, None))
            if dual is not None:  # the page ended under the two cues
                raw_lines.extend(dual.raw_lines())
                dual = None

    action_x, dialogue_x = _indents([(x, text, new_para) for x, text, new_para, role in raw_lines if role is None])
    if action_x is None or dialogue_x is None or action_x == dialogue_x:
        return Screenplay([], [])

    elements: list[Element] = []
    characters: list[str] = []
    current_char: str | None = None

    def classify(x: float | None, text: str, role: str | None) -> str:
        if role is not None:
            return role
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

    for x, text, new_para, role in raw_lines:
        kind = classify(x, text, role)
        if kind == "junk":
            continue
        prev = elements[-1] if elements else None
        open_paren = prev is not None and prev.kind == "parenthetical" and _is_open_paren(prev.text)
        if open_paren and kind in ("parenthetical", "character") and not new_para:
            # "(pushes everything else / aside so the magnet is / front and center)"
            # is one direction wrapped over three lines, not three.
            prev.text = _join_wrapped(prev.text, text)
            prev.lines.append(text)
            prev.is_beat = bool(BEAT_RE.match(prev.text))
            continue
        if kind == "character":
            current_char = character_name(text)
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
                Element(
                    "parenthetical", text, character=current_char,
                    is_beat=bool(BEAT_RE.match(text)), lines=[text],
                )
            )
        else:  # scene or action
            current_char = None
            if kind == "action" and prev and prev.kind == "action" and not new_para:
                prev.text = _join_wrapped(prev.text, text)
                prev.lines.append(text)
            else:
                number = _scene_number(text) if kind == "scene" else None
                elements.append(Element(kind, text, lines=[text], scene_number=number))

    # Title page: keep only the title and author (read aloud), drop the rest.
    title = None
    first_scene = next((i for i, e in enumerate(elements) if e.kind == "scene"), None)
    if first_scene is not None:
        intro, title = _cover_intro(elements[:first_scene])
        elements = intro + elements[first_scene:]

    # Scenes are always numbered so the user can jump between them; a script
    # that came without numbers gets 1, 2, 3... (shown in the app, not spoken).
    scenes = [e for e in elements if e.kind == "scene"]
    if scenes and not any(e.scene_number for e in scenes):
        for n, scene in enumerate(scenes, 1):
            scene.scene_number = str(n)
            scene.scene_number_added = True

    # Only people who actually speak belong in the casting list.
    speakers = {e.character for e in elements if e.kind == "dialogue" and e.character}
    characters = [c for c in characters if c in speakers]

    return Screenplay(elements, characters, title or _fallback_title(metadata_title, pdf_path))
