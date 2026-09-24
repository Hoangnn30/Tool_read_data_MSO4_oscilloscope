from __future__ import annotations

import math
import time
from typing import Optional

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QThread, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from mso4 import MSO4Client, MSO4Error


CHANNEL_COLORS = {
    "CH1": "#FFD400",
    "CH2": "#00D46A",
    "CH3": "#32A7FF",
    "CH4": "#FF4A55",
}


class AcquisitionThread(QThread):
    waveform_ready = pyqtSignal(str, object, object, object)
    acquisition_error = pyqtSignal(str)
    acquisition_rate = pyqtSignal(float)

    def __init__(
        self,
        client: MSO4Client,
        channels: list[str],
        points: int,
        interval_ms: int,
        parent=None,
    ):
        super().__init__(parent)
        self.client = client
        self.channels = channels
        self.points = points
        self.interval_ms = interval_ms
        self._running = True

    def stop(self) -> None:
        self._running = False

    def run(self) -> None:
        last_report = time.monotonic()
        frames = 0

        while self._running:
            started = time.monotonic()
            try:
                for channel in self.channels:
                    if not self._running:
                        break
                    waveform = self.client.get_waveform(channel, 1, self.points)
                    self.waveform_ready.emit(
                        channel,
                        waveform.time_s,
                        waveform.volts,
                        waveform.measurements(),
                    )
            except Exception as exc:
                if self._running:
                    self.acquisition_error.emit(str(exc))
                return

            frames += 1
            now = time.monotonic()
            if now - last_report >= 1.0:
                self.acquisition_rate.emit(frames / (now - last_report))
                frames = 0
                last_report = now

            elapsed_ms = (time.monotonic() - started) * 1000.0
            remaining = max(0.0, self.interval_ms - elapsed_ms)
            if remaining > 0:
                self.msleep(int(remaining))


