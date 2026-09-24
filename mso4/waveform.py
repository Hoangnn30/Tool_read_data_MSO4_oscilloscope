from __future__ import annotations

from dataclasses import dataclass
import numpy as np


@dataclass(slots=True)
class Waveform:
    channel: str
    time_s: np.ndarray
    volts: np.ndarray

    @property
    def points(self) -> int:
        return int(self.volts.size)

    def measurements(self) -> dict[str, float]:
        if self.volts.size == 0:
            return {
                "min": float("nan"),
                "max": float("nan"),
                "mean": float("nan"),
                "pkpk": float("nan"),
                "rms": float("nan"),
                "frequency": float("nan"),
            }

        y = self.volts.astype(float, copy=False)
        mean = float(np.mean(y))
        rms = float(np.sqrt(np.mean(np.square(y))))
        vmin = float(np.min(y))
        vmax = float(np.max(y))
        frequency = self._estimate_frequency(y)

        return {
            "min": vmin,
            "max": vmax,
            "mean": mean,
            "pkpk": vmax - vmin,
            "rms": rms,
            "frequency": frequency,
        }

    def _estimate_frequency(self, y: np.ndarray) -> float:
        if y.size < 4 or self.time_s.size != y.size:
            return float("nan")

        centered = y - float(np.mean(y))
        if np.ptp(centered) <= 1e-12:
            return float("nan")

        rising = np.flatnonzero((centered[:-1] <= 0.0) & (centered[1:] > 0.0))
        if rising.size < 2:
            return float("nan")

        times = self.time_s[rising]
        periods = np.diff(times)
        periods = periods[periods > 0.0]
        if periods.size == 0:
            return float("nan")

        return float(1.0 / np.median(periods))
