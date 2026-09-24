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
    COUPLINGS = {"DC", "AC"}
    TRIGGER_SLOPES = {"RISE", "FALL", "EITHER"}
    TRIGGER_MODES = {"AUTO", "NORMAL"}
    ACQUIRE_MODES = {"SAMPLE", "PEAKDETECT", "HIRES", "AVERAGE", "ENVELOPE"}

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

    @staticmethod
    def _parse_enum(raw: str) -> str:
        text = raw.strip().strip('"').rstrip(";")
        if " " in text:
            text = text.split()[-1]
        elif ":" in text:
            text = text.split(":")[-1]
        return text.strip().upper()

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

    def query_enum(self, command: str) -> str:
        return self._parse_enum(self.query(command))

    # ------------------------------------------------------------------
    # Front-panel style controls
    # ------------------------------------------------------------------

    def set_channel_state(self, channel: str, enabled: bool) -> None:
        ch = self._validate_channel(channel)
        self.write(f"DISPLAY:WAVEVIEW1:{ch}:STATE {'ON' if enabled else 'OFF'}")

    def set_channel_scale(self, channel: str, volts_per_div: float) -> None:
        ch = self._validate_channel(channel)
        value = float(volts_per_div)
        if value <= 0:
            raise ValueError("Vertical scale must be > 0.")
        self.write(f"{ch}:SCALE {value:.12g}")

    def set_channel_position(self, channel: str, divisions: float) -> None:
        ch = self._validate_channel(channel)
        self.write(f"{ch}:POSITION {float(divisions):.12g}")

    def set_channel_offset(self, channel: str, volts: float) -> None:
        ch = self._validate_channel(channel)
        self.write(f"{ch}:OFFSET {float(volts):.12g}")

    def set_channel_coupling(self, channel: str, coupling: str) -> None:
        ch = self._validate_channel(channel)
        value = coupling.upper().strip()
        if value not in self.COUPLINGS:
            raise ValueError(f"Unsupported coupling: {coupling}")
        self.write(f"{ch}:COUPLING {value}")

    def set_horizontal_scale(self, seconds_per_div: float) -> None:
        value = float(seconds_per_div)
        if value <= 0:
            raise ValueError("Horizontal scale must be > 0.")
        self.write(f"HORIZONTAL:MODE:SCALE {value:.12g}")

    def set_horizontal_position(self, percent: float) -> None:
        value = max(0.0, min(100.0, float(percent)))
        self.write(f"HORIZONTAL:POSITION {value:.12g}")

    def set_record_length(self, points: int, preserve_scale: bool = True) -> int:
        value = max(1000, int(points))
        scale = None
        if preserve_scale:
            try:
                scale = self.query_float("HORIZONTAL:MODE:SCALE?")
            except Exception:
                scale = None

        self.write("HORIZONTAL:MODE MANUAL")
        self.write(f"HORIZONTAL:MODE:RECORDLENGTH {value}")

        if scale is not None:
            try:
                self.write(f"HORIZONTAL:MODE:SCALE {scale:.12g}")
            except Exception:
                pass

        actual = self.get_record_length()
        return actual or value

    def set_trigger_source(self, channel: str) -> None:
        ch = self._validate_channel(channel)
        self.write(f"TRIGGER:A:EDGE:SOURCE {ch}")

    def set_trigger_level(self, channel: str, volts: float) -> None:
        ch = self._validate_channel(channel)
        self.write(f"TRIGGER:A:LEVEL:{ch} {float(volts):.12g}")

    def set_trigger_slope(self, slope: str) -> None:
        value = slope.upper().strip()
        if value not in self.TRIGGER_SLOPES:
            raise ValueError(f"Unsupported trigger slope: {slope}")
        self.write(f"TRIGGER:A:EDGE:SLOPE {value}")

    def set_trigger_mode(self, mode: str) -> None:
        value = mode.upper().strip()
        if value == "NORM":
            value = "NORMAL"
        if value not in self.TRIGGER_MODES:
            raise ValueError(f"Unsupported trigger mode: {mode}")
        self.write(f"TRIGGER:A:MODE {value}")

    def trigger_level_50_percent(self) -> None:
        self.write("TRIGGER:A SETLEVEL")

    def force_trigger(self) -> None:
        self.write("TRIGGER FORCE")

    def set_acquire_mode(self, mode: str) -> None:
        value = mode.upper().replace(" ", "").strip()
        aliases = {
            "PEAK": "PEAKDETECT",
            "PEAKDETECT": "PEAKDETECT",
            "HIRES": "HIRES",
            "AVERAGE": "AVERAGE",
            "ENVELOPE": "ENVELOPE",
            "SAMPLE": "SAMPLE",
        }
        value = aliases.get(value, value)
        if value not in self.ACQUIRE_MODES:
            raise ValueError(f"Unsupported acquisition mode: {mode}")
        self.write(f"ACQUIRE:MODE {value}")

    def set_average_count(self, count: int) -> None:
        self.write(f"ACQUIRE:NUMAVG {max(2, min(10240, int(count)))}")

    def run_acquisition(self) -> None:
        self.write("ACQUIRE:STOPAFTER RUNSTOP")
        self.write("ACQUIRE:STATE RUN")

    def stop_acquisition(self) -> None:
        self.write("ACQUIRE:STATE STOP")

    def single_acquisition(self) -> None:
        self.write("ACQUIRE:STOPAFTER SEQUENCE")
        self.write("ACQUIRE:STATE RUN")

    def autoset(self) -> None:
        self.write("AUTOSET EXECUTE")

    def factory_default(self) -> None:
        self.write("FACTORY")

    def prepare_acquisition(
        self,
        channels: list[str],
        fast_record_length: int | None = None,
    ) -> None:
        clean = [self._validate_channel(ch) for ch in channels]

        if fast_record_length is not None:
            try:
                self.set_record_length(fast_record_length, preserve_scale=True)
            except Exception:
                # Fast mode is an optimization; waveform acquisition can still work
                # even if firmware rejects the record-length adjustment.
                pass

        for ch in self.CHANNELS:
            try:
                self.set_channel_state(ch, ch in clean)
            except Exception:
                pass

        self.run_acquisition()

    def get_record_length(self) -> int | None:
        for command in (
            "HORIZONTAL:MODE:RECORDLENGTH?",
            "HORIZONTAL:RECORDLENGTH?",
        ):
            try:
                value = self.query_int(command)
                if value > 0:
                    return value
            except Exception:
                pass
        return None

    def get_scope_settings(self) -> dict[str, object]:
        """Read a compact set of front-panel settings.

        Intended for connect/refresh, not every waveform frame.
        """
        result: dict[str, object] = {}

        def qfloat(key: str, command: str) -> None:
            try:
                result[key] = self.query_float(command)
            except Exception:
                pass

        def qenum(key: str, command: str) -> None:
            try:
                result[key] = self.query_enum(command)
            except Exception:
                pass

        qfloat("horizontal_scale", "HORIZONTAL:MODE:SCALE?")
        qfloat("horizontal_position", "HORIZONTAL:POSITION?")
        qenum("trigger_source", "TRIGGER:A:EDGE:SOURCE?")
        qenum("trigger_slope", "TRIGGER:A:EDGE:SLOPE?")
        qenum("trigger_mode", "TRIGGER:A:MODE?")
        qenum("acquire_mode", "ACQUIRE:MODE?")

        record = self.get_record_length()
        if record is not None:
            result["record_length"] = record

        trigger_source = str(result.get("trigger_source", "CH1"))
        if trigger_source not in self.CHANNELS:
            trigger_source = "CH1"
        qfloat("trigger_level", f"TRIGGER:A:LEVEL:{trigger_source}?")

        channels: dict[str, dict[str, object]] = {}
        for ch in sorted(self.CHANNELS):
            values: dict[str, object] = {}
            try:
                values["enabled"] = bool(
                    self.query_int(f"DISPLAY:WAVEVIEW1:{ch}:STATE?")
                )
            except Exception:
                pass
            try:
                values["scale"] = self.query_float(f"{ch}:SCALE?")
            except Exception:
                pass
            try:
                values["position"] = self.query_float(f"{ch}:POSITION?")
            except Exception:
                pass
            try:
                values["offset"] = self.query_float(f"{ch}:OFFSET?")
            except Exception:
                pass
            try:
                values["coupling"] = self.query_enum(f"{ch}:COUPLING?")
            except Exception:
                pass
            channels[ch] = values

        result["channels"] = channels
        return result

    # ------------------------------------------------------------------
    # Waveform transfer
    # ------------------------------------------------------------------

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

            record_length = self.get_record_length()
            if record_length is not None:
                if start > record_length:
                    start = 1
                stop = min(stop, record_length)

            try:
                samples, pre, transfer_points = self._read_curve_binary(
                    inst, ch, start, stop
                )
                mode = "BINARY"
            except Exception as binary_error:
                self._recover_session(inst)
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
