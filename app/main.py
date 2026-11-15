"""Retail Behaviour Model. Run with:  python -m app.main

Research and self-awareness tool. It never places orders and has no broker connection.
"""
import sys

import pyqtgraph as pg
from PySide6.QtWidgets import QApplication


def main() -> int:
    app = QApplication(sys.argv)
    app.setOrganizationName('RetailBehaviourModel')
    app.setApplicationName('RetailBehaviourModel')
    pg.setConfigOptions(antialias=True)
    from .ui.main_window import MainWindow      # after QApplication exists, so the theme can be read
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == '__main__':
    sys.exit(main())
