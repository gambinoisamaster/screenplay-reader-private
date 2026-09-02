"""Fish-Reader: wrapper around the Fish Audio cloud API.

The API key and voice IDs live in config.py (git-ignored; see
config.example.py) so credentials never appear in application logic.

Because every request costs API credits, finished clips are cached on disk —
regenerating after a settings tweak only re-bills lines that actually changed.
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

from pydub import AudioSegment

from .base import TTSEngine

CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "cache" / "fish"


def _load_config():
    try:
        import config

        return config
    except ImportError:
        return None


class FishReaderEngine(TTSEngine):
    name = "Fish-Reader"

    def __init__(self) -> None:
        self._session = None
        self._config = _load_config()

    def available(self) -> tuple[bool, str]:
        if self._config is None:
            return False, "config.py not found — copy config.example.py to config.py"
        if not getattr(self._config, "FISH_API_KEY", ""):
            return False, "No API key — set FISH_API_KEY in config.py"
        if not getattr(self._config, "FISH_VOICES", {}):
            return False, "No voices — add entries to FISH_VOICES in config.py"
        return True, ""

    def voices(self) -> list[str]:
        return list(self._config.FISH_VOICES) if self._config else []

    def _ensure_session(self):
        if self._session is None:
            from fish_audio_sdk import Session

            self._session = Session(self._config.FISH_API_KEY)
        return self._session

    def synthesize(self, voice: str, text: str) -> AudioSegment:
        voice_id = self._config.FISH_VOICES[voice]
        backend = getattr(self._config, "FISH_MODEL", "s1")

        key = hashlib.sha1(f"{voice_id}|{backend}|{text}".encode()).hexdigest()
        cached = CACHE_DIR / f"{key}.mp3"
        if cached.exists():
            return AudioSegment.from_file(cached, format="mp3")

        from fish_audio_sdk import TTSRequest

        session = self._ensure_session()
        buf = io.BytesIO()
        for chunk in session.tts(TTSRequest(text=text, reference_id=voice_id), backend=backend):
            buf.write(chunk)

        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(buf.getvalue())
        buf.seek(0)
        return AudioSegment.from_file(buf, format="mp3")
