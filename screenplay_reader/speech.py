"""What the narrator actually says for each screenplay element.

The screen shows the script as written; the audio should sound like someone
reading it aloud. This module owns that translation:

- Scene headings: "INT. HOTEL BEL-AIR - SITTING ROOM - DAY" is spoken as
  "Interior. Hotel Bel-Air, Sitting Room. Day." — INT./EXT. are always
  expanded, never spelled out.
- Character cues: "ETHAN (CONT'D)" is spoken as "Ethan." (the narrator says
  it in a normal voice; the cue is not shouted or spelled).
- Action lines: character names introduced in caps ("KYLE, 30s, ...") are
  spoken as names. Short all-caps words that are probably acronyms ("FBI",
  "TV") are left alone.
- Dialogue: spoken verbatim. Line breaks from the PDF layout are collapsed
  to single spaces so a TTS engine can never pause at a wrapped line.
- Typographic punctuation is mapped to ASCII. Final Draft exports curly
  quotes (U+2018/U+2019, U+201C/U+201D); Pocket-TTS's tokenizer has no
  token for them and emits raw bytes instead, which is why "Can\u2019t" came out
  mangled while "Can't" is fine. Every engine sees ASCII quotes only.
"""

from __future__ import annotations

import re

from .parser import Element

_WS_RE = re.compile(r"\s+")
# Characters a screenwriting app emits that TTS tokenizers commonly lack.
_ASCII_PUNCT = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",  # single quotes / apostrophes
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"',  # double quotes
    "\u2026": "...",                                              # ellipsis
    "\u00a0": " ",                                                # non-breaking space
})
_SCENE_NUM_RE = re.compile(r"^\s*\d+[A-Z]?\s+|\s+\d+[A-Z]?\s*$")
_PREFIX_RE = re.compile(
    r"^(?P<prefix>INT\s*\.?\s*/\s*EXT|EXT\s*\.?\s*/\s*INT|I\s*/\s*E|INT|EXT)\s*[.\s:-]*\s*",
    re.I,
)
_SEP_RE = re.compile(r"\s+[-–—]+\s+|\s*--+\s*")
_HASH_RE = re.compile(r"#\s*(\d+)")

_PREFIX_WORDS = {
    "INT": "Interior",
    "EXT": "Exterior",
    "INT/EXT": "Interior, Exterior",
    "EXT/INT": "Exterior, Interior",
    "I/E": "Interior, Exterior",
}


def collapse_whitespace(text: str) -> str:
    """The guarantee every engine relies on: ASCII punctuation, no newlines,
    no runs of spaces."""
    return _WS_RE.sub(" ", text.translate(_ASCII_PUNCT)).strip()


def _title_word(word: str) -> str:
    """'BEL-AIR' -> 'Bel-Air', "O'BRIEN'S" -> "O'Brien's", "DON'T" -> "Don't"."""
    word = re.sub(r"[A-Za-z]+", lambda m: m.group(0).capitalize(), word)
    # Contraction/possessive tails after an apostrophe stay lowercase.
    return re.sub(r"(['\u2019])([A-Za-z]{1,2})\b", lambda m: m.group(1) + m.group(2).lower(), word)


def _speak_caps(text: str, min_len: int, names: set[str] = frozenset()) -> str:
    """Turn all-caps words into readable case.

    A word is converted when it is a known character name, or when it is at
    least `min_len` letters long (short caps like 'TV' are likely acronyms).
    """
    out = []
    for word in text.split(" "):
        letters = re.sub(r"[^A-Za-z]", "", word)
        if letters and letters.isupper() and (letters in names or len(letters) >= min_len):
            out.append(_title_word(word))
        else:
            out.append(word)
    return " ".join(out)


def scene_heading(text: str) -> str:
    """'INT. HOTEL BEL-AIR - GRACE KELLY SUITE - DAY' -> 'Interior. Hotel Bel-Air, Grace Kelly Suite. Day.'"""
    text = collapse_whitespace(_SCENE_NUM_RE.sub("", text))
    prefix_spoken = ""
    m = _PREFIX_RE.match(text)
    if m:
        key = re.sub(r"[\s.]", "", m.group("prefix")).upper()
        prefix_spoken = _PREFIX_WORDS.get(key, "")
        text = text[m.end():]
    parts = [p.strip(" .") for p in _SEP_RE.split(text) if p.strip(" .")]
    parts = [_speak_caps(p, min_len=2) for p in parts]
    if len(parts) >= 2:
        # Everything but the last part is the place; the last is the time of day.
        body = ", ".join(parts[:-1]) + ". " + parts[-1]
    else:
        body = ", ".join(parts)
    spoken = f"{prefix_spoken}. {body}" if prefix_spoken and body else prefix_spoken or body
    spoken = spoken.strip()
    return spoken + "." if spoken and not spoken.endswith((".", "!", "?")) else spoken


def character_cue(name: str) -> str:
    """'ETHAN' -> 'Ethan.'  'COP #2' -> 'Cop number 2.'  Already cleaned of (CONT'D)."""
    name = _HASH_RE.sub(r"number \1", collapse_whitespace(name))
    spoken = _speak_caps(name, min_len=2)
    return spoken + "." if spoken and not spoken.endswith((".", "!", "?")) else spoken


def action_line(text: str, names: set[str]) -> str:
    return _speak_caps(collapse_whitespace(text), min_len=4, names=names)


def dialogue(text: str) -> str:
    return collapse_whitespace(text)


def for_element(el: Element, names: set[str]) -> str:
    """The text handed to the TTS engine for one element."""
    if el.kind == "scene":
        return scene_heading(el.text)
    if el.kind == "character":
        return character_cue(el.character or el.text)
    if el.kind == "action":
        return action_line(el.text, names)
    return dialogue(el.text)
