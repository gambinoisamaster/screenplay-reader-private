"""Audition clips for the casting panel: rendered once per voice, cached by recording."""

from pydub.generators import Sine

from screenplay_reader import voice_preview
from screenplay_reader.voice_preview import PREVIEW_LINE, make_preview, preview_path


class FakeEngine:
    name = "Fake-TTS"

    def __init__(self):
        self.calls = []

    def synthesize(self, voice, text):
        self.calls.append((voice, text))
        return Sine(220).to_audio_segment(duration=400).apply_gain(-12)


def test_every_voice_reads_the_same_line_once(tmp_path, monkeypatch):
    monkeypatch.setattr(voice_preview, "PREVIEW_DIR", tmp_path / "previews")
    monkeypatch.setattr(voice_preview, "VOICES_DIR", tmp_path / "voices")
    engine = FakeEngine()
    path = make_preview(engine, "alba")
    assert path.is_file()
    assert engine.calls == [("alba", PREVIEW_LINE)]
    assert PREVIEW_LINE == "Forget it, Jake. It's Chinatown."
    make_preview(engine, "alba")
    assert len(engine.calls) == 1, "a cached clip is never re-rendered"


def test_re_recording_a_voice_makes_a_fresh_clip(tmp_path, monkeypatch):
    voices = tmp_path / "voices"
    voices.mkdir()
    monkeypatch.setattr(voice_preview, "PREVIEW_DIR", tmp_path / "previews")
    monkeypatch.setattr(voice_preview, "VOICES_DIR", voices)
    sample = voices / "Mare.wav"
    sample.write_bytes(b"first take")
    engine = FakeEngine()
    first = make_preview(engine, "Mare")
    sample.write_bytes(b"a longer second take")
    assert preview_path(engine.name, "Mare") != first
    second = make_preview(engine, "Mare")
    assert second.is_file() and not first.exists(), "the old recording's clip is cleaned up"
