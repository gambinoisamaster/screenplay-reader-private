"""End-to-end smoke test with a fake TTS engine — no API calls, no credits.

Usage: uv run python scripts/smoke_test.py <screenplay.pdf> [n_elements]
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pydub.generators import Sine

from screenplay_reader.audio_builder import build_audio, export_mp3
from screenplay_reader.parser import parse_screenplay


class ToneEngine:
    """Each voice is a distinct pitch; duration scales with text length."""

    name = "Tone"

    def synthesize(self, voice, text):
        freq = 200 + hash(voice) % 400
        return Sine(freq).to_audio_segment(duration=min(2000, 40 * len(text.split())))


def main():
    pdf = sys.argv[1]
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 60

    sp = parse_screenplay(pdf)
    assert sp.elements, "no elements parsed"
    assert sp.characters, "no characters found"
    print(f"parsed: {len(sp.elements)} elements, {len(sp.characters)} characters")
    print("characters:", ", ".join(sp.characters[:10]), "...")

    engine = ToneEngine()
    elements = sp.elements[:n]

    def voice_for(el):
        return (engine, el.character or "NARRATOR")

    audio, cues = build_audio(elements, voice_for, beat_seconds=1.5)

    # Timeline sanity: cues ordered, non-overlapping, within the audio.
    assert cues, "no cues produced"
    for a, b in zip(cues, cues[1:]):
        assert a.end_ms <= b.start_ms, f"overlapping cues: {a} {b}"
    assert cues[-1].end_ms <= len(audio) + 1
    spoken_kinds = {elements[c.element_index].kind for c in cues}
    assert "character" not in spoken_kinds, "character cues must not be vocalized"

    out = Path(__file__).resolve().parent.parent / "cache" / "smoke_test.mp3"
    out.parent.mkdir(exist_ok=True)
    export_mp3(audio, str(out))
    print(f"audio: {len(audio) / 1000:.1f}s, {len(cues)} cues -> {out}")
    print("OK")


if __name__ == "__main__":
    main()
