"""Render the casting-panel audition clip for every voice in the library.

Usage: uv run python scripts/make_voice_previews.py
Clips already made are skipped, so it's cheap to re-run after adding a voice.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from screenplay_reader.engines import load_engines
from screenplay_reader.voice_preview import PREVIEW_LINE, make_preview, preview_path


def main():
    engines = [e for e in load_engines() if e.available()[0]]
    if not engines:
        sys.exit("No TTS engine is available.")
    todo = [(e, v) for e in engines for v in e.voices()]
    print(f'{len(todo)} voices reading: "{PREVIEW_LINE}"')
    made = 0
    for i, (engine, voice) in enumerate(todo, 1):
        if preview_path(engine.name, voice).is_file():
            print(f"[{i}/{len(todo)}] {engine.name} · {voice}: already made")
            continue
        start = time.time()
        path = make_preview(engine, voice)
        made += 1
        print(f"[{i}/{len(todo)}] {engine.name} · {voice}: {time.time() - start:.1f}s -> {path.name}", flush=True)
    print(f"Done: {made} new clip(s).")


if __name__ == "__main__":
    main()
