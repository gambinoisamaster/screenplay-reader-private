# Screenplay Reader

A local desktop app that turns PDF screenplays into an interactive audio
experience: cast a voice for every character (plus a Narrator for action
lines), listen with live line-by-line highlighting, and export the result
as an MP3.

**Everything runs on your machine.** Screenplay text is never sent to a
cloud service — no API keys, no accounts, no third party that could retain
your pages or train on them. Only local TTS engines are supported, by
design.

Open source, for **non-commercial use** (see Credits & licenses below).

## Features

- **PDF parsing** — detects scene headings, action, character cues, dialogue,
  and parentheticals by their indentation, the way screenplay formatting
  encodes them. Speaking characters are extracted automatically.
- **Read the way a script is read aloud** — `INT.`/`EXT.` are always spoken
  as "Interior"/"Exterior" (`INT. HOTEL BEL-AIR - SITTING ROOM - DAY` →
  *"Interior. Hotel Bel-Air, Sitting Room. Day."*). A dialogue block wrapped
  over several lines in the PDF is one utterance — there is never a pause at
  a line break. Character names introduced in caps in action lines are read
  as names.
- **One voice or a full cast** — turn on **One voice reads the whole script**
  and the Narrator's voice (your cloned sample or any built-in voice) reads
  everything, dialogue included; per-character casting disappears.
- **Character names, your call** — by default the narrator says *"Ethan."*
  before each of Ethan's lines. Turn on **Skip characters' names during
  playback** in Settings and only the dialogue is spoken; the voice change
  marks the speaker. Names inside action lines are always read either way.
- **Screenplay-aware** — `(CONT'D)`, `(V.O.)`, `(O.S.)` and delivery
  directions like `(sarcastically)` are never vocalized. `(beat)` and
  `(pause)` become an adjustable 1–5 s pause, optionally colored with subtle
  filler noises from `assets/fillers/`.
- **Voice casting & cloning** — assign any voice to each character and the
  Narrator. Drop a `.wav` into `assets/voices/` and it appears as a cloneable
  voice; `Default_narrator.wav` is picked for the Narrator automatically.
- **Live highlighting, click to seek** — the script scrolls and highlights
  the line being spoken. Click any line (a scene heading, say) and playback
  jumps there.
- **Picks up where you left off** — closing the app remembers the script,
  the casting and the playback position; relaunch and press Play.
- **Optional Whisper check** — every generated line can be transcribed
  locally and compared to the script; mismatches are regenerated (up to 3
  attempts) and anything that still fails is listed so you can decide.
- **MP3 export** — save the stitched performance locally.

## TTS engines

| Engine | Runs | Notes |
| --- | --- | --- |
| **Pocket-TTS** | locally, on CPU | ~20 voices. Needs `torch>=2.5` (CPU build is fine — see gotchas) |

## Setup

Requires [uv](https://docs.astral.sh/uv/) and [ffmpeg](https://ffmpeg.org)
(`brew install ffmpeg` on macOS).

```bash
uv sync                    # app + Pocket-TTS
uv sync --extra validate   # additionally the Whisper line check (optional)
```

Pocket-TTS installs automatically except on Intel Macs (see below), where no
engine is available. The Whisper extra uses `mlx-whisper` on Apple Silicon
and `faster-whisper` elsewhere; the first check downloads the
`whisper-large-v3-turbo` weights (~1.5 GB) and caches them.

## Run

```bash
uv run main.py
```

1. **Open PDF** and pick a screenplay.
2. Assign voices in the **Casting** panel. Settings underneath: beat-pause
   length, highlighting, **One voice reads the whole script**, **Skip
   characters' names**, and the Whisper check.
3. **Generate Audio**. A green arrow appears next to **Play** when it's ready.
4. **Play** — the current line highlights and auto-scrolls. Click any line to
   jump there.
5. **Export MP3** when you're happy.

Everything the app remembers lives in `cache/` (`settings.json`,
`session.json`, `last_run.wav`); delete the folder to start fresh.

### Cloning a voice

Put a clean, mono `.wav` of 10–20 seconds in `assets/voices/`. Pocket-TTS
reproduces the speaker *and* the recording quality, so use a quiet room and
no music. The file name (minus `.wav`) becomes the voice's label.

## PyTorch CPU gotchas

- On Linux/Windows, plain `pip install torch` pulls the multi-GB CUDA build.
  This project lists `torch` as an explicit dependency so that
  `[tool.uv.sources]` can route it to PyTorch's CPU index — uv applies sources
  only to *direct* dependencies, so relying on pocket-tts to pull torch in
  transitively would silently get you the CUDA wheels. If you install
  manually, use
  `pip install torch --index-url https://download.pytorch.org/whl/cpu`.
- PyPI's default `torch` wheel on macOS **is** the CPU build, and
  download.pytorch.org ships no macOS wheels, so the override is scoped to
  Linux/Windows only.
- **Intel Macs cannot run Pocket-TTS at all** — torch dropped Intel-Mac wheels
  at 2.2, and pocket-tts needs 2.5+ (older torch generates corrupted audio).
  Both `pocket-tts` and `torch` carry a marker that skips them there; the app
  opens but reports that no voices are available.

## Testing

```bash
uv run pytest                                          # unit tests (no model needed)
uv run python scripts/smoke_test.py my_script.pdf      # parser + pipeline on a real PDF
```

`tests/test_audio_builder.py` pins down the guarantees above — one synth
call per wrapped dialogue block, names spoken or skipped per the setting,
INT./EXT. expansion, validation retries.

## Packaging

To ship a double-clickable app, use PyInstaller:

```bash
uv run --with pyinstaller pyinstaller --windowed --name "Screenplay Reader" main.py
```

The bundle lands in `dist/`.

## Credits & licenses

- **[Pocket-TTS](https://github.com/kyutai-labs/pocket-tts)** by Kyutai Labs —
  MIT license. Lightweight CPU text-to-speech and voice cloning.
- **[mlx-whisper](https://github.com/ml-explore/mlx-examples)** (MIT) /
  **[faster-whisper](https://github.com/SYSTRAN/faster-whisper)** (MIT) —
  optional local transcription for the line check, running OpenAI's Whisper
  weights (MIT).
- [pdfplumber](https://github.com/jsvine/pdfplumber) (MIT),
  [pydub](https://github.com/jiaaro/pydub) (MIT),
  [PySide6](https://doc.qt.io/qtforpython/) (LGPL), ffmpeg.
