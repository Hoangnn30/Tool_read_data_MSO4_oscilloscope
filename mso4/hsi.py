from __future__ import annotations

import threading
from typing import Iterable

import numpy as np

from .waveform import Waveform


class TekHSIUnavailable(RuntimeError):
    pass


class TekHSIWaveformClient:
    """High-speed waveform transport for supported Tektronix oscilloscopes.

    Control remains on SCPI/VISA. TekHSI is used only for waveform data,
    matching Tektronix's recommended automation architecture.
    """

    def __init__(self, host: str, port: int = 5000):
        self.host = host.strip()
        self.port = int(port)
        self._ctx = None
        self._connection = None
        self._channels: tuple[str, ...] = ()
        self._lock = threading.RLock()

    @staticmethod
    def available() -> bool:
        try:
            import tekhsi  # noqa: F401
            import tm_data_types  # noqa: F401
            return True
        except Exception:
            return False

    @property
    def connected(self) -> bool:
        return self._connection is not None

    @property
    def channels(self) -> tuple[str, ...]:
        return self._channels

    def connect(self, channels: Iterable[str]) -> None:
        normalized = tuple(
            dict.fromkeys(str(ch).strip().lower() for ch in channels)
        )
        if not normalized:
            raise ValueError("TekHSI requires at least one waveform source.")

        with self._lock:
            if self.connected and normalized == self._channels:
                return

            self.close()

            try:
                from tekhsi import TekHSIConnect
            except Exception as exc:
                raise TekHSIUnavailable(
                    "TekHSI Python package is not installed in this environment."
                ) from exc

            try:
                ctx = TekHSIConnect(
                    f"{self.host}:{self.port}",
                    list(normalized),
                )
                connection = ctx.__enter__()
            except Exception as exc:
                raise TekHSIUnavailable(
                    f"Cannot connect TekHSI to {self.host}:{self.port}: {exc}"
                ) from exc

            self._ctx = ctx
            self._connection = connection
            self._channels = normalized

    def close(self) -> None:
        with self._lock:
            ctx, self._ctx = self._ctx, None
            self._connection = None
            self._channels = ()

            if ctx is not None:
                try:
                    ctx.__exit__(None, None, None)
                except Exception:
                    pass

    def get_waveforms(self, channels: Iterable[str]) -> dict[str, Waveform]:
        requested = tuple(
            dict.fromkeys(str(ch).strip().lower() for ch in channels)
        )
        if not requested:
            return {}

        with self._lock:
            self.connect(requested)
            if self._connection is None:
                raise TekHSIUnavailable("TekHSI is not connected.")

            result: dict[str, Waveform] = {}

            # access_data() waits for one coherent acquisition. All requested
            # channel waveforms are then fetched from that same acquisition.
            with self._connection.access_data():
                for source in requested:
                    analog = self._connection.get_data(source)

                    time_s = np.asarray(
                        analog.normalized_horizontal_values,
                        dtype=np.float64,
                    )
                    volts = np.asarray(
                        analog.normalized_vertical_values,
                        dtype=np.float64,
                    )

                    if time_s.size == 0 or volts.size == 0:
                        continue

                    count = min(time_s.size, volts.size)
                    ch = source.upper()
                    result[ch] = Waveform(
                        channel=ch,
                        time_s=time_s[:count],
                        volts=volts[:count],
                    )

            return result

    def __enter__(self) -> "TekHSIWaveformClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
