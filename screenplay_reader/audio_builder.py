"""Turn parsed screenplay elements into one stitched audio track.

Produces the full mix plus a timeline mapping audio time -> element index,
which drives live highlighting during playback.

Beat pauses: a "(beat)" / "(pause)" parenthetical becomes a stretch of
near-silence. If any audio files exist in assets/fillers/, one is chosen at
random and overlaid at low volume (breaths, lip smacks, etc. — the folder
ships empty; drop files in to enable).
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from pydub import AudioSegment

from .parser import BEAT_RE, Element

FRAME_RATE = 44100
GAP_MS = 300  # breathing room between elements
FILLER_GAIN_DB = -18  # fillers should be felt, not heard
FILLERS_DIR = Path(__file__).resolve().parent.parent / "assets" / "fillers"

INLINE_PAREN_RE = re.compile(r"\(([^)]*)\)")


class GenerationCancelled(Exception):
    pass


@dataclass
class Cue:
    """One highlighted region of the final track."""

    start_ms: int
    end_ms: int
    element_index: int


def _normalize(seg: AudioSegment) -> AudioSegment:
    return seg.set_frame_rate(FRAME_RATE).set_channels(1).set_sample_width(2)


def _silence(ms: int) -> AudioSegment:
    return AudioSegment.silent(duration=ms, frame_rate=FRAME_RATE)


def _load_fillers() -> list[AudioSegment]:
    fillers = []
    if FILLERS_DIR.is_dir():
        for f in sorted(FILLERS_DIR.iterdir()):
            if f.suffix.lower() in (".wav", ".mp3", ".ogg", ".flac"):
                fillers.append(_normalize(AudioSegment.from_file(f)))
    return fillers


def _beat_segment(ms: int, fillers: list[AudioSegment]) -> AudioSegment:
    base = _silence(ms)
    if fillers:
        filler = random.choice(fillers).apply_gain(FILLER_GAIN_DB)
        pos = max(0, (ms - len(filler)) // 2)
        base = base.overlay(filler[:ms], position=pos)
    return base


def _dialogue_chunks(text: str) -> list[str | None]:
    """Split dialogue on inline parentheticals. None = beat pause;
    delivery directions like '(sarcastically)' are dropped entirely."""
    chunks: list[str | None] = []
    pos = 0
    for m in INLINE_PAREN_RE.finditer(text):
        before = text[pos : m.start()].strip()
        if before:
            chunks.append(before)
        if BEAT_RE.match(m.group(0)):
            chunks.append(None)
        pos = m.end()
    tail = text[pos:].strip()
    if tail:
        chunks.append(tail)
    return chunks


def build_audio(
    elements: list[Element],
    voice_for: Callable[[Element], tuple[object, str] | None],
    beat_seconds: float = 2.0,
    progress: Callable[[int, int, str], None] = lambda done, total, msg: None,
    cancelled: Callable[[], bool] = lambda: False,
) -> tuple[AudioSegment, list[Cue]]:
    """voice_for(element) returns (engine, voice) for anything to vocalize,
    or None to skip. Character cues and delivery parentheticals never reach
    it; beat parentheticals become pauses attributed to their element."""
    beat_ms = int(beat_seconds * 1000)
    fillers = _load_fillers()

    # (element_index, kind, payload): kind is 'speech' (payload=(engine, voice, text))
    # or 'pause'
    plan: list[tuple[int, str, tuple | None]] = []
    for i, el in enumerate(elements):
        if el.kind == "parenthetical":
            if el.is_beat:
                plan.append((i, "pause", None))
            continue  # delivery direction — never vocalized
        if el.kind == "character":
            continue  # the voice change itself announces the speaker
        ev = voice_for(el)
        if ev is None:
            continue
        engine, voice = ev
        if el.kind == "dialogue":
            for chunk in _dialogue_chunks(el.text):
                if chunk is None:
                    plan.append((i, "pause", None))
                else:
                    plan.append((i, "speech", (engine, voice, chunk)))
        else:
            plan.append((i, "speech", (engine, voice, el.text)))

    raw = bytearray()
    cues: list[Cue] = []

    def append(seg: AudioSegment, element_index: int | None) -> None:
        seg = _normalize(seg)
        start = len(raw) * 1000 // (FRAME_RATE * 2)
        raw.extend(seg.raw_data)
        end = len(raw) * 1000 // (FRAME_RATE * 2)
        if element_index is not None:
            if cues and cues[-1].element_index == element_index:
                cues[-1].end_ms = end
            else:
                cues.append(Cue(start, end, element_index))

    total = len(plan)
    for done, (idx, kind, payload) in enumerate(plan):
        if cancelled():
            raise GenerationCancelled()
        if kind == "pause":
            append(_beat_segment(beat_ms, fillers), idx)
        else:
            engine, voice, text = payload
            progress(done, total, f"{engine.name} · {voice}: {text[:40]}…")
            append(engine.synthesize(voice, text), idx)
            append(_silence(GAP_MS), None)
    progress(total, total, "Stitching complete")

    audio = AudioSegment(bytes(raw), frame_rate=FRAME_RATE, sample_width=2, channels=1)
    return audio, cues


def export_mp3(audio: AudioSegment, path: str) -> None:
    audio.export(path, format="mp3", bitrate="192k")
