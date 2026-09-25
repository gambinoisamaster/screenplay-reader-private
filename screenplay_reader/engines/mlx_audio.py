from __future__ import annotations

from pathlib import Path

import numpy as np
from pydub import AudioSegment

from .base import TTSEngine

VOICES_DIR = Path(__file__).resolve().parent.parent.parent / "assets" / "voices"


class OmniVoiceTTSEngine(TTSEngine):
    name = "OmniVoice"
    _MODEL_ID = "mlx-community/OmniVoice-bfloat16"

    def __init__(self) -> None:
        self._model = None

    def available(self) -> tuple[bool, str]:
        try:
            from mlx_audio.tts.utils import load_model  # noqa: F401
            return True, ""
        except Exception as e:
            return False, f"mlx-audio is unavailable: {e}"

    def voices(self) -> list[str]:
        samples = sorted(
            path.stem
            for path in VOICES_DIR.glob("*.wav")
            if not path.name.startswith(".")
        ) if VOICES_DIR.is_dir() else []
        return ["Auto", *samples]

    def _voice_sample(self, voice: str) -> Path | None:
        if voice == "Auto":
            return None
        sample = VOICES_DIR / f"{voice}.wav"
        return sample if sample.is_file() else None

    def _voice_reference_text(self, voice: str) -> str | None:
        sample = self._voice_sample(voice)
        if sample is None:
            return None
        transcript = sample.with_suffix(".wav.txt")
        if not transcript.is_file():
            return None
        text = transcript.read_text(encoding="utf-8").strip()
        return text or None

    def _ensure_model(self):
        if self._model is None:
            try:
                from mlx_audio.tts.utils import load_model
            except ImportError as e:
                raise RuntimeError("mlx-audio is not installed") from e

            self._model = load_model(self._MODEL_ID)
        return self._model

    def synthesize(self, voice: str, text: str) -> AudioSegment:
        model = self._ensure_model()

        generator = model.generate(
            text=text,
            language="en",
            ref_audio=self._voice_sample(voice),
            ref_text=self._voice_reference_text(voice),
        )

        try:
            result = next(iter(generator))
        except StopIteration as e:
            raise RuntimeError("mlx-audio produced no audio output") from e

        audio = getattr(result, "audio", None)
        if audio is None:
            raise RuntimeError("mlx-audio returned no waveform data")

        wave = np.asarray(audio)
        if wave.ndim == 2:
            wave = wave.mean(axis=0 if wave.shape[0] <= 2 else -1)

        if np.issubdtype(wave.dtype, np.floating):
            wave = np.clip(wave.astype(np.float32), -1.0, 1.0)
        else:
            max_val = np.iinfo(wave.dtype).max if np.issubdtype(wave.dtype, np.integer) else 1.0
            wave = np.clip(wave.astype(np.float32) / max_val, -1.0, 1.0)

        pcm = (wave * 32767).astype(np.int16)
        sample_rate = getattr(result, "sample_rate", 24000)

        return AudioSegment(
            pcm.tobytes(),
            frame_rate=int(sample_rate),
            sample_width=2,
            channels=1,
        )


# Keep imports from the initial MLX adapter working for callers outside the app.
MLXAudioTTSEngine = OmniVoiceTTSEngine
