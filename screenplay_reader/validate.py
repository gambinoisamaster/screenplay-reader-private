"""Check generated speech against its source text with Whisper.

TTS models occasionally drop, repeat or mangle words. Before a segment is
stitched into the final track, this module transcribes it locally and
compares the transcript to the text that was requested, word by word. The
audio builder regenerates any segment that fails, keeping the best attempt.

Backends (both run offline, nothing leaves the machine):
- mlx-whisper on Apple Silicon (fast, uses the GPU/Neural Engine)
- faster-whisper everywhere else (CPU)

Install with: uv sync --extra validate
The first run downloads the Whisper model (~1.5 GB) from Hugging Face and
caches it; after that it is fully offline.
"""

from __future__ import annotations

import difflib
import platform
import re
import sys
from dataclasses import dataclass

from num2words import num2words
from pydub import AudioSegment

WHISPER_SAMPLE_RATE = 16000
MLX_MODEL = "mlx-community/whisper-large-v3-turbo"
FASTER_WHISPER_MODEL = "large-v3-turbo"

# A segment fails when its word error rate exceeds this. Short lines get no
# slack: a two-word line with one wrong word is half wrong.
MAX_ERROR_RATE = 0.15
MAX_ATTEMPTS = 3

_PUNCT_RE = re.compile(r"[^\w\s']", re.UNICODE)


@dataclass
class Verdict:
    ok: bool
    error_rate: float
    transcript: str


@dataclass
class Issue:
    """A segment that never passed; the best attempt was kept."""

    element_index: int
    text: str
    transcript: str
    error_rate: float


def _is_apple_silicon() -> bool:
    return sys.platform == "darwin" and platform.machine() == "arm64"


def normalize(text: str) -> list[str]:
    """Lowercase words, punctuation dropped, digits spelled out — so 'Room 2!'
    and 'room two' compare equal."""
    text = text.lower().replace("-", " ")
    text = _PUNCT_RE.sub(" ", text)
    words = []
    for w in text.split():
        if w.isdigit():
            w = num2words(int(w))  # "2" -> "two", "21" -> "twenty-one"
            words.extend(_PUNCT_RE.sub(" ", w.replace("-", " ")).split())
        else:
            words.append(w.strip("'"))
    return [w for w in words if w]


def word_error_rate(source: str, transcript: str) -> float:
    src, hyp = normalize(source), normalize(transcript)
    if not src:
        return 0.0
    sm = difflib.SequenceMatcher(a=src, b=hyp, autojunk=False)
    errors = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag != "equal":
            errors += max(i2 - i1, j2 - j1)
    return errors / len(src)


class Transcriber:
    """Lazy-loads a Whisper backend on first use."""

    def __init__(self) -> None:
        self._backend: str | None = None
        self._model = None
        self._import_error: str | None = None
        if _is_apple_silicon():
            try:
                import mlx_whisper  # noqa: F401
                self._backend = "mlx"
            except ImportError as e:
                self._import_error = str(e)
        else:
            try:
                import faster_whisper  # noqa: F401
                self._backend = "faster"
            except ImportError as e:
                self._import_error = str(e)

    def available(self) -> tuple[bool, str]:
        if self._backend is None:
            return False, (
                "Whisper is not installed. Install with: uv sync --extra validate "
                f"({self._import_error})"
            )
        return True, ""

    @staticmethod
    def _to_float32(seg: AudioSegment):
        import numpy as np  # arrives with torch/pocket-tts; absent on Intel Macs

        seg = seg.set_frame_rate(WHISPER_SAMPLE_RATE).set_channels(1).set_sample_width(2)
        pcm = np.frombuffer(seg.raw_data, dtype=np.int16).astype(np.float32) / 32768.0
        return np.clip(pcm, -1.0, 1.0)

    def transcribe(self, seg: AudioSegment) -> str:
        audio = self._to_float32(seg)
        if self._backend == "mlx":
            import mlx_whisper

            result = mlx_whisper.transcribe(audio, path_or_hf_repo=MLX_MODEL, language="en")
            return result.get("text", "")
        if self._backend == "faster":
            if self._model is None:
                from faster_whisper import WhisperModel

                self._model = WhisperModel(FASTER_WHISPER_MODEL, device="cpu", compute_type="int8")
            segments, _ = self._model.transcribe(audio, language="en")
            return " ".join(s.text for s in segments)
        raise RuntimeError("No Whisper backend available")

    def check(self, seg: AudioSegment, source: str) -> Verdict:
        transcript = self.transcribe(seg)
        rate = word_error_rate(source, transcript)
        return Verdict(ok=rate <= MAX_ERROR_RATE, error_rate=rate, transcript=transcript.strip())
