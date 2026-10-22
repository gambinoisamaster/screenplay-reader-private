"""Custom widgets: popovers, the voice picker with audition clips, theme cards
and the scene list. Colors come from theme.current() at paint time and from
the app stylesheet (theme.stylesheet), so everything follows the theme."""

from __future__ import annotations

import threading
from collections import deque

from PySide6.QtCore import QPoint, QPointF, QRectF, QSize, Qt, QThread, QTimer, QUrl, Signal, QObject
from PySide6.QtGui import QColor, QFont, QIcon, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap, QPolygonF
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractItemView,
    QButtonGroup,
    QCompleter,
    QDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..voice_preview import make_preview, preview_path
from . import theme as themes
from .theme import ACCENT, ACCENT_HOVER, ON_ACCENT, PROGRESS_BLUES, SERIF_FAMILIES, THEMES, ui_font


# ---------- icons (painted, so they're crisp on Retina and follow the theme) ----------


def _canvas(width: int, height: int) -> tuple[QPixmap, QPainter]:
    pixmap = QPixmap(width * 2, height * 2)
    pixmap.setDevicePixelRatio(2.0)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    return pixmap, painter


def chevron_icon(color: str, size: int = 16) -> QIcon:
    pixmap, p = _canvas(size, size)
    pen = QPen(QColor(color), 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    m = size / 2
    p.drawPolyline(QPolygonF([QPointF(m - 4.5, m - 2), QPointF(m, m + 2.5), QPointF(m + 4.5, m - 2)]))
    p.end()
    return QIcon(pixmap)


def aa_icon(color: str) -> QIcon:
    """A small and a large A — the Apple Books "text & themes" mark."""
    pixmap, p = _canvas(36, 24)
    p.setPen(QColor(color))
    small, large = ui_font(12, QFont.Weight.DemiBold), ui_font(12, QFont.Weight.DemiBold)
    small.setPixelSize(15)
    large.setPixelSize(23)
    baseline = 21
    p.setFont(small)
    p.drawText(QPointF(4, baseline), "A")
    p.setFont(large)
    p.drawText(QPointF(16, baseline), "A")
    p.end()
    return QIcon(pixmap)


def list_icon(color: str) -> QIcon:
    pixmap, p = _canvas(20, 16)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(color))
    for y in (3, 8, 13):
        p.drawEllipse(QPointF(3, y), 1.5, 1.5)
        p.drawRoundedRect(QRectF(7, y - 1, 12, 2), 1, 1)
    p.end()
    return QIcon(pixmap)


def half_circle_icon(color: str, size: int = 20) -> QIcon:
    """◐ — switch between a light and a dark theme."""
    pixmap, p = _canvas(size, size)
    c = QColor(color)
    r = QRectF(3, 3, size - 6, size - 6)
    p.setPen(QPen(c, 1.6))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawEllipse(r)
    path = QPainterPath()
    path.moveTo(r.center())
    path.arcTo(r, 90, 180)
    path.closeSubpath()
    p.fillPath(path, c)
    p.end()
    return QIcon(pixmap)


def _hairline() -> QFrame:
    line = QFrame()
    line.setObjectName("hairline")
    line.setFixedHeight(1)
    return line


# ---------- popover ----------