class ChannelCard(QFrame):
    enabled_changed = pyqtSignal()

    def __init__(self, channel: str, parent=None):
        super().__init__(parent)
        self.channel = channel
        self.setObjectName("channelCard")

        color = CHANNEL_COLORS[channel]

        self.enabled = QCheckBox(channel)
        self.enabled.setChecked(channel == "CH1")
        self.enabled.setStyleSheet(
            f"QCheckBox {{ color: {color}; font-size: 16px; font-weight: 700; }}"
        )
        self.enabled.stateChanged.connect(lambda _state: self.enabled_changed.emit())

        self.coupling = QComboBox()
        self.coupling.addItems(["DC", "AC"])
        self.coupling.setEnabled(False)
        self.coupling.setToolTip("Display setting only; instrument coupling control can be added next.")

        self.scale = QLabel("-- V/div")
        self.scale.setObjectName("mutedValue")

        layout = QGridLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.addWidget(self.enabled, 0, 0)
        layout.addWidget(self.scale, 0, 1, alignment=Qt.AlignmentFlag.AlignRight)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("MSO4 LAN Scope")
        self.resize(1500, 880)

        self.client: Optional[MSO4Client] = None
        self.worker: Optional[AcquisitionThread] = None
        self.curves: dict[str, pg.PlotDataItem] = {}
        self.channel_cards: dict[str, ChannelCard] = {}
        self.measure_labels: dict[str, dict[str, QLabel]] = {}

        self._build_ui()
        self._apply_theme()
        self._set_connected(False)

    def _build_ui(self) -> None:
        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(10, 10, 10, 10)
        root_layout.setSpacing(8)

        root_layout.addWidget(self._build_topbar())

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_left_panel())
        splitter.addWidget(self._build_scope_panel())
        splitter.addWidget(self._build_measurement_panel())
        splitter.setSizes([260, 950, 280])
        splitter.setStretchFactor(1, 1)
        root_layout.addWidget(splitter, 1)

        self.setCentralWidget(root)

        status = QStatusBar()
        self.status_connection = QLabel("Disconnected")
        self.status_rate = QLabel("Acq: -- fps")
        status.addWidget(self.status_connection)
        status.addPermanentWidget(self.status_rate)
        self.setStatusBar(status)

    def _build_topbar(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("topbar")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(12, 8, 12, 8)

        title = QLabel("MSO4 LAN SCOPE")
        title.setObjectName("appTitle")
        layout.addWidget(title)

        layout.addStretch(1)

        self.ip_edit = QLineEdit("192.168.1.133")
        self.ip_edit.setPlaceholderText("MSO44 IP")
        self.ip_edit.setFixedWidth(155)

        self.port_edit = QSpinBox()
        self.port_edit.setRange(1, 65535)
        self.port_edit.setValue(4000)
        self.port_edit.setFixedWidth(90)

        self.connect_btn = QPushButton("Connect")
        self.connect_btn.clicked.connect(self._toggle_connection)

        self.run_btn = QPushButton("RUN")
        self.run_btn.setObjectName("runButton")
        self.run_btn.clicked.connect(self._toggle_run)

        layout.addWidget(QLabel("IP"))
        layout.addWidget(self.ip_edit)
        layout.addWidget(QLabel("Port"))
        layout.addWidget(self.port_edit)
        layout.addWidget(self.connect_btn)
        layout.addWidget(self.run_btn)
        return frame

    def _build_left_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("sidePanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 10, 10, 10)

        heading = QLabel("CHANNELS")
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)

        for channel in CHANNEL_COLORS:
            card = ChannelCard(channel)
            card.enabled_changed.connect(self._channels_changed)
            self.channel_cards[channel] = card
            layout.addWidget(card)

        layout.addSpacing(12)
        acquire_label = QLabel("ACQUISITION")
        acquire_label.setObjectName("sectionTitle")
        layout.addWidget(acquire_label)

        form = QFormLayout()
        self.points_combo = QComboBox()
        self.points_combo.addItems(["1000", "2500", "5000", "10000", "25000", "50000"])
        self.points_combo.setCurrentText("10000")

        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(20, 5000)
        self.interval_spin.setValue(120)
        self.interval_spin.setSuffix(" ms")

        form.addRow("Points", self.points_combo)
        form.addRow("Refresh", self.interval_spin)
        layout.addLayout(form)

        self.autoscale_btn = QPushButton("Auto scale display")
        self.autoscale_btn.clicked.connect(self._autoscale)
        layout.addWidget(self.autoscale_btn)

        layout.addSpacing(12)
        console_label = QLabel("SCPI CONSOLE")
        console_label.setObjectName("sectionTitle")
        layout.addWidget(console_label)

        self.scpi_edit = QLineEdit("*IDN?")
        self.scpi_edit.returnPressed.connect(self._send_scpi)
        self.scpi_btn = QPushButton("Send query")
        self.scpi_btn.clicked.connect(self._send_scpi)
        self.scpi_result = QLabel("Ready")
        self.scpi_result.setWordWrap(True)
        self.scpi_result.setObjectName("consoleResult")

        layout.addWidget(self.scpi_edit)
        layout.addWidget(self.scpi_btn)
        layout.addWidget(self.scpi_result)
        layout.addStretch(1)
        return panel

    def _build_scope_panel(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("scopeFrame")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(0, 0, 0, 0)

        self.plot = pg.PlotWidget()
        self.plot.setBackground("#05070A")
        self.plot.showGrid(x=True, y=True, alpha=0.28)
        self.plot.setLabel("bottom", "Time", units="s")
        self.plot.setLabel("left", "Amplitude", units="V")
        self.plot.getPlotItem().setMenuEnabled(False)
        self.plot.getPlotItem().hideButtons()
        self.plot.setClipToView(True)
        self.plot.setDownsampling(auto=True, mode="peak")

        axis_pen = pg.mkPen("#6E7781")
        self.plot.getAxis("bottom").setPen(axis_pen)
        self.plot.getAxis("left").setPen(axis_pen)
        self.plot.getAxis("bottom").setTextPen("#B9C0C8")
        self.plot.getAxis("left").setTextPen("#B9C0C8")

        for channel, color in CHANNEL_COLORS.items():
            curve = self.plot.plot(
                [],
                [],
                pen=pg.mkPen(color=color, width=1.7),
                name=channel,
            )
            curve.setVisible(channel == "CH1")
            self.curves[channel] = curve

        self.crosshair_v = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen("#4A5159"))
        self.crosshair_h = pg.InfiniteLine(angle=0, movable=False, pen=pg.mkPen("#4A5159"))
        self.plot.addItem(self.crosshair_v, ignoreBounds=True)
        self.plot.addItem(self.crosshair_h, ignoreBounds=True)

        self.plot.scene().sigMouseMoved.connect(self._mouse_moved)

        self.cursor_label = QLabel("t = --    V = --")
        self.cursor_label.setObjectName("cursorLabel")

        layout.addWidget(self.plot, 1)
        layout.addWidget(self.cursor_label)
        return frame

    def _build_measurement_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("sidePanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 10, 10, 10)

        heading = QLabel("MEASUREMENTS")
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)

        for channel, color in CHANNEL_COLORS.items():
            box = QFrame()
            box.setObjectName("measureCard")
            grid = QGridLayout(box)
            grid.setContentsMargins(9, 8, 9, 8)

            ch_label = QLabel(channel)
            ch_label.setStyleSheet(f"color: {color}; font-weight: 700;")
            grid.addWidget(ch_label, 0, 0, 1, 2)

            labels: dict[str, QLabel] = {}
            rows = [
                ("pkpk", "Vpp"),
                ("rms", "Vrms"),
                ("frequency", "Freq"),
                ("mean", "Mean"),
                ("min", "Min"),
                ("max", "Max"),
            ]
            for row, (key, caption) in enumerate(rows, start=1):
                grid.addWidget(QLabel(caption), row, 0)
                value = QLabel("--")
                value.setAlignment(Qt.AlignmentFlag.AlignRight)
                value.setObjectName("measureValue")
                grid.addWidget(value, row, 1)
                labels[key] = value

            self.measure_labels[channel] = labels
            layout.addWidget(box)

        layout.addStretch(1)
        return panel

    def _apply_theme(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget {
                background: #0D1117;
                color: #D7DEE7;
                font-family: Inter, Arial, sans-serif;
                font-size: 13px;
            }
            QFrame#topbar, QFrame#sidePanel, QFrame#scopeFrame {
                background: #111820;
                border: 1px solid #26313D;
                border-radius: 7px;
            }
            QFrame#channelCard, QFrame#measureCard {
                background: #161E27;
                border: 1px solid #273441;
                border-radius: 6px;
            }
            QLabel#appTitle {
                font-size: 18px;
                font-weight: 800;
                color: #F0F4F8;
                letter-spacing: 1px;
            }
            QLabel#sectionTitle {
                color: #8FA3B8;
                font-weight: 700;
                font-size: 11px;
                letter-spacing: 1px;
            }
            QLabel#mutedValue, QLabel#consoleResult, QLabel#cursorLabel {
                color: #93A0AD;
            }
            QLabel#measureValue {
                font-family: Menlo, Consolas, monospace;
                font-weight: 700;
            }
            QLineEdit, QSpinBox, QComboBox {
                background: #0B1016;
                border: 1px solid #344352;
                border-radius: 4px;
                padding: 6px;
                min-height: 20px;
            }
            QPushButton {
                background: #1E2A36;
                border: 1px solid #3A4A5A;
                border-radius: 5px;
                padding: 7px 12px;
                font-weight: 600;
            }
            QPushButton:hover {
                background: #263646;
            }
            QPushButton#runButton {
                background: #153E2B;
                border-color: #237A4E;
                color: #79F2AF;
                min-width: 70px;
            }
            QPushButton#runButton[active="true"] {
                background: #5C2025;
                border-color: #9A3941;
                color: #FFABB0;
            }
            QSplitter::handle {
                background: #0D1117;
                width: 7px;
            }
            QStatusBar {
                background: #0B0F14;
                border-top: 1px solid #26313D;
            }
            """
        )

    def _toggle_connection(self) -> None:
        if self.client and self.client.connected:
            self._disconnect()
            return

        host = self.ip_edit.text().strip()
        if not host:
            QMessageBox.warning(self, "MSO4", "Enter the oscilloscope IP address.")
            return

        self.connect_btn.setEnabled(False)
        self.status_connection.setText(f"Connecting to {host}...")
        try:
            client = MSO4Client(host, timeout=5.0)
            idn = client.connect()
            self.client = client
            self.scpi_result.setText(idn)
            self.status_connection.setText(f"Connected: {idn}")
            self._set_connected(True)
        except Exception as exc:
            self.client = None
            self.status_connection.setText("Connection failed")
            QMessageBox.critical(self, "MSO4 connection failed", str(exc))
        finally:
            self.connect_btn.setEnabled(True)

    def _disconnect(self) -> None:
        self._stop_acquisition()
        if self.client:
            self.client.close()
        self.client = None
        self._set_connected(False)
        self.status_connection.setText("Disconnected")
        self.status_rate.setText("Acq: -- fps")

    def _set_connected(self, connected: bool) -> None:
        self.connect_btn.setText("Disconnect" if connected else "Connect")
        self.run_btn.setEnabled(connected)
        self.scpi_btn.setEnabled(connected)
        self.scpi_edit.setEnabled(connected)
        self.ip_edit.setEnabled(not connected)
        self.port_edit.setEnabled(not connected)

    def _toggle_run(self) -> None:
        if self.worker and self.worker.isRunning():
            self._stop_acquisition()
            return

        if not self.client or not self.client.connected:
            return

        channels = [
            channel
            for channel, card in self.channel_cards.items()
            if card.enabled.isChecked()
        ]
        if not channels:
            QMessageBox.information(self, "MSO4", "Enable at least one channel.")
            return

        self.worker = AcquisitionThread(
            self.client,
            channels,
            int(self.points_combo.currentText()),
            self.interval_spin.value(),
            self,
        )
        self.worker.waveform_ready.connect(self._update_waveform)
        self.worker.acquisition_error.connect(self._acquisition_error)
        self.worker.acquisition_rate.connect(
            lambda fps: self.status_rate.setText(f"Acq: {fps:.1f} fps")
        )
        self.worker.finished.connect(self._worker_finished)
        self.worker.start()

        self.run_btn.setText("STOP")
        self.run_btn.setProperty("active", True)
        self.run_btn.style().unpolish(self.run_btn)
        self.run_btn.style().polish(self.run_btn)

    def _stop_acquisition(self) -> None:
        worker = self.worker
        self.worker = None
        if worker is not None:
            worker.stop()
            worker.wait(2500)

        self.run_btn.setText("RUN")
        self.run_btn.setProperty("active", False)
        self.run_btn.style().unpolish(self.run_btn)
        self.run_btn.style().polish(self.run_btn)

    def _worker_finished(self) -> None:
        self.run_btn.setText("RUN")
        self.run_btn.setProperty("active", False)
        self.run_btn.style().unpolish(self.run_btn)
        self.run_btn.style().polish(self.run_btn)

    def _channels_changed(self) -> None:
        running = bool(self.worker and self.worker.isRunning())
        for channel, card in self.channel_cards.items():
            self.curves[channel].setVisible(card.enabled.isChecked())

        if running:
            self._stop_acquisition()
            self._toggle_run()

    def _update_waveform(
        self,
        channel: str,
        x: np.ndarray,
        y: np.ndarray,
        measurements: dict,
    ) -> None:
        curve = self.curves.get(channel)
        if curve is None:
            return

        curve.setData(x, y)

        labels = self.measure_labels[channel]
        labels["pkpk"].setText(self._format_voltage(measurements.get("pkpk")))
        labels["rms"].setText(self._format_voltage(measurements.get("rms")))
        labels["mean"].setText(self._format_voltage(measurements.get("mean")))
        labels["min"].setText(self._format_voltage(measurements.get("min")))
        labels["max"].setText(self._format_voltage(measurements.get("max")))
        labels["frequency"].setText(self._format_frequency(measurements.get("frequency")))

    def _acquisition_error(self, message: str) -> None:
        self.status_connection.setText(f"Acquisition error: {message}")
        self._stop_acquisition()
        QMessageBox.warning(self, "Acquisition stopped", message)

    def _send_scpi(self) -> None:
        if not self.client or not self.client.connected:
            return

        command = self.scpi_edit.text().strip()
        if not command:
            return

        try:
            if command.endswith("?"):
                result = self.client.query(command)
                self.scpi_result.setText(result)
            else:
                self.client.write(command)
                self.scpi_result.setText("OK")
        except Exception as exc:
            self.scpi_result.setText(f"ERROR: {exc}")

    def _autoscale(self) -> None:
        self.plot.enableAutoRange(axis="xy", enable=True)
        self.plot.autoRange(padding=0.05)

    def _mouse_moved(self, pos) -> None:
        if not self.plot.sceneBoundingRect().contains(pos):
            return
        mouse_point = self.plot.getPlotItem().vb.mapSceneToView(pos)
        x = float(mouse_point.x())
        y = float(mouse_point.y())
        self.crosshair_v.setPos(x)
        self.crosshair_h.setPos(y)
        self.cursor_label.setText(f"t = {self._format_time(x)}    V = {self._format_voltage(y)}")

    @staticmethod
    def _format_voltage(value) -> str:
        try:
            value = float(value)
        except (TypeError, ValueError):
            return "--"
        if not math.isfinite(value):
            return "--"
        a = abs(value)
        if a < 1e-3:
            return f"{value * 1e6:.3g} µV"
        if a < 1:
            return f"{value * 1e3:.4g} mV"
        return f"{value:.4g} V"

    @staticmethod
    def _format_frequency(value) -> str:
        try:
            value = float(value)
        except (TypeError, ValueError):
            return "--"
        if not math.isfinite(value) or value <= 0:
            return "--"
        if value >= 1e6:
            return f"{value / 1e6:.4g} MHz"
        if value >= 1e3:
            return f"{value / 1e3:.4g} kHz"
        return f"{value:.4g} Hz"

    @staticmethod
    def _format_time(value: float) -> str:
        a = abs(value)
        if a < 1e-6:
            return f"{value * 1e9:.4g} ns"
        if a < 1e-3:
            return f"{value * 1e6:.4g} µs"
        if a < 1:
            return f"{value * 1e3:.4g} ms"
        return f"{value:.4g} s"

    def closeEvent(self, event) -> None:
        self._disconnect()
        event.accept()
