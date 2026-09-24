from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path


def _configure_qt_platform_plugins() -> None:
    """Point Qt at the platform plugins bundled with the PyQt6 wheel.

    This fixes the common macOS error:
        Could not find the Qt platform plugin "cocoa" in ""

    The environment variables must be configured before importing Qt modules.
    """
    if sys.platform != "darwin":
        return

    spec = importlib.util.find_spec("PyQt6")
    if spec is None or not spec.submodule_search_locations:
        return

    pyqt_dir = Path(next(iter(spec.submodule_search_locations))).resolve()
    qt_plugins = pyqt_dir / "Qt6" / "plugins"
    platform_plugins = qt_plugins / "platforms"
    cocoa_plugin = platform_plugins / "libqcocoa.dylib"

    if cocoa_plugin.exists():
        # Override stale Homebrew/Qt paths that can make PyQt6 search the wrong
        # plugin tree. PyQt6 wheels already bundle the correct Qt plugins.
        os.environ["QT_PLUGIN_PATH"] = str(qt_plugins)
        os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = str(platform_plugins)
        os.environ.setdefault("QT_QPA_PLATFORM", "cocoa")


_configure_qt_platform_plugins()

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
