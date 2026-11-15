"""Main window: Report and Signal tabs plus the status bar."""
from __future__ import annotations

import os

from PySide6.QtCore import QStandardPaths
from PySide6.QtWidgets import QLabel, QMainWindow, QTabWidget

from .report_tab import ReportTab
from .signal_tab import SignalTab

STATUS_NOTE = ('US markets close at 5:00am Perth (4:00am mid-March to early November). '
               'Run after 6:00am Perth, Tuesday to Saturday.')


def app_data_dir() -> str:
    """Folder for the Signal state and the price cache (override with BEHAVIOUR_MODEL_DATA_DIR)."""
    return os.environ.get('BEHAVIOUR_MODEL_DATA_DIR') or QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('Retail Behaviour Model')
        self.resize(1480, 940)
        data_dir = app_data_dir()
        cache_dir = os.path.join(data_dir, 'cache')
        self.message = QLabel('')
        self.statusBar().addWidget(self.message, 1)
        self.statusBar().addPermanentWidget(QLabel(STATUS_NOTE))
        self.report = ReportTab(cache_dir)
        self.signal = SignalTab(data_dir, cache_dir, self.message.setText)
        tabs = QTabWidget()
        tabs.addTab(self.report, 'Report')
        tabs.addTab(self.signal, 'Signal')
        self.tabs = tabs
        self.setCentralWidget(tabs)
