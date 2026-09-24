from __future__ import annotations

import csv
from datetime import datetime
import math
import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Optional

import numpy as np

from mso4 import MSO4Client, TekHSIUnavailable, TekHSIWaveformClient
from connection_ui import ConnectionManagerDialog
from instrumentation import ConnectionType, DeviceConfig, DeviceRegistry, DeviceStatus


CHANNEL_COLORS = {
    "CH1": "#FFD400",
    "CH2": "#00D46A",
    "CH3": "#32A7FF",
    "CH4": "#FF4A55",
}

VERTICAL_SCALES = [
    0.001,
    0.002,
    0.005,
    0.01,
    0.02,
    0.05,
    0.1,
    0.2,
    0.5,
    1.0,
    2.0,
    5.0,
    10.0,
]

TIME_SCALES = [
    1e-9,
    2e-9,
    5e-9,
    10e-9,
    20e-9,
    50e-9,
    100e-9,
    200e-9,
    500e-9,
    1e-6,
    2e-6,
    5e-6,
    10e-6,
    20e-6,
    50e-6,
    100e-6,
    200e-6,
    500e-6,
    1e-3,
    2e-3,
    5e-3,
    10e-3,
    20e-3,
    50e-3,
    100e-3,
    200e-3,
    500e-3,
    1.0,
]


