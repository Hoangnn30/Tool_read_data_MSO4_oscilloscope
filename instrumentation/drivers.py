from __future__ import annotations

from typing import Any

import pyvisa

from mso4 import MSO4Client

from .base import InstrumentDriver
from .models import ConnectionType, DeviceConfig, DeviceStatus


class MSO4InstrumentDriver(InstrumentDriver):
    capabilities = frozenset(
        {
            "waveform",
            "run_stop",
            "channels",
            "time_div",
            "volt_div",
            "trigger",
            "measurements",
            "raw_scpi",
        }
    )

    def __init__(self, config: DeviceConfig):
        super().__init__(config)
        self.client: MSO4Client | None = None

    def connect(self) -> str:
        self.status = DeviceStatus.CONNECTING
        try:
            self.client = MSO4Client(
                self.config.ip,
                timeout=self.config.timeout_s,
                resource_name=(self.config.visa_resource or None),
            )
            self.idn = self.client.connect()
            self.status = DeviceStatus.ONLINE
            return self.idn
        except Exception:
            self.status = DeviceStatus.ERROR
            raise

    def disconnect(self) -> None:
        if self.client:
            self.client.close()
        self.client = None
        self.status = DeviceStatus.OFFLINE

    def write(self, command: str) -> None:
        if not self.client:
            raise RuntimeError("Device is not connected.")
        self.client.write(command)

    def query(self, command: str) -> str:
        if not self.client:
            raise RuntimeError("Device is not connected.")
        return self.client.query(command)

    def start(self) -> None:
        if not self.client:
            raise RuntimeError("Device is not connected.")
        self.client.run_acquisition()

    def stop(self) -> None:
        if not self.client:
            raise RuntimeError("Device is not connected.")
        self.client.stop_acquisition()

    def get_data(self, **kwargs) -> Any:
        if not self.client:
            raise RuntimeError("Device is not connected.")
        channel = str(kwargs.get("channel", "CH1"))
        points = int(kwargs.get("points", 5000))
        exact = bool(kwargs.get("exact", False))
        if exact:
            return self.client.get_waveform(channel, 1, points)
        return self.client.get_waveform_fast(channel, 1, points)


class GenericVisaScpiDriver(InstrumentDriver):
    capabilities = frozenset({"raw_scpi", "scalar_read"})

    def __init__(self, config: DeviceConfig):
        super().__init__(config)
        self._rm: pyvisa.ResourceManager | None = None
        self._resource = None

    def _resource_name(self) -> str:
        if self.config.visa_resource:
            return self.config.visa_resource

        if self.config.connection_type == ConnectionType.VISA_TCPIP:
            return f"TCPIP0::{self.config.ip}::inst0::INSTR"

        if self.config.connection_type == ConnectionType.VISA_GPIB:
            if self.config.gpib_address is None:
                raise ValueError("GPIB address is required.")
            return f"GPIB0::{self.config.gpib_address}::INSTR"

        if self.config.connection_type == ConnectionType.SERIAL:
            if not self.config.com_port:
                raise ValueError("COM port is required.")
            return f"ASRL::{self.config.com_port}::INSTR"

        raise ValueError(
            f"No VISA resource mapping for {self.config.connection_type.value}."
        )

    def connect(self) -> str:
        self.status = DeviceStatus.CONNECTING
        try:
            self._rm = pyvisa.ResourceManager("@py")
            self._resource = self._rm.open_resource(self._resource_name())
            self._resource.timeout = int(self.config.timeout_s * 1000)
            self._resource.write_termination = "\n"
            self._resource.read_termination = "\n"

            try:
                self.idn = str(self._resource.query("*IDN?")).strip()
            except Exception:
                self.idn = self.config.model

            self.status = DeviceStatus.ONLINE
            return self.idn
        except Exception:
            self.status = DeviceStatus.ERROR
            self.disconnect()
            self.status = DeviceStatus.ERROR
            raise

    def disconnect(self) -> None:
        if self._resource is not None:
            try:
                self._resource.close()
            except Exception:
                pass
        self._resource = None

        if self._rm is not None:
            try:
                self._rm.close()
            except Exception:
                pass
        self._rm = None

        if self.status != DeviceStatus.ERROR:
            self.status = DeviceStatus.OFFLINE

    def write(self, command: str) -> None:
        if self._resource is None:
            raise RuntimeError("Device is not connected.")
        self._resource.write(command)

    def query(self, command: str) -> str:
        if self._resource is None:
            raise RuntimeError("Device is not connected.")
        return str(self._resource.query(command)).strip()

    def get_data(self, **kwargs) -> Any:
        command = str(kwargs.get("command", "READ?"))
        raw = self.query(command)
        try:
            return float(raw)
        except ValueError:
            return raw


def create_driver(config: DeviceConfig) -> InstrumentDriver:
    model = config.model.upper()

    if "MSO44" in model or "MSO24" in model:
        return MSO4InstrumentDriver(config)

    return GenericVisaScpiDriver(config)
