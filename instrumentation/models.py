from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class ConnectionType(str, Enum):
    VISA_TCPIP = "VISA TCPIP"
    TCP_SOCKET = "TCP Socket"
    SERIAL = "Serial COM"
    VISA_GPIB = "VISA GPIB"
    VISA_RESOURCE = "VISA Resource"


class DeviceStatus(str, Enum):
    OFFLINE = "OFFLINE"
    CONNECTING = "CONNECTING"
    ONLINE = "ONLINE"
    ERROR = "ERROR"


SUPPORTED_MODELS = (
    "Tektronix MSO44B",
    "Tektronix MSO24B",
    "Tektronix PWS4323",
    "Keithley 2100",
    "Keithley 2000",
    "Tektronix AWG2000",
    "Generic SCPI",
)


@dataclass(slots=True)
class DeviceConfig:
    name: str
    model: str
    connection_type: ConnectionType
    enabled: bool = True

    ip: str = ""
    port: int | None = None
    com_port: str = ""
    baud_rate: int = 9600
    gpib_address: int | None = None
    visa_resource: str = ""

    timeout_s: float = 5.0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def resource_preview(self) -> str:
        if self.connection_type == ConnectionType.VISA_TCPIP:
            return self.visa_resource or (
                f"TCPIP0::{self.ip}::inst0::INSTR" if self.ip else ""
            )
        if self.connection_type == ConnectionType.TCP_SOCKET:
            return f"{self.ip}:{self.port or ''}"
        if self.connection_type == ConnectionType.SERIAL:
            return f"{self.com_port} @ {self.baud_rate}"
        if self.connection_type == ConnectionType.VISA_GPIB:
            if self.gpib_address is None:
                return ""
            return f"GPIB0::{self.gpib_address}::INSTR"
        return self.visa_resource

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["connection_type"] = self.connection_type.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DeviceConfig":
        values = dict(data)
        values["connection_type"] = ConnectionType(values["connection_type"])
        return cls(**values)
