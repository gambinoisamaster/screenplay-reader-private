"""User settings and last-session state, persisted as JSON under cache/.

Two files, two lifetimes:
- settings.json  — preferences that outlive any one screenplay.
- session.json   — where the user was: which PDF, the casting, the cue
                   timeline of the last generated audio, and the playback
                   position. Lets the app reopen exactly where it was closed.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"
SETTINGS_PATH = CACHE_DIR / "settings.json"
SESSION_PATH = CACHE_DIR / "session.json"
LAST_AUDIO_PATH = CACHE_DIR / "last_run.wav"


@dataclass
class Settings:
    # Narrator announces "Ethan." before each of Ethan's lines unless this is on.
    skip_character_names: bool = False
    # One voice (the Narrator's) reads everything; per-character casting is hidden.
    single_voice: bool = False
    # Transcribe every generated segment with Whisper and regenerate mismatches.
    validate_audio: bool = False
    beat_seconds: float = 2.0
    highlight: bool = True

    @classmethod
    def load(cls) -> "Settings":
        return cls(**_read_json(SETTINGS_PATH, cls()))

    def save(self) -> None:
        _write_json(SETTINGS_PATH, asdict(self))


@dataclass
class Session:
    pdf_path: str = ""
    casting: dict[str, str] = field(default_factory=dict)  # "" = Narrator -> voice label
    cues: list[list[int]] = field(default_factory=list)  # [start_ms, end_ms, element_index]
    position_ms: int = 0
    # Settings that shaped the cached audio; if they differ from the current
    # ones the cache is stale and the user must regenerate.
    skip_character_names: bool = False
    single_voice: bool = False

    @classmethod
    def load(cls) -> "Session | None":
        data = _read_json(SESSION_PATH, None)
        if not data:
            return None
        try:
            return cls(**data)
        except TypeError:  # older/foreign file
            return None

    def save(self) -> None:
        _write_json(SESSION_PATH, asdict(self))

    def audio_is_reusable(self) -> bool:
        return bool(self.pdf_path) and Path(self.pdf_path).is_file() and LAST_AUDIO_PATH.is_file() and bool(self.cues)


def _read_json(path: Path, default):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return asdict(default) if default is not None and not isinstance(default, dict) else default
    if default is not None and not isinstance(default, dict):
        # Tolerate unknown keys from newer versions and fill in missing ones.
        known = asdict(default)
        return {k: data.get(k, v) for k, v in known.items()}
    return data


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    tmp.replace(path)  # atomic on POSIX: a crash mid-write never corrupts the file