class Popover(QWidget):
    """A floating rounded panel, like the popovers in Apple's apps, optionally with
    an arrow pointing at the button that opened it. Closes on click-outside or Esc."""

    RADIUS = 14
    ARROW_W, ARROW_H = 20, 9

    def __init__(self, parent: QWidget, *, arrow: bool = True, width: int | None = None):
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._arrow = arrow
        self._arrow_x = 0.0
        if width:
            self.setFixedWidth(width)
        self.body = QVBoxLayout(self)
        top = self.ARROW_H if arrow else 0
        self.body.setContentsMargins(12, 10 + top, 12, 12)
        self.body.setSpacing(10)

    def paintEvent(self, event):
        t = themes.current()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        top = self.ARROW_H if self._arrow else 0
        path = QPainterPath()
        path.addRoundedRect(QRectF(0.5, top + 0.5, self.width() - 1, self.height() - top - 1), self.RADIUS, self.RADIUS)
        if self._arrow:
            x = self._arrow_x
            notch = QPainterPath()
            notch.addPolygon(
                QPolygonF([QPointF(x - self.ARROW_W / 2, top + 1), QPointF(x, 0.5), QPointF(x + self.ARROW_W / 2, top + 1)])
            )
            path = path.united(notch)
        p.setPen(QPen(QColor(t.line), 1))
        p.setBrush(QColor(t.panel))
        p.drawPath(path)

    def popup_under(self, anchor: QWidget, *, align_left: bool = False) -> None:
        """Show just below anchor (above it if the screen runs out), the arrow on its center."""
        self.adjustSize()
        screen = (anchor.screen() or self.screen()).availableGeometry()
        bottom_center = anchor.mapToGlobal(QPoint(anchor.width() // 2, anchor.height()))
        left = anchor.mapToGlobal(QPoint(0, 0)).x() if align_left else bottom_center.x() - self.width() // 2
        x = max(screen.left() + 8, min(left, screen.right() - self.width() - 8))
        y = bottom_center.y() + 4
        if y + self.height() > screen.bottom() - 8:
            y = max(screen.top() + 8, anchor.mapToGlobal(QPoint(0, 0)).y() - self.height() - 4)
        margin = self.RADIUS + self.ARROW_W / 2
        self._arrow_x = max(margin, min(bottom_center.x() - x, self.width() - margin))
        self.move(x, y)
        self.show()


def _popover_list(width: int) -> QListWidget:
    """A list for a popover. Its width is fixed before rows are added: row widgets
    are sized from the list's width when they're inserted."""
    lst = QListWidget()
    lst.setObjectName("popoverList")
    lst.setFixedWidth(width)
    lst.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
    lst.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    lst.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    lst.setFrameShape(QFrame.Shape.NoFrame)
    lst.setSpacing(1)
    return lst


# ---------- voice auditions ----------


class _PreviewWorker(QThread):
    """Renders audition clips one at a time, taking jobs from the controller."""

    ready = Signal(str, str)  # label, path
    failed = Signal(str, str)  # label, error

    def __init__(self, next_job):
        super().__init__()
        self._next_job = next_job

    def run(self):
        while (job := self._next_job()) is not None:
            label, engine, voice = job
            try:
                self.ready.emit(label, str(make_preview(engine, voice)))
            except Exception as e:  # one bad voice shouldn't stop the rest
                self.failed.emit(label, str(e))


class VoicePreviews(QObject):
    """Plays a voice's audition clip, rendering it first when it isn't cached.

    state_changed(label, state) reports "idle", "loading" or "playing" so every
    play button showing that voice can update itself."""

    state_changed = Signal(str, str)
    failed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._options: dict[str, tuple[object, str]] = {}
        self.player = QMediaPlayer(self)
        self.output = QAudioOutput(self)
        self.player.setAudioOutput(self.output)
        self.player.playbackStateChanged.connect(self._on_playback_state)
        self._current: str | None = None  # the voice playing or being rendered to play
        self._state = "idle"
        self._jobs: deque[str] = deque()
        self._jobs_lock = threading.Lock()
        self._worker: _PreviewWorker | None = None
        self.before_play = lambda: None  # e.g. pause the script playback

    def set_options(self, options: list[tuple[str, tuple[object, str]]]) -> None:
        self._options = dict(options)

    def state(self, label: str) -> str:
        return self._state if label == self._current else "idle"

    def is_ready(self, label: str) -> bool:
        if label not in self._options:
            return False
        engine, voice = self._options[label]
        return preview_path(engine.name, voice).is_file()

    def toggle(self, label: str) -> None:
        if label == self._current and self._state in ("playing", "loading"):
            self.stop()
            return
        self.stop()
        if label not in self._options:
            return
        self._current = label
        if self.is_ready(label):
            self._play(label)
        else:
            self._set_state("loading")
            self._enqueue([label], front=True)

    def warm(self, labels: list[str]) -> None:
        """Render any missing clips in the background, so they play instantly later."""
        self._enqueue([label for label in labels if label in self._options and not self.is_ready(label)])

    def stop(self) -> None:
        current = self._current
        self._current = None
        self.player.stop()
        if current is not None:
            self._state = "idle"
            self.state_changed.emit(current, "idle")

    def shutdown(self) -> None:
        with self._jobs_lock:
            self._jobs.clear()
        self.stop()
        if self._worker is not None:
            self._worker.wait(5000)

    # -- internals --

    def _set_state(self, state: str) -> None:
        self._state = state
        if self._current is not None:
            self.state_changed.emit(self._current, state)

    def _play(self, label: str) -> None:
        engine, voice = self._options[label]
        self.before_play()
        self.player.setSource(QUrl.fromLocalFile(str(preview_path(engine.name, voice))))
        self.player.play()
        self._set_state("playing")

    def _enqueue(self, labels: list[str], front: bool = False) -> None:
        with self._jobs_lock:
            for label in labels:
                if label in self._jobs:
                    self._jobs.remove(label)
                if front:
                    self._jobs.appendleft(label)
                else:
                    self._jobs.append(label)
        if self._worker is None or not self._worker.isRunning():
            self._worker = _PreviewWorker(self._next_job)
            self._worker.ready.connect(self._on_ready)
            self._worker.failed.connect(self._on_failed)
            self._worker.finished.connect(self._on_worker_finished)
            self._worker.start()

    def _next_job(self):
        with self._jobs_lock:
            while self._jobs:
                label = self._jobs.popleft()
                if label in self._options:
                    engine, voice = self._options[label]
                    return label, engine, voice
        return None

    def _on_worker_finished(self):
        with self._jobs_lock:
            pending = bool(self._jobs)
        if pending:  # a job arrived just as the worker was finishing
            self._worker = None
            self._enqueue([])

    def _on_ready(self, label: str, path: str):
        if label == self._current and self._state == "loading":
            self._play(label)

    def _on_failed(self, label: str, message: str):
        if label == self._current:
            self._set_state("idle")
            self._current = None
        self.failed.emit(f"Couldn't make a preview of {label}: {message}")

    def _on_playback_state(self, state):
        if state == QMediaPlayer.PlaybackState.StoppedState and self._state == "playing":
            self.stop()


class PreviewButton(QAbstractButton):
    """▶ plays a voice's audition clip, ■ stops it; a spinning ring while it's being made."""

    def __init__(self, previews: VoicePreviews, label: str, *, filled: bool = True, size: int = 26, parent=None):
        super().__init__(parent)
        self._previews = previews
        self._label = label
        self._filled = filled
        self._state = previews.state(label)
        self._angle = 0
        self.setFixedSize(size, size)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setToolTip("Hear this voice")
        self._spin = QTimer(self)
        self._spin.setInterval(30)
        self._spin.timeout.connect(self._advance)
        previews.state_changed.connect(self._on_state)
        self.clicked.connect(self._toggle)

    def set_label(self, label: str) -> None:
        self._label = label
        self._on_state(label, self._previews.state(label))

    def _toggle(self):
        self._previews.toggle(self._label)

    def _on_state(self, label: str, state: str):
        if label != self._label:
            return
        self._state = state
        self.setToolTip("Stop" if state == "playing" else "Preparing preview…" if state == "loading" else "Hear this voice")
        if state == "loading":
            self._spin.start()
        else:
            self._spin.stop()
        self.update()

    def _advance(self):
        self._angle = (self._angle + 12) % 360
        self.update()

    def enterEvent(self, event):
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event):
        t = themes.current()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(1, 1, self.width() - 2, self.height() - 2)
        hover = self.underMouse()
        if self._state == "loading":
            p.setPen(QPen(QColor(t.line), 2))
            p.drawEllipse(r.adjusted(2, 2, -2, -2))
            p.setPen(QPen(QColor(ACCENT), 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawArc(r.adjusted(2, 2, -2, -2), -self._angle * 16, 100 * 16)
            return
        solid = self._filled or hover or self._state == "playing"
        if solid:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(ACCENT_HOVER if hover else ACCENT))
            glyph = QColor(ON_ACCENT)
        else:
            p.setPen(QPen(QColor(t.line), 1))
            p.setBrush(Qt.BrushStyle.NoBrush)
            glyph = QColor(t.text)
        p.drawEllipse(r)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(glyph)
        c = r.center()
        s = r.width() * 0.17
        if self._state == "playing":
            p.drawRoundedRect(QRectF(c.x() - s, c.y() - s, 2 * s, 2 * s), 1.5, 1.5)
        else:
            p.drawPolygon(QPolygonF([
                QPointF(c.x() - s * 0.8, c.y() - s * 1.25),
                QPointF(c.x() + s * 1.35, c.y()),
                QPointF(c.x() - s * 0.8, c.y() + s * 1.25),
            ]))


class _VoiceRow(QWidget):
    def __init__(self, label: str, previews: VoicePreviews):
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 0, 6, 0)
        name = QLabel(label)
        name.setFont(ui_font(13))
        layout.addWidget(name, 1)
        layout.addWidget(PreviewButton(previews, label, filled=False, size=24))


class VoiceMenu(Popover):
    """The full voice list under a picker: click a name to cast it, ▶ to hear it first."""

    chosen = Signal(str)

    def __init__(self, labels: list[str], current: str, previews: VoicePreviews, parent: QWidget, width: int):
        super().__init__(parent, arrow=False, width=width)
        self.body.setContentsMargins(6, 6, 6, 6)
        self.list = _popover_list(width - 12)
        for label in labels:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, label)
            item.setSizeHint(QSize(0, 36))
            self.list.addItem(item)
            self.list.setItemWidget(item, _VoiceRow(label, previews))
            if label == current:
                self.list.setCurrentItem(item)
        self.list.setFixedHeight(min(360, 38 * len(labels) + 4))
        self.list.itemClicked.connect(self._choose)
        self.list.itemActivated.connect(self._choose)
        self.body.addWidget(self.list)
        if self.list.currentItem() is not None:
            self.list.scrollToItem(self.list.currentItem(), QAbstractItemView.ScrollHint.PositionAtCenter)

    def _choose(self, item: QListWidgetItem):
        self.chosen.emit(item.data(Qt.ItemDataRole.UserRole))
        self.close()


