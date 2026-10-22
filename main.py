import sys

from PySide6.QtWidgets import QApplication

from screenplay_reader.ui.main_window import MainWindow
from screenplay_reader.ui.theme import ui_font


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Script Radio")
    app.setStyle("Fusion")
    app.setFont(ui_font(13))  # SF Pro, the macOS system font (see ui/theme.py)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
