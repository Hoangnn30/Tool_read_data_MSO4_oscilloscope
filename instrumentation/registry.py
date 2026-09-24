from __future__ import annotations

import json
from pathlib import Path
from threading import RLock

from .base import InstrumentDriver
from .drivers import create_driver
from .models import DeviceConfig, DeviceStatus


class DeviceRegistry:
    MAX_DEVICES = 15

    def __init__(self, config_path: str | Path | None = None):
        self.config_path = Path(
            config_path or Path.home() / ".mso_automation" / "devices.json"
        )
        self._configs: list[DeviceConfig] = []
        self._drivers: dict[str, InstrumentDriver] = {}
        self._lock = RLock()
        self.load()

    @property
    def devices(self) -> list[DeviceConfig]:
        with self._lock:
            return list(self._configs)

    def get(self, name: str) -> DeviceConfig | None:
        with self._lock:
            return next((d for d in self._configs if d.name == name), None)

    def get_driver(self, name: str) -> InstrumentDriver | None:
        with self._lock:
            return self._drivers.get(name)

    def get_status(self, name: str) -> DeviceStatus:
        driver = self.get_driver(name)
        return driver.status if driver else DeviceStatus.OFFLINE

    def online_devices(self) -> list[DeviceConfig]:
        return [
            config
            for config in self.devices
            if self.get_status(config.name) == DeviceStatus.ONLINE
        ]

    def upsert(self, config: DeviceConfig) -> None:
        with self._lock:
            current = self.get(config.name)
            if current is None and len(self._configs) >= self.MAX_DEVICES:
                raise ValueError(
                    f"Maximum {self.MAX_DEVICES} devices are supported."
                )

            if current is None:
                self._configs.append(config)
            else:
                index = self._configs.index(current)
                self._configs[index] = config

            self.save()

    def remove(self, name: str) -> None:
        self.disconnect(name)
        with self._lock:
            self._configs = [d for d in self._configs if d.name != name]
            self.save()

    def connect(self, name: str) -> str:
        config = self.get(name)
        if config is None:
            raise KeyError(name)

        old = self.get_driver(name)
        if old is not None:
            try:
                old.disconnect()
            except Exception:
                pass

        driver = create_driver(config)
        with self._lock:
            self._drivers[name] = driver
        return driver.connect()

    def disconnect(self, name: str) -> None:
        driver = self.get_driver(name)
        if driver:
            try:
                driver.disconnect()
            finally:
                with self._lock:
                    self._drivers.pop(name, None)

    def disconnect_all(self) -> None:
        for config in self.devices:
            self.disconnect(config.name)

    def save(self) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        payload = [config.to_dict() for config in self._configs]
        self.config_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def load(self) -> None:
        if not self.config_path.exists():
            return

        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
            configs = [DeviceConfig.from_dict(item) for item in raw]
            self._configs = configs[: self.MAX_DEVICES]
        except Exception:
            self._configs = []
