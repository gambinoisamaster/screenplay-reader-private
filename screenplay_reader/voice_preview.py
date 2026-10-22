"""Short audition clips, so a voice can be heard before it's cast.

Every voice reads the same line, rendered once by the engine that will voice
the script and leveled like any generated line (audio_builder.finish_segment),
so clips compare fairly. Clips are cached under cache/voice_previews/; the file
name carries a fingerprint of the voice sample it was cloned from, so a
re-recorded voice gets a fresh clip.

Make every clip ahead of time with: uv run python scripts/make_voice_previews.py
(the app also makes any missing clip in the background when casting opens).
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .audio_builder import finish_segment
from .engines.pocket import VOICES_DIR
from .settings import CACHE_DIR

PREVIEW_LINE = "Forget it, Jake. It's Chinatown."
PREVIEW_DIR = CACHE_DIR / "voice_previews"


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("_") or "voice"


def _prefix(engine_name: str, voice: str) -> str:
    return f"{_slug(engine_name)}--{_slug(voice)}--"


def preview_path(engine_name: str, voice: str) -> Path:
    """Where this voice's clip lives (it may not exist yet)."""
    digest = hashlib.sha1(f"{engine_name}\0{voice}\0{PREVIEW_LINE}".encode())
    sample = VOICES_DIR / f"{voice}.wav"
    if sample.is_file():  # a cloned voice: re-recording the sample makes a new clip
        stat = sample.stat()
        digest.update(f"\0{stat.st_size}\0{stat.st_mtime_ns}".encode())
    return PREVIEW_DIR / f"{_prefix(engine_name, voice)}{digest.hexdigest()[:10]}.wav"


def make_preview(engine, voice: str) -> Path:
    """Render this voice's clip if it isn't cached yet; returns its path."""
    path = preview_path(engine.name, voice)
    if path.is_file():
        return path
    clip = finish_segment(engine.synthesize(voice, PREVIEW_LINE))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.stem + ".partial.wav")
    clip.export(tmp, format="wav")
    tmp.replace(path)
    for stale in path.parent.glob(f"{_prefix(engine.name, voice)}*.wav"):
        if stale != path:
            stale.unlink(missing_ok=True)  # the clip of an older recording
    return path
