"""Pipeline tests with a fake engine that records every synth call."""

from pydub.generators import Sine

from screenplay_reader.audio_builder import build_audio
from screenplay_reader.parser import Element


class RecordingEngine:
    name = "Rec"

    def __init__(self):
        self.calls: list[tuple[str, str]] = []  # (voice, text)

    def synthesize(self, voice, text):
        self.calls.append((voice, text))
        return Sine(300).to_audio_segment(duration=200)


def script():
    return [
        Element("scene", "INT. HOTEL BEL-AIR - SITTING ROOM - DAY", lines=["INT. HOTEL BEL-AIR - SITTING ROOM - DAY"]),
        Element("action", "KYLE inspects the attire. He looks unsure.",
                lines=["KYLE inspects the attire.", "He looks unsure."]),
        Element("character", "PATRICK", character="PATRICK"),
        Element("parenthetical", "(to stylists)", character="PATRICK"),
        Element(
            "dialogue",
            "Excellent work, guys. This is exactly what we're going for. It's bold.",
            character="PATRICK",
            lines=["Excellent work, guys. This is", "exactly what we're going for. It's", "bold."],
        ),
        Element("character", "KYLE", character="KYLE"),
        Element("dialogue", "For real? (beat) Not too out there?", character="KYLE",
                lines=["For real? (beat) Not too out there?"]),
    ]


def run(speak_names: bool):
    engine = RecordingEngine()
    voice_for = lambda el: (engine, "narrator" if el.kind in ("scene", "action", "character") else el.character)
    result = build_audio(script(), voice_for, beat_seconds=1.0, speak_character_names=speak_names)
    return engine, result


def test_wrapped_dialogue_is_exactly_one_synth_call():
    engine, _ = run(speak_names=False)
    texts = [t for _, t in engine.calls]
    assert "Excellent work, guys. This is exactly what we're going for. It's bold." in texts
    assert not any("\n" in t for t in texts)
    assert not any(t.startswith("exactly") or t == "bold." for t in texts), "line was split at a wrap"


def test_action_paragraph_is_one_natural_utterance():
    engine = RecordingEngine()
    elements = [Element("action", "First sentence. Second sentence! Third sentence?", lines=["x"])]
    build_audio(elements, lambda el: (engine, "v"))
    assert [text for _, text in engine.calls] == [
        "First sentence. Second sentence! Third sentence?"
    ]


def test_no_typographic_punctuation_reaches_an_engine():
    engine = RecordingEngine()
    els = [Element("dialogue", "Can\u2019t. \u201cReally?\u201d Yes\u2026", character="COACH", lines=["x"])]
    build_audio(els, lambda el: (engine, "v"), speak_character_names=False)
    for _, text in engine.calls:
        assert all(ord(ch) < 128 or ch in "\u2014\u2013" for ch in text), text


def test_scene_heading_spoken_expanded():
    engine, _ = run(speak_names=False)
    assert engine.calls[0][1] == "Interior. Hotel Bel-Air, Sitting Room. Day."


def test_names_spoken_by_default_in_narrator_voice():
    engine, result = run(speak_names=True)
    assert ("narrator", "Patrick.") in engine.calls
    assert ("narrator", "Kyle.") in engine.calls
    # The name cue is highlighted as its own element, right before the line.
    kinds = [script()[c.element_index].kind for c in result.cues]
    assert "character" in kinds
    # Name in the action line is read regardless.
    assert any(t.startswith("Kyle inspects") for _, t in engine.calls)


def test_skip_names_setting_silences_cues_but_not_action_lines():
    engine, result = run(speak_names=False)
    assert ("narrator", "Patrick.") not in engine.calls
    assert ("narrator", "Kyle.") not in engine.calls
    kinds = {script()[c.element_index].kind for c in result.cues}
    assert "character" not in kinds
    assert any(t.startswith("Kyle inspects") for _, t in engine.calls)


def test_delivery_parenthetical_never_spoken_and_beat_pauses():
    engine, result = run(speak_names=False)
    assert not any("stylists" in t for _, t in engine.calls)
    # "(beat)" split KYLE's line into two utterances with a pause between.
    assert ("KYLE", "For real?") in engine.calls
    assert ("KYLE", "Not too out there?") in engine.calls
    kyle = [c for c in result.cues if c.element_index == 6][0]
    assert kyle.end_ms - kyle.start_ms >= 1000 + 400  # two 200 ms takes + 1 s beat


def test_cues_ordered_and_within_audio():
    _, result = run(speak_names=True)
    for a, b in zip(result.cues, result.cues[1:]):
        assert a.end_ms <= b.start_ms
    assert result.cues[-1].end_ms <= len(result.audio) + 1
    assert result.issues == []


def test_streamed_chunks_reconstruct_complete_audio():
    chunks = []
    engine = RecordingEngine()
    elements = script()

    result = build_audio(
        elements,
        lambda el: (engine, "v"),
        speak_character_names=False,
        audio_chunk=lambda data, start, end, index: chunks.append((data, start, end, index)),
    )

    assert b"".join(chunk[0] for chunk in chunks) == result.audio.raw_data
    assert chunks[0][1] == 0
    assert chunks[-1][2] == len(result.audio)


class FlakyEngine(RecordingEngine):
    """Says the wrong thing the first time it's asked for any text."""

    def synthesize(self, voice, text):
        self.calls.append((voice, text))
        return Sine(300).to_audio_segment(duration=200)


class FakeChecker:
    """Fails the first attempt of every segment, passes the second."""

    def __init__(self):
        self.seen: dict[str, int] = {}

    def check(self, seg, source):
        from screenplay_reader.validate import Verdict

        n = self.seen.get(source, 0) + 1
        self.seen[source] = n
        return Verdict(ok=n >= 2, error_rate=0.0 if n >= 2 else 1.0, transcript="garbled" if n < 2 else source)


def test_validation_retries_and_keeps_going():
    engine = FlakyEngine()
    voice_for = lambda el: (engine, "v")
    result = build_audio(script(), voice_for, speak_character_names=False, checker=FakeChecker())
    # Every segment needed exactly two attempts and none was left unresolved.
    texts = [t for _, t in engine.calls]
    assert all(texts.count(t) == 2 for t in set(texts))
    assert result.issues == []


class AlwaysFail:
    def check(self, seg, source):
        from screenplay_reader.validate import Verdict

        return Verdict(ok=False, error_rate=0.5, transcript="nope")


def test_validation_reports_segments_that_never_pass():
    engine = RecordingEngine()
    result = build_audio(script(), lambda el: (engine, "v"), speak_character_names=False, checker=AlwaysFail())
    assert len(result.issues) > 0
    assert all(i.transcript == "nope" for i in result.issues)
    from screenplay_reader.validate import MAX_ATTEMPTS

    texts = [t for _, t in engine.calls]
    assert all(texts.count(t) == MAX_ATTEMPTS for t in set(texts))
