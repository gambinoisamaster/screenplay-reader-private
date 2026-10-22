"""Sentence splitting and timing for the sentence-by-sentence highlight."""

import numpy as np

from screenplay_reader.sentences import sentence_boundaries, sentence_weight, split_sentences


def spans_text(text):
    return [text[a:b] for a, b in split_sentences(text)]


def test_splits_on_sentence_ends():
    assert spans_text("Mrs. Carroll? Mrs. Carroll, it's Mare Sheehan. I'm standin' outside. In the cold.") == [
        "Mrs. Carroll?", "Mrs. Carroll, it's Mare Sheehan.", "I'm standin' outside.", "In the cold.",
    ]


def test_abbreviations_initials_and_trailing_ellipses_dont_split():
    assert spans_text("Det. Sheehan meets Dr. Ruiz and J. R. Ewing at the U.S. Mint.") == [
        "Det. Sheehan meets Dr. Ruiz and J. R. Ewing at the U.S. Mint."
    ]
    assert spans_text("Wait... what?") == ["Wait... what?"]
    assert spans_text("So... I don't know.") == ["So...", "I don't know."]


def test_spans_are_offsets_into_the_given_range():
    text = "For real? (beat) Not too out there? No."
    spans = split_sentences(text, 17, len(text))
    assert [text[a:b] for a, b in spans] == ["Not too out there?", "No."]


def test_quotes_close_the_sentence():
    assert spans_text('She reads the sign: "No entry." Mare goes in.') == [
        'She reads the sign: "No entry."', "Mare goes in.",
    ]


def test_weight_grows_with_length_and_pauses():
    assert sentence_weight("He waits.") < sentence_weight("He waits, and waits, and waits.")


def _speech(*sections, rate=16000):
    """Tone bursts ("words") with gaps; ('pause', s) inserts silence."""
    parts = []
    t = np.arange(int(0.25 * rate)) / rate
    word = 0.5 * np.sin(2 * np.pi * 220 * t)
    gap = np.zeros(int(0.04 * rate))  # between words: too short to be a pause
    for kind, value in sections:
        if kind == "words":
            for _ in range(value):
                parts += [word, gap]
        else:
            parts.append(np.zeros(int(value * rate)))
    return np.concatenate(parts), rate


def test_boundaries_snap_to_the_pauses_between_sentences():
    # Three sentences of 6, 4 and 8 "words", split by 0.4 s pauses.
    samples, rate = _speech(("words", 6), ("pause", 0.4), ("words", 4), ("pause", 0.4), ("words", 8))
    bounds = sentence_boundaries(samples, rate, [6, 4, 8])
    first_pause = 6 * 0.29 + 0.2  # middle of the first pause
    second_pause = 6 * 0.29 + 0.4 + 4 * 0.29 + 0.2
    assert bounds[0] == 0.0 and abs(bounds[-1] - len(samples) / rate) < 1e-9
    assert abs(bounds[1] - first_pause) < 0.06
    assert abs(bounds[2] - second_pause) < 0.06


def test_boundaries_fall_back_to_proportions_without_pauses():
    samples, rate = _speech(("words", 10))
    bounds = sentence_boundaries(samples, rate, [1, 1])
    assert abs(bounds[1] - bounds[2] / 2) < 0.05
    assert bounds == sorted(bounds)


def test_one_sentence_spans_the_whole_passage():
    samples, rate = _speech(("words", 3))
    assert sentence_boundaries(samples, rate, [5]) == [0.0, len(samples) / rate]
