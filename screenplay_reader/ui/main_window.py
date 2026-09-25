from __future__ import annotations

import bisect
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QAction,
    QBrush,
    QColor,
    QFont,
    QIcon,
    QMouseEvent,
    QPainter,
    QPen,
    QPixmap,
    QPolygonF,
    QTextCharFormat,
    QTextCursor,
    QTextOption,
)
from PySide6.QtCore import QPointF
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDockWidget,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSlider,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..audio_builder import Cue, GenerationCancelled, build_audio, export_mp3
from ..engines import load_engines
from ..parser import Screenplay, parse_screenplay
from ..settings import LAST_AUDIO_PATH, Session, Settings
from ..validate import Transcriber

POSITION_SAVE_INTERVAL_MS = 3000
READY_GREEN = QColor("#2ecc40")


def _play_icon(color: QColor, size: int = 16) -> QIcon:
    """A filled triangle — shown in green next to Play once audio is ready."""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QBrush(color))
    painter.setPen(QPen(color.darker(120), 1))
    tri = QPolygonF([QPointF(3, 2), QPointF(size - 2, size / 2), QPointF(3, size - 2)])
    painter.drawPolygon(tri)
    painter.end()
    return QIcon(pm)

HIGHLIGHT = QColor("#ffe9a8")

CREDITS_HTML = """
<h3>Screenplay Reader</h3>
<p>Open-source, for non-commercial use. Built on the shoulders of:</p>
<ul>
<li><b>Pocket-TTS</b> — Kyutai Labs' pocket-sized CPU text-to-speech.
    MIT license. <a href='https://github.com/kyutai-labs/pocket-tts'>github.com/kyutai-labs/pocket-tts</a></li>
<li><b>OmniVoice</b> — local voice cloning through MLX-Audio.
    <a href='https://github.com/Blaizzy/mlx-audio'>github.com/Blaizzy/mlx-audio</a></li>
<li>pdfplumber (MIT) — screenplay PDF parsing</li>
<li>pydub (MIT) + ffmpeg — audio stitching and MP3 export</li>
<li>PySide6 / Qt (LGPL) — this interface</li>
</ul>
"""


class ClickableScript(QTextEdit):
    """Read-only script view; clicking a line asks the player to jump there."""

    clicked_at = Signal(int)  # document position

    def mousePressEvent(self, event: QMouseEvent):
        super().mousePressEvent(event)
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked_at.emit(self.cursorForPosition(event.pos()).position())


class ParseWorker(QThread):
    finished_ok = Signal(object, str)
    failed = Signal(str)

    def __init__(self, path: str):
        super().__init__()
        self.path = path

    def run(self):
        try:
            self.finished_ok.emit(parse_screenplay(self.path), self.path)
        except Exception as e:
            self.failed.emit(str(e))


