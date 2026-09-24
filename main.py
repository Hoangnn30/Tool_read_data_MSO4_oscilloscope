from __future__ import annotations

import sys

import pyqtgraph as pg
from PyQt6.QtWidgets import QApplication

from ui import MainWindow


def main() -> int:
    pg.setConfigOptions(antialias=False)
    app = QApplication(sys.argv)
    app.setApplicationName("MSO4 LAN Scope")
    app.setOrganizationName("FSEMI")
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
