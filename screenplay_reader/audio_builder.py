"""Turn parsed screenplay elements into one stitched audio track.

Produces the full mix plus a timeline mapping audio time -> element index,
which drives live highlighting during playback and click-to-seek.

Script Elements which are audio-generated (elements as defined by Final Draft) (see speech.py for the exact wording):
- Scene Headings, with INT./EXT. expanded to Interior/Exterior
- Action Lines
- Character names (only when signaling a line of dialogue). The default settings use the narrator's voice but under settings, skipping character names is possible before audio-generation.  
- If this option is turned on, the audio will simply say the line of dialogue.
- Character names inside action lines are always read either way.
- dialogue, verbatim; delivery parentheticals like "(sarcastically)" never
- "(beat)" / "(pause)" as a pause

Every piece of text is whitespace-collapsed before it reaches an engine, so
a dialogue block wrapped over three lines in the PDF is one utterance with
no pauses at the line breaks.

Optional validation: when a Transcriber is supplied, each spoken segment is
transcribed and compared to its text; a mismatch is regenerated up to
MAX_ATTEMPTS times and the best take is kept. Segments that never passed are
reported as Issues so the user can decide whether to regenerate.

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

from . import speech
from .parser import BEAT_RE, Element
from .validate import Issue, Transcriber, MAX_ATTEMPTS

FRAME_RATE = 44100
GAP_MS = 300  # breathing room between elements
CUE_GAP_MS = 150  # shorter: the name should lead straight into the line
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


@dataclass
class BuildResult:
    audio: AudioSegment
    cues: list[Cue]
    issues: list[Issue]  # empty unless validation ran and something never passed


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


def _dialogue_chunks(text: str, read_parentheticals: bool = True) -> list[str | None]:
    """Split dialogue on inline parentheticals. None = beat pause; delivery
    directions like '(sarcastically)' are spoken when read_parentheticals is on,
    otherwise dropped."""
    chunks: list[str | None] = []
    pos = 0
    for m in INLINE_PAREN_RE.finditer(text):
        before = text[pos : m.start()].strip()
        if before:
            chunks.append(before)
        if BEAT_RE.match(m.group(0)):
            chunks.append(None)
        elif read_parentheticals:
            inner = m.group(1).strip()
            if inner:
                chunks.append(inner)
        pos = m.end()
    tail = text[pos:].strip()
    if tail:
        chunks.append(tail)
    return chunks


def _synthesize_checked(
    engine, voice: str, text: str, checker: Transcriber | None, element_index: int
) -> tuple[AudioSegment, Issue | None]:
    """Generate one segment; with a checker, retry until the transcript matches."""
    assert "\n" not in text and "  " not in text, "text must be whitespace-collapsed"
    best_seg, best_rate, best_transcript = None, float("inf"), ""
    attempts = MAX_ATTEMPTS if checker else 1
    for _ in range(attempts):
        seg = engine.synthesize(voice, text)
        if checker is None:
            return seg, None
        verdict = checker.check(seg, text)
        if verdict.ok:
            return seg, None
        if verdict.error_rate < best_rate:
            best_seg, best_rate, best_transcript = seg, verdict.error_rate, verdict.transcript
    return best_seg, Issue(element_index, text, best_transcript, best_rate)


def build_audio(
    elements: list[Element],
    voice_for: Callable[[Element], tuple[object, str] | None],
    beat_seconds: float = 2.0,
    *,
    speak_character_names: bool = True,
    read_parentheticals: bool = True,
    checker: Transcriber | None = None,
    progress: Callable[[int, int, str], None] = lambda done, total, msg: None,
    audio_chunk: Callable[[bytes, int, int, int | None], None] | None = None,
    unit_completed: Callable[[int, int], None] = lambda done, total: None,
    cancelled: Callable[[], bool] = lambda: False,
) -> BuildResult:
    """voice_for(element) returns (engine, voice) for anything to vocalize,
    or None to skip. It is asked about scene, action, dialogue and — when
    speak_character_names is on — character elements (answer with the
    narrator). Delivery parentheticals never reach it; beat parentheticals
    become pauses attributed to their element."""
    beat_ms = int(beat_seconds * 1000)
    fillers = _load_fillers()
    names = {e.character for e in elements if e.character}

    # (element_index, kind, payload): kind is 'speech' (payload=(engine, voice, text, gap_ms))
    # or 'pause'
    plan: list[tuple[int, str, tuple | None]] = []
    for i, el in enumerate(elements):
        if el.kind == "parenthetical":
            if el.is_beat:
                plan.append((i, "pause", None))
            elif read_parentheticals:
                ev = voice_for(el)
                if ev is not None:
                    engine, voice = ev
                    spoken = speech.parenthetical(el.text)
                    if spoken:
                        plan.append((i, "speech", (engine, voice, spoken, GAP_MS)))
            continue  # delivery direction — spoken only when read_parentheticals is on
        if el.kind == "character" and not speak_character_names:
            continue  # the voice change itself announces the speaker
        ev = voice_for(el)
        if ev is None:
            continue
        engine, voice = ev
        if el.kind == "dialogue":
            for chunk in _dialogue_chunks(el.text, read_parentheticals):
                if chunk is None:
                    plan.append((i, "pause", None))
                else:
                    plan.append((i, "speech", (engine, voice, speech.dialogue(chunk), GAP_MS)))
        else:
            gap = CUE_GAP_MS if el.kind == "character" else GAP_MS
            plan.append((i, "speech", (engine, voice, speech.for_element(el, names), gap)))

    raw = bytearray()
    cues: list[Cue] = []
    issues: list[Issue] = []

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
        if audio_chunk is not None:
            audio_chunk(seg.raw_data, start, end, element_index)

    total = len(plan)
    for done, (idx, kind, payload) in enumerate(plan):
        if cancelled():
            raise GenerationCancelled()
        if kind == "pause":
            append(_beat_segment(beat_ms, fillers), idx)
        else:
            engine, voice, text, gap = payload
            progress(done, total, f"{engine.name} · {voice}: {text[:40]}…")
            seg, issue = _synthesize_checked(engine, voice, text, checker, idx)
            if issue:
                issues.append(issue)
            append(seg, idx)
            append(_silence(gap), None)
        unit_completed(done + 1, total)
    progress(total, total, "Stitching complete")

    audio = AudioSegment(bytes(raw), frame_rate=FRAME_RATE, sample_width=2, channels=1)
    return BuildResult(audio, cues, issues)


def export_mp3(audio: AudioSegment, path: str) -> None:
    audio.export(path, format="mp3", bitrate="192k")
