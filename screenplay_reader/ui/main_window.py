from __future__ import annotations

import bisect
from pathlib import Path

from PySide6.QtCore import QEasingCurve, QEvent, QPropertyAnimation, QRectF, Qt, QThread, QTimer, QUrl, Signal
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
from PySide6.QtMultimedia import QAudioFormat, QAudioOutput, QAudioSink, QMediaDevices, QMediaPlayer, QSoundEffect
from PySide6.QtSvg import QSvgRenderer
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

# Play button colors. Kept green on purpose as a "ready to listen" signal,
# defined separately from the theme accent so the rest of the UI stays consistent.
PLAY_GREEN = "#4F7942"        # fern green — play button, toggles, Generate Audio
PLAY_GREEN_HOVER = "#5c8c4d"
PLAY_SYMBOL = "#ffffff"

# UI fonts (Switzer is bundled in assets/fonts and loaded at startup).
UI_FONT = "Switzer"
SEMIBOLD_FONT = "Switzer Semibold"
LIGHT_FONT = "Switzer Light"
# The "one voice reads the whole script" accent color in the casting panel.
SINGLE_VOICE_ACCENT = "#ed7e7e"
# Playback speeds offered by the transport speed control.
PLAYBACK_SPEEDS = [0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0]
# Fill color for the audio-generation progress bar.
FERN_GREEN = "#4F7942"


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

class ToggleSwitch(QCheckBox):
    """A checkbox drawn as a modern iOS-style sliding switch."""

    def __init__(self, parent=None, on_color=PLAY_GREEN):
        super().__init__(parent)
        self._on_color = on_color
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(52, 30)

    def hitButton(self, pos):
        return self.rect().contains(pos)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        height = self.height()
        radius = height / 2
        on = self.isChecked()
        enabled = self.isEnabled()
        if not enabled:
            track = QColor("#3a3a38")
        elif on:
            track = QColor(self._on_color)
        else:
            track = QColor("#6f6e68")
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(track))
        painter.drawRoundedRect(QRectF(0, 0, self.width(), height), radius, radius)
        diameter = height - 6
        x = self.width() - diameter - 3 if on else 3
        painter.setBrush(QBrush(QColor("#ffffff") if enabled else QColor("#9a9992")))
        painter.drawEllipse(QRectF(x, 3, diameter, diameter))
        painter.end()


HIGHLIGHT = QColor("#ffe9a8")

