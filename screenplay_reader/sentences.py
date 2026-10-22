"""When each sentence of a spoken passage is heard, for sentence-by-sentence highlighting.

A passage (an action paragraph, a speech) is synthesized as one utterance so it
sounds natural, so no engine says when each of its sentences starts. It's
estimated instead: the passage is split into sentences, each gets a share of
the audio in proportion to its length, and every boundary is then moved onto
the nearest pause in the audio — engines leave a clear gap between sentences,
much longer than the gaps between words.
"""

from __future__ import annotations

import re

import numpy as np

# Words whose trailing period doesn't end a sentence ("Mr. Smith", "Det. Sheehan").
_ABBREVIATIONS = {
    "mr", "mrs", "ms", "mx", "dr", "st", "jr", "sr", "vs", "prof", "mt", "ft", "no",
    "sgt", "lt", "capt", "cpt", "col", "gen", "det", "ofc", "rev", "hon", "fr", "sen",
    "rep", "gov", "pres", "int", "ext", "approx", "dept", "etc", "e.g", "i.e", "a.m", "p.m",
}
# Sentence-ending punctuation (plus closing quotes/brackets) followed by a space.
_END_RE = re.compile(r"[.!?…]+[\"'’”)\]]*(?=\s)")
_WORD_BEFORE_RE = re.compile(r"([A-Za-z][A-Za-z.]*)$")

_FRAME_S = 0.01  # energy envelope resolution
_MIN_PAUSE_S = 0.09  # shorter dips are gaps between words
_SILENCE_DB = 32.0  # this far below the passage's speaking level counts as a pause


def split_sentences(text: str, start: int = 0, end: int | None = None) -> list[tuple[int, int]]:
    """Character spans (start, end) of each sentence in text[start:end], whitespace trimmed.

    A sentence ends at . ! ? or an ellipsis followed by a capital, a digit, a quote
    or an opening bracket ("So... I don't know" is two; "Wait... what?" is one), and
    never after an abbreviation or an initial ("Mr. Smith", "J. R. Ewing")."""
    end = len(text) if end is None else end
    spans: list[tuple[int, int]] = []
    cursor = start
    for m in _END_RE.finditer(text, start, end):
        following = text[m.end():end].lstrip()
        if not following or not (following[0].isupper() or following[0].isdigit() or following[0] in "\"'(“‘["):
            continue
        if text[m.start()] == ".":
            word = _WORD_BEFORE_RE.search(text[cursor:m.start()])
            if word and (
                word.group(1).lower() in _ABBREVIATIONS or len(word.group(1)) == 1 or "." in word.group(1)
            ):
                continue  # "Mr. Smith", "J. R.", "U.S. Army"
        spans.append((cursor, m.end()))
        cursor = m.end()
    spans.append((cursor, end))
    trimmed = []
    for a, b in spans:
        while a < b and text[a].isspace():
            a += 1
        while b > a and text[b - 1].isspace():
            b -= 1
        if a < b:
            trimmed.append((a, b))
    return trimmed or [(start, end)]


def sentence_weight(sentence: str) -> float:
    """Roughly how long a sentence takes to say: its letters and digits, plus a
    little for each comma, dash or colon the reader pauses at."""
    return len(re.findall(r"\w", sentence)) + 4 * len(re.findall(r"[,;:—–]|--", sentence)) + 1


def _pauses(samples: np.ndarray, rate: int) -> list[tuple[float, float]]:
    """(start, end) seconds of every pause inside the passage — not the silence
    before the first word or after the last."""
    frame = max(1, int(rate * _FRAME_S))
    n = len(samples) // frame
    if n < 3:
        return []
    frames = samples[: n * frame].astype(np.float64).reshape(n, frame)
    level = 10 * np.log10(np.mean(frames * frames, axis=1) + 1e-12)
    speaking = np.percentile(level, 90)
    quiet = level < speaking - _SILENCE_DB
    voiced = np.flatnonzero(~quiet)
    if voiced.size == 0:
        return []
    first, last = voiced[0], voiced[-1]
    pauses = []
    run_start = None
    for i in range(first, last + 1):
        if quiet[i]:
            if run_start is None:
                run_start = i
        elif run_start is not None:
            if (i - run_start) * _FRAME_S >= _MIN_PAUSE_S:
                pauses.append((run_start * _FRAME_S, i * _FRAME_S))
            run_start = None
    return pauses


def sentence_boundaries(samples: np.ndarray, rate: int, weights: list[float]) -> list[float]:
    """Seconds where each sentence starts, plus the passage's end: n weights give
    n + 1 times, the first 0.0 and the last the duration of the samples."""
    duration = len(samples) / rate
    if len(weights) <= 1 or duration <= 0:
        return [0.0, duration]
    total = sum(weights)
    estimates = []
    acc = 0.0
    for w in weights[:-1]:
        acc += w
        estimates.append(duration * acc / total)
    pauses = _pauses(samples, rate)
    centers = [(a + b) / 2 for a, b in pauses]
    shares = [duration * w / total for w in weights]

    bounds = [0.0]
    used = -1
    for k, estimate in enumerate(estimates):
        # Snap to the nearest unused pause, but never further than about half
        # of either neighbouring sentence (a far-away pause belongs to another one).
        reach = 0.5 * min(shares[k], shares[k + 1]) + 0.25
        best = None
        for j in range(used + 1, len(centers)):
            if centers[j] <= bounds[-1]:
                continue
            distance = abs(centers[j] - estimate)
            if distance <= reach and (best is None or distance < abs(centers[best] - estimate)):
                best = j
        if best is not None:
            used = best
            bounds.append(centers[best])
        else:
            bounds.append(max(estimate, bounds[-1] + 0.05))
    bounds.append(duration)
    # Keep the times strictly increasing and inside the passage.
    for i in range(1, len(bounds) - 1):
        bounds[i] = min(max(bounds[i], bounds[i - 1] + 0.01), duration - 0.01 * (len(bounds) - 1 - i))
    return bounds
