from __future__ import annotations

import bisect
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QUrl, Signal
from PySide6.QtGui import QAction, QColor, QFont, QTextCharFormat, QTextCursor
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDockWidget,
    QFileDialog,
    QFormLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QScrollArea,
    QSlider,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..audio_builder import Cue, GenerationCancelled, build_audio, export_mp3
from ..engines import load_engines
from ..parser import Screenplay, parse_screenplay

CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "cache"

HIGHLIGHT = QColor("#ffe9a8")

CREDITS_HTML = """
<h3>Screenplay Reader</h3>
<p>Open-source, for non-commercial use. Built on the shoulders of:</p>
<ul>
<li><b>Pocket-TTS</b> — Kyutai Labs' pocket-sized CPU text-to-speech.
    MIT license. <a href='https://github.com/kyutai-labs/pocket-tts'>github.com/kyutai-labs/pocket-tts</a></li>
<li><b>Fish-Reader</b> — voice synthesis by Fish Audio
    (<a href='https://fish.audio'>fish.audio</a>); local models available as
    <a href='https://github.com/fishaudio/fish-speech'>fish-speech</a>,
    Fish Audio Research License (non-commercial).</li>
<li>pdfplumber (MIT) — screenplay PDF parsing</li>
<li>pydub (MIT) + ffmpeg — audio stitching and MP3 export</li>
<li>PySide6 / Qt (LGPL) — this interface</li>
</ul>
"""


