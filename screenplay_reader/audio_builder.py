"""Turn parsed screenplay elements into one stitched audio track.

Produces the full mix plus two timelines: audio time -> element index (drives
click-to-seek and following the script), and audio time -> sentence (drives
the sentence-by-sentence highlight; see sentences.py for how a sentence's time
inside one utterance is found).

Script Elements which are audio-generated (elements as defined by Final Draft) (see speech.py for the exact wording):
- Scene Headings, with INT./EXT. expanded to Interior/Exterior
- Action Lines
- Character names (only when signaling a line of dialogue). The default settings use the narrator's voice but under settings, skipping character names is possible before audio-generation.  
- If this option is turned on, the audio will simply say the line of dialogue.
- Character names inside action lines are always read either way.
- dialogue, verbatim
- delivery parentheticals like "(sarcastically)", on their own line or inline in
  dialogue, read in the narrator's voice when "Read parentheticals" is on (the
  default); skipped when it's off.
- "(beat)" / "(pause)" as a pause

Every piece of text is whitespace-collapsed before it reaches an engine, so
a dialogue block wrapped over three lines in the PDF is one utterance with
no pauses at the line breaks.

Optional validation: when a Transcriber is supplied, each spoken segment is
transcribed and compared to its text; a mismatch is regenerated up to
MAX_ATTEMPTS times and the best take is kept. Segments that never passed are
reported as Issues so the user can decide whether to regenerate.

Even volume: every spoken segment is leveled to the same perceived loudness
(see loudness.py), so a voice sample recorded quietly or loudly makes no
difference to how loud its character sounds.

Beat pauses: a "(beat)" / "(pause)" parenthetical becomes a stretch of
near-silence. If any audio files exist in assets/fillers/, one is chosen at
random and overlaid at low volume (breaths, lip smacks, etc. — the folder
ships empty; drop files in to enable).
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np
from pydub import AudioSegment

from . import speech
from .loudness import level
from .parser import BEAT_RE, Element
from .sentences import sentence_boundaries, sentence_weight, split_sentences
from .validate import Issue, Transcriber, MAX_ATTEMPTS

FRAME_RATE = 44100
GAP_MS = 300  # breathing room between elements
CUE_GAP_MS = 150  # shorter: the name should lead straight into the line
DECLICK_MS = 8  # tiny fade at each spoken segment's edges to kill concatenation ticks
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
class SentenceCue:
    """When one sentence is heard: characters [start_char, end_char) of
    elements[element_index].text play from start_ms to end_ms."""

    start_ms: int
    end_ms: int
    element_index: int
    start_char: int
    end_char: int


@dataclass
class BuildResult:
    audio: AudioSegment
    cues: list[Cue]
    issues: list[Issue]  # empty unless validation ran and something never passed
    sentences: list[SentenceCue] = field(default_factory=list)


@dataclass
class _Step:
    """One thing to put on the track: a spoken utterance or a beat pause,
    voicing characters span[0]:span[1] of elements[element_index].text."""

    element_index: int
    kind: str  # 'speech' | 'pause'
    span: tuple[int, int]
    engine: object = None
    voice: str = ""
    text: str = ""
    gap_ms: int = 0


def _normalize(seg: AudioSegment) -> AudioSegment:
    return seg.set_frame_rate(FRAME_RATE).set_channels(1).set_sample_width(2)


def finish_segment(seg: AudioSegment) -> AudioSegment:
    """What every spoken segment goes through before it's heard: 44.1 kHz mono,
    leveled to the target loudness, edges declicked."""
    return _declick(level(_normalize(seg)))


def _bytes_to_ms(n: int) -> int:
    """Time at byte offset n of the 16-bit mono track, rounded the way
    len(AudioSegment) rounds, so the last cue/chunk ends at len(audio)."""
    return round(1000 * (n // 2) / FRAME_RATE)


def _silence(ms: int) -> AudioSegment:
    return AudioSegment.silent(duration=ms, frame_rate=FRAME_RATE)


def _declick(seg: AudioSegment) -> AudioSegment:
    """A few ms of fade at each edge so stitched segments don't 'click'.

    A Pocket-TTS segment can end (or start) on a non-zero sample; butting two of
    them together makes the waveform jump, which the ear hears as a faint tick.
    Ramping each edge to zero removes that discontinuity. The fade is far shorter
    than a syllable, so the speech itself isn't audibly clipped."""
    ms = min(DECLICK_MS, len(seg) // 2)
    if ms <= 0:
        return seg
    return seg.fade_in(ms).fade_out(ms)


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


def _trimmed(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _dialogue_chunks(text: str, read_parentheticals: bool = True) -> list[tuple[str, int, int]]:
    """Split dialogue on inline parentheticals into ('line', start, end), ('pause',
    start, end) for a beat, and ('direction', start, end) for a delivery direction
    like '(sarcastically)' — kept only when read_parentheticals is on, otherwise
    dropped. start/end are character offsets into text."""
    chunks: list[tuple[str, int, int]] = []
    pos = 0
    for m in INLINE_PAREN_RE.finditer(text):
        before = _trimmed(text, pos, m.start())
        if before[0] < before[1]:
            chunks.append(("line", *before))
        if BEAT_RE.match(m.group(0)):
            chunks.append(("pause", m.start(), m.end()))
        elif read_parentheticals:
            chunks.append(("direction", m.start(), m.end()))
        pos = m.end()
    tail = _trimmed(text, pos, len(text))
    if tail[0] < tail[1]:
        chunks.append(("line", *tail))
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
    sentence_cue: Callable[[SentenceCue], None] | None = None,
    unit_completed: Callable[[int, int], None] = lambda done, total: None,
    cancelled: Callable[[], bool] = lambda: False,
) -> BuildResult:
    """voice_for(element) returns (engine, voice) for anything to vocalize,
    or None to skip. It is asked about scene, action, dialogue and — when
    speak_character_names is on — character elements (answer with the
    narrator). When read_parentheticals is on, delivery parentheticals reach
    it too as parenthetical elements — inline ones like "Sure. (sarcastically)
    Great." included — so they're read in the same voice (the narrator, in the
    app) rather than the speaker's; when it's off they're skipped. Beat
    parentheticals become pauses attributed to their element.

    While building, audio_chunk(data, start_ms, end_ms, element_index) streams
    each piece of the track as it's made, and sentence_cue(cue) each sentence's
    timing — always before the audio it describes."""
    beat_ms = int(beat_seconds * 1000)
    fillers = _load_fillers()
    names = {e.character for e in elements if e.character}

    plan: list[_Step] = []

    def plan_direction(i: int, direction: Element, span: tuple[int, int]) -> None:
        ev = voice_for(direction)
        if ev is not None:
            engine, voice = ev
            spoken = speech.parenthetical(direction.text)
            if spoken:
                plan.append(_Step(i, "speech", span, engine, voice, spoken, GAP_MS))

    for i, el in enumerate(elements):
        whole = (0, len(el.text))
        if el.kind == "parenthetical":
            if el.is_beat:
                plan.append(_Step(i, "pause", whole))
            elif read_parentheticals:
                plan_direction(i, el, whole)
            continue  # delivery direction — spoken only when read_parentheticals is on
        if el.kind == "character" and not speak_character_names:
            continue  # the voice change itself announces the speaker
        ev = voice_for(el)
        if ev is None:
            continue
        engine, voice = ev
        if el.kind == "dialogue":
            for kind, start, end in _dialogue_chunks(el.text, read_parentheticals):
                chunk = el.text[start:end]
                if kind == "pause":
                    plan.append(_Step(i, "pause", (start, end)))
                elif kind == "direction":
                    plan_direction(i, Element("parenthetical", chunk, character=el.character), (start, end))
                else:
                    plan.append(_Step(i, "speech", (start, end), engine, voice, speech.dialogue(chunk), GAP_MS))
        else:
            gap = CUE_GAP_MS if el.kind == "character" else GAP_MS
            plan.append(_Step(i, "speech", whole, engine, voice, speech.for_element(el, names), gap))

    raw = bytearray()
    cues: list[Cue] = []
    sentences: list[SentenceCue] = []
    issues: list[Issue] = []

    def append(seg: AudioSegment, step: _Step | None) -> None:
        seg = _normalize(seg)
        start = _bytes_to_ms(len(raw))
        end = _bytes_to_ms(len(raw) + len(seg.raw_data))
        element_index = None if step is None else step.element_index
        if step is not None:
            if cues and cues[-1].element_index == element_index:
                cues[-1].end_ms = end
            else:
                cues.append(Cue(start, end, element_index))
            for cue in _sentence_cues(elements[element_index], step, seg, start, end):
                sentences.append(cue)
                if sentence_cue is not None:
                    sentence_cue(cue)
        raw.extend(seg.raw_data)
        if audio_chunk is not None:
            audio_chunk(seg.raw_data, start, end, element_index)

    total = len(plan)
    for done, step in enumerate(plan):
        if cancelled():
            raise GenerationCancelled()
        if step.kind == "pause":
            append(_beat_segment(beat_ms, fillers), step)
        else:
            progress(done, total, f"{step.engine.name} · {step.voice}: {step.text[:40]}…")
            seg, issue = _synthesize_checked(step.engine, step.voice, step.text, checker, step.element_index)
            if issue:
                issues.append(issue)
            append(finish_segment(seg), step)
            append(_silence(step.gap_ms), None)
        unit_completed(done + 1, total)
    progress(total, total, "Stitching complete")

    audio = AudioSegment(bytes(raw), frame_rate=FRAME_RATE, sample_width=2, channels=1)
    return BuildResult(audio, cues, issues, sentences)


def _sentence_cues(el: Element, step: _Step, seg: AudioSegment, start_ms: int, end_ms: int) -> list[SentenceCue]:
    """The sentences this step voices and when each is heard. Action and dialogue
    are split into sentences; a heading, a name, a direction or a pause is one."""
    if step.kind == "speech" and el.kind in ("action", "dialogue"):
        spans = split_sentences(el.text, *step.span)
    else:
        spans = [step.span]
    if len(spans) == 1:
        return [SentenceCue(start_ms, end_ms, step.element_index, *spans[0])]
    samples = np.frombuffer(seg.raw_data, dtype=np.int16) / 32768.0
    weights = [sentence_weight(el.text[a:b]) for a, b in spans]
    bounds = sentence_boundaries(samples, seg.frame_rate, weights)
    times = [start_ms + round(t * 1000) for t in bounds[:-1]] + [end_ms]
    return [
        SentenceCue(times[k], times[k + 1], step.element_index, a, b)
        for k, (a, b) in enumerate(spans)
    ]


def export_mp3(audio: AudioSegment, path: str) -> None:
    audio.export(path, format="mp3", bitrate="192k")