class VoicePicker(QFrame):
    """[⌄ | voice name ........ ▶] — the chevron opens the voice list, the name can be
    typed into to search, and ▶ plays the chosen voice's audition clip."""

    changed = Signal(str)

    def __init__(self, labels: list[str], current: str, previews: VoicePreviews, parent=None):
        super().__init__(parent)
        self.setObjectName("voicePicker")
        self._labels = labels
        self._current = current if current in labels else (labels[0] if labels else "")
        self._previews = previews
        self.setMinimumHeight(40)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(5, 4, 6, 4)
        layout.setSpacing(6)

        self.arrow = QToolButton()
        self.arrow.setObjectName("pickerArrow")
        self.arrow.setFixedSize(28, 28)
        self.arrow.setIconSize(QSize(16, 16))
        self.arrow.setCursor(Qt.CursorShape.PointingHandCursor)
        self.arrow.setToolTip("Show all voices")
        self.arrow.clicked.connect(self.open_menu)
        self.refresh_icons()

        divider = QFrame()
        divider.setObjectName("pickerDivider")
        divider.setFixedSize(1, 20)

        self.edit = QLineEdit(self._current)
        self.edit.setFont(ui_font(14))
        self.edit.setPlaceholderText("Type to search voices…")
        completer = QCompleter(labels, self.edit)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.activated.connect(self.set_current)
        self.edit.setCompleter(completer)
        self.edit.editingFinished.connect(self._snap_to_valid)

        self.play = PreviewButton(previews, self._current)

        layout.addWidget(self.arrow)
        layout.addWidget(divider)
        layout.addWidget(self.edit, 1)
        layout.addWidget(self.play)

    def currentText(self) -> str:  # noqa: N802 — same name as QComboBox's
        return self._current

    def set_current(self, label: str) -> None:
        if label not in self._labels:
            return
        self._current = label
        self.edit.setText(label)
        self.play.set_label(label)
        self.changed.emit(label)

    def refresh_icons(self) -> None:
        self.arrow.setIcon(chevron_icon(themes.current().text))

    def open_menu(self) -> None:
        menu = VoiceMenu(self._labels, self._current, self._previews, self, width=self.width())
        menu.chosen.connect(self.set_current)
        menu.popup_under(self, align_left=True)

    def mousePressEvent(self, event):
        # A click on the bubble's padding opens the list, like a dropdown.
        if event.button() == Qt.MouseButton.LeftButton:
            self.open_menu()
        super().mousePressEvent(event)

    def _snap_to_valid(self):
        text = self.edit.text().strip()
        match = next((label for label in self._labels if label.lower() == text.lower()), None)
        if match is not None and match != self._current:
            self.set_current(match)
        else:
            self.edit.setText(self._current)


