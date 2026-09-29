import sys
from pathlib import Path

from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication

from screenplay_reader.ui.main_window import MainWindow, UI_FONT

FONTS_DIR = Path(__file__).resolve().parent / "assets" / "fonts"
SWITZER_DIR = FONTS_DIR / "switzer"


def load_ui_font() -> None:
    """Register the bundled Switzer family so the UI can use it even when the
    font isn't installed on the system. Prefers the official OTFs in
    assets/fonts/switzer/, falling back to the TTFs in assets/fonts/."""
    files = sorted(SWITZER_DIR.glob("*.otf")) if SWITZER_DIR.is_dir() else []
    if not files and FONTS_DIR.is_dir():
        files = sorted(FONTS_DIR.glob("*.ttf"))
    for font_file in files:
        QFontDatabase.addApplicationFont(str(font_file))


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Script Radio")
    app.setStyle("Fusion")
    load_ui_font()  # register every bundled family
    app.setFont(QFont(UI_FONT, 13))
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
