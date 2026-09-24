from __future__ import annotations

import re
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
    """Tektronix 4 Series MSO client over VISA TCPIP/LAN (VXI-11/LXI)."""

    CHANNELS = {"CH1", "CH2", "CH3", "CH4"}

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
        self._last_transfer_info: dict[str, dict[str, object]] = {}

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
            inst.chunk_size = 4 * 1024 * 1024
            self._instrument = inst

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

    @staticmethod
    def _parse_number(raw: str) -> float:
        """Accept both plain SCPI numbers and verbose Tektronix responses."""
        text = raw.strip().strip('"')
        try:
            return float(text)
        except ValueError:
            matches = re.findall(
                r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?",
                text,
            )
            if not matches:
                raise
            return float(matches[-1])

    def query_float(self, command: str) -> float:
        raw = self.query(command)
        try:
            return self._parse_number(raw)
        except ValueError as exc:
            raise MSO4Error(
                f"Invalid numeric response for {command}: {raw!r}"
            ) from exc

    def query_int(self, command: str) -> int:
        return int(round(self.query_float(command)))

    def prepare_acquisition(self, channels: list[str]) -> None:
        """Make selected physical channels visible and start continuous acquisition.

        This mirrors the user's intent when pressing RUN in the desktop client.
        """
        clean = [self._validate_channel(ch) for ch in channels]
        with self._lock:
            inst = self._require_instrument()

            for ch in clean:
                try:
                    inst.write(f"DISPLAY:WAVEVIEW1:{ch}:STATE ON")
                except Exception:
                    # Older firmware also supports the global state command.
                    try:
                        inst.write(f"DISPLAY:GLOBAL:{ch}:STATE ON")
                    except Exception:
                        pass

            # Force normal continuous acquisition so CURVE? has fresh records.
            try:
                inst.write("ACQUIRE:STOPAFTER RUNSTOP")
            except Exception:
                pass
            try:
                inst.write("ACQUIRE:STATE RUN")
            except Exception as exc:
                raise MSO4Error(
                    f"Could not start MSO4 acquisition: {exc}"
                ) from exc

    def get_record_length(self) -> int | None:
        try:
            value = self.query_int("HORIZONTAL:RECORDLENGTH?")
            return value if value > 0 else None
        except Exception:
            return None

    def get_last_transfer_info(self, channel: str) -> dict[str, object]:
        return dict(self._last_transfer_info.get(channel.upper(), {}))

    def get_waveform(
        self,
        channel: str,
        start: int = 1,
        stop: int = 10000,
    ) -> Waveform:
        ch = self._validate_channel(channel)
        start = max(1, int(start))
        stop = max(start, int(stop))

        with self._lock:
            inst = self._require_instrument()

            # Never request beyond the physical acquisition record.
            record_length = self.get_record_length()
            if record_length is not None:
                if start > record_length:
                    start = 1
                stop = min(stop, record_length)

            # First try the fast path: signed 8-bit binary.
            try:
                samples, pre, transfer_points = self._read_curve_binary(
                    inst, ch, start, stop
                )
                mode = "BINARY"
            except Exception as binary_error:
                self._recover_session(inst)

                # ASCII is slower but highly tolerant and is an important
                # compatibility fallback for firmware/backend differences.
                try:
                    samples, pre, transfer_points = self._read_curve_ascii(
                        inst, ch, start, stop
                    )
                    mode = "ASCII"
                except Exception as ascii_error:
                    raise MSO4Error(
                        f"{ch} waveform transfer failed. "
                        f"Binary: {binary_error}. ASCII fallback: {ascii_error}"
                    ) from ascii_error

        raw = np.asarray(samples, dtype=np.float64)
        if raw.size == 0:
            raise MSO4Error(f"{ch} returned zero waveform points.")

        n = raw.size
        x = pre.xzero + (
            np.arange(n, dtype=np.float64) - pre.pt_off
        ) * pre.xincr
        y = (raw - pre.yoff) * pre.ymult + pre.yzero

        if not np.all(np.isfinite(x)) or not np.all(np.isfinite(y)):
            raise MSO4Error(f"{ch} waveform contains non-finite values.")

        self._last_transfer_info[ch] = {
            "mode": mode,
            "points": int(n),
            "reported_points": int(transfer_points),
            "record_length": int(record_length) if record_length else None,
            "xincr": float(pre.xincr),
        }

        return Waveform(channel=ch, time_s=x, volts=y)

    def _configure_transfer(
        self,
        inst: MessageBasedResource,
        ch: str,
        start: int,
        stop: int,
        encoding: str,
    ) -> tuple[WaveformPreamble, int]:
        inst.write(f"DATA:SOURCE {ch}")
        inst.write(f"DATA:START {start}")
        inst.write(f"DATA:STOP {stop}")

        if encoding == "BINARY":
            inst.write("DATA:ENCDG RIBINARY")
            inst.write("DATA:WIDTH 1")
        else:
            inst.write("DATA:ENCDG ASCII")

        pre = self._read_preamble()

        try:
            transfer_points = self.query_int("WFMOUTPRE:NR_PT?")
        except Exception:
            transfer_points = stop - start + 1

        if transfer_points <= 0:
            raise MSO4Error(
                f"{ch} reports no transferable waveform points."
            )

        return pre, transfer_points

    def _read_curve_binary(
        self,
        inst: MessageBasedResource,
        ch: str,
        start: int,
        stop: int,
    ) -> tuple[np.ndarray, WaveformPreamble, int]:
        pre, transfer_points = self._configure_transfer(
            inst, ch, start, stop, "BINARY"
        )

        samples = inst.query_binary_values(
            "CURVE?",
            datatype="b",
            is_big_endian=True,
            container=np.array,
        )
        samples = np.asarray(samples)

        if samples.size == 0:
            raise MSO4Error("CURVE? returned an empty binary block.")

        return samples, pre, transfer_points

    def _read_curve_ascii(
        self,
        inst: MessageBasedResource,
        ch: str,
        start: int,
        stop: int,
    ) -> tuple[np.ndarray, WaveformPreamble, int]:
        pre, transfer_points = self._configure_transfer(
            inst, ch, start, stop, "ASCII"
        )

        samples = inst.query_ascii_values(
            "CURVE?",
            converter="f",
            separator=",",
            container=np.array,
        )
        samples = np.asarray(samples)

        if samples.size == 0:
            raise MSO4Error("CURVE? returned no ASCII samples.")

        return samples, pre, transfer_points

    def _read_preamble(self) -> WaveformPreamble:
        return WaveformPreamble(
            xincr=self.query_float("WFMOUTPRE:XINCR?"),
            xzero=self.query_float("WFMOUTPRE:XZERO?"),
            pt_off=self.query_float("WFMOUTPRE:PT_OFF?"),
            ymult=self.query_float("WFMOUTPRE:YMULT?"),
            yzero=self.query_float("WFMOUTPRE:YZERO?"),
            yoff=self.query_float("WFMOUTPRE:YOFF?"),
        )

    @staticmethod
    def _recover_session(inst: MessageBasedResource) -> None:
        try:
            inst.clear()
        except Exception:
            pass

    def _validate_channel(self, channel: str) -> str:
        ch = channel.upper().strip()
        if ch not in self.CHANNELS:
            raise ValueError(f"Unsupported channel: {channel}")
        return ch

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