class GenerationWorker(QThread):
    progressed = Signal(int, int, str)
    finished_ok = Signal(object, object)  # AudioSegment, list[Cue]
    failed = Signal(str)

    def __init__(self, elements, voice_for, beat_seconds):
        super().__init__()
        self._args = (elements, voice_for, beat_seconds)
        self.cancel = False

    def run(self):
        elements, voice_for, beat_seconds = self._args
        try:
            audio, cues = build_audio(
                elements,
                voice_for,
                beat_seconds,
                progress=lambda d, t, m: self.progressed.emit(d, t, m),
                cancelled=lambda: self.cancel,
            )
            self.finished_ok.emit(audio, cues)
        except GenerationCancelled:
            self.failed.emit("Cancelled.")
        except Exception as e:  # surfaced in the status bar, not lost in a thread
            self.failed.emit(str(e))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Screenplay Reader")
        self.resize(1100, 780)

        self.engines = load_engines()
        self.screenplay: Screenplay | None = None
        self.element_spans: list[tuple[int, int]] = []  # text positions per element
        self.cues: list[Cue] = []
        self.audio = None
        self.worker: GenerationWorker | None = None
        self.voice_combos: dict[str, QComboBox] = {}  # "" key = Narrator

        self._build_toolbar()
        self._build_script_view()
        self._build_side_panel()
        self._build_player()
        self._report_engines()

    # ---------- UI construction ----------

    def _build_toolbar(self):
        tb = self.addToolBar("Main")
        tb.setMovable(False)

        def action(text, slot, enabled=True):
            a = QAction(text, self)
            a.triggered.connect(slot)
            a.setEnabled(enabled)
            tb.addAction(a)
            return a

        self.open_action = action("Open PDF", self.open_pdf)
        self.generate_action = action("Generate Audio", self.generate, enabled=False)
        self.play_action = action("Play", self.toggle_play, enabled=False)
        self.stop_action = action("Stop", self.stop_playback, enabled=False)
        self.export_action = action("Export MP3", self.export, enabled=False)
        action("Credits", self.show_credits)

        self.progress = QProgressBar()
        self.progress.setMaximumWidth(220)
        self.progress.hide()
        self.statusBar().addPermanentWidget(self.progress)

    def _build_script_view(self):
        self.script_view = QTextEdit()
        self.script_view.setReadOnly(True)
        font = QFont("Courier New", 13)
        font.setStyleHint(QFont.StyleHint.Monospace)
        self.script_view.setFont(font)
        self.setCentralWidget(self.script_view)

    def _build_side_panel(self):
        dock = QDockWidget("Casting", self)
        dock.setFeatures(QDockWidget.DockWidgetFeature.NoDockWidgetFeatures)
        panel = QWidget()
        layout = QVBoxLayout(panel)

        self.cast_form_host = QScrollArea()
        self.cast_form_host.setWidgetResizable(True)
        layout.addWidget(self.cast_form_host, stretch=1)

        self.beat_label = QLabel()
        self.beat_slider = QSlider(Qt.Orientation.Horizontal)
        self.beat_slider.setRange(1, 5)
        self.beat_slider.setValue(2)
        self.beat_slider.valueChanged.connect(
            lambda v: self.beat_label.setText(f"Beat pause: {v} s")
        )
        self.beat_label.setText("Beat pause: 2 s")
        layout.addWidget(self.beat_label)
        layout.addWidget(self.beat_slider)

        self.highlight_toggle = QCheckBox("Highlight lines during playback")
        self.highlight_toggle.setChecked(True)
        self.highlight_toggle.toggled.connect(
            lambda on: on or self.script_view.setExtraSelections([])
        )
        layout.addWidget(self.highlight_toggle)

        dock.setWidget(panel)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, dock)

    def _build_player(self):
        self.player = QMediaPlayer()
        self.audio_out = QAudioOutput()
        self.player.setAudioOutput(self.audio_out)
        self.player.positionChanged.connect(self._on_position)
        self.player.playbackStateChanged.connect(self._on_playback_state)

    def _report_engines(self):
        notes = []
        for e in self.engines:
            ok, why = e.available()
            if not ok:
                notes.append(f"{e.name} unavailable: {why}")
        self.statusBar().showMessage(" | ".join(notes) or "Ready", 15000)

    # ---------- voice options ----------

    def _voice_options(self) -> list[tuple[str, tuple]]:
        opts = []
        for engine in self.engines:
            if engine.available()[0]:
                for v in engine.voices():
                    opts.append((f"{engine.name}: {v}", (engine, v)))
        return opts

    def _make_combo(self, opts, index) -> QComboBox:
        combo = QComboBox()
        for label, _ in opts:
            combo.addItem(label)
        combo.setCurrentIndex(index % len(opts) if opts else 0)
        return combo

    def _rebuild_casting(self):
        opts = self._voice_options()
        form_widget = QWidget()
        form = QFormLayout(form_widget)
        self.voice_combos = {}

        self.voice_combos[""] = self._make_combo(opts, 0)
        form.addRow("Narrator", self.voice_combos[""])
        for i, name in enumerate(self.screenplay.characters):
            combo = self._make_combo(opts, i + 1)
            self.voice_combos[name] = combo
            form.addRow(name.title(), combo)
        self.cast_form_host.setWidget(form_widget)

    def _casting(self) -> dict[str, tuple]:
        opts = self._voice_options()
        return {name: opts[c.currentIndex()][1] for name, c in self.voice_combos.items() if opts}

    # ---------- script display ----------

    INDENTS = {"scene": 0, "action": 0, "character": 22, "dialogue": 10, "parenthetical": 16}

    def _render_script(self):
        self.script_view.clear()
        self.element_spans = []
        cursor = QTextCursor(self.script_view.document())

        bold = QTextCharFormat()
        bold.setFontWeight(QFont.Weight.Bold)
        gray = QTextCharFormat()
        gray.setForeground(QColor("#777777"))
        plain = QTextCharFormat()

        for el in self.screenplay.elements:
            fmt = bold if el.kind in ("scene", "character") else gray if el.kind == "parenthetical" else plain
            pad = " " * self.INDENTS[el.kind]
            start = cursor.position()
            lines = el.lines or [el.text]
            cursor.insertText("\n".join(pad + line for line in lines) + "\n", fmt)
            self.element_spans.append((start, cursor.position() - 1))
            if el.kind in ("scene", "action", "dialogue"):
                cursor.insertText("\n", plain)

    # ---------- actions ----------

    def open_pdf(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open screenplay", "", "PDF files (*.pdf)")
        if not path:
            return
        try:
            self.screenplay = parse_screenplay(path)
        except Exception as e:
            QMessageBox.critical(self, "Parse error", str(e))
            return
        if not self.screenplay.elements:
            QMessageBox.warning(self, "Nothing found", "No screenplay content detected in this PDF.")
            return
        self._render_script()
        self._rebuild_casting()
        self.generate_action.setEnabled(bool(self._voice_options()))
        n = len(self.screenplay.characters)
        self.statusBar().showMessage(
            f"Loaded {Path(path).name}: {len(self.screenplay.elements)} elements, {n} speaking characters"
        )
        if not self._voice_options():
            QMessageBox.warning(
                self, "No voices available",
                "No TTS engine is usable.\n\n"
                + "\n".join(f"{e.name}: {e.available()[1]}" for e in self.engines),
            )

    def generate(self):
        if self.worker and self.worker.isRunning():
            self.worker.cancel = True
            self.generate_action.setText("Generate Audio")
            return

        casting = self._casting()
        narrator = casting.get("")
        spoken = [e for e in self.screenplay.elements if e.kind in ("scene", "action", "dialogue")]
        if (
            QMessageBox.question(
                self, "Generate audio",
                f"Synthesize {len(spoken)} passages? Fish-Reader lines use API credits "
                "(already-generated lines come from the local cache).",
            )
            != QMessageBox.StandardButton.Yes
        ):
            return

        def voice_for(el):
            if el.kind in ("scene", "action"):
                return narrator
            return casting.get(el.character, narrator)

        self.stop_playback()
        self.worker = GenerationWorker(
            self.screenplay.elements, voice_for, float(self.beat_slider.value())
        )
        self.worker.progressed.connect(self._on_progress)
        self.worker.finished_ok.connect(self._on_generated)
        self.worker.failed.connect(self._on_failed)
        self.generate_action.setText("Cancel")
        self.progress.show()
        self.worker.start()

    def _on_progress(self, done, total, msg):
        self.progress.setMaximum(total)
        self.progress.setValue(done)
        self.statusBar().showMessage(msg)

    def _on_generated(self, audio, cues):
        self.audio = audio
        self.cues = cues
        self.generate_action.setText("Generate Audio")
        self.progress.hide()

        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        wav_path = CACHE_DIR / "last_run.wav"
        self.player.setSource(QUrl())  # release the old file before overwriting
        audio.export(wav_path, format="wav")
        self.player.setSource(QUrl.fromLocalFile(str(wav_path)))

        for a in (self.play_action, self.stop_action, self.export_action):
            a.setEnabled(True)
        mins = len(audio) // 60000
        self.statusBar().showMessage(f"Audio ready: {mins}m {len(audio) % 60000 // 1000}s")

    def _on_failed(self, msg):
        self.generate_action.setText("Generate Audio")
        self.progress.hide()
        self.statusBar().showMessage(f"Generation failed: {msg}")
        if msg != "Cancelled.":
            QMessageBox.critical(self, "Generation failed", msg)

    # ---------- playback & highlighting ----------

    def toggle_play(self):
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def stop_playback(self):
        self.player.stop()
        self.script_view.setExtraSelections([])

    def _on_playback_state(self, state):
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        self.play_action.setText("Pause" if playing else "Play")

    def _on_position(self, ms):
        if not self.cues or not self.highlight_toggle.isChecked():
            return
        i = bisect.bisect_right([c.start_ms for c in self.cues], ms) - 1
        if i < 0 or ms >= self.cues[i].end_ms:
            return
        start, end = self.element_spans[self.cues[i].element_index]
        sel = QTextEdit.ExtraSelection()
        sel.cursor = QTextCursor(self.script_view.document())
        sel.cursor.setPosition(start)
        sel.cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        sel.format.setBackground(HIGHLIGHT)
        self.script_view.setExtraSelections([sel])

        scroll_cursor = QTextCursor(self.script_view.document())
        scroll_cursor.setPosition(start)
        self.script_view.setTextCursor(scroll_cursor)
        self.script_view.ensureCursorVisible()

    # ---------- export ----------

    def export(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export MP3", "screenplay.mp3", "MP3 (*.mp3)")
        if path:
            export_mp3(self.audio, path)
            self.statusBar().showMessage(f"Exported {path}")

    def show_credits(self):
        QMessageBox.about(self, "Credits", CREDITS_HTML)
