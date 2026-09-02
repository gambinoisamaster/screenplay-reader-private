"""Pocket-TTS wrapper (Kyutai, MIT license).

Runs fully on CPU. load_model() and get_state_for_audio_prompt() are slow,
so the model is loaded once and voice states are cached in memory, as the
Pocket-TTS docs recommend. generate_audio() streams frames internally at
12.5 Hz and returns a 1-D PCM tensor.

Requires torch>=2.5, which has no Intel-Mac wheels — on such machines this
engine reports itself unavailable and the app falls back to Fish-Reader.
"""

from __future__ import annotations

from pydub import AudioSegment

from .base import TTSEngine

VOICES = [
    "alba", "anna", "azelma", "bill_boerst", "caro_davy", "charles",
    "cosette", "eponine", "eve", "fantine", "george", "jane", "jean",
    "javert", "marius", "mary", "michael", "paul", "peter_yearsley",
    "stuart_bell", "vera",
]


class PocketTTSEngine(TTSEngine):
    name = "Pocket-TTS"

    def __init__(self) -> None:
        self._model = None
        self._voice_states: dict[str, object] = {}
        try:
            import pocket_tts  # noqa: F401
            self._import_error = None
        except ImportError as e:
            self._import_error = str(e)

    def available(self) -> tuple[bool, str]:
        if self._import_error:
            return False, (
                "pocket-tts is not installed (needs torch>=2.5; unavailable on "
                "Intel Macs). Install with: uv sync --extra pocket"
            )
        return True, ""

    def voices(self) -> list[str]:
        return VOICES

    def _ensure_model(self):
        if self._model is None:
            from pocket_tts import TTSModel

            self._model = TTSModel.load_model()
        return self._model

    def synthesize(self, voice: str, text: str) -> AudioSegment:
        import numpy as np

        model = self._ensure_model()
        if voice not in self._voice_states:
            self._voice_states[voice] = model.get_state_for_audio_prompt(voice)
        audio = model.generate_audio(self._voice_states[voice], text)
        pcm = (audio.numpy().clip(-1, 1) * 32767).astype(np.int16)
        return AudioSegment(
            pcm.tobytes(), frame_rate=model.sample_rate, sample_width=2, channels=1
        )