# ---------- Themes & Settings ----------


class ThemeCard(QAbstractButton):
    """One theme as a little page: 'Aa' in its colors, its name below."""

    def __init__(self, key: str, parent=None):
        super().__init__(parent)
        self.key = key
        self.setCheckable(True)
        self.setFixedSize(118, 92)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(f"{THEMES[key].name} theme")

    def paintEvent(self, event):
        shown, card = themes.current(), THEMES[self.key]
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(2, 2, self.width() - 4, self.height() - 4)
        if self.isChecked():
            p.setPen(QPen(QColor(shown.text), 2.5))
        else:
            p.setPen(QPen(QColor(shown.line), 1))
        p.setBrush(QColor(card.page))
        p.drawRoundedRect(r, 14, 14)
        p.setPen(QColor(card.text))
        serif = QFont()
        serif.setFamilies(SERIF_FAMILIES)
        serif.setPixelSize(30)
        p.setFont(serif)
        p.drawText(QRectF(r.x(), r.y() + 8, r.width(), 40), Qt.AlignmentFlag.AlignCenter, "Aa")
        p.setFont(ui_font(12, QFont.Weight.Medium))
        p.drawText(QRectF(r.x(), r.y() + 52, r.width(), 24), Qt.AlignmentFlag.AlignCenter, card.name)


