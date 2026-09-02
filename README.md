# Screenplay Reader

A local desktop app that turns PDF screenplays into an interactive audio
experience: cast a voice for every character (plus a Narrator for action
lines), listen with live line-by-line highlighting, and export the result
as an MP3.

Open source, for **non-commercial use** (see Credits & licenses below).

## Features

- **PDF parsing** — detects scene headings, action, character cues, dialogue,
  and parentheticals by their indentation, the way screenplay formatting
  encodes them. Speaking characters are extracted automatically.
- **Screenplay-aware** — `(CONT'D)`, `(V.O.)`, `(O.S.)` and delivery
  directions like `(sarcastically)` are never vocalized. `(beat)` and
  `(pause)` become an adjustable 1–5 s pause, optionally colored with subtle
  filler noises from `assets/fillers/`.
- **Voice casting** — assign any available voice to each character and the
  Narrator.
- **Live highlighting** — the script scrolls and highlights the line being
  spoken (toggleable).
- **MP3 export** — save the stitched performance locally.

## TTS engines

| Engine | Runs | Notes |
| --- | --- | --- |
| **Pocket-TTS** | locally, on CPU | ~20 voices. Needs `torch>=2.5` (CPU build is fine — see gotchas) |
| **Fish-Reader** | Fish Audio cloud API | Any voice on fish.audio; needs an API key |

## Setup

Requires [uv](https://docs.astral.sh/uv/) and [ffmpeg](https://ffmpeg.org)
(`brew install ffmpeg` on macOS).

```bash
uv sync                    # core app + Fish-Reader
uv sync --extra pocket     # additionally Pocket-TTS (not on Intel Macs, see below)
```

Configure Fish-Reader:

```bash
cp config.example.py config.py
# then edit config.py: paste your API key and voice IDs (e.g. Adrian)
```

`config.py` is git-ignored — your key stays on your machine.

## Run

```bash
uv run main.py
```

1. **Open PDF** and pick a screenplay.
2. Assign voices in the **Casting** panel; set the beat-pause length.
3. **Generate Audio** (Fish lines bill API credits once — clips are cached in
   `cache/fish/`, so regenerating is free for unchanged lines).
4. **Play** — the current line highlights and auto-scrolls.
5. **Export MP3** when you're happy.

## PyTorch CPU gotchas

- PyPI's default `torch` wheel on macOS **is** the CPU build — nothing extra
  to do.
- On Linux/Windows, plain `pip install torch` pulls the multi-GB CUDA build.
  This project's lockfile pins the CPU index instead; if you install
  manually, use `pip install torch --index-url https://download.pytorch.org/whl/cpu`.
- **Intel Macs cannot run Pocket-TTS at all** — torch dropped Intel-Mac
  wheels at 2.2, and pocket-tts needs 2.5+ (older torch generates corrupted
  audio). The app detects this and offers Fish-Reader only.

## Testing

```bash
uv run python scripts/smoke_test.py   # parser + audio pipeline, no API calls
```

## Packaging

To ship a double-clickable app, use PyInstaller:

```bash
uv run --with pyinstaller pyinstaller --windowed --name "Screenplay Reader" main.py
```

The bundle lands in `dist/`. Ship `config.example.py` alongside it, never a
filled-in `config.py`.

## Credits & licenses

- **[Pocket-TTS](https://github.com/kyutai-labs/pocket-tts)** by Kyutai Labs —
  MIT license. Lightweight CPU text-to-speech.
- **[Fish Audio](https://fish.audio)** — the Fish-Reader engine uses their
  cloud API; their open-source
  [fish-speech](https://github.com/fishaudio/fish-speech) models are under the
  Fish Audio Research License (**non-commercial**).
- [pdfplumber](https://github.com/jsvine/pdfplumber) (MIT),
  [pydub](https://github.com/jiaaro/pydub) (MIT),
  [PySide6](https://doc.qt.io/qtforpython/) (LGPL), ffmpeg.
