"""Flatten voice-clone transcripts to a single clean line.

Each cloned voice in assets/voices/ is a pair: NAME.wav + NAME.wav.txt, where
the .txt holds the transcript of what's spoken in the sample. Transcribers like
WhisperX often split that transcript across several lines. Pocket-TTS collapses
whitespace anyway, so this is cosmetic — but a single tidy line keeps every
voice file consistent and saves a manual clean-up each time a voice is added.

Usage:
    uv run python scripts/clean_transcripts.py           # clean every .wav.txt in assets/voices/
    uv run python scripts/clean_transcripts.py a.txt b.txt   # clean only the files you name

Safe to run repeatedly: a file that's already a single clean line is left alone.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

VOICES_DIR = Path(__file__).resolve().parent.parent / "assets" / "voices"


def clean_text(text: str) -> str:
    """Collapse every run of whitespace (spaces, tabs, newlines) to one space."""
    return re.sub(r"\s+", " ", text).strip()


def clean_file(path: Path) -> bool:
    """Rewrite one transcript as a single line. Returns True if it changed."""
    original = path.read_text(encoding="utf-8")
    target = clean_text(original) + "\n"
    if original == target:
        return False
    path.write_text(target, encoding="utf-8")
    return True


def main(argv: list[str]) -> int:
    if argv:
        targets = [Path(a) for a in argv]
    else:
        if not VOICES_DIR.is_dir():
            print(f"No voices folder found at {VOICES_DIR}")
            return 1
        targets = sorted(VOICES_DIR.glob("*.wav.txt"))

    if not targets:
        print("No transcript (.wav.txt) files found.")
        return 0

    changed = 0
    for path in targets:
        if not path.is_file():
            print(f"skip (not a file): {path}")
            continue
        try:
            if clean_file(path):
                changed += 1
                print(f"cleaned:       {path.name}")
            else:
                print(f"already clean: {path.name}")
        except Exception as e:  # keep going if one file is unreadable
            print(f"error on {path.name}: {e}")

    print(f"\nDone. {changed} cleaned, {len(targets) - changed} already clean.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