class ThemesPopover(Popover):
    """Themes & Settings: script text size, light/dark, and the four reading themes."""

    theme_chosen = Signal(str)
    text_size_step = Signal(int)  # -1 smaller, +1 larger

    def __init__(self, parent: QWidget, current_key: str):
        super().__init__(parent, width=290)
        title = QLabel("Themes & Settings")
        title.setObjectName("popoverTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setFont(ui_font(13, QFont.Weight.DemiBold))
        self.body.addWidget(title)
        self.body.addWidget(_hairline())

        row = QHBoxLayout()
        row.setSpacing(10)
        segmented = QFrame()
        segmented.setObjectName("segmented")
        segmented.setFixedHeight(40)
        seg = QHBoxLayout(segmented)
        seg.setContentsMargins(4, 4, 4, 4)
        seg.setSpacing(0)
        smaller, larger = QToolButton(), QToolButton()
        for button, size, step, tip in ((smaller, 12, -1, "Smaller script text"), (larger, 17, 1, "Larger script text")):
            button.setText("A")
            button.setFont(ui_font(size, QFont.Weight.Medium))
            button.setFixedHeight(32)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setToolTip(tip)
            button.clicked.connect(lambda _=False, s=step: self.text_size_step.emit(s))
        divider = QFrame()
        divider.setObjectName("segDivider")
        divider.setFixedSize(1, 20)
        seg.addWidget(smaller, 1)
        seg.addWidget(divider, 0, Qt.AlignmentFlag.AlignVCenter)
        seg.addWidget(larger, 1)
        row.addWidget(segmented, 2)

        self.appearance = QToolButton()
        self.appearance.setObjectName("roundButton")
        self.appearance.setFixedHeight(40)
        self.appearance.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.appearance.setIconSize(QSize(20, 20))
        self.appearance.setCursor(Qt.CursorShape.PointingHandCursor)
        self.appearance.setToolTip("Switch between a light and a dark theme")
        self.appearance.clicked.connect(lambda: self.theme_chosen.emit(THEMES[self._key].partner))
        row.addWidget(self.appearance, 1)
        self.body.addLayout(row)

        grid = QGridLayout()
        grid.setSpacing(10)
        self.cards: dict[str, ThemeCard] = {}
        group = QButtonGroup(self)
        group.setExclusive(True)
        for i, key in enumerate(THEMES):
            card = ThemeCard(key)
            card.clicked.connect(lambda _=False, k=key: self.theme_chosen.emit(k))
            group.addButton(card)
            grid.addWidget(card, i // 2, i % 2)
            self.cards[key] = card
        self.body.addLayout(grid)
        self._key = current_key
        self.set_theme(current_key)

    def set_theme(self, key: str) -> None:
        self._key = key
        for k, card in self.cards.items():
            card.setChecked(k == key)
            card.update()
        self.appearance.setIcon(half_circle_icon(themes.current().text))
        self.update()


# ---------- scenes ----------


class _SceneRow(QWidget):
    def __init__(self, number: str, heading: str, time_text: str):
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 7, 10, 7)
        layout.setSpacing(10)
        num = QLabel(number)
        num.setObjectName("sceneNumber")
        num.setFont(ui_font(13, QFont.Weight.Bold))
        num.setFixedWidth(40)
        num.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        text = QLabel(heading)
        text.setFont(ui_font(13))
        text.setWordWrap(True)
        text.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        when = QLabel(time_text)
        when.setObjectName("sceneTime")
        when.setFont(ui_font(12))
        when.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        when.setMinimumWidth(44)
        layout.addWidget(num)
        layout.addWidget(text, 1)
        layout.addWidget(when)


class ScenesPopover(Popover):
    """Every scene of the script, numbered; click one to jump there."""

    jump = Signal(int)  # element index of the scene heading

    def __init__(self, parent: QWidget, scenes: list[tuple[int, str, str, str]], current: int | None):
        super().__init__(parent, width=440)
        title = QLabel("Scenes")
        title.setObjectName("popoverTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setFont(ui_font(13, QFont.Weight.DemiBold))
        self.body.addWidget(title)
        self.body.addWidget(_hairline())

        list_width = self.width() - 24  # popover padding
        self.list = _popover_list(list_width)
        row_width = list_width - 14 - 2  # scroll bar, item spacing
        total = 0
        for element_index, number, heading, time_text in scenes:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, element_index)
            row = _SceneRow(number, heading, time_text)
            height = max(34, row.heightForWidth(row_width) if row.hasHeightForWidth() else row.sizeHint().height())
            item.setSizeHint(QSize(0, height))
            total += height + 2
            self.list.addItem(item)
            self.list.setItemWidget(item, row)
            if element_index == current:
                self.list.setCurrentItem(item)
        if not scenes:
            empty = QLabel("No scene headings found in this script.")
            empty.setObjectName("hint")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.body.addWidget(empty)
        self.list.setFixedHeight(min(520, total + 4))
        self.list.itemClicked.connect(self._jump)
        self.list.itemActivated.connect(self._jump)
        self.body.addWidget(self.list)
        if self.list.currentItem() is not None:
            QTimer.singleShot(0, lambda: self.list.scrollToItem(
                self.list.currentItem(), QAbstractItemView.ScrollHint.PositionAtCenter))

    def _jump(self, item: QListWidgetItem):
        self.jump.emit(item.data(Qt.ItemDataRole.UserRole))
        self.close()


# ---------- player, progress, confirmation ----------


class PlayerCapsule(QWidget):
    """The transport — rewind, play, forward — in a capsule floating over the
    bottom of the page, in the frame's color, symmetrical around Play."""

    def __init__(self, buttons: list[QWidget], parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 7, 14, 7)
        layout.setSpacing(14)
        for button in buttons:
            layout.addWidget(button, 0, Qt.AlignmentFlag.AlignVCenter)
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(22)
        shadow.setOffset(0, 4)
        shadow.setColor(QColor(0, 0, 0, 70))
        self.setGraphicsEffect(shadow)

    def paintEvent(self, event):
        t = themes.current()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(QColor(t.chrome_line), 1))
        p.setBrush(QColor(t.chrome))
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)