class MSO4ScopeApp:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("MSO44B LAN Scope")
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        window_w = min(1560, max(1180, screen_w - 40))
        window_h = min(900, max(700, screen_h - 90))
        self.root.geometry(f"{window_w}x{window_h}")
        self.root.resizable(False, False)
        self.root.configure(bg="#070B10")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.registry = DeviceRegistry()
        self.active_device_var = tk.StringVar(value="")
        self.device_status_var = tk.StringVar(value="No active device")
        self._seed_default_device()

        self.client: Optional[MSO4Client] = None
        self.hsi_client: Optional[TekHSIWaveformClient] = None
        self.waveform_transport = "SCPI"
        self.instrument_idn = ""
        self.acq_thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()
        self._acq_generation = 0
        self._channel_apply_after_id = None
        self._resume_after_channel_change = False
        self.command_queue: queue.Queue = queue.Queue()

        # Waveform data shared between acquisition and UI.
        # Only the newest frame per channel is kept: no GUI backlog.
        self.frame_lock = threading.Lock()
        self.latest_frames: dict[str, tuple[np.ndarray, np.ndarray, dict, dict]] = {}
        self.waveforms: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        self.measurements: dict[str, dict[str, float]] = {}
        self.channel_errors: dict[str, str] = {}

        self.x_range: Optional[tuple[float, float]] = None
        self.y_range: Optional[tuple[float, float]] = None
        self._need_autoscale = True
        self._closing = False
        self._last_draw = 0.0
        self._scope_static_key = None
        self._wave_items: dict[str, int] = {}
        self._last_measure_update = {ch: 0.0 for ch in CHANNEL_COLORS}
        self._last_status_update = 0.0
        self._wheel_after_id = None
        self._selected_channel = "CH1"
        self.display_offset_div = {ch: 0.0 for ch in CHANNEL_COLORS}

        self.ip_var = tk.StringVar(value="192.168.1.133")
        self.status_var = tk.StringVar(value="Disconnected")
        self.rate_var = tk.StringVar(value="Acq: -- fps")
        self.transfer_var = tk.StringVar(value="No waveform data yet")
        self.cursor_var = tk.StringVar(value="t = --    V = --")
        self.connection_badge_var = tk.StringVar(value="● OFFLINE")

        self.points_var = tk.StringVar(value="2500")
        self.refresh_var = tk.StringVar(value="30")
        self.fast_mode_var = tk.BooleanVar(value=True)
        self.fast_record_var = tk.StringVar(value="1500")
        self.get_full_record_var = tk.BooleanVar(value=True)

        self.channel_vars = {
            ch: tk.BooleanVar(value=(ch == "CH1"))
            for ch in CHANNEL_COLORS
        }
        self.channel_scale_vars = {
            ch: tk.StringVar(value="1 V/div") for ch in CHANNEL_COLORS
        }
        self.channel_position_vars = {
            ch: tk.StringVar(value="0") for ch in CHANNEL_COLORS
        }
        self.channel_offset_vars = {
            ch: tk.StringVar(value="0") for ch in CHANNEL_COLORS
        }
        self.channel_coupling_vars = {
            ch: tk.StringVar(value="DC") for ch in CHANNEL_COLORS
        }
        self.channel_badge_vars = {
            ch: tk.StringVar(value=f"{ch}  1 V/div") for ch in CHANNEL_COLORS
        }
        self.channel_badge_labels: dict[str, tk.Label] = {}
        self.time_badge_var = tk.StringVar(value="M  1 ms/div")
        self.trigger_badge_var = tk.StringVar(value="T  CH1  0 V  @ 50%")
        self.scope_sync_var = tk.StringVar(value="SCALE --")

        self.time_scale_var = tk.StringVar(value="1 ms/div")
        self._user_time_div_s = 1e-3
        self.horizontal_position_var = tk.StringVar(value="50")

        self.trigger_source_var = tk.StringVar(value="CH1")
        self.trigger_level_var = tk.StringVar(value="0")
        self.trigger_slope_var = tk.StringVar(value="RISE")
        self.trigger_mode_var = tk.StringVar(value="AUTO")

        self.acquire_mode_var = tk.StringVar(value="SAMPLE")
        self.average_count_var = tk.StringVar(value="16")

        self.scpi_var = tk.StringVar(value="*IDN?")
        self.scpi_result_var = tk.StringVar(value="Ready")

        self.measure_vars: dict[str, dict[str, tk.StringVar]] = {}
        self.channel_cards: dict[str, tk.Frame] = {}

        self._setup_style()
        self._build_ui()
        self._select_channel("CH1")

        self.root.after(30, self._process_command_queue)
        self.root.after(16, self._render_latest_frames)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _setup_style(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure("Dark.TFrame", background="#0E151D")
        style.configure("Panel.TFrame", background="#0E151D")
        style.configure(
            "Dark.TLabel",
            background="#0E151D",
            foreground="#DCE6F0",
            font=("Arial", 10),
        )
        style.configure(
            "Title.TLabel",
            background="#0E151D",
            foreground="#F7FAFC",
            font=("Arial", 15, "bold"),
        )
        style.configure(
            "Section.TLabel",
            background="#0E151D",
            foreground="#8FA2B5",
            font=("Arial", 9, "bold"),
        )

        style.configure(
            "Dark.TButton",
            background="#18232E",
            foreground="#DCE6F0",
            bordercolor="#30404F",
            lightcolor="#18232E",
            darkcolor="#18232E",
            padding=(7, 4),
            font=("Arial", 9, "bold"),
        )
        style.map(
            "Dark.TButton",
            background=[("active", "#22313E"), ("disabled", "#111820")],
            foreground=[("disabled", "#607080")],
        )

        style.configure(
            "Primary.TButton",
            background="#0F7A4D",
            foreground="#FFFFFF",
            bordercolor="#159661",
            lightcolor="#0F7A4D",
            darkcolor="#0F7A4D",
            padding=(8, 4),
            font=("Arial", 9, "bold"),
        )
        style.map(
            "Primary.TButton",
            background=[("active", "#12945D"), ("disabled", "#153326")],
            foreground=[("disabled", "#6F8B7D")],
        )

        style.configure(
            "Stop.TButton",
            background="#8F2F3A",
            foreground="#FFFFFF",
            bordercolor="#B44250",
            lightcolor="#8F2F3A",
            darkcolor="#8F2F3A",
            padding=(8, 4),
            font=("Arial", 9, "bold"),
        )
        style.map("Stop.TButton", background=[("active", "#A83A47")])

        style.configure(
            "Action.TButton",
            background="#175C8F",
            foreground="#FFFFFF",
            bordercolor="#247AB7",
            lightcolor="#175C8F",
            darkcolor="#175C8F",
            padding=(7, 4),
            font=("Arial", 9, "bold"),
        )
        style.map(
            "Action.TButton",
            background=[("active", "#1E73AE"), ("disabled", "#172A38")],
            foreground=[("disabled", "#6B7F8D")],
        )

        style.configure(
            "Accent.TButton",
            background="#5B45A5",
            foreground="#FFFFFF",
            bordercolor="#755EC8",
            lightcolor="#5B45A5",
            darkcolor="#5B45A5",
            padding=(7, 4),
            font=("Arial", 9, "bold"),
        )
        style.map("Accent.TButton", background=[("active", "#7058C4")])

        style.configure(
            "Warn.TButton",
            background="#8A5B13",
            foreground="#FFFFFF",
            bordercolor="#A8731C",
            lightcolor="#8A5B13",
            darkcolor="#8A5B13",
            padding=(7, 4),
            font=("Arial", 9, "bold"),
        )
        style.map("Warn.TButton", background=[("active", "#A46C17")])

        style.configure(
            "Dark.TCombobox",
            fieldbackground="#091019",
            background="#091019",
            foreground="#EAF1F8",
            arrowcolor="#B9C8D6",
            bordercolor="#314151",
            lightcolor="#091019",
            darkcolor="#091019",
            padding=(5, 3),
        )
        style.map(
            "Dark.TCombobox",
            fieldbackground=[("readonly", "#091019")],
            foreground=[("readonly", "#EAF1F8")],
            selectbackground=[("readonly", "#175C8F")],
        )

    def _build_ui(self) -> None:
        self._build_menu()
        self._build_topbar()
        self._build_device_toolbar()

        # Fixed three-column layout: no draggable sash, no side-panel scrolling.
        body = tk.Frame(self.root, bg="#070B10")
        body.pack(fill="both", expand=True, padx=10, pady=4)
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(0, minsize=300, weight=0)
        body.grid_columnconfigure(1, weight=1)
        body.grid_columnconfigure(2, minsize=300, weight=0)

        left = self._build_left_panel(body)
        center = self._build_scope_panel(body)
        right = self._build_right_panel(body)

        left.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        center.grid(row=0, column=1, sticky="nsew")
        right.grid(row=0, column=2, sticky="nsew", padx=(6, 0))

        left.configure(width=300)
        right.configure(width=300)
        left.pack_propagate(False)
        right.pack_propagate(False)

        status = tk.Frame(self.root, bg="#080D12", height=28)
        status.pack(fill="x", padx=10, pady=(4, 8))
        status.pack_propagate(False)

        tk.Label(
            status,
            textvariable=self.status_var,
            bg="#080D12",
            fg="#B9C0C8",
            anchor="w",
            font=("Arial", 9),
        ).pack(side="left", padx=8, pady=4)

        tk.Label(
            status,
            textvariable=self.rate_var,
            bg="#080D12",
            fg="#B9C0C8",
            anchor="e",
            font=("Arial", 9),
        ).pack(side="right", padx=8, pady=4)

    def _seed_default_device(self) -> None:
        if self.registry.devices:
            return
        try:
            self.registry.upsert(
                DeviceConfig(
                    name="MSO44B-1",
                    model="Tektronix MSO44B",
                    connection_type=ConnectionType.VISA_TCPIP,
                    ip="192.168.1.133",
                )
            )
        except Exception:
            pass

    def _build_menu(self) -> None:
        menu = tk.Menu(self.root)

        file_menu = tk.Menu(menu, tearoff=False)
        file_menu.add_command(label="Save Data...", command=self._save_csv)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self._on_close)
        menu.add_cascade(label="File", menu=file_menu)

        edit_menu = tk.Menu(menu, tearoff=False)
        edit_menu.add_command(label="Scope View", command=self._autoscale)
        edit_menu.add_command(label="Refresh Device Settings", command=self._refresh_settings)
        menu.add_cascade(label="Edit", menu=edit_menu)

        tools_menu = tk.Menu(menu, tearoff=False)
        tools_menu.add_command(label="Get Data", command=self._focus_get_data)
        tools_menu.add_command(label="4 Channel Stack", command=self._stack_four_channels)
        tools_menu.add_separator()
        tools_menu.add_command(label="SCPI Console", command=lambda: self.scpi_entry.focus_set())
        menu.add_cascade(label="Tools", menu=tools_menu)

        connection_menu = tk.Menu(menu, tearoff=False)
        connection_menu.add_command(
            label="Manage Devices...",
            command=self._open_connection_manager,
        )
        connection_menu.add_command(
            label="Connect Active Device",
            command=self._connect_active_device,
        )
        connection_menu.add_command(
            label="Disconnect Active Device",
            command=self._disconnect_active_device,
        )
        connection_menu.add_separator()
        connection_menu.add_command(
            label="Disconnect All",
            command=self._disconnect_all_devices,
        )
        menu.add_cascade(label="Connection", menu=connection_menu)

        automation_menu = tk.Menu(menu, tearoff=False)
        automation_menu.add_command(
            label="Automation Test...",
            command=self._show_automation_placeholder,
        )
        automation_menu.add_command(
            label="IC Profiles...",
            command=self._show_profile_placeholder,
        )
        menu.add_cascade(label="Automation", menu=automation_menu)

        help_menu = tk.Menu(menu, tearoff=False)
        help_menu.add_command(label="About", command=self._show_about)
        menu.add_cascade(label="Help", menu=help_menu)

        self.root.config(menu=menu)

    def _build_device_toolbar(self) -> None:
        bar = tk.Frame(
            self.root,
            bg="#0B1118",
            highlightbackground="#223240",
            highlightthickness=1,
            height=38,
        )
        bar.pack(fill="x", padx=10, pady=(0, 4))
        bar.pack_propagate(False)

        tk.Label(
            bar,
            text="ACTIVE DEVICE",
            bg="#0B1118",
            fg="#8094A7",
            font=("Arial", 8, "bold"),
        ).pack(side="left", padx=(10, 5))

        self.device_combo = ttk.Combobox(
            bar,
            textvariable=self.active_device_var,
            state="readonly",
            width=30,
            style="Dark.TCombobox",
        )
        self.device_combo.pack(side="left", padx=3, pady=5)
        self.device_combo.bind(
            "<<ComboboxSelected>>",
            lambda _e: self._active_device_changed(),
        )

        self.device_status_label = tk.Label(
            bar,
            textvariable=self.device_status_var,
            bg="#18212A",
            fg="#8392A0",
            font=("Arial", 8, "bold"),
            padx=8,
            pady=4,
        )
        self.device_status_label.pack(side="left", padx=6)

        ttk.Button(
            bar,
            text="CONNECTIONS",
            command=self._open_connection_manager,
            style="Dark.TButton",
        ).pack(side="right", padx=5, pady=4)

        ttk.Button(
            bar,
            text="GET DATA",
            command=self._get_data_once,
            style="Action.TButton",
        ).pack(side="right", padx=3, pady=4)

        self._refresh_device_selector()

    def _open_connection_manager(self) -> None:
        ConnectionManagerDialog(
            self.root,
            self.registry,
            on_changed=self._refresh_device_selector,
        )

    def _refresh_device_selector(self) -> None:
        online = self.registry.online_devices()
        names = [device.name for device in online]
        if hasattr(self, "device_combo"):
            self.device_combo.configure(values=names)

        current = self.active_device_var.get()
        if current not in names:
            self.active_device_var.set(names[0] if names else "")

        self._active_device_changed()

    def _active_device_changed(self) -> None:
        name = self.active_device_var.get().strip()
        if not name:
            self.device_status_var.set("No online device")
            if hasattr(self, "device_status_label"):
                self.device_status_label.configure(bg="#24191B", fg="#A9797E")
            return

        config = self.registry.get(name)
        driver = self.registry.get_driver(name)
        status = self.registry.get_status(name)

        if config is None or driver is None or status != DeviceStatus.ONLINE:
            self.device_status_var.set(f"{name} • OFFLINE")
            return

        self.device_status_var.set(f"{name} • ONLINE • {config.model}")
        if hasattr(self, "device_status_label"):
            self.device_status_label.configure(bg="#11271B", fg="#67D795")

        # Existing scope UI can immediately reuse the connected MSO client.
        client = getattr(driver, "client", None)
        if client is not None:
            self.client = client
            self.ip_var.set(config.ip)
            self.connect_btn.configure(text="DISCONNECT", state="normal")
            self.run_btn.configure(state="normal")
            self.single_btn.configure(state="normal")
            self.get_data_btn.configure(state="normal")
            self.autoset_btn.configure(state="normal")
            self.default_btn.configure(state="normal")
            self.scpi_btn.configure(state="normal")
            self.connection_badge_var.set("● ONLINE")
            self.connection_badge.configure(bg="#10271C", fg="#6DDB9E")
            self.status_var.set(f"Active device: {name}")
            try:
                settings = client.get_scope_settings()
                self._apply_settings_to_ui(settings)
            except Exception:
                pass
        else:
            self.status_var.set(
                f"{config.model}: generic Get Data UI will use device capabilities."
            )

    def _connect_active_device(self) -> None:
        name = self.active_device_var.get().strip()
        if not name:
            devices = self.registry.devices
            if not devices:
                self._open_connection_manager()
                return
            name = devices[0].name

        self.status_var.set(f"Connecting {name}...")

        def worker() -> None:
            try:
                self.registry.connect(name)
                self.command_queue.put(("registry_changed", name, None))
            except Exception as exc:
                self.command_queue.put(("registry_changed", name, str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _disconnect_active_device(self) -> None:
        name = self.active_device_var.get().strip()
        if not name:
            return
        self.registry.disconnect(name)
        if self.client and not self.registry.get_driver(name):
            self.client = None
        self._refresh_device_selector()
        self.status_var.set(f"{name}: disconnected")

    def _disconnect_all_devices(self) -> None:
        self.registry.disconnect_all()
        self.client = None
        self._refresh_device_selector()
        self.status_var.set("All devices disconnected")

    def _focus_get_data(self) -> None:
        if not self.registry.online_devices():
            self._open_connection_manager()
            return
        self._get_data_once()

    def _show_automation_placeholder(self) -> None:
        messagebox.showinfo(
            "Automation Test",
            "Automation engine is installed. Next UI will contain test sequence, IC profile, run/stop, live log and PASS/FAIL results.",
        )

    def _show_profile_placeholder(self) -> None:
        messagebox.showinfo(
            "IC Profiles",
            "IC-specific voltages, channel mapping, timing and limits will be stored in profiles so test scripts do not need to change when the IC changes.",
        )

    def _show_about(self) -> None:
        messagebox.showinfo(
            "About",
            "Instrument Automation Test Platform\nMulti-device connection, Get Data and reusable test automation.",
        )

    def _build_topbar(self) -> None:
        top = ttk.Frame(self.root, style="Dark.TFrame", padding=(12, 9))
        top.pack(fill="x", padx=10, pady=(10, 6))

        brand = ttk.Frame(top, style="Dark.TFrame")
        brand.pack(side="left")

        ttk.Label(
            brand,
            text="MSO44B LAN SCOPE",
            style="Title.TLabel",
        ).pack(anchor="w")

        tk.Label(
            brand,
            text="Tektronix 4 Series • TCPIP/VISA waveform monitor",
            bg="#0E151D",
            fg="#708396",
            font=("Arial", 8),
        ).pack(anchor="w", pady=(1, 0))

        controls = ttk.Frame(top, style="Dark.TFrame")
        controls.pack(side="right")

        self.connection_badge = tk.Label(
            controls,
            textvariable=self.connection_badge_var,
            bg="#151C23",
            fg="#7E8C99",
            font=("Arial", 9, "bold"),
            padx=8,
            pady=5,
        )
        self.connection_badge.pack(side="left", padx=(0, 7))

        tk.Label(
            controls,
            text="IP",
            bg="#0E151D",
            fg="#8FA2B5",
            font=("Arial", 9, "bold"),
        ).pack(side="left", padx=(0, 4))

        self.ip_entry = tk.Entry(
            controls,
            textvariable=self.ip_var,
            width=14,
            bg="#091019",
            fg="#F0F5FA",
            insertbackground="#F0F5FA",
            relief="flat",
            highlightthickness=1,
            highlightbackground="#30404F",
            highlightcolor="#247AB7",
            font=("Menlo", 9),
        )
        self.ip_entry.pack(side="left", padx=(0, 7), ipady=5)

        self.connect_btn = ttk.Button(
            controls,
            text="CONNECT",
            command=self._toggle_connection,
            style="Action.TButton",
        )
        self.connect_btn.pack(side="left", padx=2)

        self.run_btn = ttk.Button(
            controls,
            text="RUN",
            command=self._toggle_run,
            style="Primary.TButton",
            state="disabled",
        )
        self.run_btn.pack(side="left", padx=2)

        self.single_btn = ttk.Button(
            controls,
            text="SINGLE",
            command=self._single,
            style="Dark.TButton",
            state="disabled",
        )
        self.single_btn.pack(side="left", padx=2)

        self.get_data_btn = ttk.Button(
            controls,
            text="GET DATA",
            command=self._get_data_once,
            style="Action.TButton",
            state="disabled",
        )
        self.get_data_btn.pack(side="left", padx=2)

        self.autoset_btn = ttk.Button(
            controls,
            text="AUTOSET",
            command=self._autoset,
            style="Accent.TButton",
            state="disabled",
        )
        self.autoset_btn.pack(side="left", padx=2)

        self.default_btn = ttk.Button(
            controls,
            text="DEFAULT",
            command=self._factory_default,
            style="Warn.TButton",
            state="disabled",
        )
        self.default_btn.pack(side="left", padx=2)

    def _build_left_panel(self, parent) -> tk.Frame:
        outer = tk.Frame(
            parent,
            bg="#0E151D",
            highlightbackground="#243342",
            highlightthickness=1,
        )

        self._section_label(outer, "CHANNELS", top_pad=7)

        for ch, color in CHANNEL_COLORS.items():
            self._build_channel_card(outer, ch, color)

        self._section_label(outer, "ACQUISITION", top_pad=8)

        acq = tk.Frame(outer, bg="#0E151D")
        acq.pack(fill="x", padx=8)

        self._form_label(acq, "Mode", 0)
        mode_combo = ttk.Combobox(
            acq,
            textvariable=self.acquire_mode_var,
            values=["SAMPLE", "PEAKDETECT", "HIRES", "AVERAGE", "ENVELOPE"],
            state="readonly",
            style="Dark.TCombobox",
            width=11,
        )
        mode_combo.grid(row=0, column=1, sticky="ew", pady=2)
        mode_combo.bind("<<ComboboxSelected>>", lambda _e: self._apply_acquire_mode())

        self._form_label(acq, "Avg N", 1)
        avg = self._dark_entry(acq, self.average_count_var, 7)
        avg.grid(row=1, column=1, sticky="ew", pady=2)
        avg.bind("<Return>", lambda _e: self._apply_average_count())

        self._form_label(acq, "Transfer", 2)
        ttk.Combobox(
            acq,
            textvariable=self.points_var,
            values=["1000", "2500", "5000", "10000", "25000", "50000"],
            state="readonly",
            style="Dark.TCombobox",
            width=11,
        ).grid(row=2, column=1, sticky="ew", pady=2)

        self._form_label(acq, "Refresh ms", 3)
        self._dark_entry(acq, self.refresh_var, 7).grid(
            row=3, column=1, sticky="ew", pady=2
        )

        self._form_label(acq, "Display pts", 4)
        self._dark_entry(acq, self.fast_record_var, 7).grid(
            row=4, column=1, sticky="ew", pady=2
        )
        acq.columnconfigure(1, weight=1)

        options = tk.Frame(outer, bg="#0E151D")
        options.pack(fill="x", padx=8, pady=(3, 2))

        tk.Checkbutton(
            options,
            text="Fast display",
            variable=self.fast_mode_var,
            bg="#0E151D",
            fg="#D7DEE7",
            selectcolor="#0B1016",
            activebackground="#111820",
            activeforeground="#F0F4F8",
            font=("Arial", 9),
        ).pack(side="left")

        tk.Checkbutton(
            options,
            text="GET full record",
            variable=self.get_full_record_var,
            bg="#0E151D",
            fg="#D7DEE7",
            selectcolor="#0B1016",
            activebackground="#111820",
            activeforeground="#F0F4F8",
            font=("Arial", 9),
        ).pack(side="right")

        ttk.Button(
            outer,
            text="APPLY ACQUISITION",
            command=self._apply_acquisition_controls,
            style="Dark.TButton",
        ).pack(fill="x", padx=8, pady=(2, 4))

        self._section_label(outer, "SCPI", top_pad=6)

        scpi_row = tk.Frame(outer, bg="#0E151D")
        scpi_row.pack(fill="x", padx=8)
        self.scpi_entry = self._dark_entry(scpi_row, self.scpi_var, 18)
        self.scpi_entry.pack(side="left", fill="x", expand=True, ipady=2)
        self.scpi_entry.bind("<Return>", lambda _e: self._send_scpi())

        self.scpi_btn = ttk.Button(
            scpi_row,
            text="SEND",
            command=self._send_scpi,
            style="Dark.TButton",
            state="disabled",
        )
        self.scpi_btn.pack(side="right", padx=(4, 0))

        tk.Label(
            outer,
            textvariable=self.scpi_result_var,
            bg="#0E151D",
            fg="#93A0AD",
            justify="left",
            wraplength=275,
            anchor="nw",
            font=("Arial", 8),
        ).pack(fill="x", padx=8, pady=(3, 5))

        return outer

    def _build_channel_card(self, parent, ch: str, color: str) -> None:
        card = tk.Frame(
            parent,
            bg="#121B24",
            highlightbackground="#2B3B4B",
            highlightthickness=1,
        )
        card.pack(fill="x", padx=8, pady=3)
        card.bind("<Button-1>", lambda _e, c=ch: self._select_channel(c))
        self.channel_cards[ch] = card

        accent = tk.Frame(card, bg=color, width=4)
        accent.pack(side="left", fill="y")
        accent.pack_propagate(False)

        content = tk.Frame(card, bg="#121B24")
        content.pack(side="left", fill="both", expand=True)

        row1 = tk.Frame(content, bg="#121B24")
        row1.pack(fill="x", padx=5, pady=(3, 1))

        cb = tk.Checkbutton(
            row1,
            text=ch,
            variable=self.channel_vars[ch],
            command=lambda c=ch: self._channel_state_changed(c),
            bg="#121B24",
            fg=color,
            selectcolor="#0B1016",
            activebackground="#161E27",
            activeforeground=color,
            font=("Arial", 10, "bold"),
        )
        cb.pack(side="left")

        tk.Label(row1, text="V/div", bg="#121B24", fg="#8FA3B8", font=("Arial", 8)).pack(
            side="left", padx=(5, 2)
        )
        ttk.Combobox(
            row1,
            textvariable=self.channel_scale_vars[ch],
            values=[self._format_vdiv(v) for v in VERTICAL_SCALES],
            state="readonly",
            style="Dark.TCombobox",
            width=9,
        ).pack(side="left")

        ttk.Button(
            row1,
            text="SET",
            command=lambda c=ch: self._apply_channel(c),
            style="Action.TButton",
        ).pack(side="right")

        row2 = tk.Frame(content, bg="#121B24")
        row2.pack(fill="x", padx=5, pady=(1, 4))

        tk.Label(row2, text="Pos", bg="#121B24", fg="#8FA3B8", font=("Arial", 8)).pack(side="left")
        self._dark_entry(row2, self.channel_position_vars[ch], 5).pack(
            side="left", padx=(2, 5), ipady=1
        )

        tk.Label(row2, text="Off", bg="#121B24", fg="#8FA3B8", font=("Arial", 8)).pack(side="left")
        self._dark_entry(row2, self.channel_offset_vars[ch], 5).pack(
            side="left", padx=(2, 5), ipady=1
        )

        ttk.Combobox(
            row2,
            textvariable=self.channel_coupling_vars[ch],
            values=["DC", "AC"],
            state="readonly",
            style="Dark.TCombobox",
            width=4,
        ).pack(side="right")

    def _build_scope_panel(self, parent) -> tk.Frame:
        frame = tk.Frame(
            parent,
            bg="#020609",
            highlightbackground="#243342",
            highlightthickness=1,
        )

        info = tk.Frame(frame, bg="#080D12", height=34)
        info.pack(fill="x")
        info.pack_propagate(False)

        tk.Label(
            info,
            textvariable=self.transfer_var,
            bg="#080D12",
            fg="#8FA3B8",
            anchor="w",
            font=("Menlo", 8),
        ).pack(side="left", fill="x", expand=True, padx=8)

        ttk.Button(
            info,
            text="4CH STACK",
            command=self._stack_four_channels,
            style="Accent.TButton",
        ).pack(side="right", padx=(2, 5), pady=3)

        ttk.Button(
            info,
            text="CENTER CH",
            command=self._center_selected_channel,
            style="Dark.TButton",
        ).pack(side="right", padx=2, pady=3)

        ttk.Button(
            info,
            text="SCOPE VIEW",
            command=self._autoscale,
            style="Action.TButton",
        ).pack(side="right", padx=2, pady=3)

        ttk.Button(
            info,
            text="SAVE CSV",
            command=self._save_csv,
            style="Dark.TButton",
        ).pack(side="right", padx=2, pady=3)

        self.canvas = tk.Canvas(
            frame,
            bg="#020609",
            highlightthickness=0,
            cursor="crosshair",
        )
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda _e: self._redraw_scope())
        self.canvas.bind("<Motion>", self._on_mouse_move)
        self.canvas.bind("<MouseWheel>", self._on_scope_wheel)
        self.canvas.bind("<Button-4>", self._on_scope_wheel)
        self.canvas.bind("<Button-5>", self._on_scope_wheel)

        footer = tk.Frame(frame, bg="#080D12", height=42)
        footer.pack(fill="x")
        footer.pack_propagate(False)

        channel_bar = tk.Frame(footer, bg="#080D12")
        channel_bar.pack(side="left", padx=5, pady=5)

        for ch, color in CHANNEL_COLORS.items():
            label = tk.Label(
                channel_bar,
                textvariable=self.channel_badge_vars[ch],
                bg="#111922",
                fg=color,
                font=("Menlo", 8, "bold"),
                padx=6,
                pady=4,
                relief="flat",
            )
            label.pack(side="left", padx=2)
            label.bind("<Button-1>", lambda _e, c=ch: self._select_channel(c))
            self.channel_badge_labels[ch] = label

        tk.Label(
            footer,
            textvariable=self.cursor_var,
            bg="#080D12",
            fg="#718294",
            font=("Menlo", 8),
        ).pack(side="left", fill="x", expand=True, padx=8)

        time_bar = tk.Frame(footer, bg="#080D12")
        time_bar.pack(side="right", padx=5, pady=5)

        tk.Label(
            time_bar,
            textvariable=self.time_badge_var,
            bg="#13202B",
            fg="#E3EBF2",
            font=("Menlo", 8, "bold"),
            padx=7,
            pady=4,
        ).pack(side="left", padx=2)

        tk.Label(
            time_bar,
            textvariable=self.trigger_badge_var,
            bg="#2A2111",
            fg="#F6B94A",
            font=("Menlo", 8, "bold"),
            padx=7,
            pady=4,
        ).pack(side="left", padx=2)

        self.scope_sync_label = tk.Label(
            time_bar,
            textvariable=self.scope_sync_var,
            bg="#151C23",
            fg="#7E8C99",
            font=("Menlo", 8, "bold"),
            padx=7,
            pady=4,
        )
        self.scope_sync_label.pack(side="left", padx=2)

        return frame

    def _build_right_panel(self, parent) -> tk.Frame:
        outer = tk.Frame(
            parent,
            bg="#0E151D",
            highlightbackground="#243342",
            highlightthickness=1,
        )

        self._section_label(outer, "HORIZONTAL", top_pad=7)
        horizontal = tk.Frame(outer, bg="#0E151D")
        horizontal.pack(fill="x", padx=8)

        self._form_label(horizontal, "Time/div", 0)
        ttk.Combobox(
            horizontal,
            textvariable=self.time_scale_var,
            values=[self._format_time_div(v) for v in TIME_SCALES],
            state="readonly",
            style="Dark.TCombobox",
            width=11,
        ).grid(row=0, column=1, sticky="ew", pady=2)

        self._form_label(horizontal, "Position %", 1)
        self._dark_entry(horizontal, self.horizontal_position_var, 7).grid(
            row=1, column=1, sticky="ew", pady=2
        )
        horizontal.columnconfigure(1, weight=1)

        ttk.Button(
            outer,
            text="APPLY HORIZONTAL",
            command=self._apply_horizontal,
            style="Dark.TButton",
        ).pack(fill="x", padx=8, pady=(3, 4))

        self._section_label(outer, "TRIGGER", top_pad=5)
        trigger = tk.Frame(outer, bg="#0E151D")
        trigger.pack(fill="x", padx=8)

        self._form_label(trigger, "Source", 0)
        ttk.Combobox(
            trigger,
            textvariable=self.trigger_source_var,
            values=["CH1", "CH2", "CH3", "CH4"],
            state="readonly",
            style="Dark.TCombobox",
            width=10,
        ).grid(row=0, column=1, sticky="ew", pady=2)

        self._form_label(trigger, "Level V", 1)
        self._dark_entry(trigger, self.trigger_level_var, 7).grid(
            row=1, column=1, sticky="ew", pady=2
        )

        self._form_label(trigger, "Slope", 2)
        ttk.Combobox(
            trigger,
            textvariable=self.trigger_slope_var,
            values=["RISE", "FALL", "EITHER"],
            state="readonly",
            style="Dark.TCombobox",
            width=10,
        ).grid(row=2, column=1, sticky="ew", pady=2)

        self._form_label(trigger, "Mode", 3)
        ttk.Combobox(
            trigger,
            textvariable=self.trigger_mode_var,
            values=["AUTO", "NORMAL"],
            state="readonly",
            style="Dark.TCombobox",
            width=10,
        ).grid(row=3, column=1, sticky="ew", pady=2)
        trigger.columnconfigure(1, weight=1)

        ttk.Button(
            outer,
            text="APPLY TRIGGER",
            command=self._apply_trigger,
            style="Dark.TButton",
        ).pack(fill="x", padx=8, pady=(3, 2))

        trig_buttons = tk.Frame(outer, bg="#0E151D")
        trig_buttons.pack(fill="x", padx=8)
        ttk.Button(
            trig_buttons,
            text="50%",
            command=self._trigger_50,
            style="Dark.TButton",
        ).pack(side="left", fill="x", expand=True, padx=(0, 2))
        ttk.Button(
            trig_buttons,
            text="FORCE",
            command=self._force_trigger,
            style="Dark.TButton",
        ).pack(side="left", fill="x", expand=True, padx=(2, 0))

        self._section_label(outer, "MEASUREMENTS", top_pad=7)

        table = tk.Frame(
            outer,
            bg="#121B24",
            highlightbackground="#2B3B4B",
            highlightthickness=1,
        )
        table.pack(fill="x", padx=8, pady=2)

        fields = [
            ("pkpk", "Vpp"),
            ("rms", "Vrms"),
            ("frequency", "Freq"),
            ("mean", "Mean"),
            ("min", "Min"),
            ("max", "Max"),
        ]

        tk.Label(table, text="", bg="#121B24").grid(row=0, column=0, padx=3, pady=2)
        for col, (ch, color) in enumerate(CHANNEL_COLORS.items(), start=1):
            tk.Label(
                table,
                text=ch,
                bg="#121B24",
                fg=color,
                font=("Arial", 8, "bold"),
            ).grid(row=0, column=col, padx=3, pady=2)

        for row, (key, caption) in enumerate(fields, start=1):
            tk.Label(
                table,
                text=caption,
                bg="#121B24",
                fg="#AEB8C2",
                font=("Arial", 8),
                anchor="w",
            ).grid(row=row, column=0, sticky="w", padx=4, pady=1)

            for col, ch in enumerate(CHANNEL_COLORS, start=1):
                if ch not in self.measure_vars:
                    self.measure_vars[ch] = {}
                var = tk.StringVar(value="--")
                self.measure_vars[ch][key] = var
                tk.Label(
                    table,
                    textvariable=var,
                    bg="#121B24",
                    fg="#F0F4F8",
                    font=("Menlo", 7),
                    width=8,
                    anchor="e",
                ).grid(row=row, column=col, padx=2, pady=1)

        for col in range(1, 5):
            table.columnconfigure(col, weight=1)

        ttk.Button(
            outer,
            text="REFRESH SETTINGS",
            command=self._refresh_settings,
            style="Dark.TButton",
        ).pack(fill="x", padx=8, pady=(6, 5))

        tk.Label(
            outer,
            text="Wheel: CH Position | Shift+Wheel: V/div | Ctrl/Cmd+Wheel: Time/div | Fast display keeps full record",
            bg="#0E151D",
            fg="#708090",
            font=("Arial", 8),
            anchor="center",
        ).pack(fill="x", padx=8, pady=(2, 5))

        return outer

    def _section_label(self, parent, text: str, top_pad: int = 10) -> None:
        row = tk.Frame(parent, bg="#0E151D")
        row.pack(fill="x", padx=8, pady=(top_pad, 5))
        tk.Frame(row, bg="#247AB7", width=3, height=14).pack(side="left", padx=(0, 6))
        tk.Label(
            row,
            text=text,
            bg="#0E151D",
            fg="#B6C7D8",
            font=("Arial", 9, "bold"),
        ).pack(side="left")

    @staticmethod
    def _form_label(parent, text: str, row: int) -> None:
        tk.Label(
            parent,
            text=text,
            bg=parent.cget("bg"),
            fg="#D7DEE7",
        ).grid(row=row, column=0, sticky="w", pady=3)

    @staticmethod
    def _dark_entry(parent, variable: tk.StringVar, width: int) -> tk.Entry:
        return tk.Entry(
            parent,
            textvariable=variable,
            width=width,
            bg="#091019",
            fg="#F0F5FA",
            insertbackground="#F0F5FA",
            relief="flat",
            highlightthickness=1,
            highlightbackground="#30404F",
            highlightcolor="#247AB7",
        )

    # ------------------------------------------------------------------
    # Connection / commands
    # ------------------------------------------------------------------

    def _toggle_connection(self) -> None:
        if self.client and self.client.connected:
            self._disconnect()
            return

        host = self.ip_var.get().strip()
        if not host:
            messagebox.showwarning("MSO4", "Enter the oscilloscope IP address.")
            return

        self.connect_btn.configure(state="disabled")
        self.status_var.set(f"Connecting to {host}...")

        def worker() -> None:
            try:
                client = MSO4Client(host, timeout=6.0)
                idn = client.connect()
                settings = client.get_scope_settings()
                hsi = TekHSIWaveformClient(host, port=5000)
                self.command_queue.put(
                    ("connected", client, hsi, idn, settings)
                )
            except Exception as exc:
                self.command_queue.put(("connect_error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _disconnect(self) -> None:
        self._stop_acquisition(local_only=True)

        hsi = self.hsi_client
        self.hsi_client = None
        if hsi:
            try:
                hsi.close()
            except Exception:
                pass

        client = self.client
        self.client = None
        self.instrument_idn = ""
        if client:
            try:
                client.close()
            except Exception:
                pass

        self.connect_btn.configure(text="CONNECT", state="normal")
        self.run_btn.configure(text="RUN", state="disabled")
        self.single_btn.configure(state="disabled")
        self.get_data_btn.configure(state="disabled")
        self.autoset_btn.configure(state="disabled")
        self.default_btn.configure(state="disabled")
        self.scpi_btn.configure(state="disabled")
        self.ip_entry.configure(state="normal")

        self.status_var.set("Disconnected")
        self.connection_badge_var.set("● OFFLINE")
        self.connection_badge.configure(bg="#1D1719", fg="#A8767A")
        self.rate_var.set("Acq: -- fps")
        self.transfer_var.set("No waveform data yet")

        with self.frame_lock:
            self.latest_frames.clear()
        self.waveforms.clear()
        self.x_range = None
        self.y_range = None
        self._scope_static_key = None
        self._wave_items.clear()
        self._redraw_scope()

    def _run_async(self, description: str, fn, refresh_settings: bool = False) -> None:
        if not self.client or not self.client.connected:
            return

        self.status_var.set(description)

        def worker() -> None:
            try:
                result = fn()
                settings = self.client.get_scope_settings() if refresh_settings else None
                self.command_queue.put(("command_ok", description, result, settings))
            except Exception as exc:
                self.command_queue.put(("command_error", description, str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _channel_state_changed(self, ch: str) -> None:
        enabled = bool(self.channel_vars[ch].get())

        if enabled:
            self._select_channel(ch)

        # Never reuse stale data when a channel is toggled.
        with self.frame_lock:
            self.latest_frames.pop(ch, None)
        self.waveforms.pop(ch, None)
        self.measurements.pop(ch, None)

        for var in self.measure_vars.get(ch, {}).values():
            var.set("--")

        item_id = self._wave_items.get(ch)
        if item_id is not None:
            try:
                self.canvas.itemconfigure(item_id, state="hidden")
            except tk.TclError:
                self._wave_items.pop(ch, None)

        was_running = bool(self.acq_thread and self.acq_thread.is_alive())
        self._resume_after_channel_change = (
            self._resume_after_channel_change or was_running
        )

        if was_running:
            self._stop_acquisition(local_only=True)

        # Multiple fast checkbox clicks collapse into one VISA update/restart.
        if self._channel_apply_after_id is not None:
            try:
                self.root.after_cancel(self._channel_apply_after_id)
            except Exception:
                pass

        self._channel_apply_after_id = self.root.after(
            140,
            self._apply_channel_selection,
        )
        self._redraw_scope()

    def _apply_channel_selection(self) -> None:
        self._channel_apply_after_id = None

        active = [
            ch
            for ch, var in self.channel_vars.items()
            if bool(var.get())
        ]
        resume = self._resume_after_channel_change
        self._resume_after_channel_change = False

        if not self.client or not self.client.connected:
            self._redraw_scope()
            return

        self.status_var.set(
            "Applying channels: " + (", ".join(active) if active else "none")
        )

        def worker() -> None:
            try:
                # One serialized VISA transaction sequence: physical scope state
                # exactly follows the UI checkbox state.
                for channel in CHANNEL_COLORS:
                    self.client.set_channel_state(
                        channel,
                        channel in active,
                    )

                self.command_queue.put(
                    ("channel_selection_applied", active, resume)
                )
            except Exception as exc:
                self.command_queue.put(
                    ("command_error", "Channel selection", str(exc))
                )

        threading.Thread(target=worker, daemon=True).start()

    def _apply_channel(self, ch: str) -> None:
        try:
            scale = self._parse_vdiv(self.channel_scale_vars[ch].get())
            position = float(self.channel_position_vars[ch].get())
            offset = float(self.channel_offset_vars[ch].get())
            coupling = self.channel_coupling_vars[ch].get()
        except ValueError as exc:
            messagebox.showwarning(ch, f"Invalid channel value: {exc}")
            return

        def command() -> None:
            self.client.set_channel_scale(ch, scale)
            self.client.set_channel_position(ch, position)
            self.client.set_channel_offset(ch, offset)
            self.client.set_channel_coupling(ch, coupling)

        self.scope_sync_var.set("SCALE PENDING")
        self._run_async(f"Applying {ch}", command, refresh_settings=True)

    def _apply_horizontal(self) -> None:
        try:
            scale = self._parse_time_div(self.time_scale_var.get())
            position = float(self.horizontal_position_var.get())
        except ValueError as exc:
            messagebox.showwarning("Horizontal", f"Invalid value: {exc}")
            return

        self._user_time_div_s = scale

        def command() -> None:
            actual = self.client.set_horizontal_scale(scale)
            self.client.set_horizontal_position(position)
            return actual

        self.scope_sync_var.set("SCALE PENDING")
        self._run_async("Applying horizontal", command, refresh_settings=True)

    def _apply_trigger(self) -> None:
        try:
            source = self.trigger_source_var.get()
            level = float(self.trigger_level_var.get())
            slope = self.trigger_slope_var.get()
            mode = self.trigger_mode_var.get()
        except ValueError as exc:
            messagebox.showwarning("Trigger", f"Invalid value: {exc}")
            return

        def command() -> None:
            self.client.set_trigger_source(source)
            self.client.set_trigger_level(source, level)
            self.client.set_trigger_slope(slope)
            self.client.set_trigger_mode(mode)

        self._run_async("Applying trigger", command, refresh_settings=True)

    def _apply_acquire_mode(self) -> None:
        mode = self.acquire_mode_var.get()
        self._run_async(
            f"Acquire mode {mode}",
            lambda: self.client.set_acquire_mode(mode),
        )

    def _apply_average_count(self) -> None:
        try:
            count = int(self.average_count_var.get())
        except ValueError:
            messagebox.showwarning("Average", "Average count must be an integer.")
            return

        self._run_async(
            f"Average count {count}",
            lambda: self.client.set_average_count(count),
        )

    def _apply_acquisition_controls(self) -> None:
        mode = self.acquire_mode_var.get()
        try:
            count = int(self.average_count_var.get())
        except ValueError:
            messagebox.showwarning("Acquisition", "Average count must be an integer.")
            return

        def command() -> None:
            self.client.set_acquire_mode(mode)
            if mode == "AVERAGE":
                self.client.set_average_count(count)

        self._run_async("Applying acquisition", command, refresh_settings=True)

    def _trigger_50(self) -> None:
        self._run_async(
            "Trigger level 50%",
            self.client.trigger_level_50_percent,
            refresh_settings=True,
        )

    def _force_trigger(self) -> None:
        self._run_async("Force trigger", self.client.force_trigger)

    def _autoset(self) -> None:
        self._run_async("Autoset running...", self.client.autoset, refresh_settings=True)

    def _factory_default(self) -> None:
        if not messagebox.askyesno(
            "Default Setup",
            "Reset oscilloscope to factory default setup?",
        ):
            return
        self._run_async(
            "Loading default setup...",
            self.client.factory_default,
            refresh_settings=True,
        )

    def _single(self) -> None:
        if not self.client or not self.client.connected:
            return

        self._stop_acquisition(local_only=True)

        channels = [ch for ch, var in self.channel_vars.items() if var.get()]
        if not channels:
            messagebox.showinfo("MSO4", "Enable at least one channel.")
            return

        try:
            points = max(500, int(self.points_var.get()))
        except ValueError:
            points = 5000

        self.stop_event.clear()

        def worker() -> None:
            try:
                try:
                    self.client.exit_realtime_mode()
                except Exception:
                    pass

                self.client.single_acquisition()
                self.client.wait_for_acquisition_complete(timeout=3.0)
                self._acquire_one_frame(channels, points, exact=True)
                self.command_queue.put(("single_done",))
            except Exception as exc:
                self.command_queue.put(("acq_error", f"Single: {exc}"))

        self.status_var.set("Single acquisition...")
        threading.Thread(target=worker, daemon=True).start()

    def _get_data_once(self) -> None:
        if not self.client or not self.client.connected:
            return

        channels = [ch for ch, var in self.channel_vars.items() if var.get()]
        if not channels:
            messagebox.showinfo("GET DATA", "Enable at least one channel.")
            return

        try:
            points = max(500, int(self.points_var.get()))
        except ValueError:
            points = 5000

        if self.get_full_record_var.get():
            record = self.client.get_record_length()
            if record and record > 0:
                points = record

        was_running = bool(self.acq_thread and self.acq_thread.is_alive())
        if was_running:
            self._stop_acquisition(local_only=True)

        # GET DATA is an explicit snapshot operation. Clear the local stop flag
        # before reading and use the full metadata/exact waveform path.
        self.stop_event.clear()

        self.status_var.set(f"GET DATA: reading {points} points...")
        self.transfer_var.set(f"Reading exact waveform snapshot ({points} pts)...")

        def worker() -> None:
            try:
                try:
                    self.client.exit_realtime_mode()
                except Exception:
                    pass

                started = time.monotonic()
                successful = self._acquire_one_frame(channels, points, exact=True)
                if successful == 0:
                    errors = " | ".join(
                        f"{ch}: {msg}" for ch, msg in self.channel_errors.items()
                    )
                    raise RuntimeError(errors or "No waveform data returned.")
                elapsed = time.monotonic() - started
                self.command_queue.put(("get_data_done", was_running, points, elapsed))
            except Exception as exc:
                self.command_queue.put(("command_error", "GET DATA", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _refresh_settings(self) -> None:
        self._run_async(
            "Reading scope settings...",
            lambda: None,
            refresh_settings=True,
        )

    def _send_scpi(self) -> None:
        if not self.client or not self.client.connected:
            return

        command = self.scpi_var.get().strip()
        if not command:
            return

        self.scpi_btn.configure(state="disabled")
        self.scpi_result_var.set("Working...")

        def worker() -> None:
            try:
                if command.endswith("?"):
                    result = self.client.query(command)
                else:
                    self.client.write(command)
                    result = "OK"
                self.command_queue.put(("scpi_result", result))
            except Exception as exc:
                self.command_queue.put(("scpi_result", f"ERROR: {exc}"))

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------------
    # Acquisition
    # ------------------------------------------------------------------

    def _toggle_run(self) -> None:
        if self.acq_thread and self.acq_thread.is_alive():
            self._stop_acquisition()
        else:
            self._start_acquisition()

    def _start_acquisition(self) -> None:
        if not self.client or not self.client.connected:
            return

        channels = [ch for ch, var in self.channel_vars.items() if var.get()]
        if not channels:
            messagebox.showinfo("MSO4", "Enable at least one channel.")
            return

        try:
            points = max(500, int(self.points_var.get()))
            refresh_ms = max(20, int(self.refresh_var.get()))
            fast_record = max(500, int(self.fast_record_var.get()))
            fast_mode = bool(self.fast_mode_var.get())
            if fast_mode:
                # Keep total display bandwidth bounded as more channels are enabled.
                # 1CH: up to requested points, 4CH: about 1/4 per channel.
                per_channel_budget = max(800, int(5000 / max(1, len(channels))))
                points = min(fast_record, per_channel_budget)
        except ValueError:
            messagebox.showwarning(
                "Acquisition",
                "Transfer points, refresh and fast record must be integers.",
            )
            return

        # A RUN owns its own stop event and generation. A blocked old
        # VXI-11 read can finish later, but it cannot publish a stale frame.
        self._acq_generation += 1
        generation = self._acq_generation
        session_stop = threading.Event()
        self.stop_event = session_stop

        self.channel_errors.clear()
        self._need_autoscale = True

        self.run_btn.configure(text="STOP", style="Stop.TButton")
        self.status_var.set(f"Starting {self._selected_channel} first...")
        self.transfer_var.set("Waiting for first waveform...")

        ordered_channels = list(channels)
        if self._selected_channel in ordered_channels:
            ordered_channels = [
                self._selected_channel,
                *[
                    ch
                    for ch in ordered_channels
                    if ch != self._selected_channel
                ],
            ]

        def worker() -> None:
            # Control plane: SCPI/VISA. Data plane: TekHSI first.
            try:
                if fast_mode:
                    realtime_info = self.client.enter_realtime_mode(
                        record_points=max(2000, min(10000, fast_record)),
                        time_scale=self._user_time_div_s,
                    )
                    self.command_queue.put(
                        ("realtime_mode", realtime_info)
                    )

                self.client.run_acquisition()
            except Exception as exc:
                self.command_queue.put(
                    ("acq_error", f"Cannot start acquisition: {exc}")
                )
                return

            self.command_queue.put(("prepared", None))

            # ----------------------------------------------------------
            # Preferred path: Tektronix High Speed Interface (port 5000)
            # ----------------------------------------------------------
            if (
                self.hsi_client is not None
                and TekHSIWaveformClient.available()
            ):
                try:
                    self.hsi_client.connect(ordered_channels)
                    self.waveform_transport = "TekHSI"
                    self.command_queue.put(("transport", "TekHSI"))

                    last_report = time.monotonic()
                    acquisitions = 0
                    first_sent = False

                    while (
                        not session_stop.is_set()
                        and generation == self._acq_generation
                    ):
                        started = time.monotonic()
                        batch = self.hsi_client.get_waveforms(
                            ordered_channels
                        )

                        if (
                            session_stop.is_set()
                            or generation != self._acq_generation
                        ):
                            break

                        if not batch:
                            continue

                        now = time.monotonic()

                        for ch, waveform in batch.items():
                            if ch not in ordered_channels:
                                continue

                            full_n = len(waveform.time_s)
                            if full_n == 0:
                                continue

                            # Keep complete time coverage but bound UI work.
                            stride = max(
                                1,
                                int(math.ceil(full_n / max(500, points))),
                            )
                            if stride > 1:
                                time_s = waveform.time_s[::stride]
                                volts = waveform.volts[::stride]
                            else:
                                time_s = waveform.time_s
                                volts = waveform.volts

                            display_waveform = type(waveform)(
                                channel=ch,
                                time_s=time_s,
                                volts=volts,
                            )

                            if (
                                now
                                - self._last_measure_update.get(ch, 0.0)
                                >= 0.20
                            ):
                                measurements = (
                                    display_waveform.measurements()
                                )
                                self._last_measure_update[ch] = now
                            else:
                                measurements = None

                            info = {
                                "mode": "TEKHSI",
                                "points": int(len(time_s)),
                                "record_length": int(full_n),
                                "resample": int(stride),
                                "time_start": (
                                    float(time_s[0])
                                    if len(time_s)
                                    else None
                                ),
                                "time_stop": (
                                    float(time_s[-1])
                                    if len(time_s)
                                    else None
                                ),
                            }

                            with self.frame_lock:
                                self.latest_frames[ch] = (
                                    time_s,
                                    volts,
                                    measurements,
                                    info,
                                )

                        if not first_sent:
                            self.command_queue.put(
                                (
                                    "first_waveform",
                                    "+".join(batch.keys()),
                                )
                            )
                            first_sent = True

                        acquisitions += 1

                        if now - last_report >= 1.0:
                            self.command_queue.put(
                                (
                                    "rate",
                                    acquisitions / (now - last_report),
                                )
                            )
                            acquisitions = 0
                            last_report = now

                        elapsed_ms = (
                            time.monotonic() - started
                        ) * 1000.0
                        remaining = max(0.0, refresh_ms - elapsed_ms)
                        if remaining:
                            session_stop.wait(remaining / 1000.0)

                    return

                except Exception as exc:
                    try:
                        self.hsi_client.close()
                    except Exception:
                        pass

                    self.waveform_transport = "SCPI"
                    self.command_queue.put(
                        ("hsi_fallback", str(exc))
                    )

            # ----------------------------------------------------------
            # Fallback: VISA/SCPI CURVE?
            # ----------------------------------------------------------
            self.waveform_transport = "SCPI"
            last_report = time.monotonic()
            updates = 0
            channel_index = 0
            consecutive_failures = 0
            per_tick_ms = max(
                2.0,
                refresh_ms / max(1, len(ordered_channels)),
            )

            while (
                not session_stop.is_set()
                and generation == self._acq_generation
            ):
                started = time.monotonic()
                ch = ordered_channels[channel_index]
                channel_index = (
                    channel_index + 1
                ) % len(ordered_channels)

                successful = self._acquire_one_frame(
                    [ch],
                    points,
                    exact=False,
                    stop_event=session_stop,
                    generation=generation,
                )

                if successful:
                    if updates == 0:
                        self.command_queue.put(
                            ("first_waveform", ch)
                        )
                    consecutive_failures = 0
                    updates += 1
                else:
                    consecutive_failures += 1
                    if consecutive_failures >= max(
                        4,
                        len(ordered_channels) * 3,
                    ):
                        errors = " | ".join(
                            f"{c}: {msg}"
                            for c, msg in self.channel_errors.items()
                        )
                        self.command_queue.put(
                            (
                                "acq_error",
                                "Repeated waveform read failures. "
                                + errors,
                            )
                        )
                        return

                now = time.monotonic()
                if now - last_report >= 1.0:
                    self.command_queue.put(
                        (
                            "rate",
                            updates / (now - last_report),
                        )
                    )
                    updates = 0
                    last_report = now

                elapsed_ms = (
                    time.monotonic() - started
                ) * 1000.0
                remaining = max(0.0, per_tick_ms - elapsed_ms)
                if remaining:
                    session_stop.wait(remaining / 1000.0)

        self.acq_thread = threading.Thread(target=worker, daemon=True)
        self.acq_thread.start()

    def _acquire_one_frame(
        self,
        channels: list[str],
        points: int,
        exact: bool = False,
        stop_event: threading.Event | None = None,
        generation: int | None = None,
    ) -> int:
        successful = 0

        for ch in channels:
            if stop_event is not None and stop_event.is_set():
                break
            if generation is not None and generation != self._acq_generation:
                break

            try:
                if exact:
                    waveform = self.client.get_waveform(ch, 1, points)
                else:
                    waveform = self.client.get_waveform_fast(ch, 1, points)
                info = self.client.get_last_transfer_info(ch)

                if stop_event is not None and stop_event.is_set():
                    break
                if generation is not None and generation != self._acq_generation:
                    break

                now = time.monotonic()
                if exact or (now - self._last_measure_update.get(ch, 0.0) >= 0.20):
                    measurements = waveform.measurements()
                    self._last_measure_update[ch] = now
                else:
                    measurements = None

                with self.frame_lock:
                    self.latest_frames[ch] = (
                        waveform.time_s,
                        waveform.volts,
                        measurements,
                        info,
                    )

                self.channel_errors.pop(ch, None)
                successful += 1

            except Exception as exc:
                self.channel_errors[ch] = str(exc)

        return successful

    def _stop_acquisition(self, local_only: bool = False) -> None:
        self._acq_generation += 1
        self.stop_event.set()
        self.run_btn.configure(text="RUN", style="Primary.TButton")
        self.rate_var.set("Acq: -- fps")

        if not local_only and self.client and self.client.connected:
            def worker() -> None:
                try:
                    self.client.stop_acquisition()
                except Exception:
                    pass

                try:
                    restored = self.client.exit_realtime_mode()
                    self.command_queue.put(
                        ("realtime_restored", restored)
                    )
                except Exception:
                    pass

            threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------------
    # Efficient rendering
    # ------------------------------------------------------------------

    def _render_latest_frames(self) -> None:
        if self._closing:
            return

        frames: dict[str, tuple[np.ndarray, np.ndarray, dict, dict]] = {}
        with self.frame_lock:
            if self.latest_frames:
                frames = self.latest_frames.copy()
                self.latest_frames.clear()

        if frames:
            latest_status = None
            selected_channels = {
                ch for ch, var in self.channel_vars.items() if bool(var.get())
            }

            for ch, (x, y, measurements, info) in frames.items():
                if ch not in selected_channels:
                    continue

                self.waveforms[ch] = (x, y)
                if measurements is not None:
                    self.measurements[ch] = measurements
                    self._update_measurements(ch, measurements)

                mode = info.get("mode", "?")
                npts = info.get("points", len(y))
                record = info.get("record_length")
                t0 = info.get("time_start")
                t1 = info.get("time_stop")
                range_text = ""
                if t0 is not None and t1 is not None:
                    range_text = f" | {self._format_time(float(t0))} .. {self._format_time(float(t1))}"
                resample = info.get("resample")
                latest_status = (
                    f"{ch}: {npts} pts {mode}"
                    + (f" | record {record}" if record else "")
                    + (f" | resample x{resample}" if resample else "")
                    + range_text
                )

            now = time.monotonic()
            if now - self._last_status_update >= 0.50:
                if latest_status:
                    self.transfer_var.set(latest_status)

                # Validate time span at low rate; this does not need per-frame work.
                try:
                    visible_span = 10.0 * self._parse_time_div(
                        self.time_scale_var.get()
                    )
                except Exception:
                    visible_span = 0.0

                spans = []
                for _ch, (_x, _y, _m, _info) in frames.items():
                    _t0 = _info.get("time_start")
                    _t1 = _info.get("time_stop")
                    if _t0 is not None and _t1 is not None:
                        spans.append(abs(float(_t1) - float(_t0)))

                if visible_span > 0.0 and spans:
                    max_span = max(spans)
                    if max_span >= visible_span * 0.98:
                        self.scope_sync_var.set("SCALE SYNC")
                        if hasattr(self, "scope_sync_label"):
                            self.scope_sync_label.configure(
                                bg="#142019", fg="#72D99A"
                            )
                    else:
                        self.scope_sync_var.set("TIME SPAN !")
                        if hasattr(self, "scope_sync_label"):
                            self.scope_sync_label.configure(
                                bg="#35181B", fg="#FF7680"
                            )
                self._last_status_update = now
            # Tk Canvas is most responsive when draw work stays below acquisition rate.
            if now - self._last_draw >= 1.0 / 30.0:
                if self._need_autoscale:
                    self._autoscale()
                    self._need_autoscale = False
                else:
                    self._redraw_scope()
                self._last_draw = now

        self.root.after(30, self._render_latest_frames)

    def _autoscale(self) -> None:
        """Return to the real oscilloscope scale instead of arbitrary local fitting."""
        self.display_offset_div = {ch: 0.0 for ch in CHANNEL_COLORS}
        if self.client and self.client.connected:
            self._refresh_settings()
        self.status_var.set("SCOPE VIEW: 10 horizontal div × 8 vertical div")
        self._redraw_scope()

    @staticmethod
    def _safe_float(value: str, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _scope_geometry(self, width: int, height: int) -> dict[str, float]:
        # Extra margins hold exact numeric Time/div and selected-channel V/div rulers.
        left = 68.0
        right = max(left + 100.0, float(width) - 10.0)
        top = 12.0
        bottom = max(top + 80.0, float(height) - 30.0)
        plot_w = right - left
        plot_h = bottom - top
        return {
            "left": left,
            "right": right,
            "top": top,
            "bottom": bottom,
            "width": plot_w,
            "height": plot_h,
            "x_div": plot_w / 10.0,
            "y_div": plot_h / 8.0,
            "center_y": top + plot_h / 2.0,
        }

    def _redraw_scope(self) -> None:
        if not hasattr(self, "canvas"):
            return

        c = self.canvas
        width = max(20, c.winfo_width())
        height = max(20, c.winfo_height())
        g = self._scope_geometry(width, height)

        # Redraw grid/rulers only when geometry or scope scale changes.
        static_key = (
            width,
            height,
            self.time_scale_var.get(),
            self.horizontal_position_var.get(),
            self._selected_channel,
            self.channel_scale_vars[self._selected_channel].get(),
            self.channel_position_vars[self._selected_channel].get(),
            self.channel_offset_vars[self._selected_channel].get(),
        )
        static_changed = static_key != self._scope_static_key

        if static_changed:
            c.delete("all")
            self._wave_items.clear()
            self._draw_grid(width, height, g)
            self._draw_scale_rulers(g)
            c.addtag_all("scope_static")
            self._scope_static_key = static_key
        else:
            c.delete("scope_overlay")

        active = [
            ch
            for ch in CHANNEL_COLORS
            if self.channel_vars[ch].get() and ch in self.waveforms
        ]

        for ch, item_id in list(self._wave_items.items()):
            if ch not in active:
                try:
                    c.itemconfigure(item_id, state="hidden")
                except tk.TclError:
                    self._wave_items.pop(ch, None)

        try:
            time_div = self._parse_time_div(self.time_scale_var.get())
        except Exception:
            time_div = 1e-3
        time_div = max(time_div, 1e-15)

        hpos = self._safe_float(self.horizontal_position_var.get(), 50.0)
        hpos = max(0.0, min(100.0, hpos))
        trigger_x = g["left"] + (hpos / 100.0) * g["width"]

        # Trigger-time reference, exactly matching HORIZONTAL:POSITION semantics.
        c.create_line(
            trigger_x,
            g["top"],
            trigger_x,
            g["bottom"],
            fill="#C88A2D",
            width=1,
            dash=(3, 4),
            tags=("scope_overlay",),
        )
        c.create_polygon(
            trigger_x - 6,
            g["top"],
            trigger_x + 6,
            g["top"],
            trigger_x,
            g["top"] + 8,
            fill="#F1A93A",
            outline="",
            tags=("scope_overlay",),
        )

        if not active:
            message = (
                "CONNECTED • press RUN or GET DATA"
                if self.client and self.client.connected
                else "CONNECT TO MSO44B"
            )
            c.create_text(
                width / 2,
                height / 2,
                text=message,
                fill="#566778",
                font=("Arial", 13, "bold"),
                tags=("scope_overlay",),
            )
            self._update_scope_badges()
            return

        max_draw_points = max(400, min(1800, int(g["width"] * 1.25)))

        for ch in active:
            x, y = self.waveforms[ch]
            if len(x) < 2:
                continue

            if len(x) > max_draw_points:
                idx = np.linspace(
                    0,
                    len(x) - 1,
                    max_draw_points,
                    dtype=np.int64,
                )
                xd = x[idx]
                yd = y[idx]
            else:
                xd = x
                yd = y

            try:
                volts_div = self._parse_vdiv(self.channel_scale_vars[ch].get())
            except Exception:
                volts_div = 1.0
            volts_div = max(abs(volts_div), 1e-15)

            position_div = self._safe_float(
                self.channel_position_vars[ch].get(),
                0.0,
            )
            offset_v = self._safe_float(
                self.channel_offset_vars[ch].get(),
                0.0,
            )

            # Tek-style graticule:
            # 1 horizontal box = time_div seconds.
            # 1 vertical box = channel volts_div volts.
            xp = trigger_x + (xd / time_div) * g["x_div"]
            screen_div = ((yd - offset_v) / volts_div) + position_div
            yp = g["center_y"] - screen_div * g["y_div"]

            coords = np.column_stack((xp, yp)).ravel().tolist()
            item_id = self._wave_items.get(ch)
            if item_id is None:
                item_id = c.create_line(
                    *coords,
                    fill=CHANNEL_COLORS[ch],
                    width=(2.4 if ch == self._selected_channel else 1.7),
                    smooth=False,
                    tags=("wave_trace",),
                )
                self._wave_items[ch] = item_id
            else:
                c.coords(item_id, *coords)
                c.itemconfigure(
                    item_id,
                    state="normal",
                    fill=CHANNEL_COLORS[ch],
                    width=(2.4 if ch == self._selected_channel else 1.7),
                )

            self._draw_channel_reference_marker(
                ch,
                volts_div,
                position_div,
                offset_v,
                g,
            )

        self._draw_trigger_level(g)
        self._update_scope_badges()

    def _draw_grid(
        self,
        width: int,
        height: int,
        g: dict[str, float],
    ) -> None:
        c = self.canvas

        c.create_rectangle(
            g["left"],
            g["top"],
            g["right"],
            g["bottom"],
            outline="#40505E",
            width=1,
        )

        # Tektronix-like 10 × 8 major graticule boxes.
        for i in range(11):
            x = g["left"] + i * g["x_div"]
            major = i in {0, 5, 10}
            c.create_line(
                x,
                g["top"],
                x,
                g["bottom"],
                fill=("#334454" if major else "#192630"),
                width=(1.2 if major else 1),
            )

        for i in range(9):
            y = g["top"] + i * g["y_div"]
            major = i in {0, 4, 8}
            c.create_line(
                g["left"],
                y,
                g["right"],
                y,
                fill=("#334454" if major else "#192630"),
                width=(1.2 if major else 1),
            )

        # Minor center-axis ticks make each box easier to read without clutter.
        center_x = g["left"] + 5 * g["x_div"]
        center_y = g["top"] + 4 * g["y_div"]
        for i in range(1, 40):
            if i % 5 == 0:
                continue
            y = g["top"] + i * (g["y_div"] / 5.0)
            c.create_line(center_x - 2, y, center_x + 2, y, fill="#40505E")
        for i in range(1, 50):
            if i % 5 == 0:
                continue
            x = g["left"] + i * (g["x_div"] / 5.0)
            c.create_line(x, center_y - 2, x, center_y + 2, fill="#40505E")

        c.create_text(
            g["right"] - 5,
            g["bottom"] + 10,
            text="10 DIV × 8 DIV",
            fill="#526474",
            anchor="e",
            font=("Menlo", 7),
        )

    def _draw_scale_rulers(self, g: dict[str, float]) -> None:
        """Render exact major-division rulers from current MSO44B settings."""
        try:
            time_div = self._parse_time_div(self.time_scale_var.get())
        except Exception:
            time_div = 1e-3
        time_div = max(abs(time_div), 1e-15)

        hpos = self._safe_float(self.horizontal_position_var.get(), 50.0)
        hpos = max(0.0, min(100.0, hpos))
        trigger_x = g["left"] + (hpos / 100.0) * g["width"]

        # Every vertical major line is exactly one Time/div.
        for i in range(11):
            x = g["left"] + i * g["x_div"]
            t = ((x - trigger_x) / g["x_div"]) * time_div
            is_zero = abs(t) <= time_div * 0.02
            self.canvas.create_text(
                x,
                g["bottom"] + 11,
                text=self._format_time(t),
                fill=("#E4EAF0" if is_zero else "#6F8192"),
                anchor="n",
                font=("Menlo", 7, "bold" if is_zero else "normal"),
            )

        ch = self._selected_channel
        try:
            volts_div = self._parse_vdiv(self.channel_scale_vars[ch].get())
        except Exception:
            volts_div = 1.0
        volts_div = max(abs(volts_div), 1e-15)

        position = self._safe_float(self.channel_position_vars[ch].get(), 0.0)
        offset_v = self._safe_float(self.channel_offset_vars[ch].get(), 0.0)
        color = CHANNEL_COLORS[ch]

        # Every horizontal major line is exactly one V/div for selected channel.
        for i in range(9):
            y = g["top"] + i * g["y_div"]
            screen_div = (g["center_y"] - y) / g["y_div"]
            volts = (screen_div - position) * volts_div + offset_v
            self.canvas.create_text(
                g["left"] - 7,
                y,
                text=self._format_voltage(volts),
                fill=color,
                anchor="e",
                font=("Menlo", 7),
            )

        self.canvas.create_text(
            6,
            g["top"],
            text=f"{ch}\n{self.channel_scale_vars[ch].get()}",
            fill=color,
            anchor="nw",
            justify="left",
            font=("Menlo", 8, "bold"),
        )

    def _draw_channel_reference_marker(
        self,
        ch: str,
        volts_div: float,
        position_div: float,
        offset_v: float,
        g: dict[str, float],
    ) -> None:
        # Marker represents 0 V on the active channel's own V/div scale.
        zero_div = ((0.0 - offset_v) / volts_div) + position_div
        py = g["center_y"] - zero_div * g["y_div"]
        if py < g["top"] - 10 or py > g["bottom"] + 10:
            return

        color = CHANNEL_COLORS[ch]
        self.canvas.create_polygon(
            g["left"],
            py,
            g["left"] + 11,
            py - 7,
            g["left"] + 11,
            py + 7,
            fill=color,
            outline="",
            tags=("scope_overlay",),
        )
        self.canvas.create_text(
            g["left"] + 14,
            py,
            text=(f"{ch}*" if ch == self._selected_channel else ch),
            fill=color,
            anchor="w",
            font=("Arial", 7, "bold"),
            tags=("scope_overlay",),
        )

    def _draw_trigger_level(self, g: dict[str, float]) -> None:
        source = self.trigger_source_var.get().strip().upper()
        if source not in CHANNEL_COLORS or not self.channel_vars[source].get():
            return

        try:
            scale = self._parse_vdiv(self.channel_scale_vars[source].get())
        except Exception:
            scale = 1.0
        scale = max(abs(scale), 1e-15)

        position = self._safe_float(
            self.channel_position_vars[source].get(),
            0.0,
        )
        offset_v = self._safe_float(
            self.channel_offset_vars[source].get(),
            0.0,
        )
        level = self._safe_float(self.trigger_level_var.get(), 0.0)

        trigger_div = ((level - offset_v) / scale) + position
        py = g["center_y"] - trigger_div * g["y_div"]
        if py < g["top"] or py > g["bottom"]:
            return

        self.canvas.create_line(
            g["left"],
            py,
            g["right"],
            py,
            fill="#7A5825",
            width=1,
            dash=(2, 5),
            tags=("scope_overlay",),
        )
        self.canvas.create_polygon(
            g["right"],
            py,
            g["right"] - 10,
            py - 6,
            g["right"] - 10,
            py + 6,
            fill="#F1A93A",
            outline="",
            tags=("scope_overlay",),
        )
        self.canvas.create_text(
            g["right"] - 13,
            py,
            text="T",
            fill="#F1A93A",
            anchor="e",
            font=("Arial", 7, "bold"),
            tags=("scope_overlay",),
        )

    def _update_scope_badges(self) -> None:
        for ch, color in CHANNEL_COLORS.items():
            scale_text = self.channel_scale_vars[ch].get()
            position = self.channel_position_vars[ch].get()
            self.channel_badge_vars[ch].set(
                f"{ch}  {scale_text}  P{position}"
            )

            label = self.channel_badge_labels.get(ch)
            if label is not None:
                enabled = self.channel_vars[ch].get()
                selected = ch == self._selected_channel
                label.configure(
                    fg=(color if enabled else "#52606D"),
                    bg=("#1A2632" if selected else "#111922"),
                    relief=("solid" if selected else "flat"),
                    bd=(1 if selected else 0),
                )

        self.time_badge_var.set(
            f"M  {self.time_scale_var.get()}  •  10 div"
        )
        source = self.trigger_source_var.get().strip().upper()
        level = self.trigger_level_var.get()
        position = self.horizontal_position_var.get()
        self.trigger_badge_var.set(
            f"T  {source}  {level} V  @ {position}%"
        )

    def _on_mouse_move(self, event) -> None:
        width = max(20, self.canvas.winfo_width())
        height = max(20, self.canvas.winfo_height())
        g = self._scope_geometry(width, height)

        try:
            time_div = self._parse_time_div(self.time_scale_var.get())
        except Exception:
            time_div = 1e-3

        hpos = self._safe_float(self.horizontal_position_var.get(), 50.0)
        hpos = max(0.0, min(100.0, hpos))
        trigger_x = g["left"] + (hpos / 100.0) * g["width"]
        t = ((event.x - trigger_x) / g["x_div"]) * time_div

        ch = self._selected_channel
        try:
            volts_div = self._parse_vdiv(self.channel_scale_vars[ch].get())
        except Exception:
            volts_div = 1.0
        position = self._safe_float(self.channel_position_vars[ch].get(), 0.0)
        offset_v = self._safe_float(self.channel_offset_vars[ch].get(), 0.0)

        screen_div = (g["center_y"] - event.y) / g["y_div"]
        v = (screen_div - position) * volts_div + offset_v

        self.cursor_var.set(
            f"{ch}  t={self._format_time(t)}   V={self._format_voltage(v)}"
        )

    # ------------------------------------------------------------------
    # Queue / settings sync
    # ------------------------------------------------------------------

    def _process_command_queue(self) -> None:
        if self._closing:
            return

        try:
            while True:
                msg = self.command_queue.get_nowait()
                kind = msg[0]

                if kind == "connected":
                    _, client, hsi, idn, settings = msg
                    self.client = client
                    self.hsi_client = hsi
                    self.instrument_idn = str(idn)
                    self.connect_btn.configure(text="DISCONNECT", state="normal")
                    self.run_btn.configure(state="normal")
                    self.single_btn.configure(state="normal")
                    self.get_data_btn.configure(state="normal")
                    self.autoset_btn.configure(state="normal")
                    self.default_btn.configure(state="normal")
                    self.scpi_btn.configure(state="normal")
                    self.ip_entry.configure(state="disabled")
                    hsi_ready = TekHSIWaveformClient.available()
                    self.waveform_transport = "TekHSI" if hsi_ready else "SCPI"
                    self.status_var.set(
                        f"Connected: {idn} | waveform: {self.waveform_transport}"
                    )
                    self.connection_badge_var.set("● ONLINE")
                    self.connection_badge.configure(bg="#10271C", fg="#6DDB9E")
                    self.scpi_result_var.set(idn)
                    self._apply_settings_to_ui(settings)
                    self._redraw_scope()

                elif kind == "registry_changed":
                    _, name, error = msg
                    self._refresh_device_selector()
                    if error:
                        self.status_var.set(f"{name}: connection error")
                        messagebox.showerror("Connection", error)
                    else:
                        self.active_device_var.set(name)
                        self._active_device_changed()

                elif kind == "connect_error":
                    self.connect_btn.configure(state="normal")
                    self.status_var.set("Connection failed")
                    messagebox.showerror("MSO4 connection failed", msg[1])

                elif kind == "command_ok":
                    _, description, _result, settings = msg
                    self.status_var.set(f"OK: {description}")
                    if settings:
                        self._apply_settings_to_ui(settings)

                elif kind == "command_error":
                    _, description, error = msg
                    self.status_var.set(f"Error: {description}")
                    messagebox.showwarning(description, error)

                elif kind == "channel_selection_applied":
                    _, active, resume = msg
                    self.status_var.set(
                        "Channels: " + (", ".join(active) if active else "none")
                    )
                    if resume and active and self.client and self.client.connected:
                        self.root.after(40, self._start_acquisition)
                    else:
                        self._redraw_scope()

                elif kind == "stack4_ready":
                    _, resume = msg
                    self.status_var.set("CH1–CH4 configured")
                    if resume and self.client and self.client.connected:
                        self.root.after(40, self._start_acquisition)
                    else:
                        self._redraw_scope()

                elif kind == "realtime_mode":
                    info = msg[1] or {}
                    record = info.get("record_length")
                    short_ok = bool(info.get("short_record_applied", True))
                    self.time_scale_var.set(
                        self._format_time_div(self._user_time_div_s)
                    )
                    if short_ok:
                        self.transfer_var.set(
                            f"Realtime record {record or '?'} pts"
                        )
                    else:
                        self.transfer_var.set(
                            f"Time/div locked at "
                            f"{self._format_time_div(self._user_time_div_s)}"
                        )

                elif kind == "realtime_restored":
                    self.time_scale_var.set(
                        self._format_time_div(self._user_time_div_s)
                    )
                    self._redraw_scope()

                elif kind == "transport":
                    self.transfer_var.set(
                        f"Waveform transport: {msg[1]} :5000"
                    )
                    self.status_var.set(
                        f"RUN started | waveform: {msg[1]}"
                    )

                elif kind == "hsi_fallback":
                    self.transfer_var.set(
                        "TekHSI unavailable; using SCPI fallback"
                    )
                    self.status_var.set(
                        f"TekHSI fallback: {msg[1]}"
                    )

                elif kind == "prepared":
                    record = msg[1]
                    if record:
                        self.status_var.set(f"RUN | record length {record}")
                    else:
                        self.status_var.set("RUN started")

                elif kind == "first_waveform":
                    self.status_var.set(f"RUN | {msg[1]} waveform received")

                elif kind == "rate":
                    self.rate_var.set(f"Acq: {msg[1]:.1f} fps")

                elif kind == "single_done":
                    self.status_var.set("Single acquisition complete")
                    self.run_btn.configure(text="RUN")

                elif kind == "get_data_done":
                    _, resume, points, elapsed = msg
                    self.status_var.set(
                        f"GET DATA complete: {points} pts in {elapsed:.3f}s"
                    )
                    self._need_autoscale = True
                    if resume and self.client and self.client.connected:
                        self.root.after(80, self._start_acquisition)

                elif kind == "csv_saved":
                    _, filename, channels, points, resume = msg
                    self.status_var.set(
                        f"Saved exact CSV: {filename}"
                    )
                    self.transfer_var.set(
                        f"CSV exact | {','.join(channels)} | "
                        f"up to {points} pts/channel"
                    )
                    if resume and self.client and self.client.connected:
                        self.root.after(80, self._start_acquisition)

                elif kind == "csv_save_error":
                    _, error, resume = msg
                    self.status_var.set("CSV save failed")
                    messagebox.showerror("Save CSV", error)
                    if resume and self.client and self.client.connected:
                        self.root.after(80, self._start_acquisition)

                elif kind == "acq_error":
                    self._stop_acquisition(local_only=True)
                    self.status_var.set("Acquisition error")
                    messagebox.showwarning("Acquisition stopped", msg[1])

                elif kind == "scpi_result":
                    self.scpi_result_var.set(str(msg[1]))
                    if self.client and self.client.connected:
                        self.scpi_btn.configure(state="normal")

        except queue.Empty:
            pass

        self.root.after(30, self._process_command_queue)

    def _apply_settings_to_ui(self, settings: dict[str, object]) -> None:
        if not settings:
            return

        if "horizontal_scale" in settings:
            actual_time_div = float(settings["horizontal_scale"])
            running = bool(self.acq_thread and self.acq_thread.is_alive())
            if not running:
                self._user_time_div_s = actual_time_div
                self.time_scale_var.set(
                    self._format_time_div(actual_time_div)
                )
            else:
                self.time_scale_var.set(
                    self._format_time_div(self._user_time_div_s)
                )

        if "horizontal_position" in settings:
            self.horizontal_position_var.set(
                f"{float(settings['horizontal_position']):.6g}"
            )

        if "trigger_source" in settings:
            source = str(settings["trigger_source"]).upper()
            if source in CHANNEL_COLORS:
                self.trigger_source_var.set(source)

        if "trigger_level" in settings:
            self.trigger_level_var.set(
                f"{float(settings['trigger_level']):.6g}"
            )

        if "trigger_slope" in settings:
            slope = str(settings["trigger_slope"]).upper()
            if slope in {"RISE", "FALL", "EITHER"}:
                self.trigger_slope_var.set(slope)

        if "trigger_mode" in settings:
            mode = str(settings["trigger_mode"]).upper()
            if mode.startswith("NORM"):
                mode = "NORMAL"
            if mode in {"AUTO", "NORMAL"}:
                self.trigger_mode_var.set(mode)

        if "acquire_mode" in settings:
            mode = str(settings["acquire_mode"]).upper()
            aliases = {
                "PEAK": "PEAKDETECT",
                "PEAKDETECT": "PEAKDETECT",
                "HIRES": "HIRES",
                "AVERAGE": "AVERAGE",
                "ENVELOPE": "ENVELOPE",
                "SAMPLE": "SAMPLE",
            }
            mode = aliases.get(mode, mode)
            if mode in {"SAMPLE", "PEAKDETECT", "HIRES", "AVERAGE", "ENVELOPE"}:
                self.acquire_mode_var.set(mode)

        channels = settings.get("channels", {})
        if isinstance(channels, dict):
            for ch, values in channels.items():
                if ch not in CHANNEL_COLORS or not isinstance(values, dict):
                    continue

                if "enabled" in values:
                    self.channel_vars[ch].set(bool(values["enabled"]))

                if "scale" in values:
                    self.channel_scale_vars[ch].set(
                        self._format_vdiv(float(values["scale"]))
                    )

                if "position" in values:
                    self.channel_position_vars[ch].set(
                        f"{float(values['position']):.6g}"
                    )

                if "offset" in values:
                    self.channel_offset_vars[ch].set(
                        f"{float(values['offset']):.6g}"
                    )

                if "coupling" in values:
                    coupling = str(values["coupling"]).upper()
                    if coupling in {"DC", "AC"}:
                        self.channel_coupling_vars[ch].set(coupling)

        self.scope_sync_var.set("SCALE SYNC")
        if hasattr(self, "scope_sync_label"):
            self.scope_sync_label.configure(bg="#142019", fg="#72D99A")

        if hasattr(self, "canvas"):
            self._redraw_scope()

    def _select_channel(self, channel: str) -> None:
        if channel in CHANNEL_COLORS:
            self._selected_channel = channel
            for ch, card in self.channel_cards.items():
                card.configure(
                    highlightbackground=(
                        CHANNEL_COLORS[ch] if ch == channel else "#2B3B4B"
                    ),
                    highlightthickness=(2 if ch == channel else 1),
                )
            self.status_var.set(
                f"Selected {channel} | display {self.display_offset_div.get(channel, 0.0):+.2f} div"
            )
            self._redraw_scope()

    def _stack_four_channels(self) -> None:
        positions = {
            "CH1": 3.0,
            "CH2": 1.0,
            "CH3": -1.0,
            "CH4": -3.0,
        }

        for ch, pos in positions.items():
            self.channel_vars[ch].set(True)
            self.channel_position_vars[ch].set(f"{pos:g}")

        was_running = bool(self.acq_thread and self.acq_thread.is_alive())
        if was_running:
            self._stop_acquisition(local_only=True)

        # Remove any old traces so all four lanes wait for fresh data.
        with self.frame_lock:
            self.latest_frames.clear()
        self.waveforms.clear()
        self._redraw_scope()

        if not self.client or not self.client.connected:
            return

        self.status_var.set("Configuring CH1–CH4...")

        def worker() -> None:
            try:
                # Configure state + vertical positions in one serialized worker.
                for ch, pos in positions.items():
                    self.client.set_channel_state(ch, True)
                    self.client.set_channel_position(ch, pos)

                self.command_queue.put(("stack4_ready", was_running))
            except Exception as exc:
                self.command_queue.put(
                    ("command_error", "4CH STACK", str(exc))
                )

        threading.Thread(target=worker, daemon=True).start()

    def _center_selected_channel(self) -> None:
        ch = self._selected_channel
        self.channel_position_vars[ch].set("0")
        self._redraw_scope()

        if self.client and self.client.connected:
            self._run_async(
                f"{ch} position 0 div",
                lambda: self.client.set_channel_position(ch, 0.0),
                refresh_settings=True,
            )

    @staticmethod
    def _wheel_direction(event) -> int:
        if getattr(event, "num", None) == 4:
            return 1
        if getattr(event, "num", None) == 5:
            return -1
        delta = getattr(event, "delta", 0)
        return 1 if delta > 0 else (-1 if delta < 0 else 0)

    def _scroll_panel(self, canvas: tk.Canvas, event) -> str:
        direction = self._wheel_direction(event)
        if direction:
            canvas.yview_scroll(-direction, "units")
        return "break"

    def _on_scope_wheel(self, event) -> str:
        direction = self._wheel_direction(event)
        if direction == 0:
            return "break"

        state = int(getattr(event, "state", 0))
        shift = bool(state & 0x0001)
        time_modifier = bool(state & (0x0004 | 0x0008 | 0x0010 | 0x0040))
        ch = self._selected_channel

        if shift:
            try:
                current = self._parse_vdiv(self.channel_scale_vars[ch].get())
            except Exception:
                current = 1.0

            idx = min(
                range(len(VERTICAL_SCALES)),
                key=lambda i: abs(VERTICAL_SCALES[i] - current),
            )
            idx = max(0, min(len(VERTICAL_SCALES) - 1, idx - direction))
            new_value = VERTICAL_SCALES[idx]
            self.channel_scale_vars[ch].set(self._format_vdiv(new_value))
            self._redraw_scope()

            if self.client and self.client.connected:
                self._schedule_wheel_apply("vertical", ch, new_value)

        elif time_modifier:
            try:
                current = self._parse_time_div(self.time_scale_var.get())
            except Exception:
                current = 1e-3

            idx = min(
                range(len(TIME_SCALES)),
                key=lambda i: abs(TIME_SCALES[i] - current),
            )
            idx = max(0, min(len(TIME_SCALES) - 1, idx - direction))
            new_value = TIME_SCALES[idx]
            self.time_scale_var.set(self._format_time_div(new_value))
            self._redraw_scope()

            if self.client and self.client.connected:
                self._schedule_wheel_apply("horizontal", None, new_value)

        else:
            current = self._safe_float(
                self.channel_position_vars[ch].get(),
                0.0,
            )
            new_position = max(-5.0, min(5.0, current + direction * 0.25))
            self.channel_position_vars[ch].set(f"{new_position:.2f}")
            self._redraw_scope()

            if self.client and self.client.connected:
                self._schedule_wheel_apply("position", ch, new_position)

        return "break"

    def _schedule_wheel_apply(
        self,
        kind: str,
        channel: str | None,
        value: float,
    ) -> None:
        self.scope_sync_var.set("SCALE PENDING")
        if hasattr(self, "scope_sync_label"):
            self.scope_sync_label.configure(bg="#2A2111", fg="#F6B94A")

        if self._wheel_after_id is not None:
            try:
                self.root.after_cancel(self._wheel_after_id)
            except Exception:
                pass

        def apply() -> None:
            self._wheel_after_id = None
            if not self.client or not self.client.connected:
                return
            if kind == "vertical" and channel:
                self._run_async(
                    f"{channel} V/div {self._format_vdiv(value)}",
                    lambda: self.client.set_channel_scale(channel, value),
                    refresh_settings=True,
                )
            elif kind == "horizontal":
                self._user_time_div_s = value
                self._run_async(
                    f"Time/div {self._format_time_div(value)}",
                    lambda: self.client.set_horizontal_scale(value),
                    refresh_settings=True,
                )
            elif kind == "position" and channel:
                self._run_async(
                    f"{channel} position {value:+.2f} div",
                    lambda: self.client.set_channel_position(channel, value),
                    refresh_settings=True,
                )

        self._wheel_after_id = self.root.after(120, apply)

    # ------------------------------------------------------------------
    # Save / export
    # ------------------------------------------------------------------

    def _save_csv(self) -> None:
        """Capture and save an exact scope snapshot with verified metadata.

        Realtime display data may be decimated for speed. SAVE CSV therefore
        never writes self.waveforms directly. It performs a fresh exact
        16-bit SCPI transfer after restoring the normal acquisition record.
        """
        if not self.client or not self.client.connected:
            messagebox.showinfo("Save CSV", "Connect the oscilloscope first.")
            return

        channels = [
            ch
            for ch in CHANNEL_COLORS
            if bool(self.channel_vars[ch].get())
        ]
        if not channels:
            messagebox.showinfo(
                "Save CSV",
                "Select at least one channel to save.",
            )
            return

        path = filedialog.asksaveasfilename(
            title="Save exact waveform CSV",
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv")],
            initialfile=(
                "mso44b_"
                + datetime.now().strftime("%Y%m%d_%H%M%S")
                + ".csv"
            ),
        )
        if not path:
            return

        try:
            requested_points = max(500, int(self.points_var.get()))
        except ValueError:
            requested_points = 5000

        save_full_record = bool(self.get_full_record_var.get())
        was_running = bool(
            self.acq_thread and self.acq_thread.is_alive()
        )

        # Invalidate the realtime reader immediately so no stale/decimated
        # frames can be mistaken for the saved snapshot.
        if was_running:
            self._stop_acquisition(local_only=True)

        self.status_var.set("SAVE CSV: capturing exact waveform...")
        self.transfer_var.set("Exact 16-bit snapshot for CSV...")

        def worker() -> None:
            try:
                # Stop the scope and detach HSI while taking a deterministic
                # exact snapshot through the SCPI waveform path.
                try:
                    self.client.stop_acquisition()
                except Exception:
                    pass

                if self.hsi_client is not None:
                    try:
                        self.hsi_client.close()
                    except Exception:
                        pass

                try:
                    self.client.exit_realtime_mode()
                except Exception:
                    pass

                settings = self.client.get_scope_settings()

                points = requested_points
                record_length = self.client.get_record_length(force=True)
                if save_full_record and record_length and record_length > 0:
                    points = int(record_length)

                captured_at = datetime.now().astimezone().isoformat(
                    timespec="milliseconds"
                )

                waveforms = {}
                transfer_info = {}
                measurements = {}

                for ch in channels:
                    waveform = self.client.get_waveform(
                        ch,
                        1,
                        points,
                    )
                    waveforms[ch] = waveform
                    transfer_info[ch] = (
                        self.client.get_last_transfer_info(ch)
                    )
                    measurements[ch] = waveform.measurements()

                idn = self.instrument_idn
                if not idn:
                    try:
                        idn = self.client.query("*IDN?")
                    except Exception:
                        idn = ""

                resource = (
                    self.client.active_resource_name
                    or self.client.resource_name
                )
                control_transport = (
                    self.client.active_transport or "VISA/SCPI"
                )

                with open(
                    path,
                    "w",
                    newline="",
                    encoding="utf-8",
                ) as fp:
                    writer = csv.writer(fp)

                    # General metadata.
                    writer.writerow(["[GENERAL]"])
                    writer.writerow(["field", "value", "unit"])
                    writer.writerow(
                        ["captured_at", captured_at, "ISO-8601"]
                    )
                    writer.writerow(
                        ["instrument_idn", idn, ""]
                    )
                    writer.writerow(
                        ["visa_resource", resource, ""]
                    )
                    writer.writerow(
                        ["control_transport", control_transport, ""]
                    )
                    writer.writerow(
                        ["waveform_capture", "SCPI exact binary", ""]
                    )
                    writer.writerow(
                        ["binary_width", "16", "bit"]
                    )
                    writer.writerow(
                        [
                            "requested_full_record",
                            int(save_full_record),
                            "bool",
                        ]
                    )
                    writer.writerow(
                        [
                            "record_length",
                            record_length or "",
                            "points",
                        ]
                    )

                    hscale = settings.get("horizontal_scale")
                    hpos = settings.get("horizontal_position")
                    writer.writerow(
                        [
                            "time_div",
                            hscale if hscale is not None else "",
                            "s/div",
                        ]
                    )
                    writer.writerow(
                        [
                            "horizontal_position",
                            hpos if hpos is not None else "",
                            "%",
                        ]
                    )
                    writer.writerow(
                        [
                            "trigger_source",
                            settings.get("trigger_source", ""),
                            "",
                        ]
                    )
                    writer.writerow(
                        [
                            "trigger_level",
                            settings.get("trigger_level", ""),
                            "V",
                        ]
                    )
                    writer.writerow(
                        [
                            "trigger_slope",
                            settings.get("trigger_slope", ""),
                            "",
                        ]
                    )
                    writer.writerow(
                        [
                            "trigger_mode",
                            settings.get("trigger_mode", ""),
                            "",
                        ]
                    )
                    writer.writerow(
                        [
                            "acquire_mode",
                            settings.get("acquire_mode", ""),
                            "",
                        ]
                    )
                    writer.writerow([])

                    # Per-channel metadata and measurements.
                    writer.writerow(["[CHANNEL_METADATA]"])
                    writer.writerow(
                        [
                            "channel",
                            "points",
                            "v_div_V",
                            "position_div",
                            "offset_V",
                            "coupling",
                            "sample_interval_s",
                            "time_start_s",
                            "time_stop_s",
                            "v_min_V",
                            "v_max_V",
                            "v_pp_V",
                            "v_rms_V",
                            "v_mean_V",
                            "frequency_Hz",
                        ]
                    )

                    channel_settings = settings.get("channels", {})
                    if not isinstance(channel_settings, dict):
                        channel_settings = {}

                    for ch in channels:
                        wf = waveforms[ch]
                        info = transfer_info[ch]
                        meas = measurements[ch]
                        ch_settings = channel_settings.get(ch, {})
                        if not isinstance(ch_settings, dict):
                            ch_settings = {}

                        dt = info.get("xincr")
                        if dt is None and len(wf.time_s) > 1:
                            dt = float(
                                wf.time_s[1] - wf.time_s[0]
                            )

                        writer.writerow(
                            [
                                ch,
                                wf.points,
                                ch_settings.get("scale", ""),
                                ch_settings.get("position", ""),
                                ch_settings.get("offset", ""),
                                ch_settings.get("coupling", ""),
                                dt if dt is not None else "",
                                (
                                    float(wf.time_s[0])
                                    if wf.points else ""
                                ),
                                (
                                    float(wf.time_s[-1])
                                    if wf.points else ""
                                ),
                                meas.get("min", ""),
                                meas.get("max", ""),
                                meas.get("pkpk", ""),
                                meas.get("rms", ""),
                                meas.get("mean", ""),
                                meas.get("frequency", ""),
                            ]
                        )

                    writer.writerow([])

                    # Raw waveform values. One Time/Voltage pair per channel.
                    writer.writerow(["[WAVEFORM_DATA]"])
                    header = []
                    for ch in channels:
                        header.extend(
                            [
                                f"{ch}_time_s",
                                f"{ch}_voltage_V",
                            ]
                        )
                    writer.writerow(header)

                    max_len = max(
                        waveforms[ch].points for ch in channels
                    )

                    for i in range(max_len):
                        row = []
                        for ch in channels:
                            wf = waveforms[ch]
                            if i < wf.points:
                                # repr(float) preserves enough precision for
                                # round-trip numerical reconstruction.
                                row.extend(
                                    [
                                        repr(float(wf.time_s[i])),
                                        repr(float(wf.volts[i])),
                                    ]
                                )
                            else:
                                row.extend(["", ""])
                        writer.writerow(row)

                self.command_queue.put(
                    (
                        "csv_saved",
                        Path(path).name,
                        channels,
                        points,
                        was_running,
                    )
                )

            except Exception as exc:
                self.command_queue.put(
                    (
                        "csv_save_error",
                        str(exc),
                        was_running,
                    )
                )

        threading.Thread(target=worker, daemon=True).start()

    def _save_screenshot(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Save scope screenshot",
            defaultextension=".ps",
            filetypes=[("PostScript", "*.ps")],
            initialfile="mso44_scope.ps",
        )
        if not path:
            return

        self.canvas.postscript(file=path, colormode="color")
        self.status_var.set(f"Saved screenshot: {Path(path).name}")

    # ------------------------------------------------------------------
    # Measurement / formatting
    # ------------------------------------------------------------------

    def _update_measurements(self, ch: str, values: dict[str, float]) -> None:
        vars_for_ch = self.measure_vars[ch]
        vars_for_ch["pkpk"].set(self._format_voltage(values.get("pkpk")))
        vars_for_ch["rms"].set(self._format_voltage(values.get("rms")))
        vars_for_ch["mean"].set(self._format_voltage(values.get("mean")))
        vars_for_ch["min"].set(self._format_voltage(values.get("min")))
        vars_for_ch["max"].set(self._format_voltage(values.get("max")))
        vars_for_ch["frequency"].set(
            self._format_frequency(values.get("frequency"))
        )

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
            return f"{value * 1e6:.3g} uV"
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

        if value >= 1e9:
            return f"{value / 1e9:.4g} GHz"
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
            return f"{value * 1e6:.4g} us"
        if a < 1:
            return f"{value * 1e3:.4g} ms"
        return f"{value:.4g} s"

    @staticmethod
    def _format_vdiv(value: float) -> str:
        if value < 1.0:
            return f"{value * 1000:g} mV/div"
        return f"{value:g} V/div"

    @staticmethod
    def _parse_vdiv(text: str) -> float:
        value = text.strip().lower().replace("/div", "").strip()
        if value.endswith("mv"):
            return float(value[:-2]) / 1000.0
        if value.endswith("v"):
            return float(value[:-1])
        return float(value)

    @staticmethod
    def _format_time_div(value: float) -> str:
        if value < 1e-6:
            return f"{value * 1e9:g} ns/div"
        if value < 1e-3:
            return f"{value * 1e6:g} us/div"
        if value < 1:
            return f"{value * 1e3:g} ms/div"
        return f"{value:g} s/div"

    @staticmethod
    def _parse_time_div(text: str) -> float:
        value = text.strip().lower().replace("/div", "").strip()
        if value.endswith("ns"):
            return float(value[:-2]) * 1e-9
        if value.endswith("us"):
            return float(value[:-2]) * 1e-6
        if value.endswith("ms"):
            return float(value[:-2]) * 1e-3
        if value.endswith("s"):
            return float(value[:-1])
        return float(value)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def _on_close(self) -> None:
        self._closing = True
        self.stop_event.set()

        try:
            self.registry.disconnect_all()
        except Exception:
            pass

        if self.hsi_client:
            try:
                self.hsi_client.close()
            except Exception:
                pass

        if self.client:
            try:
                self.client.close()
            except Exception:
                pass

        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()
