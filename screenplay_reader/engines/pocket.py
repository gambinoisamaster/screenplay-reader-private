"""Pocket-TTS wrapper (Kyutai, MIT license). Runs fully on CPU.

Two kinds of voices:
- Predefined voices that ship with the model ("alba", "javert", ...).
- Cloned voices: any .wav in assets/voices/. Pocket-TTS conditions on the
  sample's speaker, style and prosody, so a clean ~10-20 s recording is
  enough. They are listed under the file's name (minus .wav).

load_model() and get_state_for_audio_prompt() are slow, so the model is
loaded once and every voice state is cached in memory for the session.
Pocket-TTS itself replaces newlines with spaces and splits long text on
sentence punctuation only, so wrapped screenplay lines can never produce a
pause — see speech.collapse_whitespace() for the belt-and-braces guarantee.

Requires torch>=2.5, which has no Intel-Mac wheels — on such machines this
engine reports itself unavailable and the app runs without any voices.
"""

from __future__ import annotations

from pathlib import Path

from pydub import AudioSegment

from .base import TTSEngine

VOICES_DIR = Path(__file__).resolve().parent.parent.parent / "assets" / "voices"
CLONE_SUFFIX = ""  # label is the bare file stem, e.g. "Default_Male_Narrator"
DEFAULT_NARRATOR_STEM = "Default_Male_Narrator"

PREDEFINED_VOICES = [
    "alba", "anna", "azelma", "bill_boerst", "caro_davy", "charles",
    "cosette", "eponine", "eve", "fantine", "george", "jane", "jean",
    "javert", "marius", "mary", "michael", "paul", "peter_yearsley",
    "stuart_bell", "vera",
]


def clone_samples() -> dict[str, Path]:
    """Voice label -> wav path, for every sample in assets/voices/."""
    if not VOICES_DIR.is_dir():
        return {}
    return {
        f"{p.stem}{CLONE_SUFFIX}": p
        for p in sorted(VOICES_DIR.iterdir())
        if p.suffix.lower() == ".wav" and not p.name.startswith(".")
    }


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
                "Intel Macs). Install with: uv sync"
            )
        return True, ""

    def voices(self) -> list[str]:
        # Cloned voices first so the app's default narrator is easy to find.
        return list(clone_samples()) + PREDEFINED_VOICES

    def default_narrator(self) -> str | None:
        label = f"{DEFAULT_NARRATOR_STEM}{CLONE_SUFFIX}"
        return label if label in clone_samples() else None

    def _ensure_model(self):
        if self._model is None:
            from pocket_tts import TTSModel

            self._model = TTSModel.load_model()
        return self._model

    def _state_for(self, voice: str):
        if voice not in self._voice_states:
            model = self._ensure_model()
            samples = clone_samples()
            prompt = str(samples[voice]) if voice in samples else voice
            self._voice_states[voice] = model.get_state_for_audio_prompt(prompt)
        return self._voice_states[voice]

    def synthesize(self, voice: str, text: str) -> AudioSegment:
        import numpy as np

        model = self._ensure_model()
        audio = model.generate_audio(self._state_for(voice), text)
        pcm = (audio.numpy().clip(-1, 1) * 32767).astype(np.int16)
        return AudioSegment(
            pcm.tobytes(), frame_rate=model.sample_rate, sample_width=2, channels=1
        )