class GenerationWorker(QThread):
    progressed = Signal(int, int, str)
    finished_ok = Signal(object)  # BuildResult
    failed = Signal(str)

    def __init__(self, elements, voice_for, beat_seconds, speak_names, checker):
        super().__init__()
        self._args = (elements, voice_for, beat_seconds, speak_names, checker)
        self.cancel = False

    def run(self):
        elements, voice_for, beat_seconds, speak_names, checker = self._args
        try:
            result = build_audio(
                elements,
                voice_for,
                beat_seconds,
                speak_character_names=speak_names,
                checker=checker,
                progress=lambda d, t, m: self.progressed.emit(d, t, m),
                cancelled=lambda: self.cancel,
            )
            self.finished_ok.emit(result)
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
        self.settings = Settings.load()
        self.transcriber = Transcriber()
        self.screenplay: Screenplay | None = None
        self.pdf_path: str = ""
        self.element_spans: list[tuple[int, int]] = []  # text positions per element
        self.cues: list[Cue] = []
        self.audio_ready = False
        self.parse_worker: ParseWorker | None = None
        self.worker: GenerationWorker | None = None
        self.voice_combos: dict[str, QComboBox] = {}  # "" key = Narrator
        self._pending_casting: dict[str, str] = {}  # restored from last session
        self.casting_dialog: QDialog | None = None
        self.dialog_cast_form_host: QScrollArea | None = None
        self.dialog_single_voice_toggle: QCheckBox | None = None
        self.dialog_voice_combos: dict[str, QComboBox] = {}
        self._last_saved_position = -1
        self._last_highlighted_element = None

        self._build_toolbar()
        self._build_script_view()
        self._build_side_panel()
        self._build_player()
        self._build_landing_page()
        self.page_stack = QStackedWidget()
        self.page_stack.addWidget(self.landing_page)
        self.page_stack.addWidget(self.transport_widget)
        self._apply_theme(self.settings.theme)
        self._report_engines()
        self.setCentralWidget(self.page_stack)
        self.page_stack.setCurrentWidget(self.landing_page)
        self.toolbar.hide()
        self.settings_dock.hide()

    # ---------- UI construction ----------

    def _build_toolbar(self):
        tb = self.addToolBar("Main")
        self.toolbar = tb
        tb.setMovable(False)
        # Keep button labels visible once Play gains its ready icon (the
        # default toolbar style would show the icon alone).
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)

        def action(text, slot, enabled=True):
            a = QAction(text, self)
            a.triggered.connect(slot)
            a.setEnabled(enabled)
            tb.addAction(a)
            return a

        self.open_action = action("Open PDF", self.open_pdf)
        self.generate_action = action("Generate Audio", self.generate, enabled=False)
        self.play_action = action("Play", self.toggle_play, enabled=False)
        self._ready_icon = _play_icon(READY_GREEN)
        self._set_ready_indicator(False)
        self.stop_action = action("Pause", self.pause_playback, enabled=False)
        self.export_action = action("Export MP3", self.export, enabled=False)
        self.settings_toggle_action = action("Settings", self.toggle_settings_panel)
        action("Credits", self.show_credits)

        self.progress = QProgressBar()
        self.progress.setMaximumWidth(220)
        self.progress.hide()
        self.statusBar().addPermanentWidget(self.progress)

    def _build_script_view(self):
        self.script_view = ClickableScript()
        self.script_view.setReadOnly(True)
        self.script_view.setToolTip("Click any line to play from there")
        self.script_view.clicked_at.connect(self._on_script_clicked)
        font = QFont("Courier New", 24)
        font.setStyleHint(QFont.StyleHint.Monospace)
        self.script_view.setFont(font)
        self.script_view.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.script_view.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.script_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.script_view.setFrameStyle(QTextEdit.Shape.NoFrame)
        self.script_view.setDocumentTitle("Script")
        self.script_view.setMinimumWidth(0)
        self.script_view.setMaximumWidth(16777215)

        self.transport_widget = QWidget()
        self.transport_widget.setObjectName("transportWidget")
        transport_layout = QVBoxLayout(self.transport_widget)
        transport_layout.setContentsMargins(8, 8, 8, 8)
        transport_layout.setSpacing(10)

        self.transport_bar = QWidget()
        self.transport_bar.setObjectName("transportBar")
        transport_bar_layout = QHBoxLayout(self.transport_bar)
        transport_bar_layout.setContentsMargins(0, 8, 0, 0)
        transport_bar_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.rewind_button = QPushButton("⏮")
        self.rewind_button.setToolTip("Rewind 10s")
        self.rewind_button.clicked.connect(self.rewind_playback)
        self.rewind_button.setFixedSize(52, 52)

        self.play_button = QPushButton("▶")
        self.play_button.setToolTip("Play / Pause")
        self.play_button.clicked.connect(self.toggle_play)
        self.play_button.setFixedSize(60, 60)

        self.forward_button = QPushButton("⏭")
        self.forward_button.setToolTip("Forward 10s")
        self.forward_button.clicked.connect(self.forward_playback)
        self.forward_button.setFixedSize(52, 52)

        transport_bar_layout.addWidget(self.rewind_button)
        transport_bar_layout.addWidget(self.play_button)
        transport_bar_layout.addWidget(self.forward_button)
        self.transport_bar.setLayout(transport_bar_layout)
        self.script_stage = QWidget()
        self.script_stage.setObjectName("scriptStage")
        script_stage_layout = QHBoxLayout(self.script_stage)
        script_stage_layout.setContentsMargins(0, 0, 0, 0)
        script_stage_layout.addWidget(self.script_view)

        transport_layout.addWidget(self.script_stage, 1)
        transport_layout.addWidget(self.transport_bar)

        self.setCentralWidget(self.transport_widget)

    def _build_landing_page(self):
        self.landing_page = QWidget()
        self.landing_page.setObjectName("landingPage")
        layout = QVBoxLayout(self.landing_page)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(18)

        title = QLabel("screenplay reader")
        title.setObjectName("landingTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_font = QFont("Georgia", 32)
        title_font.setBold(True)
        title.setFont(title_font)
        layout.addWidget(title)

        actions = QHBoxLayout()
        actions.setSpacing(16)
        new_script = QPushButton("New Script")
        new_script.clicked.connect(self.open_pdf)
        previous_script = QPushButton("Previous Script")
        previous_script.setEnabled(Session.load() is not None)
        previous_script.clicked.connect(self.open_previous)
        actions.addWidget(new_script)
        actions.addWidget(previous_script)
        layout.addLayout(actions)

    def _build_side_panel(self):
        self.settings_dock = QDockWidget("Settings", self)
        self.settings_dock.setFeatures(QDockWidget.DockWidgetFeature.NoDockWidgetFeatures)
        panel = QWidget()
        layout = QVBoxLayout(panel)

        self.cast_form_host = QScrollArea()
        self.cast_form_host.setWidgetResizable(True)
        layout.addWidget(self.cast_form_host, stretch=1)

        settings_title = QLabel("<b>Settings</b>")
        layout.addWidget(settings_title)

        self.beat_label = QLabel()
        self.beat_slider = QSlider(Qt.Orientation.Horizontal)
        self.beat_slider.setRange(1, 5)
        self.beat_slider.setValue(int(self.settings.beat_seconds))
        self.beat_slider.valueChanged.connect(self._on_beat_changed)
        self.beat_label.setText(f"Beat pause: {self.beat_slider.value()} s")
        layout.addWidget(self.beat_label)
        layout.addWidget(self.beat_slider)

        self.highlight_toggle = QCheckBox("Highlight lines during playback")
        self.highlight_toggle.setChecked(self.settings.highlight)
        self.highlight_toggle.toggled.connect(self._on_highlight_toggled)
        layout.addWidget(self.highlight_toggle)

        self.single_voice_toggle = QCheckBox("One voice reads the whole script")
        self.single_voice_toggle.setToolTip(
            "The Narrator's voice reads every line, including all characters' dialogue.\n"
            "Per-character casting is hidden. Takes effect on the next Generate."
        )
        self.single_voice_toggle.setChecked(self.settings.single_voice)
        self.single_voice_toggle.toggled.connect(self._on_single_voice_toggled)
        layout.addWidget(self.single_voice_toggle)

        self.skip_names_toggle = QCheckBox("Skip characters' names during playback")
        self.skip_names_toggle.setToolTip(
            "Off: the narrator says \"Ethan.\" before each of Ethan's lines.\n"
            "On: only the dialogue is spoken; the voice change marks the speaker.\n"
            "Names inside action lines are always read. Takes effect on the next Generate."
        )
        self.skip_names_toggle.setChecked(self.settings.skip_character_names)
        self.skip_names_toggle.toggled.connect(self._on_skip_names_toggled)
        layout.addWidget(self.skip_names_toggle)

        ok, why = self.transcriber.available()
        label = "Check every line with Whisper (slower)" if ok else "Check every line with Whisper (install validate extra)"
        self.validate_toggle = QCheckBox(label)
        self.validate_toggle.setEnabled(ok)
        self.validate_toggle.setToolTip(
            "Transcribes each generated line locally and regenerates any that don't match the script."
            if ok else why
        )
        self.validate_toggle.setChecked(self.settings.validate_audio and ok)
        self.validate_toggle.toggled.connect(self._on_validate_toggled)
        layout.addWidget(self.validate_toggle)

        self.theme_buttons = QWidget()
        theme_layout = QHBoxLayout(self.theme_buttons)
        theme_layout.setContentsMargins(0, 0, 0, 0)
        theme_layout.setSpacing(8)

        self.light_mode_btn = QPushButton("Light Mode")
        self.light_mode_btn.clicked.connect(lambda: self._apply_theme("light"))
        self.dark_mode_btn = QPushButton("Dark Mode")
        self.dark_mode_btn.clicked.connect(lambda: self._apply_theme("dark"))
        theme_layout.addWidget(self.light_mode_btn)
        theme_layout.addWidget(self.dark_mode_btn)
        layout.addWidget(self.theme_buttons)

        self.settings_dock.setWidget(panel)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.settings_dock)
        self.settings_dock.hide()

    # ---------- settings ----------

    def _on_beat_changed(self, v):
        self.beat_label.setText(f"Beat pause: {v} s")
        self.settings.beat_seconds = float(v)
        self.settings.save()

    def _on_highlight_toggled(self, on):
        self.settings.highlight = on
        self.settings.save()
        if not on:
            self.script_view.setExtraSelections([])

    def _on_single_voice_toggled(self, on):
        self.settings.single_voice = on
        self.settings.save()
        for toggle in (self.single_voice_toggle, self.dialog_single_voice_toggle):
            if toggle is not None and toggle.isChecked() != on:
                toggle.blockSignals(True)
                toggle.setChecked(on)
                toggle.blockSignals(False)
        if self.screenplay:
            self._pending_casting = self._casting_labels()  # keep choices while hidden
            self._rebuild_casting()
        if self.audio_ready:
            self.statusBar().showMessage("Voice setting changed — Generate Audio again to apply it.")

    def _on_skip_names_toggled(self, on):
        self.settings.skip_character_names = on
        self.settings.save()
        if self.audio_ready:
            self.statusBar().showMessage("Names setting changed — Generate Audio again to apply it.")

    def _on_validate_toggled(self, on):
        self.settings.validate_audio = on
        self.settings.save()

    def _build_player(self):
        self.player = QMediaPlayer()
        self.audio_out = QAudioOutput()
        self.player.setAudioOutput(self.audio_out)
        self.player.positionChanged.connect(self._on_position)
        self.player.playbackStateChanged.connect(self._on_playback_state)
        self._position_timer = QTimer(self)
        self._position_timer.setInterval(POSITION_SAVE_INTERVAL_MS)
        self._position_timer.timeout.connect(self._save_position)

    def _set_ready_indicator(self, ready: bool):
        """Green arrow next to Play = there is audio to play."""
        self.play_action.setIcon(self._ready_icon if ready else QIcon())
        self.play_action.setToolTip("Audio is ready — press Play" if ready else "Generate audio first")

    def _apply_theme(self, mode: str):
        self.settings.theme = mode
        self.settings.save()

        if mode == "dark":
            bg = "#1C1C1E"
            fg = "#D4D4D2"
            panel = "#2A2A2D"
            panel_alt = "#202022"
            highlight = "#456637"
            accent = "#A3B899"
            border = "#444444"
            input_bg = "#2B2B2E"
            toolbar_bg = "#1C1C1E"
            button_bg = "#2A2A2D"
            script_bg = "#1C1C1E"
            script_fg = "#D4D4D2"
            script_pad = "padding: 20px 190px 20px 190px;"
        else:
            bg = "#F1E1C9"
            fg = "#34281B"
            panel = "#E9D9B9"
            panel_alt = "#F9F2E7"
            highlight = "#C8ACC7"
            accent = "#8A4F66"
            border = "#D7C7A8"
            input_bg = "#F6EDE0"
            toolbar_bg = "#F1E1C9"
            button_bg = "#F8F0E6"
            script_bg = "#F1E1C9"
            script_fg = "#34281B"
            script_pad = "padding: 20px 190px 20px 190px;"

        self.setStyleSheet(
            f"QMainWindow {{ background: {bg}; color: {fg}; }}"
            f"QWidget {{ background: {bg}; color: {fg}; }}"
            f"QDockWidget {{ background: {panel}; color: {fg}; border: 1px solid {border}; }}"
            f"QDockWidget::title {{ background: {panel_alt}; color: {fg}; padding: 6px; }}"
            f"QLabel {{ color: {fg}; }}"
            f"QCheckBox {{ color: {fg}; spacing: 8px; }}"
            f"QSlider::groove:horizontal {{ background: {border}; height: 6px; border-radius: 3px; }}"
            f"QSlider::handle:horizontal {{ background: {accent}; border: 1px solid {accent}; width: 14px; margin: -4px 0; border-radius: 7px; }}"
            f"QComboBox {{ background: {input_bg}; color: {fg}; border: 1px solid {border}; padding: 4px; }}"
            f"QAbstractScrollArea {{ background: {bg}; }}"
            f"QTextEdit {{ background: {script_bg}; color: {script_fg}; border: none; {script_pad} }}"
            f"QPushButton {{ background: {button_bg}; color: {fg}; border: 1px solid {border}; border-radius: 12px; padding: 6px 14px; }}"
            f"QToolBar {{ background: {toolbar_bg}; border: none; spacing: 8px; }}"
            f"QToolButton {{ color: {fg}; background: {button_bg}; border: 1px solid {border}; border-radius: 10px; padding: 3px 10px; }}"
            f"QProgressBar {{ background: {panel}; border: 1px solid {border}; border-radius: 6px; }}"
            f"QProgressBar::chunk {{ background: {accent}; border-radius: 5px; }}"
            f"QWidget#transportWidget {{ background: {bg}; }}"
            f"QWidget#scriptStage {{ background: {bg}; }}"
            f"QWidget#landingPage {{ background: {bg}; }}"
            f"QLabel#landingTitle {{ color: {accent}; }}"
            f"QWidget#transportBar {{ background: transparent; }}"
            f"QPushButton#transportButton {{ background: {button_bg}; color: {fg}; border: 1px solid {border}; border-radius: 26px; font-size: 20px; min-width: 52px; min-height: 52px; }}"
            f"QPushButton#transportButtonPrimary {{ background: {accent}; color: {fg}; border: 1px solid {accent}; border-radius: 30px; font-size: 22px; min-width: 60px; min-height: 60px; }}"
        )
        self.script_view.setStyleSheet(
            f"QTextEdit {{ background: {script_bg}; color: {script_fg}; border: none; {script_pad} }}"
        )
        self._highlight_color = QColor(highlight)
        self._accent_color = QColor(accent)

        if self.light_mode_btn is not None:
            self.light_mode_btn.setChecked(mode == "light")
            self.dark_mode_btn.setChecked(mode == "dark")

    def _report_engines(self):
        notes = []
        for e in self.engines:
            ok, why = e.available()
            if not ok:
                notes.append(f"{e.name} unavailable: {why}")
        self.statusBar().showMessage(" | ".join(notes) or "Ready", 15000)

    def toggle_settings_panel(self):
        if self.settings_dock.isVisible():
            self.settings_dock.hide()
        else:
            self.settings_dock.show()
            self.settings_dock.raise_()

    def _open_casting_panel(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Casting Panel")
        dialog.setModal(True)
        dialog.setMinimumSize(560, 520)
        dialog.resize(640, 680)

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(28, 24, 28, 22)
        layout.setSpacing(12)

        title = QLabel("Casting Panel")
        title.setObjectName("castingTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_font = QFont("Georgia", 30)
        title_font.setBold(True)
        title.setFont(title_font)
        layout.addWidget(title)

        subtitle = QLabel(
            "Assign voices for each character or choose one voice to read the lines of dialogue below."
        )
        subtitle.setWordWrap(True)
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle.setObjectName("castingSubtitle")
        layout.addWidget(subtitle)

        self.dialog_single_voice_toggle = QCheckBox("One voice reads the whole script")
        self.dialog_single_voice_toggle.setChecked(self.settings.single_voice)
        self.dialog_single_voice_toggle.toggled.connect(self._on_single_voice_toggled)
        layout.addWidget(self.dialog_single_voice_toggle)

        self.dialog_cast_form_host = QScrollArea()
        self.dialog_cast_form_host.setWidgetResizable(True)
        layout.addWidget(self.dialog_cast_form_host, stretch=1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Continue")
        buttons.accepted.connect(dialog.accept)
        layout.addWidget(buttons)

        self.casting_dialog = dialog
        self._rebuild_casting()
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        if accepted:
            self._pending_casting = self._casting_labels(self.dialog_voice_combos)
            self._rebuild_casting()
        self.casting_dialog = None
        self.dialog_cast_form_host = None
        self.dialog_single_voice_toggle = None
        self.dialog_voice_combos = {}

    # ---------- voice options ----------

    def _voice_options(self) -> list[tuple[str, tuple]]:
        opts = []
        for engine in self.engines:
            if engine.available()[0]:
                for v in engine.voices():
                    opts.append((f"{engine.name}: {v}", (engine, v)))
        return opts

    def _make_combo(self, opts, index, preferred: str | None = None) -> QComboBox:
        combo = QComboBox()
        for label, _ in opts:
            combo.addItem(label)
        labels = [label for label, _ in opts]
        if preferred in labels:
            combo.setCurrentIndex(labels.index(preferred))
        else:
            combo.setCurrentIndex(index % len(opts) if opts else 0)
        return combo

    def _default_narrator_label(self) -> str | None:
        for engine in self.engines:
            pick = getattr(engine, "default_narrator", lambda: None)()
            if pick and engine.available()[0]:
                return f"{engine.name}: {pick}"
        return None

    def _rebuild_casting(self):
        opts = self._voice_options()
        self.voice_combos = {}
        self.dialog_voice_combos = {}
        remembered = self._pending_casting

        narrator_pick = remembered.get("") or self._default_narrator_label()
        hosts = [self.cast_form_host]
        if self.dialog_cast_form_host is not None:
            hosts.append(self.dialog_cast_form_host)

        for host in hosts:
            form_widget = QWidget()
            form = QFormLayout(form_widget)
            narrator_combo = self._make_combo(opts, 0, narrator_pick)
            if host is self.cast_form_host:
                self.voice_combos[""] = narrator_combo
            else:
                self.dialog_voice_combos[""] = narrator_combo
            form.addRow("Narrator" if not self.settings.single_voice else "Voice", narrator_combo)
            if self.settings.single_voice:
                hint = QLabel("Reads every line, all characters included.")
                hint.setStyleSheet("color: #777;")
                form.addRow("", hint)
            else:
                # Characters skip the narrator's voice so they never sound like the reader.
                n_clones = 1 if narrator_pick else 0
                for i, name in enumerate(self.screenplay.characters):
                    combo = self._make_combo(opts, i + 1 + n_clones, remembered.get(name))
                    if host is self.cast_form_host:
                        self.voice_combos[name] = combo
                    else:
                        self.dialog_voice_combos[name] = combo
                    form.addRow(name.title(), combo)
            host.setWidget(form_widget)

    def _casting(self) -> dict[str, tuple]:
        opts = self._voice_options()
        return {name: opts[c.currentIndex()][1] for name, c in self.voice_combos.items() if opts}

    def _casting_labels(self, combos: dict[str, QComboBox] | None = None) -> dict[str, str]:
        combos = self.voice_combos if combos is None else combos
        return {name: c.currentText() for name, c in combos.items()}

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

        line_count = self.script_view.viewport().height() // max(1, self.script_view.fontMetrics().lineSpacing())
        cursor.insertText("\n" * max(1, line_count), plain)

    # ---------- actions ----------

    def open_pdf(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open screenplay", "", "PDF files (*.pdf)")
        if not path:
            return
        if self.parse_worker and self.parse_worker.isRunning():
            return
        self._pending_casting = {}
        self._show_workspace()
        self.open_action.setEnabled(False)
        self.statusBar().showMessage(f"Loading {Path(path).name}…")
        self.parse_worker = ParseWorker(path)
        self.parse_worker.finished_ok.connect(self._on_pdf_parsed)
        self.parse_worker.failed.connect(self._on_pdf_parse_failed)
        self.parse_worker.start()

    def _on_pdf_parsed(self, screenplay: Screenplay, path: str):
        self.open_action.setEnabled(True)
        self.parse_worker = None
        if not screenplay.elements:
            QMessageBox.warning(self, "Nothing found", "No screenplay content detected in this PDF.")
            return
        self._discard_audio()
        self.screenplay = screenplay
        self.pdf_path = path
        self._render_script()
        self._rebuild_casting()
        self._open_casting_panel()
        self.generate_action.setEnabled(bool(self._voice_options()))
        self.statusBar().showMessage(
            f"Loaded {Path(path).name}: {len(self.screenplay.elements)} elements, "
            f"{len(self.screenplay.characters)} speaking characters"
        )
        if not self._voice_options():
            QMessageBox.warning(
                self, "No voices available",
                "No TTS engine is usable.\n\n"
                + "\n".join(f"{e.name}: {e.available()[1]}" for e in self.engines),
            )

    def _on_pdf_parse_failed(self, message: str):
        self.open_action.setEnabled(True)
        self.parse_worker = None
        self.statusBar().showMessage("PDF loading failed")
        QMessageBox.critical(self, "Parse error", message)

    def open_previous(self):
        if not self._resume_last_session():
            QMessageBox.information(self, "No previous script", "There is no reusable previous screenplay session.")

    def _show_workspace(self):
        self.page_stack.setCurrentWidget(self.transport_widget)
        self.toolbar.show()

    def _load_pdf(self, path: str) -> bool:
        try:
            screenplay = parse_screenplay(path)
        except Exception as e:
            QMessageBox.critical(self, "Parse error", str(e))
            return False
        if not screenplay.elements:
            QMessageBox.warning(self, "Nothing found", "No screenplay content detected in this PDF.")
            return False
        self.screenplay = screenplay
        self.pdf_path = path
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
        return True

    def _discard_audio(self):
        """A different script is loaded; the cached audio no longer applies."""
        self.stop_playback()
        self.player.setSource(QUrl())
        self.cues = []
        self.audio_ready = False
        self._set_ready_indicator(False)
        for a in (self.play_action, self.stop_action, self.export_action):
            a.setEnabled(False)

    def generate(self):
        if self.worker and self.worker.isRunning():
            self.worker.cancel = True
            self.generate_action.setText("Generate Audio")
            return

        casting = self._casting()
        narrator = casting.get("")
        speak_names = not self.settings.skip_character_names
        spoken_kinds = ("scene", "action", "dialogue") + (("character",) if speak_names else ())
        spoken = [e for e in self.screenplay.elements if e.kind in spoken_kinds]
        checker = self.transcriber if self.settings.validate_audio and self.transcriber.available()[0] else None
        note = " Each line will be checked with Whisper; this takes longer." if checker else ""
        if (
            QMessageBox.question(
                self, "Generate audio",
                f"Synthesize {len(spoken)} passages?{note}",
            )
            != QMessageBox.StandardButton.Yes
        ):
            return

        single_voice = self.settings.single_voice

        def voice_for(el):
            if single_voice or el.kind in ("scene", "action", "character"):
                return narrator  # names are announced by the narrator, never the character
            return casting.get(el.character, narrator)

        self.stop_playback()
        self.worker = GenerationWorker(
            self.screenplay.elements, voice_for, float(self.beat_slider.value()), speak_names, checker
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

    def _on_generated(self, result):
        audio, self.cues = result.audio, result.cues
        self.generate_action.setText("Generate Audio")
        self.progress.hide()

        LAST_AUDIO_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.player.setSource(QUrl())  # release the old file before overwriting
        audio.export(LAST_AUDIO_PATH, format="wav")
        self.player.setSource(QUrl.fromLocalFile(str(LAST_AUDIO_PATH)))
        self.audio_ready = True
        self._set_ready_indicator(True)
        self._save_session(position_ms=0)

        for a in (self.play_action, self.stop_action, self.export_action):
            a.setEnabled(True)
        mins = len(audio) // 60000
        self.statusBar().showMessage(f"Audio ready: {mins}m {len(audio) % 60000 // 1000}s")
        if result.issues:
            self._report_issues(result.issues)

    def _report_issues(self, issues):
        worst = sorted(issues, key=lambda i: -i.error_rate)[:12]
        rows = "".join(
            f"<li><b>{int(i.error_rate * 100)}% off</b> — script: <i>{i.text}</i><br>"
            f"&nbsp;&nbsp;heard: <i>{i.transcript or '(nothing)'}</i></li>"
            for i in worst
        )
        more = f"<p>…and {len(issues) - len(worst)} more.</p>" if len(issues) > len(worst) else ""
        QMessageBox.warning(
            self, "Some lines didn't pass the Whisper check",
            f"<p>{len(issues)} line(s) still didn't match the script after 3 attempts. "
            f"The best take of each was kept. Click a line to listen and decide whether to "
            f"regenerate.</p><ul>{rows}</ul>{more}",
        )

    def _on_failed(self, msg):
        self.generate_action.setText("Generate Audio")
        self.progress.hide()
        self.statusBar().showMessage(f"Generation failed: {msg}")
        if msg != "Cancelled.":
            QMessageBox.critical(self, "Generation failed", msg)

    # ---------- playback & highlighting ----------

    def toggle_play(self):
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.pause_playback()
        else:
            self.player.play()

    def pause_playback(self):
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._save_session(self.player.position())
            self.player.pause()
            self.script_view.setExtraSelections([])
            self.statusBar().showMessage(f"Paused — resumed at {self.player.position() // 60000}m {self.player.position() % 60000 // 1000}s")

    def stop_playback(self):
        self.pause_playback()

    def _on_playback_state(self, state):
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        self.play_action.setText("Pause" if playing else "Play")
        self.play_button.setText("Ⅱ" if playing else "▶")
        if playing:
            self._position_timer.start()
        else:
            self._position_timer.stop()
            self._save_position()

    def _element_at(self, doc_pos: int) -> int | None:
        """Index of the element whose rendered text contains this document position."""
        if not self.element_spans:
            return None
        i = bisect.bisect_right([start for start, _ in self.element_spans], doc_pos) - 1
        return i if i >= 0 else None

    def _on_script_clicked(self, doc_pos: int):
        if not self.audio_ready or not self.cues:
            return
        i = self._element_at(doc_pos)
        if i is None:
            return
        # Jump to this element's cue, or the next spoken one (a delivery
        # parenthetical, or a name cue when names are skipped, has no audio).
        target = next((c for c in self.cues if c.element_index >= i), None)
        if target is None:
            return
        self._last_highlighted_element = i
        self.player.setPosition(target.start_ms)
        self._on_position(target.start_ms)
        if self.player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
            self.player.play()

    def _on_position(self, ms):
        if not self.cues or not self.highlight_toggle.isChecked():
            return
        i = bisect.bisect_right([c.start_ms for c in self.cues], ms) - 1
        if i < 0 or ms >= self.cues[i].end_ms:
            return
        self._last_highlighted_element = self.cues[i].element_index
        start, end = self.element_spans[self.cues[i].element_index]
        sel = QTextEdit.ExtraSelection()
        sel.cursor = QTextCursor(self.script_view.document())
        sel.cursor.setPosition(start)
        sel.cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        sel.format.setBackground(self._highlight_color)
        self.script_view.setExtraSelections([sel])

        scroll_cursor = QTextCursor(self.script_view.document())
        scroll_cursor.setPosition(start)
        self.script_view.setTextCursor(scroll_cursor)
        self.script_view.ensureCursorVisible()
        scrollbar = self.script_view.verticalScrollBar()
        rect = self.script_view.cursorRect(scroll_cursor)
        margin = 8
        scrollbar.setValue(scrollbar.value() + rect.top() - margin)

    def rewind_playback(self):
        if not self.audio_ready:
            return
        self.player.setPosition(max(0, self.player.position() - 10000))
        self._on_position(self.player.position())

    def forward_playback(self):
        if not self.audio_ready:
            return
        max_ms = self.player.duration()
        self.player.setPosition(min(max_ms, self.player.position() + 10000))
        self._on_position(self.player.position())

    # ---------- export ----------

    def export(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export MP3", "screenplay.mp3", "MP3 (*.mp3)")
        if path:
            from pydub import AudioSegment

            export_mp3(AudioSegment.from_wav(LAST_AUDIO_PATH), path)
            self.statusBar().showMessage(f"Exported {path}")

    def show_credits(self):
        QMessageBox.about(self, "Credits", CREDITS_HTML)

    # ---------- session: pick up where you left off ----------

    def _save_session(self, position_ms: int | None = None):
        if not self.audio_ready or not self.pdf_path:
            return
        if position_ms is None:
            position_ms = self.player.position()
        Session(
            pdf_path=self.pdf_path,
            casting=self._casting_labels(),
            cues=[[c.start_ms, c.end_ms, c.element_index] for c in self.cues],
            position_ms=int(position_ms),
            skip_character_names=self.settings.skip_character_names,
            single_voice=self.settings.single_voice,
        ).save()
        self._last_saved_position = int(position_ms)

    def _save_position(self):
        if self.audio_ready and self.player.position() != self._last_saved_position:
            self._save_session()

    def _resume_last_session(self):
        session = Session.load()
        if session is None or not session.audio_is_reusable():
            return False
        self._pending_casting = session.casting
        if not self._load_pdf(session.pdf_path):
            return False
        self.cues = [Cue(*c) for c in session.cues]
        if any(c.element_index >= len(self.element_spans) for c in self.cues):
            return False  # the PDF changed since the audio was made
        self.player.setSource(QUrl.fromLocalFile(str(LAST_AUDIO_PATH)))
        self.audio_ready = True
        self._set_ready_indicator(True)
        for a in (self.play_action, self.stop_action, self.export_action):
            a.setEnabled(True)
        if (session.skip_character_names, session.single_voice) != (
            self.settings.skip_character_names, self.settings.single_voice
        ):
            self.statusBar().showMessage("Resumed — voice settings changed since this audio was made; regenerate to apply.")
        else:
            self.statusBar().showMessage(f"Resumed {Path(session.pdf_path).name} at {session.position_ms // 60000}m {session.position_ms % 60000 // 1000}s")
        # Seeking before the media is loaded is ignored; wait for it.
        def seek_once(status):
            if status in (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia):
                self.player.setPosition(session.position_ms)
                self._on_position(session.position_ms)
                self.player.mediaStatusChanged.disconnect(seek_once)
        self.player.mediaStatusChanged.connect(seek_once)
        self._show_workspace()
        return True

    def closeEvent(self, event):
        if self.parse_worker and self.parse_worker.isRunning():
            self.parse_worker.wait(3000)
        if self.worker and self.worker.isRunning():
            self.worker.cancel = True
            self.worker.wait(3000)
        self._save_position()
        super().closeEvent(event)