CREDITS_HTML = """
<h3>Script Radio</h3>
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
    user_scrolled = Signal()

    def mousePressEvent(self, event: QMouseEvent):
        super().mousePressEvent(event)
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked_at.emit(self.cursorForPosition(event.pos()).position())

    def wheelEvent(self, event):
        self.user_scrolled.emit()
        super().wheelEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (
            Qt.Key.Key_Up,
            Qt.Key.Key_Down,
            Qt.Key.Key_PageUp,
            Qt.Key.Key_PageDown,
            Qt.Key.Key_Home,
            Qt.Key.Key_End,
        ):
            self.user_scrolled.emit()
        super().keyPressEvent(event)


class ScriptRadioLanding(QWidget):
    ART_WIDTH = 1100
    ART_HEIGHT = 780

    def __init__(self, artwork_path: Path, previous_enabled: bool, parent=None):
        super().__init__(parent)
        self._artwork = QSvgRenderer(str(artwork_path), self)

        self.new_script_button = QPushButton(self)
        self.new_script_button.setAccessibleName("New Script")
        self.new_script_button.setToolTip("Open a screenplay PDF")

        self.previous_script_button = QPushButton(self)
        self.previous_script_button.setAccessibleName("Previous Script")
        self.previous_script_button.setToolTip("Resume the previous screenplay")
        self.previous_script_button.setEnabled(previous_enabled)

        self.setStyleSheet(
            "QPushButton { background: transparent; border: 1px solid transparent; "
            "border-radius: 0; padding: 0; }"
            "QPushButton:hover { background: rgba(255, 255, 255, 45); border-color: #171717; }"
            "QPushButton:focus { border: 2px dashed #171717; }"
            "QPushButton:disabled { background: transparent; border-color: transparent; }"
        )
        self._position_buttons()

    def _art_rect(self) -> QRectF:
        scale = min(self.width() / self.ART_WIDTH, self.height() / self.ART_HEIGHT)
        width = self.ART_WIDTH * scale
        height = self.ART_HEIGHT * scale
        return QRectF((self.width() - width) / 2, (self.height() - height) / 2, width, height)

    def _position_buttons(self):
        art = self._art_rect()
        scale = art.width() / self.ART_WIDTH
        for button, x, y, width, height in (
            (self.new_script_button, 77, 707, 190, 44),
            (self.previous_script_button, 833, 707, 190, 44),
        ):
            button.setGeometry(
                round(art.x() + x * scale),
                round(art.y() + y * scale),
                round(width * scale),
                round(height * scale),
            )

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#a8a8a5"))
        if self._artwork.isValid():
            self._artwork.render(painter, self._art_rect())

    def resizeEvent(self, event):
        self._position_buttons()
        super().resizeEvent(event)


def _centered_scroll_value(
    current_value: int,
    element_top: int,
    element_bottom: int,
    viewport_height: int,
    maximum: int,
) -> int:
    element_center = (element_top + element_bottom) // 2
    target = current_value + element_center - viewport_height // 2
    return max(0, min(maximum, target))


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
    generation_progress = Signal(int, int)
    audio_chunk = Signal(bytes, int, int, int)
    finished_ok = Signal(object)  # BuildResult
    failed = Signal(str)

    def __init__(self, elements, voice_for, beat_seconds, speak_names, checker, read_parentheticals=True):
        super().__init__()
        self._args = (elements, voice_for, beat_seconds, speak_names, checker, read_parentheticals)
        self.cancel = False

    def run(self):
        elements, voice_for, beat_seconds, speak_names, checker, read_parentheticals = self._args
        try:
            result = build_audio(
                elements,
                voice_for,
                beat_seconds,
                speak_character_names=speak_names,
                read_parentheticals=read_parentheticals,
                checker=checker,
                progress=lambda d, t, m: self.progressed.emit(d, t, m),
                audio_chunk=lambda data, start, end, index: self.audio_chunk.emit(
                    data, start, end, -1 if index is None else index
                ),
                unit_completed=lambda done, total: self.generation_progress.emit(done, total),
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
        self.setWindowTitle("Script Radio")
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
        self.single_voice_toggle: QCheckBox | None = None  # settings-dock copy (removed)
        self.light_mode_btn: QPushButton | None = None  # created inside the Settings popup
        self.dark_mode_btn: QPushButton | None = None
        self.dialog_voice_combos: dict[str, QComboBox] = {}
        self._last_saved_position = -1
        self._last_highlighted_element = None
        self._auto_follow = True
        self._scroll_target_element: int | None = None
        self._scroll_animation: QPropertyAnimation | None = None
        self._stream_buffer = bytearray()
        self._stream_buffer_offset = 0
        self._stream_sink: QAudioSink | None = None
        self._stream_device = None
        self._stream_available = False
        self._stream_playing = False
        self._stream_suspended = False
        self._stream_generation_finished = False
        self._stream_generated_ms = 0

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

        self.open_action = action("New PDF", self.open_pdf)
        self.generate_action = action("Re-Generate Audio", self._regenerate, enabled=False)
        self.export_action = action("Export MP3", self.export, enabled=False)
        self.settings_toggle_action = action("Settings", self._open_settings_dialog)
        action("Credits", self.show_credits)

        self.progress = QProgressBar()
        self.progress.setMaximumWidth(240)
        self.progress.setTextVisible(True)
        self.progress.setFormat("Generating  %p%")
        self.progress.hide()
        self.statusBar().addPermanentWidget(self.progress)

    def _build_script_view(self):
        self.script_view = ClickableScript()
        self.script_view.setReadOnly(True)
        self.script_view.clicked_at.connect(self._on_script_clicked)
        font = QFont()
        # Explicit real fonts (all present on macOS) instead of a "monospace"
        # style hint, which made Qt search for a non-existent "Courier New,monospace".
        font.setFamilies(["Courier New", "Menlo", "Courier"])
        font.setPointSize(24)
        self.script_view.setFont(font)
        self.script_view.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.script_view.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.script_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.script_view.setFrameStyle(QTextEdit.Shape.NoFrame)
        self.script_view.setDocumentTitle("Script")
        self.script_view.user_scrolled.connect(self._suspend_auto_follow)
        scrollbar = self.script_view.verticalScrollBar()
        scrollbar.installEventFilter(self)
        scrollbar.sliderPressed.connect(self._suspend_auto_follow)
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
        transport_bar_layout.setContentsMargins(0, 8, 0, 20)
        transport_bar_layout.setSpacing(12)

        self.rewind_button = QPushButton("⏮")
        self.rewind_button.setObjectName("transportButton")
        self.rewind_button.setToolTip("Rewind 10s")
        self.rewind_button.clicked.connect(self.rewind_playback)
        self.rewind_button.setFixedSize(52, 52)

        self.play_button = QPushButton("▶")
        self.play_button.setObjectName("transportButtonPrimary")
        self.play_button.setToolTip("Play / Pause")
        self.play_button.clicked.connect(self.toggle_play)
        self.play_button.setFixedSize(60, 60)
        self.play_button.setEnabled(False)
        self._ready_icon = _play_icon(QColor(PLAY_SYMBOL))

        self.forward_button = QPushButton("⏭")
        self.forward_button.setObjectName("transportButton")
        self.forward_button.setToolTip("Forward 10s")
        self.forward_button.clicked.connect(self.forward_playback)
        self.forward_button.setFixedSize(52, 52)
        self.rewind_button.setEnabled(False)
        self.forward_button.setEnabled(False)

        self.speed_combo = QComboBox()
        self.speed_combo.setObjectName("speedCombo")
        for rate in PLAYBACK_SPEEDS:
            self.speed_combo.addItem(f"{rate:g}×")
        try:
            speed_index = PLAYBACK_SPEEDS.index(self.settings.playback_rate)
        except ValueError:
            speed_index = PLAYBACK_SPEEDS.index(1.0)
        self.speed_combo.setCurrentIndex(speed_index)
        self.speed_combo.setFixedWidth(96)
        self.speed_combo.setToolTip("Playback speed (applies to finished audio)")
        self.speed_combo.currentIndexChanged.connect(self._on_speed_changed)

        # A spacer the same width as the speed box keeps the buttons centered.
        speed_spacer = QWidget()
        speed_spacer.setFixedWidth(96)

        transport_bar_layout.addWidget(speed_spacer)
        transport_bar_layout.addStretch(1)
        transport_bar_layout.addWidget(self.rewind_button)
        transport_bar_layout.addWidget(self.play_button)
        transport_bar_layout.addWidget(self.forward_button)
        transport_bar_layout.addStretch(1)
        transport_bar_layout.addWidget(self.speed_combo, 0, Qt.AlignmentFlag.AlignVCenter)
        self.transport_bar.setLayout(transport_bar_layout)
        self.script_stage = QWidget()
        self.script_stage.setObjectName("scriptStage")
        script_stage_layout = QHBoxLayout(self.script_stage)
        script_stage_layout.setContentsMargins(0, 0, 0, 0)
        script_stage_layout.addWidget(self.script_view)

        transport_layout.addWidget(self.script_stage, 1)
        transport_layout.addWidget(self.transport_bar)

        self.setCentralWidget(self.transport_widget)
        self._set_ready_indicator(False) 

    def _build_landing_page(self):
        artwork = Path(__file__).resolve().parents[2] / "design-previews" / "01-bootleg-press.svg"
        self.landing_page = ScriptRadioLanding(
            artwork,
            previous_enabled=Session.load() is not None,
        )
        self.landing_page.setObjectName("landingPage")
        self.landing_page.new_script_button.clicked.connect(self.open_pdf)
        self.landing_page.previous_script_button.clicked.connect(self.open_previous)

    def _build_side_panel(self):
        # Casting combos live in this off-screen scroll area; casting and
        # settings are both popups now, so there is no left dock to shift the script.
        self.cast_form_host = QScrollArea()
        self.cast_form_host.setWidgetResizable(True)

    # ---------- settings ----------

    def _on_speed_changed(self, index):
        rate = PLAYBACK_SPEEDS[index]
        self.settings.playback_rate = rate
        self.settings.save()
        self.player.setPlaybackRate(rate)
        self.statusBar().showMessage(f"Playback speed: {rate:g}×")

    def _on_read_parentheticals_toggled(self, on):
        self.settings.read_parentheticals = on
        self.settings.save()
        if self.audio_ready:
            self.statusBar().showMessage("Parentheticals setting changed — Re-Generate Audio to apply it.")

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
        self.player.setPlaybackRate(self.settings.playback_rate)
        self.ready_sound = QSoundEffect()
        self.ready_sound.setSource(QUrl.fromLocalFile("/Users/stevemacario/Claude/Projects/code/Ding_ready_SFX.wav"))
        self.ready_sound.setVolume(0.3)  # the microwave ding, softened
        self.audio_out = QAudioOutput()
        self.audio_out.setDevice(QMediaDevices.defaultAudioOutput())
        self.audio_out.setVolume(1.0)
        self.audio_out.setMuted(False)
        self.player.setAudioOutput(self.audio_out)
        self.player.positionChanged.connect(self._on_position)
        self.player.playbackStateChanged.connect(self._on_playback_state)
        self.player.errorOccurred.connect(self._on_player_error)
        self._position_timer = QTimer(self)
        self._position_timer.setInterval(POSITION_SAVE_INTERVAL_MS)
        self._position_timer.timeout.connect(self._save_position)
        self._stream_timer = QTimer(self)
        self._stream_timer.setInterval(20)
        self._stream_timer.timeout.connect(self._pump_stream)

    def _set_ready_indicator(self, ready: bool):
    # Green arrow next to Play = there is audio to play.
        self.play_button.setIcon(self._ready_icon if ready else QIcon())
        self.play_button.setText("" if ready else "▶") # <-- Add this line to clear the text
        self.play_button.setToolTip(
            "Audio is ready - press Play" if ready else "Generate audio first"
        )
    
        if ready:
            self.ready_sound.play()


    def _apply_theme(self, mode: str):
        self.settings.theme = mode
        self.settings.save()

        if mode == "dark":
            bg = "#171717"
            fg = "#eeece5"
            panel = "#202020"
            panel_alt = "#101010"
            highlight = "#55534e"
            accent = "#eeece5"
            border = "#68665f"
            input_bg = "#282827"
            toolbar_bg = "#090909"
            button_bg = "#292928"
            script_bg = "#20201f"
            script_fg = "#eeece5"
            scroll_handle = "#4a4945"
            scroll_handle_hover = "#66655f"
            sv_bg = "#2b1f1f"
        else:
            bg = "#d8d7d1"
            fg = "#171717"
            panel = "#c9c8c2"
            panel_alt = "#e6e5df"
            highlight = "#c3c1ba"
            accent = "#171717"
            border = "#85837d"
            input_bg = "#efeee8"
            toolbar_bg = "#171717"
            button_bg = "#efeee8"
            script_bg = "#e9e8e2"
            script_fg = "#1b1b1a"
            scroll_handle = "#bdbcb6"
            scroll_handle_hover = "#9a9992"
            sv_bg = "#f8e7e7"

        # Wider left margin (like a real script page) so the block reads centered.
        script_pad = "padding: 20px 100px 20px 280px;"

        self.setStyleSheet(
            f"QMainWindow, QWidget {{ background: {bg}; color: {fg}; }}"
            f"QToolBar {{ background: {toolbar_bg}; color: #eeece5; border: none; border-bottom: 2px solid #2a2a28; spacing: 8px; padding: 6px 8px; }}"
            f"QToolButton {{ background: #24231f; color: #eeece5; border: 1px solid #55534e; border-radius: 9px; padding: 7px 14px; font-family: '{SEMIBOLD_FONT}'; }}"
            f"QToolButton:hover {{ background: #eeece5; color: #171717; border: 1px solid #eeece5; }}"
            f"QToolButton:pressed {{ background: #cbcac3; color: #171717; }}"
            f"QStatusBar {{ background: {toolbar_bg}; color: #eeece5; border-top: 1px solid {border}; }}"
            f"QDockWidget {{ background: {panel}; color: {fg}; border: 1px solid {border}; }}"
            f"QDockWidget::title {{ background: {panel_alt}; color: {fg}; padding: 6px; }}"
            f"QLabel {{ color: {fg}; }}"
            f"QCheckBox {{ color: {fg}; spacing: 8px; }}"
            f"QSlider::groove:horizontal {{ background: {border}; height: 6px; border-radius: 3px; }}"
            f"QSlider::handle:horizontal {{ background: {accent}; border: 1px solid {accent}; width: 14px; margin: -4px 0; border-radius: 7px; }}"
            f"QComboBox {{ background: {input_bg}; color: {fg}; border: none; border-radius: 10px; padding: 6px 12px; }}"
            f"QComboBox::drop-down {{ border: none; width: 24px; }}"
            f"QComboBox QAbstractItemView {{ background: {input_bg}; color: {fg}; border: 1px solid {border}; border-radius: 8px; outline: none; padding: 4px; selection-background-color: {PLAY_GREEN}; selection-color: #ffffff; }}"
            f"QAbstractScrollArea {{ background: {bg}; }}"
            f"QTextEdit {{ background: {script_bg}; color: {script_fg}; border: none; font-family: 'Courier New'; {script_pad} }}"
            f"QPushButton {{ background: {button_bg}; color: {fg}; border: 1px solid {border}; border-radius: 12px; padding: 6px 14px; }}"
            f"QProgressBar {{ background: {panel}; color: {fg}; border: 1px solid {border}; border-radius: 8px; text-align: center; }}"
            f"QProgressBar::chunk {{ background: {FERN_GREEN}; border-radius: 7px; }}"
            f"QWidget#transportWidget {{ background: {bg}; }}"
            f"QWidget#scriptStage {{ background: {bg}; }}"
            f"QWidget#landingPage {{ background: {bg}; }}"
            f"QLabel#landingTitle {{ color: {accent}; }}"
            f"QWidget#transportBar {{ background: transparent; }}"
            f"QPushButton#transportButton {{ background: {button_bg}; color: {fg}; border: 1px solid {border}; border-radius: 26px; font-size: 20px; min-width: 52px; min-height: 52px; }}"
            f"QPushButton#transportButtonPrimary {{ background: {PLAY_GREEN}; color: {PLAY_SYMBOL}; border: 1px solid {PLAY_GREEN}; border-radius: 30px; font-size: 22px; min-width: 60px; min-height: 60px; }}"
            f"QPushButton#transportButtonPrimary:hover {{ background: {PLAY_GREEN_HOVER}; color: {PLAY_SYMBOL}; border: 1px solid {PLAY_GREEN_HOVER}; }}"
            f"QPushButton#transportButtonPrimary:disabled {{ background: {button_bg}; color: {border}; border: 1px solid {border}; }}"
            f"QComboBox#speedCombo {{ background: {button_bg}; color: {fg}; border: none; border-radius: 15px; padding: 4px 12px; }}"
            f"QComboBox#speedCombo QAbstractItemView {{ background: {input_bg}; color: {fg}; selection-background-color: {PLAY_GREEN}; selection-color: #ffffff; }}"
            f"QScrollBar:vertical {{ background: transparent; width: 12px; margin: 2px; }}"
            f"QScrollBar::handle:vertical {{ background: {scroll_handle}; min-height: 36px; border-radius: 5px; }}"
            f"QScrollBar::handle:vertical:hover {{ background: {scroll_handle_hover}; }}"
            f"QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; background: none; border: none; }}"
            f"QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}"
            f"QScrollBar:horizontal {{ background: transparent; height: 12px; margin: 2px; }}"
            f"QScrollBar::handle:horizontal {{ background: {scroll_handle}; min-width: 36px; border-radius: 5px; }}"
            f"QScrollBar::handle:horizontal:hover {{ background: {scroll_handle_hover}; }}"
            f"QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0px; background: none; border: none; }}"
            f"QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{ background: none; }}"
            f"QLabel#castingTitle {{ color: {accent}; }}"
            f"QLabel#castingSubtitle {{ color: {fg}; }}"
            f"QLabel#singleVoiceLabel {{ color: {SINGLE_VOICE_ACCENT}; }}"
            f"QPushButton#generateAudioButton {{ background: {PLAY_GREEN}; color: {PLAY_SYMBOL}; border: 1px solid {PLAY_GREEN}; border-radius: 16px; padding: 10px 24px; font-family: '{SEMIBOLD_FONT}'; font-size: 15px; }}"
            f"QPushButton#generateAudioButton:hover {{ background: {PLAY_GREEN_HOVER}; border: 1px solid {PLAY_GREEN_HOVER}; }}"
            f"QPushButton#goBackButton {{ background: transparent; color: {fg}; border: 1px solid {border}; border-radius: 16px; padding: 10px 22px; }}"
            f"QPushButton#goBackButton:hover {{ background: {panel}; }}"
        )
        self._highlight_color = QColor(highlight)
        self._accent_color = QColor(accent)
        self._ready_icon = _play_icon(QColor(PLAY_SYMBOL))

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

    def _open_settings_dialog(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Script Radio | Settings")
        dialog.setModal(True)
        dialog.setMinimumWidth(380)

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(28, 24, 28, 22)
        layout.setSpacing(16)

        title = QLabel("Settings")
        title.setObjectName("castingTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setFont(QFont(SEMIBOLD_FONT, 26))
        layout.addWidget(title)

        theme_label = QLabel("Theme")
        theme_label.setObjectName("settingsHeading")
        layout.addWidget(theme_label)

        theme_row = QHBoxLayout()
        theme_row.setSpacing(10)
        self.light_mode_btn = QPushButton("Light Mode")
        self.light_mode_btn.setCheckable(True)
        self.light_mode_btn.setChecked(self.settings.theme == "light")
        self.light_mode_btn.clicked.connect(lambda: self._apply_theme("light"))
        self.dark_mode_btn = QPushButton("Dark Mode")
        self.dark_mode_btn.setCheckable(True)
        self.dark_mode_btn.setChecked(self.settings.theme == "dark")
        self.dark_mode_btn.clicked.connect(lambda: self._apply_theme("dark"))
        theme_row.addWidget(self.light_mode_btn)
        theme_row.addWidget(self.dark_mode_btn)
        layout.addLayout(theme_row)

        layout.addStretch(1)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        done_btn = QPushButton("Done")
        done_btn.setDefault(True)
        done_btn.clicked.connect(dialog.accept)
        button_row.addWidget(done_btn)
        layout.addLayout(button_row)

        dialog.exec()
        # The theme buttons are transient; drop the references so _apply_theme
        # never touches deleted widgets after the dialog closes.
        self.light_mode_btn = None
        self.dark_mode_btn = None

    def _open_casting_flow(self):
        """Casting Panel -> Customize Audio -> generate. 'Go Back' in Customize
        Audio loops the user back to casting to change their voice choices."""
        while True:
            if not self._open_casting_panel():
                return  # cancelled at the casting panel
            result = self._open_customize_audio()
            if result == "generate":
                self._start_generation()
                return
            if result == "cancel":
                return
            # result == "back": reopen the casting panel

    def _open_casting_panel(self) -> bool:
        dialog = QDialog(self)
        dialog.setWindowTitle("Script Radio | Casting")
        dialog.setModal(True)
        dialog.setMinimumSize(560, 520)
        dialog.resize(640, 680)

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(28, 24, 28, 22)
        layout.setSpacing(12)

        title = QLabel("Casting Panel")
        title.setObjectName("castingTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setFont(QFont(SEMIBOLD_FONT, 26))
        layout.addWidget(title)

        subtitle = QLabel(
            "Assign voices for each character or choose one voice to read the lines of dialogue below."
        )
        subtitle.setWordWrap(True)
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle.setObjectName("castingSubtitle")
        subtitle.setFont(QFont(UI_FONT, 14))
        layout.addWidget(subtitle)

        sv_row = QWidget()
        sv_row_layout = QHBoxLayout(sv_row)
        sv_row_layout.setContentsMargins(6, 4, 6, 4)
        sv_label = QLabel("One voice reads the whole script")
        sv_label.setObjectName("singleVoiceLabel")
        sv_label.setFont(QFont(SEMIBOLD_FONT, 15))
        self.dialog_single_voice_toggle = ToggleSwitch(on_color=SINGLE_VOICE_ACCENT)
        self.dialog_single_voice_toggle.setChecked(self.settings.single_voice)
        self.dialog_single_voice_toggle.toggled.connect(self._on_single_voice_toggled)
        sv_row_layout.addWidget(sv_label)
        sv_row_layout.addStretch(1)
        sv_row_layout.addWidget(self.dialog_single_voice_toggle, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(sv_row)

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
        return accepted

    def _open_customize_audio(self) -> str:
        """Final pre-generation window. 'Generate Audio' here starts synthesis
        immediately — there is no extra confirmation popup."""
        dialog = QDialog(self)
        dialog.setWindowTitle("Script Radio | Customize Audio")
        dialog.setModal(True)
        dialog.setMinimumSize(520, 500)

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(28, 24, 28, 22)
        layout.setSpacing(12)

        title = QLabel("Customize Audio")
        title.setObjectName("castingTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setFont(QFont(SEMIBOLD_FONT, 26))
        layout.addWidget(title)

        subtitle = QLabel("Fine-tune the reading, then press Generate Audio to start.")
        subtitle.setObjectName("castingSubtitle")
        subtitle.setWordWrap(True)
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle.setFont(QFont(UI_FONT, 14))
        layout.addWidget(subtitle)

        def add_toggle(text, checked, handler, *, enabled=True, tooltip=""):
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(6, 8, 6, 8)
            label = QLabel(text)
            label.setWordWrap(True)
            label.setEnabled(enabled)
            switch = ToggleSwitch()
            switch.setChecked(checked)
            switch.setEnabled(enabled)
            switch.toggled.connect(handler)
            if tooltip:
                label.setToolTip(tooltip)
                switch.setToolTip(tooltip)
            row_layout.addWidget(label)
            row_layout.addStretch(1)
            row_layout.addWidget(switch, 0, Qt.AlignmentFlag.AlignVCenter)
            layout.addWidget(row)
            return switch

        add_toggle(
            "Highlight lines during playback",
            self.settings.highlight,
            self._on_highlight_toggled,
        )
        add_toggle(
            "Read parentheticals",
            self.settings.read_parentheticals,
            self._on_read_parentheticals_toggled,
            tooltip="Reads delivery directions like “(sarcastically)” aloud.",
        )
        add_toggle(
            "Skip characters’ names",
            self.settings.skip_character_names,
            self._on_skip_names_toggled,
            tooltip="On: only the dialogue is spoken; the voice change marks the speaker.",
        )
        whisper_ok, whisper_why = self.transcriber.available()
        add_toggle(
            "Check every line with Whisper (slower)",
            self.settings.validate_audio and whisper_ok,
            self._on_validate_toggled,
            enabled=whisper_ok,
            tooltip=(
                "Transcribes each line locally and regenerates any mismatches."
                if whisper_ok else whisper_why
            ),
        )

        beat_row = QWidget()
        beat_layout = QVBoxLayout(beat_row)
        beat_layout.setContentsMargins(6, 12, 6, 4)
        beat_value = QLabel()
        beat_slider = QSlider(Qt.Orientation.Horizontal)
        beat_slider.setRange(0, 6)
        beat_slider.setValue(int(round(self.settings.beat_seconds)))
        beat_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        beat_slider.setTickInterval(1)
        beat_slider.setSingleStep(1)
        beat_value.setText(f"Beat (Pause): {beat_slider.value()} s")

        def on_beat(value):
            beat_value.setText(f"Beat (Pause): {value} s")
            self.settings.beat_seconds = float(value)
            self.settings.save()

        beat_slider.valueChanged.connect(on_beat)
        beat_layout.addWidget(beat_value)
        beat_layout.addWidget(beat_slider)
        layout.addWidget(beat_row)

        layout.addStretch(1)

        go_back = {"clicked": False}
        button_row = QHBoxLayout()
        back_btn = QPushButton("Go Back")
        back_btn.setObjectName("goBackButton")
        back_btn.setToolTip("Return to the Casting Panel to change voices")
        back_btn.clicked.connect(lambda: (go_back.__setitem__("clicked", True), dialog.reject()))
        button_row.addWidget(back_btn)
        button_row.addStretch(1)
        generate_btn = QPushButton("Generate Audio")
        generate_btn.setObjectName("generateAudioButton")
        generate_btn.setDefault(True)
        generate_btn.clicked.connect(dialog.accept)
        button_row.addWidget(generate_btn)
        layout.addLayout(button_row)

        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        if accepted:
            return "generate"
        return "back" if go_back["clicked"] else "cancel"

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
        combo.setFont(QFont(LIGHT_FONT, 14))  # voice names in Switzer Light
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
            form.addRow(self._cast_name_label("Narrator" if not self.settings.single_voice else "Voice"), narrator_combo)
            if self.settings.single_voice:
                hint = QLabel("Reads every line, all characters included.")
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
                    form.addRow(self._cast_name_label(name.title()), combo)
            host.setWidget(form_widget)

    def _cast_name_label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setFont(QFont(UI_FONT, 14))  # character names in Switzer Regular
        return label

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
        self._open_casting_flow()
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
        self._reset_stream()
        self.player.setSource(QUrl())
        self.cues = []
        self.audio_ready = False
        self._set_ready_indicator(False)
        for button in (self.play_button, self.rewind_button, self.forward_button):
            button.setEnabled(False)
        self.export_action.setEnabled(False)

    def _regenerate(self):
        """Toolbar 'Re-Generate Audio': cancel an in-progress run, otherwise
        reopen Casting → Customize Audio to start a fresh rendition."""
        if self.worker and self.worker.isRunning():
            self.worker.cancel = True
            self.generate_action.setText("Re-Generate Audio")
            self.statusBar().showMessage("Generation cancelled.")
            return
        if not self.screenplay:
            return
        self._open_casting_flow()

    def _start_generation(self):
        """Kick off synthesis. Called by the Customize Audio window's
        'Generate Audio' button — no separate confirmation."""
        if self.worker and self.worker.isRunning():
            return

        casting = self._casting()
        narrator = casting.get("")
        speak_names = not self.settings.skip_character_names
        checker = self.transcriber if self.settings.validate_audio and self.transcriber.available()[0] else None

        single_voice = self.settings.single_voice

        def voice_for(el):
            if single_voice or el.kind in ("scene", "action", "character", "parenthetical"):
                return narrator  # directions and names are read by the narrator
            return casting.get(el.character, narrator)

        self.stop_playback()
        self._reset_stream()
        self.audio_ready = False
        self.cues = []
        self.player.setSource(QUrl())
        self._set_ready_indicator(False)
        for button in (self.play_button, self.rewind_button, self.forward_button):
            button.setEnabled(False)
        self.export_action.setEnabled(False)
        self.worker = GenerationWorker(
            self.screenplay.elements,
            voice_for,
            float(self.settings.beat_seconds),
            speak_names,
            checker,
            self.settings.read_parentheticals,
        )
        self.worker.progressed.connect(self._on_progress)
        self.worker.generation_progress.connect(self._on_generation_progress)
        self.worker.audio_chunk.connect(self._on_audio_chunk)
        self.worker.finished_ok.connect(self._on_generated)
        self.worker.failed.connect(self._on_failed)
        self.generate_action.setText("Cancel Generation")
        self.progress.show()
        self.worker.start()

    def _on_progress(self, done, total, msg):
        self.progress.setMaximum(total)
        self.progress.setValue(done)
        self.statusBar().showMessage(msg)

    def _on_generation_progress(self, done: int, total: int):
        self.progress.setMaximum(total)
        self.progress.setValue(done)
        threshold = max(1, (total + 9) // 10)
        if not self._stream_available and done >= threshold:
            self._stream_available = True
            self.play_button.setEnabled(True)
            self._set_ready_indicator(True)
            self.statusBar().showMessage(
                f"First audio ready ({done}/{total} passages); generation continues in the background"
            )

    def _on_audio_chunk(self, data: bytes, start_ms: int, end_ms: int, element_index: int):
        self._stream_buffer.extend(data)
        self._stream_generated_ms = max(self._stream_generated_ms, end_ms)
        if element_index >= 0:
            if self.cues and self.cues[-1].element_index == element_index:
                self.cues[-1].end_ms = end_ms
            else:
                self.cues.append(Cue(start_ms, end_ms, element_index))
        if self._stream_sink is not None and self._stream_playing:
            if self._stream_suspended:
                self._stream_sink.resume()
                self._stream_suspended = False
            self._pump_stream()

    def _on_generated(self, result):
        audio, self.cues = result.audio, result.cues
        self.generate_action.setText("Re-Generate Audio")
        self.progress.hide()

        LAST_AUDIO_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.player.setSource(QUrl())  # release the old file before overwriting
        audio.export(LAST_AUDIO_PATH, format="wav")
        self.player.setSource(QUrl.fromLocalFile(str(LAST_AUDIO_PATH)))
        self.player.setPlaybackRate(self.settings.playback_rate)
        self.audio_ready = True
        self._stream_generation_finished = True
        if self._stream_sink is None:
            self._stream_buffer.clear()
            self._stream_buffer_offset = 0
            self._set_ready_indicator(True)
            self._save_session(position_ms=0)
        else:
            self.cues = result.cues
            if self._stream_playing:
                self._pump_stream()
            else:
                self._handoff_stream_to_player()
        # Once the full audio exists, all transport controls work (seeking
        # switches from the live stream to the finished file).
        for button in (self.play_button, self.rewind_button, self.forward_button):
            button.setEnabled(True)
        self.export_action.setEnabled(True)
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
        self.generate_action.setText("Re-Generate Audio")
        self.progress.hide()
        self._stream_generation_finished = True
        if self._stream_sink is None and not self._stream_available:
            self._stream_buffer.clear()
            self._stream_buffer_offset = 0
        if self._stream_sink is not None and self._stream_playing:
            self._pump_stream()
        self.statusBar().showMessage(f"Generation failed: {msg}")
        if msg != "Cancelled.":
            QMessageBox.critical(self, "Generation failed", msg)

    # ---------- playback & highlighting ----------

    def toggle_play(self):
        if self._stream_sink is not None:
            if self._stream_playing:
                self.pause_playback()
            else:
                self._resume_stream()
            return
        if not self.audio_ready and self._stream_available:
            self._start_stream()
            return
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.pause_playback()
        else:
            self._auto_follow = True
            if self._last_highlighted_element is not None:
                self._center_element(self._last_highlighted_element)
            self.audio_out.setDevice(QMediaDevices.defaultAudioOutput())
            self.audio_out.setVolume(1.0)
            self.audio_out.setMuted(False)
            self.player.play()

    def _on_player_error(self, error, message: str):
        if error != QMediaPlayer.Error.NoError:
            self.statusBar().showMessage(f"Audio playback failed: {message}")

    def eventFilter(self, watched, event):
        if watched is self.script_view.verticalScrollBar() and event.type() in (
            QEvent.Type.MouseButtonPress,
            QEvent.Type.Wheel,
            QEvent.Type.KeyPress,
        ):
            self._suspend_auto_follow()
        return super().eventFilter(watched, event)

    def _suspend_auto_follow(self):
        self._auto_follow = False
        if self._scroll_animation is not None:
            self._scroll_animation.stop()

    def pause_playback(self):
        if self._stream_sink is not None and self._stream_playing:
            self._stream_sink.suspend()
            self._stream_suspended = True
            self._stream_playing = False
            self._stream_timer.stop()
            self._set_stream_button(False)
            return
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._save_session(self.player.position())
            self.player.pause()
            self.script_view.setExtraSelections([])
            self.statusBar().showMessage(f"Paused — resumed at {self.player.position() // 60000}m {self.player.position() % 60000 // 1000}s")

    def stop_playback(self):
        if self._stream_sink is not None:
            self._stop_stream_sink()
        self.pause_playback()

    def _reset_stream(self):
        self._stop_stream_sink()
        self._stream_buffer.clear()
        self._stream_buffer_offset = 0
        self._stream_available = False
        self._stream_generation_finished = False
        self._stream_generated_ms = 0

    def _stop_stream_sink(self):
        self._stream_timer.stop()
        if self._stream_sink is not None:
            self._stream_sink.stop()
            self._stream_sink.deleteLater()
        self._stream_sink = None
        self._stream_device = None
        self._stream_playing = False
        self._stream_suspended = False

    def _start_stream(self):
        self._auto_follow = True
        if self._last_highlighted_element is not None:
            self._center_element(self._last_highlighted_element)
        device = QMediaDevices.defaultAudioOutput()
        audio_format = QAudioFormat()
        audio_format.setSampleRate(44100)
        audio_format.setChannelCount(1)
        audio_format.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        if not device.isFormatSupported(audio_format):
            self.statusBar().showMessage("This audio device does not support live playback format")
            return
        self._stream_sink = QAudioSink(device, audio_format, self)
        self._stream_sink.setVolume(1.0)
        self._stream_sink.setBufferSize(44100 * 2 * 2)
        self._stream_device = self._stream_sink.start()
        if self._stream_device is None:
            self._stop_stream_sink()
            self.statusBar().showMessage("Could not start live audio output")
            return
        self._stream_playing = True
        self._set_stream_button(True)
        self._stream_timer.start()
        self._pump_stream()

    def _resume_stream(self):
        if self._stream_sink is None:
            self._start_stream()
            return
        if self._stream_suspended:
            self._stream_sink.resume()
            self._stream_suspended = False
        self._stream_playing = True
        self._auto_follow = True
        if self._last_highlighted_element is not None:
            self._center_element(self._last_highlighted_element)
        self._set_stream_button(True)
        self._stream_timer.start()
        self._pump_stream()

    def _set_stream_button(self, playing: bool):
        if playing:
            self.play_button.setIcon(QIcon())
            self.play_button.setText("Ⅱ")
        else:
            self.play_button.setIcon(self._ready_icon if self._stream_available or self.audio_ready else QIcon())
            self.play_button.setText("" if self._stream_available or self.audio_ready else "▶")

    def _pump_stream(self):
        if self._stream_sink is None or self._stream_device is None or not self._stream_playing:
            return
        available = len(self._stream_buffer) - self._stream_buffer_offset
        if available:
            writable = min(available, self._stream_sink.bytesFree())
            writable -= writable % 2
            if writable:
                chunk = bytes(self._stream_buffer[self._stream_buffer_offset:self._stream_buffer_offset + writable])
                written = self._stream_device.write(chunk)
                if written < 0:
                    self.statusBar().showMessage("Live audio output stopped unexpectedly")
                    self._stop_stream_sink()
                    return
                self._stream_buffer_offset += written
                if self._stream_buffer_offset == len(self._stream_buffer):
                    self._stream_buffer.clear()
                    self._stream_buffer_offset = 0

        position_ms = min(self._stream_sink.processedUSecs() // 1000, self._stream_generated_ms)
        self._on_position(int(position_ms))
        if not self._stream_buffer and self._stream_sink.bytesFree() >= self._stream_sink.bufferSize():
            if self._stream_generation_finished:
                if self.audio_ready:
                    self._handoff_stream_to_player()
                else:
                    self._stop_stream_sink()
                    self._stream_available = False
                    self.play_button.setEnabled(False)
            elif not self._stream_suspended:
                self._stream_sink.suspend()
                self._stream_suspended = True
                self.statusBar().showMessage("Waiting for more generated audio…")

    def _handoff_stream_to_player(self):
        if self._stream_sink is None:
            return
        position_ms = int(self._stream_sink.processedUSecs() // 1000)
        was_playing = self._stream_playing
        self._stop_stream_sink()
        self.play_button.setEnabled(True)
        self.rewind_button.setEnabled(True)
        self.forward_button.setEnabled(True)

        def seek_when_loaded(status):
            if status in (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia):
                self.player.setPosition(position_ms)
                self._on_position(position_ms)
                self._save_session(position_ms=position_ms)
                self.player.mediaStatusChanged.disconnect(seek_when_loaded)
                if was_playing:
                    self.player.play()
                else:
                    self._on_playback_state(QMediaPlayer.PlaybackState.StoppedState)

        self.player.mediaStatusChanged.connect(seek_when_loaded)
        seek_when_loaded(self.player.mediaStatus())

    def _on_playback_state(self, state):
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        if playing:
            self.play_button.setIcon(QIcon())
            self.play_button.setText("Ⅱ")
            self._position_timer.start()
        else:
            self.play_button.setIcon(self._ready_icon if self.audio_ready else QIcon())
            self.play_button.setText("" if self.audio_ready else "▶")
            self._position_timer.stop()
            self._save_position()

    def _element_at(self, doc_pos: int) -> int | None:
        """Index of the element whose rendered text contains this document position."""
        if not self.element_spans:
            return None
        i = bisect.bisect_right([start for start, _ in self.element_spans], doc_pos) - 1
        return i if i >= 0 else None

    def _switch_to_file_playback(self):
        """Leave the live stream and continue from the finished file, so the
        user can seek. No-op unless we're currently streaming. Safe once
        audio_ready is True (the full WAV is loaded into self.player)."""
        if self._stream_sink is None:
            return
        position_ms = int(min(self._stream_sink.processedUSecs() // 1000, self._stream_generated_ms))
        was_playing = self._stream_playing
        self._stop_stream_sink()
        if self.player.source().isEmpty():
            self.player.setSource(QUrl.fromLocalFile(str(LAST_AUDIO_PATH)))
        self.player.setPlaybackRate(self.settings.playback_rate)
        self.player.setPosition(position_ms)
        for button in (self.play_button, self.rewind_button, self.forward_button):
            button.setEnabled(True)
        if was_playing:
            self.player.play()

    def _on_script_clicked(self, doc_pos: int):
        if not self.audio_ready or not self.cues:
            return
        self._switch_to_file_playback()
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
        self._auto_follow = True
        self._on_position(target.start_ms)
        self._center_element(target.element_index, force=True)
        if self.player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
            self.player.play()

    def _on_position(self, ms):
        if not self.cues or not self.settings.highlight:
            return
        i = bisect.bisect_right([c.start_ms for c in self.cues], ms) - 1
        if i < 0 or ms >= self.cues[i].end_ms:
            return
        element_index = self.cues[i].element_index
        self._last_highlighted_element = element_index
        start, end = self.element_spans[element_index]
        # Skip the leading indentation spaces so the highlight hugs the text
        # itself (a short centered cue like "PAUL" shouldn't light up the margin).
        if 0 <= element_index < len(self.screenplay.elements):
            indent = self.INDENTS.get(self.screenplay.elements[element_index].kind, 0)
            start = min(start + indent, end)
        sel = QTextEdit.ExtraSelection()
        sel.cursor = QTextCursor(self.script_view.document())
        sel.cursor.setPosition(start)
        sel.cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        sel.format.setBackground(self._highlight_color)
        self.script_view.setExtraSelections([sel])

        if self._auto_follow and element_index != self._scroll_target_element:
            self._center_element(element_index)

    def _center_element(self, element_index: int, *, force: bool = False):
        if not force and not self._auto_follow:
            return
        if element_index < 0 or element_index >= len(self.element_spans):
            return

        start, end = self.element_spans[element_index]
        start_cursor = QTextCursor(self.script_view.document())
        start_cursor.setPosition(start)
        end_cursor = QTextCursor(self.script_view.document())
        end_cursor.setPosition(max(start, end - 1))
        start_rect = self.script_view.cursorRect(start_cursor)
        end_rect = self.script_view.cursorRect(end_cursor)
        scrollbar = self.script_view.verticalScrollBar()
        target = _centered_scroll_value(
            scrollbar.value(),
            min(start_rect.top(), end_rect.top()),
            max(start_rect.bottom(), end_rect.bottom()),
            self.script_view.viewport().height(),
            scrollbar.maximum(),
        )
        if self._scroll_animation is not None:
            self._scroll_animation.stop()
            self._scroll_animation.deleteLater()
        self._scroll_target_element = element_index
        self._scroll_animation = QPropertyAnimation(scrollbar, b"value", self)
        self._scroll_animation.setDuration(450)
        self._scroll_animation.setStartValue(scrollbar.value())
        self._scroll_animation.setEndValue(target)
        self._scroll_animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._scroll_animation.start()

    def rewind_playback(self):
        if not self.audio_ready:
            return
        self._switch_to_file_playback()
        self.player.setPosition(max(0, self.player.position() - 10000))
        self._on_position(self.player.position())

    def forward_playback(self):
        if not self.audio_ready:
            return
        self._switch_to_file_playback()
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
        self.player.setPlaybackRate(self.settings.playback_rate)
        self.audio_ready = True
        self._set_ready_indicator(True)
        for button in (self.play_button, self.rewind_button, self.forward_button):
            button.setEnabled(True)
        self.export_action.setEnabled(True)
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
        self._stop_stream_sink()
        self._save_position()
        super().closeEvent(event)