class GradientProgressBar(QProgressBar):
    """Generation progress in blues: the gradient spans the whole bar, so the fill
    starts very light and reaches the darkest blue only at 100%."""

    # Where the gradient gets dark enough for white text (blue at ~55% of the bar).
    WHITE_TEXT_FROM = 0.55

    def paintEvent(self, event):
        t = themes.current()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = r.height() / 2
        bar = QPainterPath()
        bar.addRoundedRect(r, radius, radius)
        p.setPen(QPen(QColor(t.chrome_line), 1))
        p.setBrush(QColor("#FFFFFF"))
        p.drawPath(bar)

        span = self.maximum() - self.minimum()
        done = (self.value() - self.minimum()) / span if span > 0 else 0.0
        split = r.left() + r.width() * max(0.0, min(1.0, done))
        if done > 0:
            gradient = QLinearGradient(r.left(), 0, r.right(), 0)
            for stop, color in zip((0.0, 0.35, 0.7, 1.0), PROGRESS_BLUES):
                gradient.setColorAt(stop, QColor(color))
            filled = QPainterPath()
            filled.addRect(QRectF(r.left(), r.top(), split - r.left(), r.height()))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(gradient)
            p.drawPath(bar.intersected(filled))

        # Dark text over the track and the light blues, white over the dark blues.
        p.setFont(ui_font(11, QFont.Weight.Medium))
        text = self.text()
        white_from = r.left() + r.width() * self.WHITE_TEXT_FROM
        white = QRectF(white_from, 0, max(0.0, split - white_from), self.height())
        p.save()
        dark_area = QPainterPath()
        dark_area.addRect(QRectF(self.rect()))
        cut = QPainterPath()
        cut.addRect(white)
        p.setClipPath(dark_area.subtracted(cut))
        p.setPen(QColor("#1C1C1E"))
        p.drawText(r, Qt.AlignmentFlag.AlignCenter, text)
        p.restore()
        if white.width() > 0:
            p.save()
            p.setClipRect(white)
            p.setPen(QColor("#FFFFFF"))
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, text)
            p.restore()


def confirm(parent: QWidget, message: str, *, no: str, yes: str) -> bool:
    """A yes/no question with the safe answer on the left and the destructive
    one, in red, on the right."""
    dialog = QDialog(parent)
    dialog.setWindowTitle("Script Radio")
    dialog.setModal(True)
    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(26, 22, 26, 20)
    layout.setSpacing(18)
    label = QLabel(message)
    label.setWordWrap(True)
    label.setFont(ui_font(14))
    label.setMinimumWidth(340)
    layout.addWidget(label)
    row = QHBoxLayout()
    back = QPushButton(no)
    back.setDefault(True)
    back.setAutoDefault(True)
    back.clicked.connect(dialog.reject)
    go = QPushButton(yes)
    go.setObjectName("dangerButton")
    go.setAutoDefault(False)
    go.clicked.connect(dialog.accept)
    row.addWidget(back)
    row.addStretch(1)
    row.addWidget(go)
    layout.addLayout(row)
    return dialog.exec() == QDialog.DialogCode.Accepted

