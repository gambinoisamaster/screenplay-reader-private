from __future__ import annotations

import bisect
import hashlib
from collections import Counter
from pathlib import Path

from PySide6.QtCore import QEasingCurve, QEvent, QPoint, QPropertyAnimation, QRectF, QSize, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QAction,
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QFontMetricsF,
    QIcon,
    QMouseEvent,
    QPainter,
    QPalette,
    QPen,
    QPixmap,
    QPolygonF,
    QTextCharFormat,
    QTextCursor,
    QTextOption,
)
from PySide6.QtCore import QPointF
from PySide6.QtMultimedia import QAudioFormat, QAudioOutput, QAudioSink, QMediaDevices, QMediaPlayer, QSoundEffect
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSlider,
    QStackedWidget,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .. import speech
from ..audio_builder import Cue, GenerationCancelled, SentenceCue, build_audio, export_mp3
from ..engines import load_engines
from ..parser import Screenplay, character_name, parse_screenplay
from ..settings import LAST_AUDIO_PATH, STREAM_AUDIO_PATH, Session, Settings, load_voice_memory, save_voice_memory
from ..validate import Transcriber
from . import theme as themes
from .theme import ACCENT, ON_ACCENT, SCRIPT_FAMILIES, SINGLE_VOICE_ACCENT, Theme, theme_for, tooltip_stylesheet, ui_font
from .widgets import (
    GradientProgressBar,
    PlayerCapsule,
    ScenesPopover,
    ThemesPopover,
    VoicePicker,
    VoicePreviews,
    aa_icon,
    confirm,
    list_icon,
)

POSITION_SAVE_INTERVAL_MS = 3000
PLAY_SYMBOL = ON_ACCENT
# Playback speeds offered by the transport speed control.
PLAYBACK_SPEEDS = [0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0]
# Script text sizes the Themes & Settings A / A buttons step through.
SCRIPT_FONT_SIZES = range(14, 42, 2)
# Blank page around the script text: left, top, right, bottom (a script page's wide left margin).
PAGE_MARGINS = (280, 20, 100, 20)


STREAM_RATE = 44100  # the live stream's format: 16-bit mono


def _ms_to_bytes(ms: int) -> int:
    return max(0, int(ms)) * STREAM_RATE // 1000 * 2


def script_digest(screenplay: Screenplay) -> str:
    """Fingerprint of how a script was read, so saved audio and its timeline are
    only reused while the parsed script is exactly the same."""
    h = hashlib.sha1()
    for el in screenplay.elements:
        h.update(f"{el.kind}\x1f{el.text}\x1e".encode())
    return h.hexdigest()


