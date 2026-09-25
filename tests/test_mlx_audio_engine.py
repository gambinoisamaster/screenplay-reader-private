from pydub import AudioSegment

from screenplay_reader.engines import load_engines
from screenplay_reader.engines.mlx_audio import OmniVoiceTTSEngine


def test_omnivoice_engine_is_registered():
    engines = load_engines()
    names = {engine.name for engine in engines}
    assert "OmniVoice" in names

def test_omnivoice_uses_selected_reference_audio(tmp_path, monkeypatch):
    voices_dir = tmp_path / "voices"
    voices_dir.mkdir()
    (voices_dir / "Narrator.wav").touch()
    (voices_dir / "Narrator.wav.txt").write_text("Hello world.", encoding="utf-8")

    class Result:
        audio = [0.0, 0.5, -0.5] * 8000
        sample_rate = 24000

    class Model:
        def __init__(self):
            self.arguments = None

        def generate(self, **kwargs):
            self.arguments = kwargs
            return iter([Result()])

    model = Model()
    monkeypatch.setattr("screenplay_reader.engines.mlx_audio.VOICES_DIR", voices_dir)
    engine = OmniVoiceTTSEngine()
    monkeypatch.setattr(engine, "_ensure_model", lambda: model)

    audio = engine.synthesize("Narrator", "Hello world")

    assert isinstance(audio, AudioSegment)
    assert audio.frame_rate == 24000
    assert audio.channels == 1
    assert len(audio) > 0
    assert model.arguments["language"] == "en"
    assert model.arguments["ref_audio"] == voices_dir / "Narrator.wav"
    assert model.arguments["ref_text"] == "Hello world."
    assert "duration_s" not in model.arguments

    audio = engine.synthesize("Auto", "Hello world")
    assert isinstance(audio, AudioSegment)
    assert audio.frame_rate == 24000
    assert audio.channels == 1
    assert len(audio) > 0
