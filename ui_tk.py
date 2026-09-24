from __future__ import annotations

import math
import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Optional

import numpy as np

from mso4 import MSO4Client


CHANNEL_COLORS = {
    "CH1": "#FFD400",
    "CH2": "#00D46A",
    "CH3": "#32A7FF",
    "CH4": "#FF4A55",
}


class MSO4ScopeApp:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("MSO4 LAN Scope")
        self.root.geometry("1450x860")
        self.root.minsize(1050, 650)
        self.root.configure(bg="#0D1117")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.client: Optional[MSO4Client] = None
        self.acq_thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()
        self.ui_queue: queue.Queue = queue.Queue()

        self.waveforms: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        self.measurements: dict[str, dict[str, float]] = {}

        self.x_range: Optional[tuple[float, float]] = None
        self.y_range: Optional[tuple[float, float]] = None

        self.ip_var = tk.StringVar(value="192.168.1.133")
        self.points_var = tk.StringVar(value="10000")
        self.refresh_var = tk.StringVar(value="120")
        self.status_var = tk.StringVar(value="Disconnected")
        self.rate_var = tk.StringVar(value="Acq: -- fps")
        self.scpi_var = tk.StringVar(value="*IDN?")
        self.scpi_result_var = tk.StringVar(value="Ready")
        self.cursor_var = tk.StringVar(value="t = --    V = --")
        self.channel_vars = {
            ch: tk.BooleanVar(value=(ch == "CH1"))
            for ch in CHANNEL_COLORS
        }
        self.measure_vars: dict[str, dict[str, tk.StringVar]] = {}

        self._setup_style()
        self._build_ui()
        self.root.after(40, self._process_ui_queue)

    def _setup_style(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(
            "Dark.TFrame",
            background="#111820",
        )
        style.configure(
            "Panel.TFrame",
            background="#111820",
            relief="flat",
        )
        style.configure(
            "Dark.TLabel",
            background="#111820",
            foreground="#D7DEE7",
            font=("Arial", 11),
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
        style.configure(
            "Dark.TButton",
            font=("Arial", 10, "bold"),
        )
        style.configure(
            "Dark.TCheckbutton",
            background="#111820",
            foreground="#D7DEE7",
        )
        style.map(
            "Dark.TCheckbutton",
            background=[("active", "#111820")],
            foreground=[("active", "#F0F4F8")],
        )
        style.configure(
            "Dark.TCombobox",
            fieldbackground="#0B1016",
            background="#0B1016",
            foreground="#D7DEE7",
            arrowcolor="#D7DEE7",
        )

    def _build_ui(self) -> None:
        top = ttk.Frame(self.root, style="Dark.TFrame", padding=(12, 8))
        top.pack(fill="x", padx=10, pady=(10, 6))

        ttk.Label(top, text="MSO4 LAN SCOPE", style="Title.TLabel").pack(side="left")

        right = ttk.Frame(top, style="Dark.TFrame")
        right.pack(side="right")

        ttk.Label(right, text="IP", style="Dark.TLabel").pack(side="left", padx=(0, 5))
        self.ip_entry = tk.Entry(
            right,
            textvariable=self.ip_var,
            width=16,
            bg="#0B1016",
            fg="#F0F4F8",
            insertbackground="#F0F4F8",
            relief="flat",
        )
        self.ip_entry.pack(side="left", padx=(0, 8), ipady=5)

        ttk.Label(
            right,
            text="TCPIP0::<IP>::inst0::INSTR",
            style="Dark.TLabel",
        ).pack(side="left", padx=(0, 8))

        self.connect_btn = ttk.Button(
            right,
            text="Connect",
            command=self._toggle_connection,
            style="Dark.TButton",
        )
        self.connect_btn.pack(side="left", padx=4)

        self.run_btn = ttk.Button(
            right,
            text="RUN",
            command=self._toggle_run,
            style="Dark.TButton",
            state="disabled",
        )
        self.run_btn.pack(side="left", padx=4)

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
        right_panel = self._build_measure_panel(body)

        body.add(left, minsize=220, width=250)
        body.add(center, minsize=600)
        body.add(right_panel, minsize=235, width=270)

        status = tk.Frame(self.root, bg="#0B0F14", height=28)
        status.pack(fill="x", padx=10, pady=(4, 10))
        tk.Label(
            status,
            textvariable=self.status_var,
            bg="#0B0F14",
            fg="#B9C0C8",
            anchor="w",
        ).pack(side="left", padx=8, pady=4)
        tk.Label(
            status,
            textvariable=self.rate_var,
            bg="#0B0F14",
            fg="#B9C0C8",
            anchor="e",
        ).pack(side="right", padx=8, pady=4)

    def _build_left_panel(self, parent) -> tk.Frame:
        frame = tk.Frame(parent, bg="#111820")
        tk.Label(
            frame,
            text="CHANNELS",
            bg="#111820",
            fg="#8FA3B8",
            font=("Arial", 9, "bold"),
        ).pack(anchor="w", padx=12, pady=(12, 8))

        for ch, color in CHANNEL_COLORS.items():
            row = tk.Frame(frame, bg="#161E27", highlightbackground="#273441", highlightthickness=1)
            row.pack(fill="x", padx=10, pady=4)

            cb = tk.Checkbutton(
                row,
                text=ch,
                variable=self.channel_vars[ch],
                command=self._channels_changed,
                bg="#161E27",
                fg=color,
                selectcolor="#0B1016",
                activebackground="#161E27",
                activeforeground=color,
                font=("Arial", 12, "bold"),
                relief="flat",
            )
            cb.pack(side="left", padx=8, pady=7)

        tk.Label(
            frame,
            text="ACQUISITION",
            bg="#111820",
            fg="#8FA3B8",
            font=("Arial", 9, "bold"),
        ).pack(anchor="w", padx=12, pady=(16, 6))

        form = tk.Frame(frame, bg="#111820")
        form.pack(fill="x", padx=10)

        tk.Label(form, text="Points", bg="#111820", fg="#D7DEE7").grid(row=0, column=0, sticky="w", pady=4)
        points = ttk.Combobox(
            form,
            textvariable=self.points_var,
            values=["1000", "2500", "5000", "10000", "25000", "50000"],
            state="readonly",
            width=12,
            style="Dark.TCombobox",
        )
        points.grid(row=0, column=1, sticky="ew", pady=4)

        tk.Label(form, text="Refresh", bg="#111820", fg="#D7DEE7").grid(row=1, column=0, sticky="w", pady=4)
        refresh_entry = tk.Entry(
            form,
            textvariable=self.refresh_var,
            width=10,
            bg="#0B1016",
            fg="#F0F4F8",
            insertbackground="#F0F4F8",
            relief="flat",
        )
        refresh_entry.grid(row=1, column=1, sticky="ew", pady=4, ipady=4)
        form.columnconfigure(1, weight=1)

        ttk.Button(
            frame,
            text="Auto scale display",
            command=self._autoscale,
            style="Dark.TButton",
        ).pack(fill="x", padx=10, pady=(10, 4))

        tk.Label(
            frame,
            text="SCPI CONSOLE",
            bg="#111820",
            fg="#8FA3B8",
            font=("Arial", 9, "bold"),
        ).pack(anchor="w", padx=12, pady=(18, 6))

        self.scpi_entry = tk.Entry(
            frame,
            textvariable=self.scpi_var,
            bg="#0B1016",
            fg="#F0F4F8",
            insertbackground="#F0F4F8",
            relief="flat",
        )
        self.scpi_entry.pack(fill="x", padx=10, ipady=5)
        self.scpi_entry.bind("<Return>", lambda _event: self._send_scpi())

        self.scpi_btn = ttk.Button(
            frame,
            text="Send",
            command=self._send_scpi,
            style="Dark.TButton",
            state="disabled",
        )
        self.scpi_btn.pack(fill="x", padx=10, pady=6)

        tk.Label(
            frame,
            textvariable=self.scpi_result_var,
            bg="#111820",
            fg="#93A0AD",
            justify="left",
            wraplength=210,
            anchor="nw",
        ).pack(fill="x", padx=10, pady=(2, 10))

        return frame

    def _build_scope_panel(self, parent) -> tk.Frame:
        frame = tk.Frame(parent, bg="#05070A", highlightbackground="#26313D", highlightthickness=1)

        self.canvas = tk.Canvas(
            frame,
            bg="#05070A",
            highlightthickness=0,
            cursor="crosshair",
        )
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda _event: self._redraw_scope())
        self.canvas.bind("<Motion>", self._on_mouse_move)

        tk.Label(
            frame,
            textvariable=self.cursor_var,
            bg="#0B0F14",
            fg="#93A0AD",
            anchor="w",
        ).pack(fill="x", padx=0, pady=0)

        return frame

    def _build_measure_panel(self, parent) -> tk.Frame:
        frame = tk.Frame(parent, bg="#111820")

        tk.Label(
            frame,
            text="MEASUREMENTS",
            bg="#111820",
            fg="#8FA3B8",
            font=("Arial", 9, "bold"),
        ).pack(anchor="w", padx=12, pady=(12, 8))

        fields = [
            ("pkpk", "Vpp"),
            ("rms", "Vrms"),
            ("frequency", "Freq"),
            ("mean", "Mean"),
            ("min", "Min"),
            ("max", "Max"),
        ]

        for ch, color in CHANNEL_COLORS.items():
            card = tk.Frame(frame, bg="#161E27", highlightbackground="#273441", highlightthickness=1)
            card.pack(fill="x", padx=10, pady=4)

            tk.Label(
                card,
                text=ch,
                bg="#161E27",
                fg=color,
                font=("Arial", 11, "bold"),
            ).grid(row=0, column=0, columnspan=2, sticky="w", padx=8, pady=(7, 4))

            vars_for_ch: dict[str, tk.StringVar] = {}
            for idx, (key, label) in enumerate(fields, start=1):
                tk.Label(
                    card,
                    text=label,
                    bg="#161E27",
                    fg="#B9C0C8",
                ).grid(row=idx, column=0, sticky="w", padx=8, pady=2)
                var = tk.StringVar(value="--")
                tk.Label(
                    card,
                    textvariable=var,
                    bg="#161E27",
                    fg="#F0F4F8",
                    font=("Menlo", 10, "bold"),
                ).grid(row=idx, column=1, sticky="e", padx=8, pady=2)
                vars_for_ch[key] = var

            card.columnconfigure(1, weight=1)
            self.measure_vars[ch] = vars_for_ch

        return frame

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
                client = MSO4Client(host, timeout=5.0)
                idn = client.connect()
                self.ui_queue.put(("connected", client, idn))
            except Exception as exc:
                self.ui_queue.put(("connect_error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _disconnect(self) -> None:
        self._stop_acquisition()
        client = self.client
        self.client = None
        if client:
            try:
                client.close()
            except Exception:
                pass

        self.connect_btn.configure(text="Connect", state="normal")
        self.run_btn.configure(text="RUN", state="disabled")
        self.scpi_btn.configure(state="disabled")
        self.ip_entry.configure(state="normal")
        self.status_var.set("Disconnected")
        self.rate_var.set("Acq: -- fps")

    def _toggle_run(self) -> None:
        if self.acq_thread and self.acq_thread.is_alive():
            self._stop_acquisition()
            return
        self._start_acquisition()

    def _start_acquisition(self) -> None:
        if not self.client or not self.client.connected:
            return

        channels = [ch for ch, var in self.channel_vars.items() if var.get()]
        if not channels:
            messagebox.showinfo("MSO4", "Enable at least one channel.")
            return

        try:
            points = int(self.points_var.get())
            refresh_ms = max(20, int(self.refresh_var.get()))
        except ValueError:
            messagebox.showwarning("MSO4", "Points/Refresh must be numeric.")
            return

        self.stop_event.clear()
        self.run_btn.configure(text="STOP")

        def worker() -> None:
            last_report = time.monotonic()
            frames = 0

            while not self.stop_event.is_set():
                started = time.monotonic()
                try:
                    for ch in channels:
                        if self.stop_event.is_set():
                            return
                        waveform = self.client.get_waveform(ch, 1, points)
                        self.ui_queue.put(
                            (
                                "waveform",
                                ch,
                                waveform.time_s,
                                waveform.volts,
                                waveform.measurements(),
                            )
                        )
                except Exception as exc:
                    if not self.stop_event.is_set():
                        self.ui_queue.put(("acq_error", str(exc)))
                    return

                frames += 1
                now = time.monotonic()
                if now - last_report >= 1.0:
                    self.ui_queue.put(("rate", frames / (now - last_report)))
                    frames = 0
                    last_report = now

                elapsed_ms = (time.monotonic() - started) * 1000.0
                remaining = max(0.0, refresh_ms - elapsed_ms)
                if remaining:
                    self.stop_event.wait(remaining / 1000.0)

        self.acq_thread = threading.Thread(target=worker, daemon=True)
        self.acq_thread.start()

    def _stop_acquisition(self) -> None:
        self.stop_event.set()
        self.run_btn.configure(text="RUN")
        self.rate_var.set("Acq: -- fps")

    def _channels_changed(self) -> None:
        running = bool(self.acq_thread and self.acq_thread.is_alive())
        if running:
            self._stop_acquisition()
            self.root.after(80, self._start_acquisition)
        self._redraw_scope()

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
                self.ui_queue.put(("scpi_result", result))
            except Exception as exc:
                self.ui_queue.put(("scpi_result", f"ERROR: {exc}"))

        threading.Thread(target=worker, daemon=True).start()

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
            pad = max(abs(ymax), 1.0) * 0.1
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
            ch for ch in CHANNEL_COLORS
            if self.channel_vars[ch].get() and ch in self.waveforms
        ]
        if not active:
            c.create_text(
                width / 2,
                height / 2,
                text="Connect MSO44 and press RUN",
                fill="#586574",
                font=("Arial", 14),
            )
            return

        if self.x_range is None or self.y_range is None:
            self._autoscale()
            return

        xmin, xmax = self.x_range
        ymin, ymax = self.y_range
        xspan = max(xmax - xmin, 1e-15)
        yspan = max(ymax - ymin, 1e-15)

        max_draw_points = max(400, width * 2)

        for ch in active:
            x, y = self.waveforms[ch]
            if len(x) < 2:
                continue

            if len(x) > max_draw_points:
                idx = np.linspace(0, len(x) - 1, max_draw_points, dtype=np.int64)
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

    def _draw_grid(self, width: int, height: int) -> None:
        c = self.canvas
        for i in range(11):
            x = width * i / 10.0
            color = "#39424C" if i == 5 else "#232B33"
            c.create_line(x, 0, x, height, fill=color, width=1)
        for i in range(9):
            y = height * i / 8.0
            color = "#39424C" if i == 4 else "#232B33"
            c.create_line(0, y, width, y, fill=color, width=1)

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

    def _process_ui_queue(self) -> None:
        try:
            while True:
                msg = self.ui_queue.get_nowait()
                kind = msg[0]

                if kind == "connected":
                    _, client, idn = msg
                    self.client = client
                    self.connect_btn.configure(text="Disconnect", state="normal")
                    self.run_btn.configure(state="normal")
                    self.scpi_btn.configure(state="normal")
                    self.ip_entry.configure(state="disabled")
                    self.status_var.set(f"Connected: {idn}")
                    self.scpi_result_var.set(idn)

                elif kind == "connect_error":
                    self.connect_btn.configure(state="normal")
                    self.status_var.set("Connection failed")
                    messagebox.showerror("MSO4 connection failed", msg[1])

                elif kind == "waveform":
                    _, ch, x, y, measurements = msg
                    self.waveforms[ch] = (x, y)
                    self.measurements[ch] = measurements
                    self._update_measurements(ch, measurements)
                    if self.x_range is None or self.y_range is None:
                        self._autoscale()
                    else:
                        self._redraw_scope()

                elif kind == "rate":
                    self.rate_var.set(f"Acq: {msg[1]:.1f} fps")

                elif kind == "acq_error":
                    self._stop_acquisition()
                    self.status_var.set(f"Acquisition error: {msg[1]}")
                    messagebox.showwarning("Acquisition stopped", msg[1])

                elif kind == "scpi_result":
                    self.scpi_result_var.set(str(msg[1]))
                    if self.client and self.client.connected:
                        self.scpi_btn.configure(state="normal")

        except queue.Empty:
            pass

        self.root.after(40, self._process_ui_queue)

    def _update_measurements(self, ch: str, values: dict[str, float]) -> None:
        vars_for_ch = self.measure_vars[ch]
        vars_for_ch["pkpk"].set(self._format_voltage(values.get("pkpk")))
        vars_for_ch["rms"].set(self._format_voltage(values.get("rms")))
        vars_for_ch["mean"].set(self._format_voltage(values.get("mean")))
        vars_for_ch["min"].set(self._format_voltage(values.get("min")))
        vars_for_ch["max"].set(self._format_voltage(values.get("max")))
        vars_for_ch["frequency"].set(self._format_frequency(values.get("frequency")))

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

    def _on_close(self) -> None:
        self.stop_event.set()
        if self.client:
            try:
                self.client.close()
            except Exception:
                pass
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()
