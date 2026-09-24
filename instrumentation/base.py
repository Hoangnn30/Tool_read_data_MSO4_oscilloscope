from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from .models import DeviceConfig, DeviceStatus


class InstrumentDriver(ABC):
    """Stable interface used by Get Data and automation scripts."""

    capabilities: frozenset[str] = frozenset()

    def __init__(self, config: DeviceConfig):
        self.config = config
        self.status = DeviceStatus.OFFLINE
        self.idn = ""

    @abstractmethod
    def connect(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def disconnect(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def write(self, command: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def query(self, command: str) -> str:
        raise NotImplementedError

    def get_data(self, **kwargs) -> Any:
        raise NotImplementedError(
            f"{self.config.model} does not implement get_data() yet."
        )

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def set_value(self, name: str, value: Any, **kwargs) -> None:
        raise NotImplementedError(
            f"{self.config.model} does not implement set_value({name!r})."
        )
