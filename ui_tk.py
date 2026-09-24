from __future__ import annotations

import csv
import math
import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Optional

import numpy as np

from mso4 import MSO4Client


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
        self.root.geometry("1580x930")
        self.root.minsize(1180, 720)
        self.root.configure(bg="#0D1117")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.client: Optional[MSO4Client] = None
        self.acq_thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()
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

        self.ip_var = tk.StringVar(value="192.168.1.133")
        self.status_var = tk.StringVar(value="Disconnected")
        self.rate_var = tk.StringVar(value="Acq: -- fps")
        self.transfer_var = tk.StringVar(value="No waveform data yet")
        self.cursor_var = tk.StringVar(value="t = --    V = --")

        self.points_var = tk.StringVar(value="5000")
        self.refresh_var = tk.StringVar(value="50")
        self.fast_mode_var = tk.BooleanVar(value=True)
        self.fast_record_var = tk.StringVar(value="10000")

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

        self.time_scale_var = tk.StringVar(value="1 ms/div")
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

        self._setup_style()
        self._build_ui()

        self.root.after(30, self._process_command_queue)
        self.root.after(30, self._render_latest_frames)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _setup_style(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure("Dark.TFrame", background="#111820")
        style.configure("Panel.TFrame", background="#111820")
        style.configure(
            "Dark.TLabel",
            background="#111820",
            foreground="#D7DEE7",
            font=("Arial", 10),
        )
        style.configure(
            "Title.TLabel",
            background="#111820",
            foreground="#F0F4F8",
            font=("Arial", 16, "bold"),
        )
        style.configure(
            "Section.TLabel",
            background="#111820",
            foreground="#8FA3B8",
            font=("Arial", 9, "bold"),
        )
        style.configure("Dark.TButton", font=("Arial", 10, "bold"))
        style.configure(
            "Dark.TCombobox",
            fieldbackground="#0B1016",
            background="#0B1016",
            foreground="#E8EEF5",
            arrowcolor="#E8EEF5",
        )
        style.map(
            "Dark.TCombobox",
            fieldbackground=[("readonly", "#0B1016")],
            foreground=[("readonly", "#E8EEF5")],
        )

    def _build_ui(self) -> None:
        self._build_topbar()

        body = tk.PanedWindow(
            self.root,
            orient=tk.HORIZONTAL,
            bg="#0D1117",
            sashwidth=6,
            bd=0,
            relief="flat",
        )
        body.pack(fill="both", expand=True, padx=10, pady=4)

        left = self._build_left_panel(body)
        center = self._build_scope_panel(body)
        right = self._build_right_panel(body)

        body.add(left, minsize=290, width=315)
        body.add(center, minsize=620)
        body.add(right, minsize=285, width=310)

        status = tk.Frame(self.root, bg="#0B0F14", height=30)
        status.pack(fill="x", padx=10, pady=(4, 10))

        tk.Label(
            status,
            textvariable=self.status_var,
            bg="#0B0F14",
            fg="#B9C0C8",
            anchor="w",
        ).pack(side="left", padx=8, pady=5)

        tk.Label(
            status,
            textvariable=self.rate_var,
            bg="#0B0F14",
            fg="#B9C0C8",
            anchor="e",
        ).pack(side="right", padx=8, pady=5)

    def _build_topbar(self) -> None:
        top = ttk.Frame(self.root, style="Dark.TFrame", padding=(12, 8))
        top.pack(fill="x", padx=10, pady=(10, 6))

        ttk.Label(top, text="MSO44B LAN SCOPE", style="Title.TLabel").pack(side="left")

        controls = ttk.Frame(top, style="Dark.TFrame")
        controls.pack(side="right")

        ttk.Label(controls, text="IP", style="Dark.TLabel").pack(side="left", padx=(0, 5))

        self.ip_entry = tk.Entry(
            controls,
            textvariable=self.ip_var,
            width=15,
            bg="#0B1016",
            fg="#F0F4F8",
            insertbackground="#F0F4F8",
            relief="flat",
        )
        self.ip_entry.pack(side="left", padx=(0, 6), ipady=5)

        self.connect_btn = ttk.Button(
            controls,
            text="CONNECT",
            command=self._toggle_connection,
            style="Dark.TButton",
        )
        self.connect_btn.pack(side="left", padx=3)

        self.run_btn = ttk.Button(
            controls,
            text="RUN",
            command=self._toggle_run,
            style="Dark.TButton",
            state="disabled",
        )
        self.run_btn.pack(side="left", padx=3)

        self.single_btn = ttk.Button(
            controls,
            text="SINGLE",
            command=self._single,
            style="Dark.TButton",
            state="disabled",
        )
        self.single_btn.pack(side="left", padx=3)

        self.autoset_btn = ttk.Button(
            controls,
            text="AUTOSET",
            command=self._autoset,
            style="Dark.TButton",
            state="disabled",
        )
        self.autoset_btn.pack(side="left", padx=3)

        self.default_btn = ttk.Button(
            controls,
            text="DEFAULT",
            command=self._factory_default,
            style="Dark.TButton",
            state="disabled",
        )
        self.default_btn.pack(side="left", padx=3)

    def _build_left_panel(self, parent) -> tk.Frame:
        outer = tk.Frame(parent, bg="#111820")
        canvas = tk.Canvas(outer, bg="#111820", highlightthickness=0)
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg="#111820")

        inner.bind(
            "<Configure>",
            lambda _e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self._section_label(inner, "CHANNELS")

        for ch, color in CHANNEL_COLORS.items():
            self._build_channel_card(inner, ch, color)

        self._section_label(inner, "ACQUISITION", top_pad=16)

        acq = tk.Frame(inner, bg="#111820")
        acq.pack(fill="x", padx=10)

        self._form_label(acq, "Mode", 0)
        mode_combo = ttk.Combobox(
            acq,
            textvariable=self.acquire_mode_var,
            values=["SAMPLE", "PEAKDETECT", "HIRES", "AVERAGE", "ENVELOPE"],
            state="readonly",
            style="Dark.TCombobox",
            width=13,
        )
        mode_combo.grid(row=0, column=1, sticky="ew", pady=3)
        mode_combo.bind("<<ComboboxSelected>>", lambda _e: self._apply_acquire_mode())

        self._form_label(acq, "Average N", 1)
        avg = self._dark_entry(acq, self.average_count_var, 9)
        avg.grid(row=1, column=1, sticky="ew", pady=3)
        avg.bind("<Return>", lambda _e: self._apply_average_count())

        self._form_label(acq, "Transfer pts", 2)
        points_combo = ttk.Combobox(
            acq,
            textvariable=self.points_var,
            values=["1000", "2500", "5000", "10000", "25000", "50000"],
            state="readonly",
            style="Dark.TCombobox",
            width=13,
        )
        points_combo.grid(row=2, column=1, sticky="ew", pady=3)

        self._form_label(acq, "Refresh ms", 3)
        refresh = self._dark_entry(acq, self.refresh_var, 9)
        refresh.grid(row=3, column=1, sticky="ew", pady=3)

        self._form_label(acq, "Fast record", 4)
        fast = self._dark_entry(acq, self.fast_record_var, 9)
        fast.grid(row=4, column=1, sticky="ew", pady=3)

        fast_cb = tk.Checkbutton(
            acq,
            text="Fast display mode",
            variable=self.fast_mode_var,
            bg="#111820",
            fg="#D7DEE7",
            selectcolor="#0B1016",
            activebackground="#111820",
            activeforeground="#F0F4F8",
        )
        fast_cb.grid(row=5, column=0, columnspan=2, sticky="w", pady=(5, 2))

        acq.columnconfigure(1, weight=1)

        ttk.Button(
            inner,
            text="APPLY ACQUISITION",
            command=self._apply_acquisition_controls,
            style="Dark.TButton",
        ).pack(fill="x", padx=10, pady=(8, 4))

        self._section_label(inner, "SCPI CONSOLE", top_pad=16)

        self.scpi_entry = self._dark_entry(inner, self.scpi_var, 24)
        self.scpi_entry.pack(fill="x", padx=10, ipady=3)
        self.scpi_entry.bind("<Return>", lambda _e: self._send_scpi())

        self.scpi_btn = ttk.Button(
            inner,
            text="SEND SCPI",
            command=self._send_scpi,
            style="Dark.TButton",
            state="disabled",
        )
        self.scpi_btn.pack(fill="x", padx=10, pady=5)

        tk.Label(
            inner,
            textvariable=self.scpi_result_var,
            bg="#111820",
            fg="#93A0AD",
            justify="left",
            wraplength=255,
            anchor="nw",
        ).pack(fill="x", padx=10, pady=(2, 10))

        return outer

    def _build_channel_card(self, parent, ch: str, color: str) -> None:
        card = tk.Frame(
            parent,
            bg="#161E27",
            highlightbackground="#273441",
            highlightthickness=1,
        )
        card.pack(fill="x", padx=10, pady=4)

        top = tk.Frame(card, bg="#161E27")
        top.pack(fill="x", padx=7, pady=(6, 3))

        cb = tk.Checkbutton(
            top,
            text=ch,
            variable=self.channel_vars[ch],
            command=lambda c=ch: self._channel_state_changed(c),
            bg="#161E27",
            fg=color,
            selectcolor="#0B1016",
            activebackground="#161E27",
            activeforeground=color,
            font=("Arial", 11, "bold"),
        )
        cb.pack(side="left")

        ttk.Button(
            top,
            text="SET",
            command=lambda c=ch: self._apply_channel(c),
            style="Dark.TButton",
        ).pack(side="right")

        grid = tk.Frame(card, bg="#161E27")
        grid.pack(fill="x", padx=7, pady=(2, 7))

        tk.Label(grid, text="V/div", bg="#161E27", fg="#B9C0C8").grid(row=0, column=0, sticky="w")
        scale = ttk.Combobox(
            grid,
            textvariable=self.channel_scale_vars[ch],
            values=[self._format_vdiv(v) for v in VERTICAL_SCALES],
            state="readonly",
            style="Dark.TCombobox",
            width=11,
        )
        scale.grid(row=0, column=1, sticky="ew", padx=(5, 0), pady=2)

        tk.Label(grid, text="Pos div", bg="#161E27", fg="#B9C0C8").grid(row=1, column=0, sticky="w")
        self._dark_entry(grid, self.channel_position_vars[ch], 8).grid(
            row=1, column=1, sticky="ew", padx=(5, 0), pady=2
        )

        tk.Label(grid, text="Offset V", bg="#161E27", fg="#B9C0C8").grid(row=2, column=0, sticky="w")
        self._dark_entry(grid, self.channel_offset_vars[ch], 8).grid(
            row=2, column=1, sticky="ew", padx=(5, 0), pady=2
        )

        tk.Label(grid, text="Coupling", bg="#161E27", fg="#B9C0C8").grid(row=3, column=0, sticky="w")
        coupling = ttk.Combobox(
            grid,
            textvariable=self.channel_coupling_vars[ch],
            values=["DC", "AC"],
            state="readonly",
            style="Dark.TCombobox",
            width=11,
        )
        coupling.grid(row=3, column=1, sticky="ew", padx=(5, 0), pady=2)

        grid.columnconfigure(1, weight=1)

    def _build_scope_panel(self, parent) -> tk.Frame:
        frame = tk.Frame(
            parent,
            bg="#05070A",
            highlightbackground="#26313D",
            highlightthickness=1,
        )

        info = tk.Frame(frame, bg="#0B0F14")
        info.pack(fill="x")

        tk.Label(
            info,
            textvariable=self.transfer_var,
            bg="#0B0F14",
            fg="#8FA3B8",
            anchor="w",
        ).pack(side="left", fill="x", expand=True, padx=6, pady=4)

        ttk.Button(
            info,
            text="AUTO SCALE",
            command=self._autoscale,
            style="Dark.TButton",
        ).pack(side="right", padx=3)

        ttk.Button(
            info,
            text="SAVE CSV",
            command=self._save_csv,
            style="Dark.TButton",
        ).pack(side="right", padx=3)

        ttk.Button(
            info,
            text="SCREENSHOT",
            command=self._save_screenshot,
            style="Dark.TButton",
        ).pack(side="right", padx=3)

        self.canvas = tk.Canvas(
            frame,
            bg="#05070A",
            highlightthickness=0,
            cursor="crosshair",
        )
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda _e: self._redraw_scope())
        self.canvas.bind("<Motion>", self._on_mouse_move)

        tk.Label(
            frame,
            textvariable=self.cursor_var,
            bg="#0B0F14",
            fg="#93A0AD",
            anchor="w",
        ).pack(fill="x", padx=6, pady=3)

        return frame

    def _build_right_panel(self, parent) -> tk.Frame:
        outer = tk.Frame(parent, bg="#111820")
        canvas = tk.Canvas(outer, bg="#111820", highlightthickness=0)
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg="#111820")

        inner.bind(
            "<Configure>",
            lambda _e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self._section_label(inner, "HORIZONTAL")

        horizontal = tk.Frame(inner, bg="#111820")
        horizontal.pack(fill="x", padx=10)

        self._form_label(horizontal, "Time/div", 0)
        time_combo = ttk.Combobox(
            horizontal,
            textvariable=self.time_scale_var,
            values=[self._format_time_div(v) for v in TIME_SCALES],
            state="readonly",
            style="Dark.TCombobox",
            width=13,
        )
        time_combo.grid(row=0, column=1, sticky="ew", pady=3)

        self._form_label(horizontal, "Position %", 1)
        self._dark_entry(horizontal, self.horizontal_position_var, 9).grid(
            row=1, column=1, sticky="ew", pady=3
        )
        horizontal.columnconfigure(1, weight=1)

        ttk.Button(
            inner,
            text="APPLY HORIZONTAL",
            command=self._apply_horizontal,
            style="Dark.TButton",
        ).pack(fill="x", padx=10, pady=(6, 4))

        self._section_label(inner, "TRIGGER", top_pad=16)

        trigger = tk.Frame(inner, bg="#111820")
        trigger.pack(fill="x", padx=10)

        self._form_label(trigger, "Source", 0)
        ttk.Combobox(
            trigger,
            textvariable=self.trigger_source_var,
            values=["CH1", "CH2", "CH3", "CH4"],
            state="readonly",
            style="Dark.TCombobox",
            width=12,
        ).grid(row=0, column=1, sticky="ew", pady=3)

        self._form_label(trigger, "Level V", 1)
        self._dark_entry(trigger, self.trigger_level_var, 9).grid(
            row=1, column=1, sticky="ew", pady=3
        )

        self._form_label(trigger, "Slope", 2)
        ttk.Combobox(
            trigger,
            textvariable=self.trigger_slope_var,
            values=["RISE", "FALL", "EITHER"],
            state="readonly",
            style="Dark.TCombobox",
            width=12,
        ).grid(row=2, column=1, sticky="ew", pady=3)

        self._form_label(trigger, "Mode", 3)
        ttk.Combobox(
            trigger,
            textvariable=self.trigger_mode_var,
            values=["AUTO", "NORMAL"],
            state="readonly",
            style="Dark.TCombobox",
            width=12,
        ).grid(row=3, column=1, sticky="ew", pady=3)

        trigger.columnconfigure(1, weight=1)

        ttk.Button(
            inner,
            text="APPLY TRIGGER",
            command=self._apply_trigger,
            style="Dark.TButton",
        ).pack(fill="x", padx=10, pady=(6, 3))

        trig_buttons = tk.Frame(inner, bg="#111820")
        trig_buttons.pack(fill="x", padx=10)

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

        self._section_label(inner, "MEASUREMENTS", top_pad=16)

        fields = [
            ("pkpk", "Vpp"),
            ("rms", "Vrms"),
            ("frequency", "Freq"),
            ("mean", "Mean"),
            ("min", "Min"),
            ("max", "Max"),
        ]

        for ch, color in CHANNEL_COLORS.items():
            card = tk.Frame(
                inner,
                bg="#161E27",
                highlightbackground="#273441",
                highlightthickness=1,
            )
            card.pack(fill="x", padx=10, pady=4)

            tk.Label(
                card,
                text=ch,
                bg="#161E27",
                fg=color,
                font=("Arial", 10, "bold"),
            ).grid(row=0, column=0, columnspan=2, sticky="w", padx=7, pady=(5, 2))

            vars_for_ch: dict[str, tk.StringVar] = {}
            for idx, (key, label) in enumerate(fields, start=1):
                tk.Label(
                    card,
                    text=label,
                    bg="#161E27",
                    fg="#B9C0C8",
                ).grid(row=idx, column=0, sticky="w", padx=7, pady=1)

                var = tk.StringVar(value="--")
                tk.Label(
                    card,
                    textvariable=var,
                    bg="#161E27",
                    fg="#F0F4F8",
                    font=("Menlo", 9, "bold"),
                ).grid(row=idx, column=1, sticky="e", padx=7, pady=1)
                vars_for_ch[key] = var

            card.columnconfigure(1, weight=1)
            self.measure_vars[ch] = vars_for_ch

        ttk.Button(
            inner,
            text="REFRESH SETTINGS",
            command=self._refresh_settings,
            style="Dark.TButton",
        ).pack(fill="x", padx=10, pady=(14, 10))

        return outer

    def _section_label(self, parent, text: str, top_pad: int = 10) -> None:
        tk.Label(
            parent,
            text=text,
            bg="#111820",
            fg="#8FA3B8",
            font=("Arial", 9, "bold"),
        ).pack(anchor="w", padx=12, pady=(top_pad, 6))

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
            bg="#0B1016",
            fg="#F0F4F8",
            insertbackground="#F0F4F8",
            relief="flat",
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
                self.command_queue.put(("connected", client, idn, settings))
            except Exception as exc:
                self.command_queue.put(("connect_error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _disconnect(self) -> None:
        self._stop_acquisition(local_only=True)

        client = self.client
        self.client = None
        if client:
            try:
                client.close()
            except Exception:
                pass

        self.connect_btn.configure(text="CONNECT", state="normal")
        self.run_btn.configure(text="RUN", state="disabled")
        self.single_btn.configure(state="disabled")
        self.autoset_btn.configure(state="disabled")
        self.default_btn.configure(state="disabled")
        self.scpi_btn.configure(state="disabled")
        self.ip_entry.configure(state="normal")

        self.status_var.set("Disconnected")
        self.rate_var.set("Acq: -- fps")
        self.transfer_var.set("No waveform data yet")

        with self.frame_lock:
            self.latest_frames.clear()
        self.waveforms.clear()
        self.x_range = None
        self.y_range = None
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
        enabled = self.channel_vars[ch].get()
        if self.client and self.client.connected:
            self._run_async(
                f"{ch} {'ON' if enabled else 'OFF'}",
                lambda: self.client.set_channel_state(ch, enabled),
            )

        if not enabled:
            self.waveforms.pop(ch, None)

        if self.acq_thread and self.acq_thread.is_alive():
            self._stop_acquisition(local_only=True)
            self.root.after(80, self._start_acquisition)
        else:
            self._redraw_scope()

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

        self._run_async(f"Applying {ch}", command, refresh_settings=True)

    def _apply_horizontal(self) -> None:
        try:
            scale = self._parse_time_div(self.time_scale_var.get())
            position = float(self.horizontal_position_var.get())
        except ValueError as exc:
            messagebox.showwarning("Horizontal", f"Invalid value: {exc}")
            return

        def command() -> None:
            self.client.set_horizontal_scale(scale)
            self.client.set_horizontal_position(position)

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

        def worker() -> None:
            try:
                self.client.single_acquisition()
                time.sleep(0.08)
                self._acquire_one_frame(channels)
                self.command_queue.put(("single_done",))
            except Exception as exc:
                self.command_queue.put(("acq_error", f"Single: {exc}"))

        self.status_var.set("Single acquisition...")
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
            fast_record = max(points, int(self.fast_record_var.get()))
        except ValueError:
            messagebox.showwarning(
                "Acquisition",
                "Transfer points, refresh and fast record must be integers.",
            )
            return

        self.stop_event.clear()
        self.channel_errors.clear()
        self._need_autoscale = True

        self.run_btn.configure(text="STOP")
        self.status_var.set("Starting acquisition...")
        self.transfer_var.set("Waiting for waveform...")

        def worker() -> None:
            try:
                self.client.prepare_acquisition(
                    channels,
                    fast_record_length=(fast_record if self.fast_mode_var.get() else None),
                )
                record_length = self.client.get_record_length()
                self.command_queue.put(("prepared", record_length))
            except Exception as exc:
                self.command_queue.put(("acq_error", f"Cannot start acquisition: {exc}"))
                return

            last_report = time.monotonic()
            frames = 0

            while not self.stop_event.is_set():
                started = time.monotonic()
                successful = self._acquire_one_frame(channels)

                if successful == 0:
                    errors = " | ".join(
                        f"{ch}: {msg}" for ch, msg in self.channel_errors.items()
                    )
                    self.command_queue.put(
                        (
                            "acq_error",
                            "No selected channel returned waveform data. " + errors,
                        )
                    )
                    return

                frames += 1
                now = time.monotonic()
                if now - last_report >= 1.0:
                    self.command_queue.put(("rate", frames / (now - last_report)))
                    frames = 0
                    last_report = now

                elapsed_ms = (time.monotonic() - started) * 1000.0
                remaining = max(0.0, refresh_ms - elapsed_ms)
                if remaining:
                    self.stop_event.wait(remaining / 1000.0)

        self.acq_thread = threading.Thread(target=worker, daemon=True)
        self.acq_thread.start()

    def _acquire_one_frame(self, channels: list[str]) -> int:
        try:
            points = max(500, int(self.points_var.get()))
        except ValueError:
            points = 5000

        successful = 0

        for ch in channels:
            if self.stop_event.is_set():
                break

            try:
                waveform = self.client.get_waveform(ch, 1, points)
                info = self.client.get_last_transfer_info(ch)
                measurements = waveform.measurements()

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
        self.stop_event.set()
        self.run_btn.configure(text="RUN")
        self.rate_var.set("Acq: -- fps")

        if not local_only and self.client and self.client.connected:
            def worker() -> None:
                try:
                    self.client.stop_acquisition()
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

            for ch, (x, y, measurements, info) in frames.items():
                self.waveforms[ch] = (x, y)
                self.measurements[ch] = measurements
                self._update_measurements(ch, measurements)

                mode = info.get("mode", "?")
                npts = info.get("points", len(y))
                record = info.get("record_length")
                latest_status = (
                    f"{ch}: {npts} pts {mode}"
                    + (f" | record {record}" if record else "")
                )

            if latest_status:
                self.transfer_var.set(latest_status)

            now = time.monotonic()
            # Cap drawing around 30 FPS even when LAN acquisition is faster.
            if now - self._last_draw >= 1.0 / 30.0:
                if self._need_autoscale:
                    self._autoscale()
                    self._need_autoscale = False
                else:
                    self._redraw_scope()
                self._last_draw = now

        self.root.after(30, self._render_latest_frames)

    def _autoscale(self) -> None:
        xs = []
        ys = []

        for ch, (x, y) in self.waveforms.items():
            if self.channel_vars[ch].get() and len(x) and len(y):
                xs.append(x)
                ys.append(y)

        if not xs or not ys:
            self.x_range = None
            self.y_range = None
            self._redraw_scope()
            return

        xmin = min(float(np.min(x)) for x in xs)
        xmax = max(float(np.max(x)) for x in xs)
        ymin = min(float(np.min(y)) for y in ys)
        ymax = max(float(np.max(y)) for y in ys)

        if xmax <= xmin:
            xmax = xmin + 1.0

        if ymax <= ymin:
            pad = max(abs(ymax), 0.01) * 0.1
            ymin -= pad
            ymax += pad
        else:
            pad = (ymax - ymin) * 0.08
            ymin -= pad
            ymax += pad

        self.x_range = (xmin, xmax)
        self.y_range = (ymin, ymax)
        self._redraw_scope()

    def _redraw_scope(self) -> None:
        if not hasattr(self, "canvas"):
            return

        c = self.canvas
        width = max(2, c.winfo_width())
        height = max(2, c.winfo_height())

        c.delete("all")
        self._draw_grid(width, height)

        active = [
            ch
            for ch in CHANNEL_COLORS
            if self.channel_vars[ch].get() and ch in self.waveforms
        ]

        if not active:
            message = (
                "Connected - press RUN"
                if self.client and self.client.connected
                else "Connect to MSO44B"
            )
            c.create_text(
                width / 2,
                height / 2,
                text=message,
                fill="#586574",
                font=("Arial", 14),
            )
            return

        if self.x_range is None or self.y_range is None:
            return

        xmin, xmax = self.x_range
        ymin, ymax = self.y_range
        xspan = max(xmax - xmin, 1e-15)
        yspan = max(ymax - ymin, 1e-15)

        # No point drawing more than ~2 samples per horizontal pixel.
        max_draw_points = max(500, min(5000, width * 2))

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

            xp = (xd - xmin) / xspan * width
            yp = height - (yd - ymin) / yspan * height

            coords = np.column_stack((xp, yp)).ravel().tolist()
            c.create_line(
                *coords,
                fill=CHANNEL_COLORS[ch],
                width=2,
                smooth=False,
            )

        self._draw_channel_markers(width, height, ymin, ymax)

    def _draw_grid(self, width: int, height: int) -> None:
        for i in range(11):
            x = width * i / 10.0
            color = "#45505C" if i == 5 else "#242D36"
            self.canvas.create_line(x, 0, x, height, fill=color, width=1)

        for i in range(9):
            y = height * i / 8.0
            color = "#45505C" if i == 4 else "#242D36"
            self.canvas.create_line(0, y, width, y, fill=color, width=1)

        for i in range(10):
            x = width * (i + 0.5) / 10.0
            self.canvas.create_line(
                x,
                height / 2 - 3,
                x,
                height / 2 + 3,
                fill="#39424C",
            )

    def _draw_channel_markers(
        self,
        width: int,
        height: int,
        ymin: float,
        ymax: float,
    ) -> None:
        span = max(ymax - ymin, 1e-15)

        for ch in CHANNEL_COLORS:
            if not self.channel_vars[ch].get() or ch not in self.waveforms:
                continue

            _, y = self.waveforms[ch]
            if len(y) == 0:
                continue

            baseline = float(np.mean(y))
            py = height - (baseline - ymin) / span * height
            py = max(8, min(height - 8, py))

            self.canvas.create_polygon(
                0,
                py,
                11,
                py - 7,
                11,
                py + 7,
                fill=CHANNEL_COLORS[ch],
                outline="",
            )
            self.canvas.create_text(
                15,
                py,
                text=ch[-1],
                fill=CHANNEL_COLORS[ch],
                anchor="w",
                font=("Arial", 8, "bold"),
            )

    def _on_mouse_move(self, event) -> None:
        if self.x_range is None or self.y_range is None:
            return

        width = max(1, self.canvas.winfo_width())
        height = max(1, self.canvas.winfo_height())
        xmin, xmax = self.x_range
        ymin, ymax = self.y_range

        t = xmin + (event.x / width) * (xmax - xmin)
        v = ymax - (event.y / height) * (ymax - ymin)

        self.cursor_var.set(
            f"t = {self._format_time(t)}    V = {self._format_voltage(v)}"
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
                    _, client, idn, settings = msg
                    self.client = client
                    self.connect_btn.configure(text="DISCONNECT", state="normal")
                    self.run_btn.configure(state="normal")
                    self.single_btn.configure(state="normal")
                    self.autoset_btn.configure(state="normal")
                    self.default_btn.configure(state="normal")
                    self.scpi_btn.configure(state="normal")
                    self.ip_entry.configure(state="disabled")
                    self.status_var.set(f"Connected: {idn}")
                    self.scpi_result_var.set(idn)
                    self._apply_settings_to_ui(settings)
                    self._redraw_scope()

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

                elif kind == "prepared":
                    record = msg[1]
                    if record:
                        self.status_var.set(f"RUN | record length {record}")
                    else:
                        self.status_var.set("RUN")

                elif kind == "rate":
                    self.rate_var.set(f"Acq: {msg[1]:.1f} fps")

                elif kind == "single_done":
                    self.status_var.set("Single acquisition complete")
                    self.run_btn.configure(text="RUN")

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
            self.time_scale_var.set(
                self._format_time_div(float(settings["horizontal_scale"]))
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

    # ------------------------------------------------------------------
    # Save / export
    # ------------------------------------------------------------------

    def _save_csv(self) -> None:
        active = [
            ch
            for ch in CHANNEL_COLORS
            if self.channel_vars[ch].get() and ch in self.waveforms
        ]
        if not active:
            messagebox.showinfo("Save CSV", "No waveform data to save.")
            return

        path = filedialog.asksaveasfilename(
            title="Save waveform CSV",
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv")],
            initialfile="mso44_waveform.csv",
        )
        if not path:
            return

        max_len = max(len(self.waveforms[ch][0]) for ch in active)

        with open(path, "w", newline="", encoding="utf-8") as fp:
            writer = csv.writer(fp)
            header = []
            for ch in active:
                header.extend([f"{ch}_time_s", f"{ch}_volts"])
            writer.writerow(header)

            for i in range(max_len):
                row = []
                for ch in active:
                    x, y = self.waveforms[ch]
                    if i < len(x):
                        row.extend([x[i], y[i]])
                    else:
                        row.extend(["", ""])
                writer.writerow(row)

        self.status_var.set(f"Saved CSV: {Path(path).name}")

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

        if self.client:
            try:
                self.client.close()
            except Exception:
                pass

        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()
