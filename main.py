import sys

from PySide6.QtWidgets import QApplication

from screenplay_reader.ui.main_window import MainWindow

STYLE = """
QMainWindow, QDockWidget, QWidget { background: #fafafa; color: #222; }
QTextEdit { background: #ffffff; border: none; padding: 24px; }
QToolBar { background: #f0f0f0; border-bottom: 1px solid #ddd; spacing: 6px; padding: 4px; }
QToolButton { padding: 5px 10px; border-radius: 5px; }
QToolButton:hover { background: #e2e2e2; }
QComboBox { padding: 2px 6px; }
QDockWidget::title { padding: 6px; font-weight: bold; }
"""


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Screenplay Reader")
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
