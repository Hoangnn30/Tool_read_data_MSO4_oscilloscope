from __future__ import annotations

import threading
import tkinter as tk
from tkinter import messagebox, ttk

try:
    from serial.tools import list_ports
except Exception:
    list_ports = None

from instrumentation import ConnectionType, DeviceConfig, DeviceRegistry, DeviceStatus
from instrumentation.models import SUPPORTED_MODELS


class ConnectionManagerDialog(tk.Toplevel):
    def __init__(self, master, registry: DeviceRegistry, on_changed=None):
        super().__init__(master)
        self.registry = registry
        self.on_changed = on_changed

        self.title("Connection Manager")
        self.geometry("1040x650")
        self.minsize(900, 560)
        self.configure(bg="#0B1118")
        self.transient(master)

        self.name_var = tk.StringVar()
        self.model_var = tk.StringVar(value=SUPPORTED_MODELS[0])
        self.type_var = tk.StringVar(value=ConnectionType.VISA_TCPIP.value)
        self.ip_var = tk.StringVar()
        self.port_var = tk.StringVar(value="4000")
        self.com_var = tk.StringVar()
        self.baud_var = tk.StringVar(value="9600")
        self.gpib_var = tk.StringVar(value="0")
        self.visa_var = tk.StringVar()
        self.timeout_var = tk.StringVar(value="5.0")
        self.status_var = tk.StringVar(value="Ready")

        self._build()
        self.refresh()

    def _build(self) -> None:
        header = tk.Frame(self, bg="#0E151D", height=52)
        header.pack(fill="x")
        header.pack_propagate(False)

        tk.Label(
            header,
            text="Connection Manager",
            bg="#0E151D",
            fg="#F4F7FA",
            font=("Arial", 15, "bold"),
        ).pack(side="left", padx=14)

        tk.Label(
            header,
            text="Maximum 15 instruments",
            bg="#0E151D",
            fg="#7F93A6",
            font=("Arial", 9),
        ).pack(side="right", padx=14)

        body = tk.Frame(self, bg="#0B1118")
        body.pack(fill="both", expand=True, padx=12, pady=10)
        body.grid_columnconfigure(0, weight=3)
        body.grid_columnconfigure(1, weight=2)
        body.grid_rowconfigure(0, weight=1)

        left = tk.Frame(body, bg="#101820", highlightbackground="#263746", highlightthickness=1)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))

        columns = ("name", "model", "type", "resource", "status")
        self.tree = ttk.Treeview(left, columns=columns, show="headings", height=20)
        self.tree.heading("name", text="Name")
        self.tree.heading("model", text="Model")
        self.tree.heading("type", text="Connection")
        self.tree.heading("resource", text="Resource")
        self.tree.heading("status", text="Status")
        self.tree.column("name", width=120, anchor="w")
        self.tree.column("model", width=155, anchor="w")
        self.tree.column("type", width=110, anchor="w")
        self.tree.column("resource", width=240, anchor="w")
        self.tree.column("status", width=85, anchor="center")
        self.tree.pack(fill="both", expand=True, padx=8, pady=8)
        self.tree.bind("<<TreeviewSelect>>", self._load_selected)

        buttons = tk.Frame(left, bg="#101820")
        buttons.pack(fill="x", padx=8, pady=(0, 8))

        ttk.Button(buttons, text="NEW", command=self.clear_form).pack(side="left", padx=2)
        ttk.Button(buttons, text="SAVE", command=self.save_device).pack(side="left", padx=2)
        ttk.Button(buttons, text="REMOVE", command=self.remove_selected).pack(side="left", padx=2)
        ttk.Button(buttons, text="CONNECT", command=self.connect_selected).pack(side="right", padx=2)
        ttk.Button(buttons, text="DISCONNECT", command=self.disconnect_selected).pack(side="right", padx=2)

        right = tk.Frame(body, bg="#101820", highlightbackground="#263746", highlightthickness=1)
        right.grid(row=0, column=1, sticky="nsew")

        tk.Label(
            right,
            text="DEVICE CONFIGURATION",
            bg="#101820",
            fg="#93A8BC",
            font=("Arial", 9, "bold"),
        ).pack(anchor="w", padx=12, pady=(12, 8))

        form = tk.Frame(right, bg="#101820")
        form.pack(fill="x", padx=12)

        self._row(form, 0, "Name", self.name_var)
        self._combo_row(form, 1, "Model", self.model_var, SUPPORTED_MODELS)
        self._combo_row(
            form,
            2,
            "Connection",
            self.type_var,
            [item.value for item in ConnectionType],
            on_change=self._connection_type_changed,
        )
        self._row(form, 3, "IP", self.ip_var)
        self._row(form, 4, "TCP Port", self.port_var)
        self._combo_row(form, 5, "COM Port", self.com_var, self._serial_ports())
        self._combo_row(
            form,
            6,
            "Baud rate",
            self.baud_var,
            ["9600", "19200", "38400", "57600", "115200", "230400", "460800", "921600"],
        )
        self._row(form, 7, "GPIB address", self.gpib_var)
        self._row(form, 8, "VISA resource", self.visa_var)
        self._row(form, 9, "Timeout (s)", self.timeout_var)
        form.grid_columnconfigure(1, weight=1)

        self.preview_var = tk.StringVar(value="")
        tk.Label(
            right,
            text="RESOURCE PREVIEW",
            bg="#101820",
            fg="#70859A",
            font=("Arial", 8, "bold"),
        ).pack(anchor="w", padx=12, pady=(14, 4))
        tk.Label(
            right,
            textvariable=self.preview_var,
            bg="#091019",
            fg="#DDE7F0",
            font=("Menlo", 9),
            anchor="w",
            justify="left",
            wraplength=360,
            padx=8,
            pady=8,
        ).pack(fill="x", padx=12)

        ttk.Button(right, text="TEST CONNECTION", command=self.test_form_connection).pack(
            fill="x", padx=12, pady=(12, 4)
        )

        tk.Label(
            right,
            textvariable=self.status_var,
            bg="#101820",
            fg="#8EA2B5",
            anchor="w",
            justify="left",
            wraplength=360,
        ).pack(fill="x", padx=12, pady=8)

        for var in (
            self.name_var,
            self.model_var,
            self.type_var,
            self.ip_var,
            self.port_var,
            self.com_var,
            self.baud_var,
            self.gpib_var,
            self.visa_var,
        ):
            var.trace_add("write", lambda *_: self._update_preview())

        self._connection_type_changed()

    def _row(self, parent, row: int, label: str, var: tk.StringVar) -> None:
        tk.Label(parent, text=label, bg="#101820", fg="#C7D2DC").grid(
            row=row, column=0, sticky="w", pady=4
        )
        entry = tk.Entry(
            parent,
            textvariable=var,
            bg="#091019",
            fg="#F0F4F8",
            insertbackground="#F0F4F8",
            relief="flat",
            highlightthickness=1,
            highlightbackground="#2D3C4A",
        )
        entry.grid(row=row, column=1, sticky="ew", padx=(8, 0), pady=4, ipady=4)

    def _combo_row(
        self,
        parent,
        row: int,
        label: str,
        var: tk.StringVar,
        values,
        on_change=None,
    ) -> None:
        tk.Label(parent, text=label, bg="#101820", fg="#C7D2DC").grid(
            row=row, column=0, sticky="w", pady=4
        )
        combo = ttk.Combobox(parent, textvariable=var, values=list(values), state="readonly")
        combo.grid(row=row, column=1, sticky="ew", padx=(8, 0), pady=4)
        if on_change:
            combo.bind("<<ComboboxSelected>>", lambda _e: on_change())

    def _serial_ports(self) -> list[str]:
        if list_ports is None:
            return []
        try:
            return [port.device for port in list_ports.comports()]
        except Exception:
            return []

    def _connection_type_changed(self) -> None:
        self._update_preview()

    def _config_from_form(self) -> DeviceConfig:
        name = self.name_var.get().strip()
        if not name:
            raise ValueError("Device name is required.")

        ctype = ConnectionType(self.type_var.get())

        port = None
        if self.port_var.get().strip():
            port = int(self.port_var.get())

        gpib = None
        if self.gpib_var.get().strip():
            gpib = int(self.gpib_var.get())

        config = DeviceConfig(
            name=name,
            model=self.model_var.get().strip(),
            connection_type=ctype,
            ip=self.ip_var.get().strip(),
            port=port,
            com_port=self.com_var.get().strip(),
            baud_rate=int(self.baud_var.get() or "9600"),
            gpib_address=gpib,
            visa_resource=self.visa_var.get().strip(),
            timeout_s=float(self.timeout_var.get() or "5"),
        )

        if ctype in {ConnectionType.VISA_TCPIP, ConnectionType.TCP_SOCKET} and not config.ip:
            raise ValueError("IP address is required.")
        if ctype == ConnectionType.SERIAL and not config.com_port:
            raise ValueError("COM port is required.")

        return config

    def _update_preview(self) -> None:
        try:
            self.preview_var.set(self._config_from_form().resource_preview)
        except Exception:
            self.preview_var.set("")

    def clear_form(self) -> None:
        count = len(self.registry.devices) + 1
        self.name_var.set(f"Device{count}")
        self.model_var.set(SUPPORTED_MODELS[0])
        self.type_var.set(ConnectionType.VISA_TCPIP.value)
        self.ip_var.set("")
        self.port_var.set("4000")
        self.com_var.set("")
        self.baud_var.set("9600")
        self.gpib_var.set("0")
        self.visa_var.set("")
        self.timeout_var.set("5.0")
        self.status_var.set("New device")

    def save_device(self) -> None:
        try:
            config = self._config_from_form()
            self.registry.upsert(config)
            self.status_var.set(f"Saved {config.name}")
            self.refresh()
            self._notify_changed()
        except Exception as exc:
            messagebox.showerror("Save device", str(exc), parent=self)

    def remove_selected(self) -> None:
        name = self._selected_name()
        if not name:
            return
        if not messagebox.askyesno("Remove device", f"Remove {name}?", parent=self):
            return
        self.registry.remove(name)
        self.refresh()
        self.clear_form()
        self._notify_changed()

    def connect_selected(self) -> None:
        name = self._selected_name() or self.name_var.get().strip()
        if not name:
            return
        self._connect_name(name)

    def disconnect_selected(self) -> None:
        name = self._selected_name()
        if not name:
            return
        self.registry.disconnect(name)
        self.status_var.set(f"{name}: disconnected")
        self.refresh()
        self._notify_changed()

    def test_form_connection(self) -> None:
        try:
            config = self._config_from_form()
            self.registry.upsert(config)
        except Exception as exc:
            messagebox.showerror("Connection", str(exc), parent=self)
            return
        self._connect_name(config.name)

    def _connect_name(self, name: str) -> None:
        self.status_var.set(f"{name}: connecting...")

        def worker() -> None:
            try:
                idn = self.registry.connect(name)
                self.after(0, lambda: self._connection_done(name, True, idn))
            except Exception as exc:
                self.after(0, lambda: self._connection_done(name, False, str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _connection_done(self, name: str, ok: bool, message: str) -> None:
        if ok:
            self.status_var.set(f"{name}: ONLINE\n{message}")
        else:
            self.status_var.set(f"{name}: ERROR\n{message}")
        self.refresh()
        self._notify_changed()

    def refresh(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)

        for config in self.registry.devices:
            status = self.registry.get_status(config.name)
            self.tree.insert(
                "",
                "end",
                iid=config.name,
                values=(
                    config.name,
                    config.model,
                    config.connection_type.value,
                    config.resource_preview,
                    status.value,
                ),
                tags=(status.value,),
            )

        self.tree.tag_configure(DeviceStatus.ONLINE.value, foreground="#55D98B")
        self.tree.tag_configure(DeviceStatus.OFFLINE.value, foreground="#9AA7B3")
        self.tree.tag_configure(DeviceStatus.ERROR.value, foreground="#FF6B75")
        self.tree.tag_configure(DeviceStatus.CONNECTING.value, foreground="#F0B34B")

    def _selected_name(self) -> str:
        selection = self.tree.selection()
        return str(selection[0]) if selection else ""

    def _load_selected(self, _event=None) -> None:
        name = self._selected_name()
        config = self.registry.get(name)
        if not config:
            return

        self.name_var.set(config.name)
        self.model_var.set(config.model)
        self.type_var.set(config.connection_type.value)
        self.ip_var.set(config.ip)
        self.port_var.set("" if config.port is None else str(config.port))
        self.com_var.set(config.com_port)
        self.baud_var.set(str(config.baud_rate))
        self.gpib_var.set("" if config.gpib_address is None else str(config.gpib_address))
        self.visa_var.set(config.visa_resource)
        self.timeout_var.set(str(config.timeout_s))
        self.status_var.set(
            f"{config.name}: {self.registry.get_status(config.name).value}"
        )

    def _notify_changed(self) -> None:
        if self.on_changed:
            try:
                self.on_changed()
            except Exception:
                pass