def _char_map(text: str, rendered: str, base: int, skip: int) -> list[int]:
    """Document position of each character of an element's text, plus one past
    the end. The rendered lines hold the same characters in the same order; only
    the whitespace differs (indentation, and line breaks where the text has a
    space — or nothing, where a word broken at a hyphen was rejoined)."""
    positions = []
    j = skip
    for ch in text:
        if not ch.isspace():
            while j < len(rendered) and rendered[j] != ch:
                j += 1
        positions.append(base + min(j, len(rendered)))
        if not ch.isspace():
            j += 1
    positions.append(base + min(j, len(rendered)))
    return positions


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

    def __init__(self, parent=None, on_color=ACCENT):
        super().__init__(parent)
        self._on_color = on_color
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(40, 24)

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
        diameter = height - 4
        x = self.width() - diameter - 2 if on else 2
        painter.setBrush(QBrush(QColor("#ffffff") if enabled else QColor("#9a9992")))
        painter.drawEllipse(QRectF(x, 2, diameter, diameter))
        painter.end()


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
    """Read-only script view; clicking a line asks the player to jump there.

    Scene numbers aren't part of the text: they're painted in the margins on
    both sides of their heading, the way a printed script hangs them outside
    the text column."""

    clicked_at = Signal(int)  # document position
    user_scrolled = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.scene_marks: list[tuple[int, str]] = []  # (document position of a heading, its number)
        self.page_margins = (0, 0, 0, 0)  # left, top, right, bottom of the text column
        self.number_color = QColor("#000000")

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self.scene_marks:
            return
        doc = self.document()
        first = self.cursorForPosition(QPoint(0, 0)).position()
        last = self.cursorForPosition(QPoint(self.viewport().width(), self.viewport().height())).position()
        starts = [pos for pos, _ in self.scene_marks]
        font = QFont(self.font())
        font.setBold(True)
        metrics = QFontMetricsF(font)
        gap = metrics.horizontalAdvance("  ")  # a two-character gutter, as on the page
        left, _, right, _ = self.page_margins
        text_right = doc.textWidth() - right
        offset = self.verticalScrollBar().value()
        painter = QPainter(self.viewport())
        painter.setFont(font)
        painter.setPen(self.number_color)
        for pos, number in self.scene_marks[max(0, bisect.bisect_left(starts, first) - 1):]:
            if pos > last:
                break
            block = doc.findBlock(pos)
            line = block.layout().lineAt(0)
            if not line.isValid():
                continue
            baseline = doc.documentLayout().blockBoundingRect(block).top() + line.y() + line.ascent() - offset
            painter.drawText(QPointF(left - gap - metrics.horizontalAdvance(number), baseline), number)
            painter.drawText(QPointF(text_right + gap, baseline), number)
        painter.end()

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
    """The start screen: the Script Radio record on black, with New Script and
    Previous Script beneath it."""

    BUTTON_BAND = 96  # room under the record for the buttons

    def __init__(self, artwork_path: Path, previous_enabled: bool, parent=None):
        super().__init__(parent)
        self._artwork = QPixmap(str(artwork_path))

        self.new_script_button = QPushButton("New Script", self)
        self.new_script_button.setToolTip("Open a screenplay PDF")
        self.previous_script_button = QPushButton("Previous Script", self)
        self.previous_script_button.setToolTip("Resume the previous screenplay")
        self.previous_script_button.setEnabled(previous_enabled)
        font = QFont()
        font.setFamilies(SCRIPT_FAMILIES)  # the typewriter face of the record's label
        font.setPointSize(17)
        font.setBold(True)
        for button in (self.new_script_button, self.previous_script_button):
            button.setFont(font)
            button.setFixedSize(220, 46)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(
            "QPushButton { background: transparent; color: #FFFFFF; border: 2px solid #FFFFFF;"
            " border-radius: 23px; padding: 0; }"
            "QPushButton:hover { background: #FFFFFF; color: #000000; }"
            "QPushButton:pressed { background: #D9D9D9; color: #000000; border-color: #D9D9D9; }"
            "QPushButton:disabled { color: #5A5A5A; border-color: #3A3A3A; }"
        )
        self._position_buttons()

    def _art_rect(self) -> QRectF:
        side = max(0, min(self.width(), self.height() - self.BUTTON_BAND) - 24)
        return QRectF((self.width() - side) / 2, 12, side, side)

    def _position_buttons(self):
        art = self._art_rect()
        gap = 24
        total = self.new_script_button.width() + gap + self.previous_script_button.width()
        x = int((self.width() - total) / 2)
        y = int(art.bottom() + (self.height() - art.bottom() - self.new_script_button.height()) / 2)
        self.new_script_button.move(x, y)
        self.previous_script_button.move(x + self.new_script_button.width() + gap, y)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.fillRect(self.rect(), QColor("#000000"))  # the artwork's own black, edge to edge
        if not self._artwork.isNull():
            painter.drawPixmap(self._art_rect(), self._artwork, QRectF(self._artwork.rect()))

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
    # Bytes of audio now in the stream file, then start_ms, end_ms, element_index (-1: a gap)
    audio_chunk = Signal(int, int, int, int)
    sentence_ready = Signal(object)  # SentenceCue, always before its audio_chunk
    finished_ok = Signal(object)  # BuildResult
    failed = Signal(str)

    def __init__(self, elements, voice_for, beat_seconds, speak_names, checker, read_parentheticals=True):
        super().__init__()
        self._args = (elements, voice_for, beat_seconds, speak_names, checker, read_parentheticals)
        self.cancel = False

    def run(self):
        elements, voice_for, beat_seconds, speak_names, checker, read_parentheticals = self._args
        try:
            STREAM_AUDIO_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(STREAM_AUDIO_PATH, "wb") as stream:
                written = 0

                def on_chunk(data, start, end, index):
                    # On disk before the window hears of it, so it can always read what it's told about.
                    nonlocal written
                    stream.write(data)
                    stream.flush()
                    written += len(data)
                    self.audio_chunk.emit(written, start, end, -1 if index is None else index)

                result = build_audio(
                    elements,
                    voice_for,
                    beat_seconds,
                    speak_character_names=speak_names,
                    read_parentheticals=read_parentheticals,
                    checker=checker,
                    progress=lambda d, t, m: self.progressed.emit(d, t, m),
                    audio_chunk=on_chunk,
                    sentence_cue=self.sentence_ready.emit,
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
        self.voice_memory = load_voice_memory()  # character -> voice, remembered across scripts
        self.transcriber = Transcriber()
        self.screenplay: Screenplay | None = None
        self.pdf_path: str = ""
        self.element_spans: list[tuple[int, int]] = []  # text positions per element
        self.char_maps: list[list[int]] = []  # per element: document position of each character
        self._paren_spans: list[tuple[int, int]] = []  # parentheticals, drawn in the muted color
        self.cues: list[Cue] = []  # element timeline: click-to-seek, scenes, following the script
        self._cue_starts: list[int] = []
        self.sentence_cues: list[SentenceCue] = []  # sentence timeline: the highlight
        self._sentence_starts: list[int] = []
        self._highlight_range: tuple[int, int] | None = None  # document range highlighted now
        self.audio_ready = False
        self.parse_worker: ParseWorker | None = None
        self.worker: GenerationWorker | None = None
        self.cast_labels: dict[str, str] = {}  # character ("" = Narrator) -> voice label
        self._pending_casting: dict[str, str] = {}  # picks to restore (last session, single-voice toggles)
        self.casting_dialog: QDialog | None = None
        self.dialog_cast_form_host: QScrollArea | None = None
        self.dialog_single_voice_toggle: QCheckBox | None = None
        self.dialog_voice_combos: dict[str, VoicePicker] = {}
        self.voice_previews = VoicePreviews(self)
        self.voice_previews.before_play = self._pause_for_preview
        self.voice_previews.failed.connect(lambda msg: self.statusBar().showMessage(msg, 8000))
        self._themes_popover: ThemesPopover | None = None
        self.theme: Theme = theme_for(self.settings.theme)
        self._last_saved_position = -1
        self._last_highlighted_element = None
        self._auto_follow = True
        self._scroll_target_element: int | None = None
        self._scroll_animation: QPropertyAnimation | None = None
        # The live stream plays from STREAM_AUDIO_PATH as it's written, so it can seek.
        self._stream_bytes = 0  # how much of the stream file holds audio so far
        self._stream_pos = 0  # the next byte to hand to the sound card
        self._stream_origin_ms = 0  # where in the track the current sound-card run started
        self._stream_reader = None
        self._stream_sink: QAudioSink | None = None
        self._stream_device = None
        self._stream_available = False
        self._stream_playing = False
        self._stream_suspended = False
        self._stream_generation_finished = False
        self._stream_generated_ms = 0

        self._build_toolbar()
        self._build_script_view()
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
            # Its tooltip would only repeat the label (tooltips are for extra information).
            tb.widgetForAction(a).installEventFilter(self)
            return a

        def pill(tooltip, slot, icon_size):
            button = QToolButton()
            button.setObjectName("pillButton")
            button.setToolTip(tooltip)
            button.setIconSize(icon_size)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(slot)
            tb.addWidget(button)
            return button

        self.open_action = action("New PDF", self.open_pdf)
        # Like Apple Books: a list button for the scenes, "Aa" for themes & text size.
        self.scenes_button = pill("Jump to any scene", self._open_scenes_popover, QSize(20, 16))
        self.scenes_button.setText("Scenes")
        self.scenes_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.scenes_button.setEnabled(False)
        self.themes_button = pill("Choose themes and adjust settings", self._open_themes_popover, QSize(36, 24))
        self.generate_action = action("Re-Generate Audio", self._regenerate, enabled=False)

        # The script's title, centered on the window rather than in the space the
        # buttons leave, so it sits where the eye expects it (see _place_title).
        self.title_label = QLabel(tb)
        self.title_label.setObjectName("scriptTitle")
        title_font = QFont()
        title_font.setFamilies(SCRIPT_FAMILIES)
        title_font.setPointSize(17)
        title_font.setBold(True)
        title_font.setUnderline(True)
        self.title_label.setFont(title_font)
        self.title_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._title_text = ""
        tb.installEventFilter(self)

        # Bottom right: the generation progress, and Export MP3 in its place once
        # the audio is done.
        self.progress = GradientProgressBar()
        self.progress.setFixedSize(240, 22)
        self.progress.setTextVisible(True)
        self.progress.setFormat("Generating  %p%")
        self.progress.hide()
        self.export_button = QPushButton("Export MP3")
        self.export_button.setObjectName("exportButton")
        self.export_button.setFixedHeight(22)
        self.export_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.export_button.clicked.connect(self.export)
        self.export_button.hide()
        self.statusBar().addPermanentWidget(self.progress)
        self.statusBar().addPermanentWidget(self.export_button)
        self.statusBar().setSizeGripEnabled(False)

        # Credits live in the menu bar: "About Script Radio" in the app menu, and Help ▸ Credits.
        help_menu = self.menuBar().addMenu("Help")
        about = QAction("About Script Radio", self)
        about.setMenuRole(QAction.MenuRole.AboutRole)
        about.triggered.connect(self.show_credits)
        credits = QAction("Credits", self)
        credits.setMenuRole(QAction.MenuRole.NoRole)
        credits.triggered.connect(self.show_credits)
        help_menu.addAction(about)
        help_menu.addAction(credits)

    def _build_script_view(self):
        self.script_view = ClickableScript()
        self.script_view.setObjectName("scriptView")
        self.script_view.setReadOnly(True)
        self.script_view.clicked_at.connect(self._on_script_clicked)
        self._apply_script_font()
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

        # The page fills the window between the toolbar and the status bar; the
        # player floats over its bottom edge.
        self.transport_widget = QWidget()
        self.transport_widget.setObjectName("transportWidget")
        transport_layout = QVBoxLayout(self.transport_widget)
        transport_layout.setContentsMargins(0, 0, 0, 0)
        transport_layout.setSpacing(0)
        self.script_stage = QWidget()
        self.script_stage.setObjectName("scriptStage")
        script_stage_layout = QHBoxLayout(self.script_stage)
        script_stage_layout.setContentsMargins(0, 0, 0, 0)
        script_stage_layout.addWidget(self.script_view)
        transport_layout.addWidget(self.script_stage, 1)

        self.rewind_button = QPushButton("⏮")
        self.rewind_button.setObjectName("transportButton")
        self.rewind_button.setToolTip("Rewind 10 seconds")
        self.rewind_button.clicked.connect(self.rewind_playback)
        self.rewind_button.setFixedSize(46, 46)

        self.play_button = QPushButton("▶")
        self.play_button.setObjectName("transportButtonPrimary")
        self.play_button.setToolTip("Play / Pause")
        self.play_button.clicked.connect(self.toggle_play)
        self.play_button.setFixedSize(56, 56)
        self.play_button.setEnabled(False)
        self._ready_icon = _play_icon(QColor(PLAY_SYMBOL))

        self.forward_button = QPushButton("⏭")
        self.forward_button.setObjectName("transportButton")
        self.forward_button.setToolTip("Forward 10 seconds")
        self.forward_button.clicked.connect(self.forward_playback)
        self.forward_button.setFixedSize(46, 46)
        self.rewind_button.setEnabled(False)
        self.forward_button.setEnabled(False)

        self.player_capsule = PlayerCapsule(
            [self.rewind_button, self.play_button, self.forward_button], self.script_stage
        )

        self.speed_combo = QComboBox(self.script_stage)
        self.speed_combo.setObjectName("speedCombo")
        for rate in PLAYBACK_SPEEDS:
            self.speed_combo.addItem(f"{rate:g}×")
        try:
            speed_index = PLAYBACK_SPEEDS.index(self.settings.playback_rate)
        except ValueError:
            speed_index = PLAYBACK_SPEEDS.index(1.0)
        self.speed_combo.setCurrentIndex(speed_index)
        self.speed_combo.setFixedWidth(74)
        self.speed_combo.setToolTip("Playback speed")
        self.speed_combo.currentIndexChanged.connect(self._on_speed_changed)

        # Cancel floats just above the progress bar while audio is being made.
        self.cancel_button = QPushButton("Cancel Generation", self.script_stage)
        self.cancel_button.setObjectName("cancelButton")
        self.cancel_button.setFixedHeight(30)  # the stylesheet rounds it with half of this
        self.cancel_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cancel_button.clicked.connect(self._confirm_cancel)
        self.cancel_button.hide()

        self.script_stage.installEventFilter(self)
        self.setCentralWidget(self.transport_widget)
        self._set_ready_indicator(False)

    def _place_overlays(self):
        """Float the player over the bottom of the page, centered, with the speed
        control beside it, and Cancel Generation just above the progress bar."""
        stage = self.script_stage
        capsule = self.player_capsule
        capsule.adjustSize()
        x = (stage.width() - capsule.width()) // 2
        y = stage.height() - capsule.height() - 20
        capsule.move(x, y)
        capsule.raise_()
        speed = self.speed_combo
        speed.move(x + capsule.width() + 14, y + (capsule.height() - speed.height()) // 2)
        speed.raise_()
        if self.cancel_button.isVisible():
            self.cancel_button.adjustSize()
            bar = stage.mapFromGlobal(self.progress.mapToGlobal(QPoint(0, 0)))
            bar_center = bar.x() + self.progress.width() // 2 if self.progress.isVisible() else stage.width() - 140
            cx = min(stage.width() - self.cancel_button.width() - 12, bar_center - self.cancel_button.width() // 2)
            self.cancel_button.move(cx, stage.height() - self.cancel_button.height() - 8)
            self.cancel_button.raise_()

    def _place_title(self):
        """Center the script's title on the toolbar, shortened with "…" rather than
        ever running into the buttons on the left."""
        tb, label = self.toolbar, self.title_label
        last_button = tb.widgetForAction(self.generate_action)
        left_edge = (last_button.geometry().right() if last_button is not None else 0) + 24
        room = max(0, int(2 * (tb.width() / 2 - left_edge)))
        label.setText(QFontMetrics(label.font()).elidedText(self._title_text, Qt.TextElideMode.ElideRight, room))
        label.adjustSize()
        label.move(int(tb.width() / 2 - label.width() / 2), int((tb.height() - label.height()) / 2))
        label.raise_()

    def _set_title(self, title: str):
        self._title_text = title.upper()
        self._place_title()

    def _build_landing_page(self):
        artwork = Path(__file__).resolve().parents[2] / "assets" / "art" / "landing.jpg"
        self.landing_page = ScriptRadioLanding(
            artwork,
            previous_enabled=Session.load() is not None,
        )
        self.landing_page.setObjectName("landingPage")
        self.landing_page.new_script_button.clicked.connect(self.open_pdf)
        self.landing_page.previous_script_button.clicked.connect(self.open_previous)

    def _apply_script_font(self):
        font = QFont()
        # Explicit real fonts (all present on macOS) instead of a "monospace"
        # style hint, which made Qt search for a non-existent "Courier New,monospace".
        font.setFamilies(["Courier New", "Menlo", "Courier"])
        font.setPointSize(self.settings.script_font_size)
        self.script_view.setFont(font)
        self._apply_page_margins()

    def _apply_page_margins(self):
        """Inset the script text like a printed page. Margins live in the document,
        not the widget, so the scroll bar stays at the page's right edge."""
        doc = self.script_view.document()
        doc.setDocumentMargin(0)
        fmt = doc.rootFrame().frameFormat()
        left, top, right, bottom = PAGE_MARGINS
        fmt.setLeftMargin(left)
        fmt.setTopMargin(top)
        fmt.setRightMargin(right)
        fmt.setBottomMargin(bottom)
        doc.rootFrame().setFrameFormat(fmt)

    # ---------- settings ----------

    def _on_speed_changed(self, index):
        rate = PLAYBACK_SPEEDS[index]
        self.settings.playback_rate = rate
        self.settings.save()
        self.player.setPlaybackRate(rate)
        if not self.audio_ready and self._stream_available:
            # The live stream plays at 1×; the finished file can change speed.
            self.statusBar().showMessage(f"Playback speed {rate:g}× starts once the audio finishes generating.", 8000)
        else:
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
            self._clear_highlight()

    def _on_single_voice_toggled(self, on):
        self.settings.single_voice = on
        self.settings.save()
        toggle = self.dialog_single_voice_toggle
        if toggle is not None and toggle.isChecked() != on:
            toggle.blockSignals(True)
            toggle.setChecked(on)
            toggle.blockSignals(False)
        if self.screenplay:
            # Keep the picks made so far while the character rows are hidden.
            self._pending_casting = {**self._pending_casting, **self._casting_labels(), **self._dialog_labels()}
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

    def _set_ready_indicator(self, ready: bool, play_sound: bool = False):
        # Green arrow next to Play = there is audio to play.
        self.play_button.setIcon(self._ready_icon if ready else QIcon())
        self.play_button.setText("" if ready else "▶")
        self.play_button.setToolTip(
            "Audio is ready - press Play" if ready else "Generate audio first"
        )
        # The "ding" only fires for freshly generated audio, never when
        # reopening audio that was made in an earlier session.
        if ready and play_sound:
            self.ready_sound.play()


    def _apply_theme(self, key: str):
        """Restyle everything for a reading theme (see theme.py)."""
        t = theme_for(key)
        self.theme = t
        themes.set_current(t)
        self.settings.theme = t.key
        self.settings.save()

        self.setStyleSheet(themes.stylesheet(t))
        palette = QPalette()
        for role, color in (
            (QPalette.ColorRole.Window, t.window), (QPalette.ColorRole.WindowText, t.text),
            (QPalette.ColorRole.Base, t.raised), (QPalette.ColorRole.AlternateBase, t.raised_hover),
            (QPalette.ColorRole.Text, t.text), (QPalette.ColorRole.Button, t.raised),
            (QPalette.ColorRole.ButtonText, t.text), (QPalette.ColorRole.Highlight, t.highlight),
            (QPalette.ColorRole.HighlightedText, t.text), (QPalette.ColorRole.ToolTipBase, t.panel),
            (QPalette.ColorRole.ToolTipText, t.text), (QPalette.ColorRole.PlaceholderText, t.muted),
        ):
            palette.setColor(role, QColor(color))
        QApplication.setPalette(palette)
        # Tooltips are windows of their own, so their style goes on the application.
        QApplication.instance().setStyleSheet(tooltip_stylesheet(t))

        self._highlight_color = QColor(t.highlight)
        self.script_view.number_color = QColor(t.text)
        self.script_view.viewport().update()
        self.player_capsule.update()
        self._ready_icon = _play_icon(QColor(PLAY_SYMBOL))
        self.scenes_button.setIcon(list_icon(t.on_chrome))
        self.themes_button.setIcon(aa_icon(t.on_chrome))
        self._recolor_parentheticals()
        if self._highlight_range is not None:
            self._show_highlight(*self._highlight_range)
        for picker in self.dialog_voice_combos.values():
            picker.refresh_icons()
        if self._themes_popover is not None:
            self._themes_popover.set_theme(t.key)


    def _report_engines(self):
        notes = []
        for e in self.engines:
            ok, why = e.available()
            if not ok:
                notes.append(f"{e.name} unavailable: {why}")
        self.statusBar().showMessage(" | ".join(notes) or "Ready", 15000)

    def _open_themes_popover(self):
        """The "Aa" button: script text size and the reading themes, like Apple Books."""
        popover = ThemesPopover(self, self.theme.key)
        popover.theme_chosen.connect(self._apply_theme)
        popover.text_size_step.connect(self._step_script_font)
        popover.destroyed.connect(lambda: setattr(self, "_themes_popover", None))
        self._themes_popover = popover
        popover.popup_under(self.themes_button)

    def _step_script_font(self, step: int):
        sizes = list(SCRIPT_FONT_SIZES)
        current = min(sizes, key=lambda s: abs(s - self.settings.script_font_size))
        size = sizes[max(0, min(len(sizes) - 1, sizes.index(current) + step))]
        if size == self.settings.script_font_size:
            return
        self.settings.script_font_size = size
        self.settings.save()
        self._apply_script_font()
        if self._last_highlighted_element is not None:
            QTimer.singleShot(0, lambda: self._center_element(self._last_highlighted_element, force=True))
        self.statusBar().showMessage(f"Script text size: {size} pt", 3000)

    def _open_scenes_popover(self):
        if not self.screenplay:
            return
        current = None
        if self._last_highlighted_element is not None:
            current = next(
                (i for i in reversed(range(self._last_highlighted_element + 1))
                 if self.screenplay.elements[i].kind == "scene"),
                None,
            )
        popover = ScenesPopover(self, self._scene_entries(), current)
        popover.jump.connect(self._jump_to_element)
        popover.popup_under(self.scenes_button)

    def _scene_entries(self) -> list[tuple[int, str, str, str]]:
        """(element index, number, heading, start time) for every scene heading."""
        entries = []
        for i, el in enumerate(self.screenplay.elements):
            if el.kind != "scene":
                continue
            heading = speech.strip_scene_number(el.text)
            cue = self._first_cue_from(i)
            when = ""
            # A time once the scene's audio exists (while generating: if it's been made yet).
            if cue is not None and (self.audio_ready or cue.start_ms < self._stream_generated_ms):
                seconds = cue.start_ms // 1000
                h, m, s = seconds // 3600, seconds // 60 % 60, seconds % 60
                when = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
            entries.append((i, el.scene_number or "", heading, when))
        return entries

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
        dialog.setMinimumSize(600, 520)
        dialog.resize(700, 720)

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(28, 24, 28, 22)
        layout.setSpacing(12)

        title = QLabel("Casting Panel")
        title.setObjectName("castingTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setFont(ui_font(26, QFont.Weight.DemiBold))
        layout.addWidget(title)

        subtitle = QLabel(
            "Assign voices for each character or choose one voice to read the lines of dialogue below."
        )
        subtitle.setWordWrap(True)
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle.setObjectName("castingSubtitle")
        subtitle.setFont(ui_font(14))
        layout.addWidget(subtitle)

        sv_row = QWidget()
        sv_row_layout = QHBoxLayout(sv_row)
        sv_row_layout.setContentsMargins(6, 4, 6, 4)
        sv_label = QLabel("One voice reads the whole script")
        sv_label.setObjectName("singleVoiceLabel")
        sv_label.setFont(ui_font(15, QFont.Weight.DemiBold))
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
        # Every voice gets an audition clip; render any that aren't cached yet.
        options = self._voice_options()
        self.voice_previews.set_options(options)
        self.voice_previews.warm([label for label, _ in options])
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        self.voice_previews.stop()
        if accepted:
            chosen = self._dialog_labels()
            self._pending_casting = {**self._pending_casting, **chosen}
            self._remember_voices(chosen)
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
        title.setFont(ui_font(26, QFont.Weight.DemiBold))
        layout.addWidget(title)

        subtitle = QLabel("Fine-tune the reading, then press Generate Audio to start.")
        subtitle.setObjectName("castingSubtitle")
        subtitle.setWordWrap(True)
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle.setFont(ui_font(14))
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
        # Label is just the voice name; the engine prefix ("Pocket-TTS: ") is
        # dropped. It's only added back to disambiguate if two available engines
        # ever expose a voice with the same name.
        raw = [(v, engine) for engine in self.engines if engine.available()[0] for v in engine.voices()]
        counts = Counter(name for name, _ in raw)
        return [
            (name if counts[name] == 1 else f"{name} ({engine.name})", (engine, name))
            for name, engine in raw
        ]

    def _default_narrator_label(self) -> str | None:
        options = self._voice_options()
        for engine in self.engines:
            pick = getattr(engine, "default_narrator", lambda: None)()
            if pick and engine.available()[0]:
                for label, (eng, v) in options:
                    if eng is engine and v == pick:
                        return label
        return None

    def _initial_casting(self, labels: list[str]) -> dict[str, str]:
        """Voice label for the Narrator ("") and every character: the pick already
        made for this script, else the voice the character had in another script,
        else a default. Characters' defaults skip past the narrator's voice."""
        if not labels:
            return {}

        def valid(pick: str | None) -> str | None:
            if pick and pick not in labels and ": " in pick:
                pick = pick.split(": ", 1)[1]  # old "Engine: voice" format
            return pick if pick in labels else None

        def recall(mapping: dict[str, str], name: str) -> str | None:
            """The pick saved for this character, also under an older spelling of its
            cue ("MAN’S VOICE", "CHIEF CARTER *", "JAN (ON THE PHONE)")."""
            if valid(mapping.get(name)):
                return valid(mapping[name])
            return next((valid(v) for k, v in mapping.items() if k and valid(v) and character_name(k) == name), None)

        remembered = self._pending_casting
        # A remembered narrator whose sample was renamed or deleted is skipped, so the
        # default narrator wins instead of whatever voice happens to be listed first.
        narrator = next(
            (p for p in map(valid, (remembered.get(""), self.voice_memory.get(""), self._default_narrator_label())) if p),
            None,
        )
        casting = {"": narrator or labels[0]}
        offset = 1 + (1 if narrator else 0)
        for i, name in enumerate(self.screenplay.characters if self.screenplay else []):
            casting[name] = (
                recall(remembered, name) or recall(self.voice_memory, name) or labels[(i + offset) % len(labels)]
            )
        return casting

    def _rebuild_casting(self):
        """Recompute the casting and, while the Casting Panel is open, its rows."""
        labels = [label for label, _ in self._voice_options()]
        self.cast_labels = self._initial_casting(labels)
        self.dialog_voice_combos = {}
        host = self.dialog_cast_form_host
        if host is None:
            return
        form_widget = QWidget()
        form = QFormLayout(form_widget)
        form.setContentsMargins(4, 4, 12, 4)
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        names = [""] if self.settings.single_voice else ["", *(self.screenplay.characters if self.screenplay else [])]
        for name in names:
            picker = VoicePicker(labels, self.cast_labels.get(name, labels[0] if labels else ""), self.voice_previews)
            self.dialog_voice_combos[name] = picker
            title = ("Voice" if self.settings.single_voice else "Narrator") if name == "" else speech.title_case(name)
            form.addRow(self._cast_name_label(title), picker)
            if name == "" and self.settings.single_voice:
                hint = QLabel("Reads every line, all characters included.")
                hint.setObjectName("hint")
                form.addRow("", hint)
        host.setWidget(form_widget)

    def _cast_name_label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setFont(ui_font(14))
        label.setMinimumWidth(150)
        label.setMinimumHeight(40)  # the picker's height, so the name lines up with its middle
        label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        return label

    def _dialog_labels(self) -> dict[str, str]:
        return {name: picker.currentText() for name, picker in self.dialog_voice_combos.items()}

    def _casting(self) -> dict[str, tuple]:
        by_label = dict(self._voice_options())
        return {name: by_label[label] for name, label in self.cast_labels.items() if label in by_label}

    def _remember_voices(self, labels: dict[str, str]) -> None:
        """Persist character -> voice choices so future scripts auto-fill."""
        valid = {label for label, _ in self._voice_options()}
        for name, label in labels.items():
            if label in valid:
                self.voice_memory[name] = label
        save_voice_memory(self.voice_memory)

    def _casting_labels(self) -> dict[str, str]:
        return dict(self.cast_labels)

    def _pause_for_preview(self):
        """A voice preview is about to play; don't talk over the script."""
        if self._stream_sink is not None and self._stream_playing:
            self.pause_playback()
        elif self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.pause_playback()


    # ---------- script display ----------

    INDENTS = {"scene": 0, "action": 0, "character": 22, "dialogue": 10, "parenthetical": 16}

    def _render_script(self):
        self.script_view.clear()
        self._apply_page_margins()
        self.element_spans = []
        self.char_maps = []
        self._paren_spans = []
        self._highlight_range = None
        cursor = QTextCursor(self.script_view.document())

        bold = QTextCharFormat()
        bold.setFontWeight(QFont.Weight.Bold)
        plain = QTextCharFormat()

        scene_marks = []
        for el in self.screenplay.elements:
            fmt = bold if el.kind in ("scene", "character") else plain
            pad = " " * self.INDENTS[el.kind]
            start = cursor.position()
            if el.kind == "scene":
                # The heading sits at the margin; its number (the script's own, or the
                # app's for an unnumbered script) is painted outside it, both sides.
                shown = speech.strip_scene_number(el.text)
                offset = el.text.find(shown) if shown else -1
                if offset < 0:
                    shown, offset = el.text, 0
                cursor.insertText(shown + "\n", fmt)
                self.char_maps.append([start + min(max(i - offset, 0), len(shown)) for i in range(len(el.text) + 1)])
                if el.scene_number:
                    scene_marks.append((start, el.scene_number))
            else:
                lines = el.lines or [el.text]
                rendered = "\n".join(pad + line for line in lines)
                cursor.insertText(rendered + "\n", fmt)
                self.char_maps.append(_char_map(el.text, rendered, start, len(pad)))
            self.element_spans.append((start, cursor.position() - 1))
            if el.kind == "parenthetical":
                self._paren_spans.append((start, cursor.position() - 1))
            if el.kind in ("scene", "action", "dialogue"):
                cursor.insertText("\n", plain)

        line_count = self.script_view.viewport().height() // max(1, self.script_view.fontMetrics().lineSpacing())
        cursor.insertText("\n" * max(1, line_count), plain)
        self.script_view.scene_marks = scene_marks
        self.script_view.page_margins = PAGE_MARGINS
        self._recolor_parentheticals()

    def _recolor_parentheticals(self):
        """Parentheticals are set in the theme's muted color, like a script's asides."""
        if not self._paren_spans:
            return
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(self.theme.muted))
        cursor = QTextCursor(self.script_view.document())
        for start, end in self._paren_spans:
            cursor.setPosition(start)
            cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
            cursor.mergeCharFormat(fmt)

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
        self._set_title(screenplay.title)
        self._render_script()
        self.scenes_button.setEnabled(True)
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
        QTimer.singleShot(0, self._place_title)
        QTimer.singleShot(0, self._place_overlays)

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
        self._set_title(screenplay.title)
        self._render_script()
        self.scenes_button.setEnabled(True)
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
        self._set_cues([], [])
        self.audio_ready = False
        self._set_ready_indicator(False)
        for button in (self.play_button, self.rewind_button, self.forward_button):
            button.setEnabled(False)
        self._show_generation(False)

    def _show_generation(self, running: bool) -> None:
        """Bottom right: while audio is being made, the progress bar with Cancel
        Generation above it; once there's audio, Export MP3 in the bar's place."""
        self.progress.setVisible(running)
        self.cancel_button.setVisible(running)
        self.export_button.setVisible(not running and self.audio_ready)
        self.generate_action.setEnabled(not running and self.screenplay is not None and bool(self._voice_options()))
        if running:
            QTimer.singleShot(0, self._place_overlays)  # once the status bar has laid the bar out

    def _confirm_cancel(self):
        if not (self.worker and self.worker.isRunning()):
            return
        if confirm(
            self,
            "Are you sure you want to cancel the audio generation in-progress?",
            no="No, go back",
            yes="Yes, cancel",
        ) and self.worker and self.worker.isRunning():
            self.worker.cancel = True
            self.cancel_button.setEnabled(False)
            self.statusBar().showMessage("Cancelling…")

    def _set_cues(self, cues: list[Cue], sentences: list[SentenceCue]) -> None:
        """Replace both timelines (and the start-time indexes used to search them)."""
        self.cues = list(cues)
        self._cue_starts = [c.start_ms for c in self.cues]
        self.sentence_cues = list(sentences)
        self._sentence_starts = [c.start_ms for c in self.sentence_cues]

    def _regenerate(self):
        """Toolbar 'Re-Generate Audio': reopen Casting → Customize Audio to start
        a fresh rendition (it's disabled while audio is being made)."""
        if (self.worker and self.worker.isRunning()) or not self.screenplay:
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
        self._set_cues([], [])
        self._clear_highlight()
        self.player.setSource(QUrl())
        self._set_ready_indicator(False)
        for button in (self.play_button, self.rewind_button, self.forward_button):
            button.setEnabled(False)
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
        self.worker.sentence_ready.connect(self._on_sentence_cue)
        self.worker.finished_ok.connect(self._on_generated)
        self.worker.failed.connect(self._on_failed)
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.cancel_button.setEnabled(True)
        self._show_generation(True)
        self.worker.start()

    def _on_progress(self, done, total, msg):
        self.progress.setMaximum(total)
        self.progress.setValue(done)
        self.statusBar().showMessage(msg)

    def _on_generation_progress(self, done: int, total: int):
        self.progress.setMaximum(total)
        self.progress.setValue(done)
        if done >= total:
            self.cancel_button.hide()  # at 100% there's nothing left to cancel
        threshold = max(1, (total + 9) // 10)
        if not self._stream_available and done >= threshold:
            self._stream_available = True
            for button in (self.play_button, self.rewind_button, self.forward_button):
                button.setEnabled(True)
            self._set_ready_indicator(True, play_sound=True)
            self.statusBar().showMessage(
                f"First audio ready ({done}/{total} passages); generation continues in the background"
            )

    def _on_audio_chunk(self, stream_bytes: int, start_ms: int, end_ms: int, element_index: int):
        self._stream_bytes = stream_bytes
        self._stream_generated_ms = max(self._stream_generated_ms, end_ms)
        if element_index >= 0:
            if self.cues and self.cues[-1].element_index == element_index:
                self.cues[-1].end_ms = end_ms
            else:
                self.cues.append(Cue(start_ms, end_ms, element_index))
                self._cue_starts.append(start_ms)
        if self._stream_sink is not None and self._stream_playing:
            if self._stream_suspended:
                self._stream_sink.resume()
                self._stream_suspended = False
            self._pump_stream()

    def _on_sentence_cue(self, cue: SentenceCue):
        self.sentence_cues.append(cue)
        self._sentence_starts.append(cue.start_ms)

    def _on_generated(self, result):
        audio = result.audio
        self._set_cues(result.cues, result.sentences)

        LAST_AUDIO_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.player.setSource(QUrl())  # release the old file before overwriting
        audio.export(LAST_AUDIO_PATH, format="wav")
        self.player.setSource(QUrl.fromLocalFile(str(LAST_AUDIO_PATH)))
        self.player.setPlaybackRate(self.settings.playback_rate)
        self.audio_ready = True
        self._stream_generation_finished = True
        if self._stream_sink is None:
            self._release_stream_file()
            self._set_ready_indicator(True)
            self._save_session(position_ms=0)
        else:
            # Carry on from the same spot in the finished file, where speed changes work.
            self._handoff_stream_to_player()
        # Once the full audio exists, all transport controls work (seeking
        # switches from the live stream to the finished file).
        for button in (self.play_button, self.rewind_button, self.forward_button):
            button.setEnabled(True)
        self._show_generation(False)  # Export MP3 takes the finished bar's place
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
        self._show_generation(False)
        self._stream_generation_finished = True
        if self._stream_sink is None and not self._stream_available:
            self._release_stream_file()
        if self._stream_sink is not None and self._stream_playing:
            self._pump_stream()
        self.statusBar().showMessage(f"Generation failed: {msg}")
        if msg == "Cancelled.":
            self.statusBar().showMessage("Audio generation cancelled.")
        else:
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
        if event.type() == QEvent.Type.ToolTip and isinstance(watched, QToolButton):
            if watched.toolTip().replace("&", "").strip() == watched.text().replace("&", "").strip():
                return True  # a tip that only repeats the button's label is noise
        if event.type() == QEvent.Type.Resize:
            if watched is self.script_stage:
                self._place_overlays()
            elif watched is self.toolbar:
                QTimer.singleShot(0, self._place_title)
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
            self._clear_highlight()
            self.statusBar().showMessage(f"Paused — resumed at {self.player.position() // 60000}m {self.player.position() % 60000 // 1000}s")

    def stop_playback(self):
        if self._stream_sink is not None:
            self._stop_stream_sink()
        self.pause_playback()

    def _reset_stream(self):
        self._stop_stream_sink()
        self._close_stream_reader()
        self._stream_bytes = 0
        self._stream_pos = 0
        self._stream_origin_ms = 0
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

    def _stream_reader_file(self):
        if self._stream_reader is None:
            self._stream_reader = open(STREAM_AUDIO_PATH, "rb")
        return self._stream_reader

    def _close_stream_reader(self):
        if self._stream_reader is not None:
            self._stream_reader.close()
            self._stream_reader = None

    def _release_stream_file(self):
        """The finished WAV has the same audio; drop the stream's copy."""
        self._close_stream_reader()
        if self._stream_generation_finished:
            STREAM_AUDIO_PATH.unlink(missing_ok=True)

    def _stream_position_ms(self) -> int:
        """Where the live stream is in the track."""
        if self._stream_sink is None:
            return self._stream_origin_ms
        heard = self._stream_origin_ms + self._stream_sink.processedUSecs() // 1000
        return int(min(heard, self._stream_generated_ms))

    def _stream_seek(self, ms: int, *, play: bool | None = None):
        """Jump within the audio generated so far, while the rest is still being made."""
        ms = max(0, min(int(ms), self._stream_generated_ms - 250))
        playing = self._stream_playing if play is None else play
        self._stop_stream_sink()
        self._stream_origin_ms = ms
        self._stream_pos = _ms_to_bytes(ms)
        self._auto_follow = True
        if playing:
            self._start_stream()
        else:
            self._set_stream_button(False)
        self._on_position(ms)

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
        available = self._stream_bytes - self._stream_pos
        if available > 0:
            writable = min(available, self._stream_sink.bytesFree())
            writable -= writable % 2
            if writable:
                reader = self._stream_reader_file()
                reader.seek(self._stream_pos)
                written = self._stream_device.write(reader.read(writable))
                if written < 0:
                    self.statusBar().showMessage("Live audio output stopped unexpectedly")
                    self._stop_stream_sink()
                    return
                self._stream_pos += written

        self._on_position(self._stream_position_ms())
        drained = self._stream_pos >= self._stream_bytes
        if drained and self._stream_sink.bytesFree() >= self._stream_sink.bufferSize():
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
        position_ms = self._stream_position_ms()
        was_playing = self._stream_playing
        self._stop_stream_sink()
        self._release_stream_file()
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
        position_ms = self._stream_position_ms()
        was_playing = self._stream_playing
        self._stop_stream_sink()
        self._release_stream_file()
        if self.player.source().isEmpty():
            self.player.setSource(QUrl.fromLocalFile(str(LAST_AUDIO_PATH)))
        self.player.setPlaybackRate(self.settings.playback_rate)
        self.player.setPosition(position_ms)
        for button in (self.play_button, self.rewind_button, self.forward_button):
            button.setEnabled(True)
        if was_playing:
            self.player.play()

    def _first_cue_from(self, element_index: int) -> Cue | None:
        """The first stretch of audio at or after this element (a parenthetical that
        isn't read, or a name cue when names are skipped, has none of its own)."""
        i = bisect.bisect_left([c.element_index for c in self.cues], element_index)
        return self.cues[i] if i < len(self.cues) else None

    def _on_script_clicked(self, doc_pos: int):
        i = self._element_at(doc_pos)
        if i is not None:
            self._seek_to_element(i)

    def _jump_to_element(self, element_index: int):
        """Scenes list: go to that scene, and play from it if its audio exists."""
        self._last_highlighted_element = element_index
        self._center_element(element_index, force=True)
        self._seek_to_element(element_index)

    def _seek_to_element(self, element_index: int):
        if not self.cues:
            return
        target = self._first_cue_from(element_index)
        if not self.audio_ready:
            # Still generating: jump within what's been made so far.
            if not self._stream_available:
                return
            if target is None or target.start_ms >= self._stream_generated_ms - 250:
                self.statusBar().showMessage("That part hasn't been generated yet.", 5000)
                return
            self._last_highlighted_element = element_index
            self._stream_seek(target.start_ms, play=True)
            self._center_element(target.element_index, force=True)
            return
        self._switch_to_file_playback()
        if target is None:
            return
        self._last_highlighted_element = element_index
        self.player.setPosition(target.start_ms)
        self._auto_follow = True
        self._on_position(target.start_ms)
        self._center_element(target.element_index, force=True)
        if self.player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
            self.player.play()

    def _doc_range(self, element_index: int, start_char: int, end_char: int) -> tuple[int, int]:
        """Document range of characters [start_char, end_char) of an element's text."""
        positions = self.char_maps[element_index]
        start_char = max(0, min(start_char, len(positions) - 1))
        end_char = max(start_char, min(end_char, len(positions) - 1))
        if end_char == start_char:
            return positions[start_char], positions[start_char]
        return positions[start_char], positions[end_char - 1] + 1

    def _on_position(self, ms):
        """Highlight what's being heard: the current sentence, or the whole element
        for audio made before sentence timing existed."""
        if not self.settings.highlight or not self.screenplay:
            return
        if self.sentence_cues:
            i = bisect.bisect_right(self._sentence_starts, ms) - 1
            if i < 0 or ms >= self.sentence_cues[i].end_ms:
                return  # between sentences: keep the last highlight
            cue = self.sentence_cues[i]
            element_index = cue.element_index
            if element_index >= len(self.char_maps):
                return
            start, end = self._doc_range(element_index, cue.start_char, cue.end_char)
        elif self.cues:
            i = bisect.bisect_right(self._cue_starts, ms) - 1
            if i < 0 or ms >= self.cues[i].end_ms:
                return
            element_index = self.cues[i].element_index
            if element_index >= len(self.element_spans):
                return
            start, end = self.element_spans[element_index]
        else:
            return
        element_changed = element_index != self._last_highlighted_element
        self._last_highlighted_element = element_index
        if (start, end) != self._highlight_range:
            self._show_highlight(start, end)
            if self._auto_follow:
                if element_changed and element_index != self._scroll_target_element:
                    self._center_element(element_index)
                else:
                    self._keep_in_view(start, end)

    def _show_highlight(self, start: int, end: int):
        """Highlight a document range line by line, skipping each line's indentation
        so the color hugs the words instead of running into the margin."""
        self._highlight_range = (start, end)
        doc = self.script_view.document()
        selections = []
        block = doc.findBlock(start)
        while block.isValid() and block.position() < end:
            text = block.text()
            text_start = block.position() + len(text) - len(text.lstrip(" "))
            line_start, line_end = max(start, text_start), min(end, block.position() + len(text))
            if line_start < line_end:
                sel = QTextEdit.ExtraSelection()
                sel.cursor = QTextCursor(doc)
                sel.cursor.setPosition(line_start)
                sel.cursor.setPosition(line_end, QTextCursor.MoveMode.KeepAnchor)
                sel.format.setBackground(self._highlight_color)
                selections.append(sel)
            block = block.next()
        self.script_view.setExtraSelections(selections)

    def _clear_highlight(self):
        self._highlight_range = None
        self.script_view.setExtraSelections([])

    def _range_rect(self, start: int, end: int) -> tuple[int, int]:
        doc = self.script_view.document()
        first, last = QTextCursor(doc), QTextCursor(doc)
        first.setPosition(start)
        last.setPosition(max(start, end - 1))
        top_rect, bottom_rect = self.script_view.cursorRect(first), self.script_view.cursorRect(last)
        return min(top_rect.top(), bottom_rect.top()), max(top_rect.bottom(), bottom_rect.bottom())

    def _keep_in_view(self, start: int, end: int):
        """A long passage: re-center when the sentence being read drifts out of the
        middle of the view."""
        top, bottom = self._range_rect(start, end)
        height = self.script_view.viewport().height()
        if top < height * 0.15 or bottom > height * 0.85:
            self._scroll_to_range(start, end)

    def _center_element(self, element_index: int, *, force: bool = False):
        if not force and not self._auto_follow:
            return
        if element_index < 0 or element_index >= len(self.element_spans):
            return
        start, end = self.element_spans[element_index]
        self._scroll_target_element = element_index
        top, bottom = self._range_rect(start, end)
        if bottom - top > self.script_view.viewport().height() * 0.7 and self._highlight_range:
            start, end = self._highlight_range  # too tall to center: center the sentence instead
        self._scroll_to_range(start, end)

    def _scroll_to_range(self, start: int, end: int):
        top, bottom = self._range_rect(start, end)
        scrollbar = self.script_view.verticalScrollBar()
        target = _centered_scroll_value(
            scrollbar.value(), top, bottom, self.script_view.viewport().height(), scrollbar.maximum()
        )
        if self._scroll_animation is not None:
            self._scroll_animation.stop()
            self._scroll_animation.deleteLater()
        self._scroll_animation = QPropertyAnimation(scrollbar, b"value", self)
        self._scroll_animation.setDuration(450)
        self._scroll_animation.setStartValue(scrollbar.value())
        self._scroll_animation.setEndValue(target)
        self._scroll_animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._scroll_animation.start()

    def rewind_playback(self):
        if not self.audio_ready:
            if self._stream_available:  # still generating: within what's been made
                self._stream_seek(self._stream_position_ms() - 10000)
            return
        self._switch_to_file_playback()
        self.player.setPosition(max(0, self.player.position() - 10000))
        self._on_position(self.player.position())

    def forward_playback(self):
        if not self.audio_ready:
            if self._stream_available:
                self._stream_seek(self._stream_position_ms() + 10000)
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
            sentence_cues=[[c.start_ms, c.end_ms, c.element_index, c.start_char, c.end_char] for c in self.sentence_cues],
            script_digest=script_digest(self.screenplay),
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
        if session.script_digest != script_digest(self.screenplay):
            # The script reads differently now (the PDF changed, or the app reads
            # scripts better): the old audio's timeline would point at the wrong
            # lines, so open the script without it.
            self._show_workspace()
            self.statusBar().showMessage(
                "This script is read differently since its audio was made — press Re-Generate Audio for fresh audio."
            )
            return True
        self._set_cues([Cue(*c) for c in session.cues], [SentenceCue(*c) for c in session.sentence_cues])
        self.player.setSource(QUrl.fromLocalFile(str(LAST_AUDIO_PATH)))
        self.player.setPlaybackRate(self.settings.playback_rate)
        self.audio_ready = True
        self._set_ready_indicator(True)
        for button in (self.play_button, self.rewind_button, self.forward_button):
            button.setEnabled(True)
        self._show_generation(False)
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
        self._close_stream_reader()
        self.voice_previews.shutdown()
        self._save_position()
        super().closeEvent(event)
