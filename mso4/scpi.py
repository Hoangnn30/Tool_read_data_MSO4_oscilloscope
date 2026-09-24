from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pyvisa
from pyvisa.resources import MessageBasedResource

from .waveform import Waveform


class MSO4Error(RuntimeError):
    pass


@dataclass(slots=True)
class WaveformPreamble:
    xincr: float
    xzero: float
    pt_off: float
    ymult: float
    yzero: float
    yoff: float


class MSO4Client:
    """Tektronix 4 Series MSO client over VISA TCPIP/LAN (VXI-11/LXI).

    Resource format:
        TCPIP0::<ip-address>::inst0::INSTR

    This intentionally does not use raw TCP port 80/4000.
    """

    def __init__(
        self,
        host: str,
        timeout: float = 5.0,
        backend: str = "@py",
        resource_name: str | None = None,
    ):
        self.host = host.strip()
        self.timeout = float(timeout)
        self.backend = backend
        self.resource_name = (
            resource_name.strip()
            if resource_name
            else f"TCPIP0::{self.host}::inst0::INSTR"
        )
        self._rm: Optional[pyvisa.ResourceManager] = None
        self._instrument: Optional[MessageBasedResource] = None
        self._lock = threading.RLock()

    @property
    def connected(self) -> bool:
        return self._instrument is not None

    def connect(self) -> str:
        self.close()

        try:
            self._rm = pyvisa.ResourceManager(self.backend)
            inst = self._rm.open_resource(self.resource_name)

            if not isinstance(inst, MessageBasedResource):
                inst.close()
                raise MSO4Error(
                    f"VISA resource is not message-based: {self.resource_name}"
                )

            inst.timeout = int(self.timeout * 1000)
            inst.write_termination = "\n"
            inst.read_termination = "\n"
            inst.chunk_size = 1024 * 1024
            self._instrument = inst

            # Clear stale protocol/input state when supported by the VISA backend.
            try:
                inst.clear()
            except Exception:
                pass

            idn = self.query("*IDN?")
            if not idn:
                raise MSO4Error(
                    f"Connected to {self.resource_name}, but *IDN? returned no data."
                )

            return idn

        except Exception as exc:
            self.close()
            if isinstance(exc, MSO4Error):
                raise
            raise MSO4Error(
                "TCPIP/LAN VISA connection failed. "
                f"Resource: {self.resource_name}. Error: {exc}"
            ) from exc

    def close(self) -> None:
        inst, self._instrument = self._instrument, None
        if inst is not None:
            try:
                inst.close()
            except Exception:
                pass

        rm, self._rm = self._rm, None
        if rm is not None:
            try:
                rm.close()
            except Exception:
                pass

    def write(self, command: str) -> None:
        with self._lock:
            inst = self._require_instrument()
            try:
                inst.write(command.rstrip("\r\n"))
            except Exception as exc:
                raise MSO4Error(f"SCPI write failed: {exc}") from exc

    def query(self, command: str) -> str:
        with self._lock:
            inst = self._require_instrument()
            try:
                return str(inst.query(command.rstrip("\r\n"))).strip()
            except Exception as exc:
                raise MSO4Error(f"SCPI query failed ({command}): {exc}") from exc

    def query_float(self, command: str) -> float:
        raw = self.query(command)
        try:
            return float(raw)
        except ValueError as exc:
            raise MSO4Error(
                f"Invalid numeric response for {command}: {raw!r}"
            ) from exc

    def get_waveform(
        self,
        channel: str,
        start: int = 1,
        stop: int = 10000,
    ) -> Waveform:
        ch = channel.upper().strip()
        if ch not in {"CH1", "CH2", "CH3", "CH4"}:
            raise ValueError(f"Unsupported channel: {channel}")

        start = max(1, int(start))
        stop = max(start, int(stop))

        with self._lock:
            inst = self._require_instrument()

            try:
                # Tektronix waveform-transfer setup.
                inst.write(f"DATA:SOURCE {ch}")
                inst.write(f"DATA:START {start}")
                inst.write(f"DATA:STOP {stop}")
                inst.write("DATA:ENCDG RIBINARY")
                inst.write("DATA:WIDTH 1")

                pre = self._read_preamble()

                # CURVE? returns an IEEE-488.2 definite-length binary block.
                # datatype='b' = signed 8-bit samples, matching DATA:WIDTH 1.
                samples = inst.query_binary_values(
                    "CURVE?",
                    datatype="b",
                    is_big_endian=True,
                    container=np.array,
                )

            except Exception as exc:
                raise MSO4Error(
                    f"Waveform read failed on {ch} through {self.resource_name}: {exc}"
                ) from exc

        raw = np.asarray(samples, dtype=np.float64)
        n = raw.size

        x = pre.xzero + (
            np.arange(n, dtype=np.float64) - pre.pt_off
        ) * pre.xincr
        y = (raw - pre.yoff) * pre.ymult + pre.yzero

        return Waveform(channel=ch, time_s=x, volts=y)

    def _read_preamble(self) -> WaveformPreamble:
        return WaveformPreamble(
            xincr=self.query_float("WFMOUTPRE:XINCR?"),
            xzero=self.query_float("WFMOUTPRE:XZERO?"),
            pt_off=self.query_float("WFMOUTPRE:PT_OFF?"),
            ymult=self.query_float("WFMOUTPRE:YMULT?"),
            yzero=self.query_float("WFMOUTPRE:YZERO?"),
            yoff=self.query_float("WFMOUTPRE:YOFF?"),
        )

    def _require_instrument(self) -> MessageBasedResource:
        if self._instrument is None:
            raise MSO4Error(
                "Not connected. Use a VISA TCPIP/LAN resource such as "
                f"TCPIP0::{self.host}::inst0::INSTR."
            )
        return self._instrument

    def __enter__(self) -> "MSO4Client":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
