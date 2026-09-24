from __future__ import annotations

import re
import threading
import time
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
        self._fast_transfer_cache: dict[str, dict[str, object]] = {}
        self._fast_active_source: str | None = None
        self._record_length_cache: int | None = None

    @property
    def connected(self) -> bool:
        return self._instrument is not None

    def connect(self) -> str:
        """Open a clean VISA/VXI-11 session with retry and backend fallback.

        Tektronix scopes can occasionally leave the VXI-11 core link in a stale
        state after a previous process exits or a LAN connection is interrupted.
        Recreating both ResourceManager and resource is more reliable than
        retrying on the same broken socket.
        """
        self.close()

        backend_candidates: list[str | None] = [self.backend]
        if self.backend == "@py":
            # If NI-VISA/Keysight VISA is installed, also try the system backend.
            backend_candidates.append(None)

        errors: list[str] = []
        delays = (0.0, 0.35, 0.9)

        for backend in backend_candidates:
            backend_name = backend or "system VISA"

            for attempt, delay in enumerate(delays, start=1):
                if delay:
                    time.sleep(delay)

                self.close()

                try:
                    rm = (
                        pyvisa.ResourceManager(backend)
                        if backend is not None
                        else pyvisa.ResourceManager()
                    )
                    self._rm = rm

                    inst = rm.open_resource(self.resource_name)

                    if not isinstance(inst, MessageBasedResource):
                        try:
                            inst.close()
                        finally:
                            raise MSO4Error(
                                f"VISA resource is not message-based: "
                                f"{self.resource_name}"
                            )

                    # Give VXI-11 link setup more room than ordinary SCPI reads.
                    inst.timeout = max(int(self.timeout * 1000), 8000)
                    inst.write_termination = "\n"
                    inst.read_termination = "\n"
                    inst.chunk_size = 4 * 1024 * 1024
                    self._instrument = inst

                    try:
                        inst.clear()
                    except Exception:
                        # Some VXI-11 implementations reject device_clear while
                        # still accepting normal SCPI traffic.
                        pass

                    idn = self.query("*IDN?")
                    if not idn:
                        raise MSO4Error(
                            f"Connected to {self.resource_name}, but *IDN? "
                            "returned no data."
                        )

                    return idn

                except Exception as exc:
                    errors.append(
                        f"{backend_name} attempt {attempt}: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    self.close()

        details = " | ".join(errors[-6:])
        hint = (
            "The LAN address may still be reachable, but the oscilloscope "
            "VXI-11 service closed the session while creating the VISA link. "
            "Close other remote-control applications and retry. If it persists, "
            "toggle/restart the instrument LAN remote interface or reboot the "
            "oscilloscope."
        )
        raise MSO4Error(
            "TCPIP/LAN VISA VXI-11 connection failed. "
            f"Resource: {self.resource_name}. {hint} Attempts: {details}"
        )

    def close(self) -> None:
        self.invalidate_waveform_cache()
        self._record_length_cache = None
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

    def invalidate_waveform_cache(self) -> None:
        self._fast_transfer_cache.clear()
        self._fast_active_source = None

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
        self.invalidate_waveform_cache()

    def set_channel_position(self, channel: str, divisions: float) -> None:
        ch = self._validate_channel(channel)
        self.write(f"{ch}:POSITION {float(divisions):.12g}")
        self.invalidate_waveform_cache()

    def set_channel_offset(self, channel: str, volts: float) -> None:
        ch = self._validate_channel(channel)
        self.write(f"{ch}:OFFSET {float(volts):.12g}")
        self.invalidate_waveform_cache()

    def set_channel_coupling(self, channel: str, coupling: str) -> None:
        ch = self._validate_channel(channel)
        value = coupling.upper().strip()
        if value not in self.COUPLINGS:
            raise ValueError(f"Unsupported coupling: {coupling}")
        self.write(f"{ch}:COUPLING {value}")
        self.invalidate_waveform_cache()

    def set_horizontal_scale(self, seconds_per_div: float) -> None:
        value = float(seconds_per_div)
        if value <= 0:
            raise ValueError("Horizontal scale must be > 0.")
        self.write(f"HORIZONTAL:MODE:SCALE {value:.12g}")
        self._record_length_cache = None
        self.invalidate_waveform_cache()

    def set_horizontal_position(self, percent: float) -> None:
        value = max(0.0, min(100.0, float(percent)))
        self.write(f"HORIZONTAL:POSITION {value:.12g}")
        self.invalidate_waveform_cache()

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

        self._record_length_cache = None
        actual = self.get_record_length(force=True)
        self.invalidate_waveform_cache()
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

    def get_acquisition_state(self) -> str:
        return self.query("ACQUIRE:STATE?")

    def wait_for_acquisition_complete(self, timeout: float = 3.0) -> bool:
        """Wait for a SEQUENCE/SINGLE acquisition to stop.

        Returns True when ACQUIRE:STATE reports stopped, False on timeout.
        """
        deadline = time.monotonic() + max(0.1, float(timeout))
        while time.monotonic() < deadline:
            raw = self.get_acquisition_state().strip().upper()
            try:
                value = self._parse_number(raw)
                if int(round(value)) == 0:
                    return True
            except ValueError:
                if raw.endswith("STOP") or raw == "OFF":
                    return True
            time.sleep(0.02)
        return False

    def autoset(self) -> None:
        self.write("AUTOSET EXECUTE")
        self.invalidate_waveform_cache()

    def factory_default(self) -> None:
        self.write("FACTORY")
        self.invalidate_waveform_cache()

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

        # Configure binary transfer once. RUN frames will reuse this setup and
        # cached waveform preambles instead of issuing many small LAN queries.
        self.invalidate_waveform_cache()
        with self._lock:
            inst = self._require_instrument()
            inst.write("DATA:ENCDG RIBINARY")
            inst.write("DATA:WIDTH 1")

        self.run_acquisition()

        # Cache once for subsequent fast waveform setup. Avoid repeating this
        # LAN round-trip for every channel before its first frame.
        try:
            self.get_record_length(force=True)
        except Exception:
            pass

    def get_record_length(self, force: bool = False) -> int | None:
        if not force and self._record_length_cache:
            return self._record_length_cache

        for command in (
            "HORIZONTAL:MODE:RECORDLENGTH?",
            "HORIZONTAL:RECORDLENGTH?",
        ):
            try:
                value = self.query_int(command)
                if value > 0:
                    self._record_length_cache = value
                    return value
            except Exception:
                pass
        return self._record_length_cache

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

    def prepare_fast_waveform(
        self,
        channel: str,
        start: int = 1,
        stop: int = 5000,
    ) -> None:
        """Prepare a full-record, resampled binary transfer for fast display.

        The oscilloscope record itself is left untouched. DATA:RESAMPLE reduces
        LAN traffic while preserving the full horizontal time span.
        """
        ch = self._validate_channel(channel)
        desired_points = max(200, int(stop))

        with self._lock:
            inst = self._require_instrument()

            record_length = self.get_record_length()
            if record_length is None or record_length <= 0:
                record_length = desired_points

            data_start = 1
            data_stop = int(record_length)
            resample = max(1, int(np.ceil(record_length / desired_points)))

            inst.write(f"DATA:SOURCE {ch}")
            inst.write("DATA:MODE VECTOR")
            inst.write(f"DATA:START {data_start}")
            inst.write(f"DATA:STOP {data_stop}")
            inst.write(f"DATA:RESAMPLE {resample}")
            inst.write("DATA:ENCDG RIBINARY")
            inst.write("DATA:WIDTH 1")

            # Query timing/scaling in one SCPI round-trip. Falling back to
            # individual queries keeps compatibility with older firmware.
            pre = self._read_preamble_fast()
            transfer_points = int(np.ceil(record_length / resample))

            self._fast_transfer_cache[ch] = {
                "desired_points": desired_points,
                "data_start": data_start,
                "data_stop": data_stop,
                "record_length": int(record_length),
                "resample": int(resample),
                "pre": pre,
                "transfer_points": max(1, int(transfer_points)),
            }
            self._fast_active_source = ch

    def get_waveform_fast(
        self,
        channel: str,
        start: int = 1,
        stop: int = 5000,
    ) -> Waveform:
        """Fast display path using DATA:RESAMPLE across the complete record."""
        ch = self._validate_channel(channel)
        desired_points = max(200, int(stop))

        cached = self._fast_transfer_cache.get(ch)
        if (
            cached is None
            or int(cached.get("desired_points", -1)) != desired_points
        ):
            self.prepare_fast_waveform(ch, 1, desired_points)
            cached = self._fast_transfer_cache[ch]

        pre = cached["pre"]
        transfer_points = int(cached["transfer_points"])
        record_length = int(cached["record_length"])
        resample = int(cached["resample"])

        with self._lock:
            inst = self._require_instrument()
            try:
                # After prepare_fast_waveform(), transfer geometry is stable.
                # On a single-channel RUN this reduces the hot path to CURVE?
                # only. With multiple channels, only DATA:SOURCE is added.
                if self._fast_active_source != ch:
                    inst.write(f"DATA:SOURCE {ch}")
                    self._fast_active_source = ch

                samples = inst.query_binary_values(
                    "CURVE?",
                    datatype="b",
                    is_big_endian=True,
                    container=np.array,
                )
            except Exception:
                # A settings change or another transfer mode may have invalidated
                # the DATA configuration. Rebuild once, then return to hot path.
                self._recover_session(inst)
                self._fast_transfer_cache.pop(ch, None)
                self._fast_active_source = None
                self.prepare_fast_waveform(ch, 1, desired_points)
                cached = self._fast_transfer_cache[ch]
                pre = cached["pre"]
                transfer_points = int(cached["transfer_points"])
                record_length = int(cached["record_length"])
                resample = int(cached["resample"])

                inst = self._require_instrument()
                samples = inst.query_binary_values(
                    "CURVE?",
                    datatype="b",
                    is_big_endian=True,
                    container=np.array,
                )

        raw = np.asarray(samples, dtype=np.float64)
        if raw.size == 0:
            raise MSO4Error(f"{ch} returned zero waveform points.")

        n = raw.size

        # Tektronix WFMOUTPRE timing values are queried after resampling, so the
        # resulting X axis remains tied to the actual trigger location.
        x = pre.xzero + (
            np.arange(n, dtype=np.float64) - pre.pt_off
        ) * pre.xincr
        y = (raw - pre.yoff) * pre.ymult + pre.yzero

        self._last_transfer_info[ch] = {
            "mode": "FAST-RESAMPLE",
            "byte_width": 1,
            "points": int(n),
            "reported_points": int(transfer_points),
            "record_length": int(record_length),
            "resample": int(resample),
            "xincr": float(pre.xincr),
            "xzero": float(pre.xzero),
            "pt_off": float(pre.pt_off),
            "ymult": float(pre.ymult),
            "yzero": float(pre.yzero),
            "yoff": float(pre.yoff),
            "time_start": float(x[0]) if n else None,
            "time_stop": float(x[-1]) if n else None,
            "voltage_min": float(np.min(y)) if n else None,
            "voltage_max": float(np.max(y)) if n else None,
        }

        return Waveform(channel=ch, time_s=x, volts=y)

    def get_waveform(
        self,
        channel: str,
        start: int = 1,
        stop: int = 10000,
    ) -> Waveform:
        # Exact transfer changes DATA:WIDTH/RESAMPLE, so continuous RUN must
        # rebuild its fast cache afterwards.
        self.invalidate_waveform_cache()
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
            "byte_width": (2 if mode == "BINARY" else None),
            "points": int(n),
            "reported_points": int(transfer_points),
            "record_length": int(record_length) if record_length else None,
            "xincr": float(pre.xincr),
            "xzero": float(pre.xzero),
            "pt_off": float(pre.pt_off),
            "ymult": float(pre.ymult),
            "yzero": float(pre.yzero),
            "yoff": float(pre.yoff),
            "time_start": float(x[0]) if n else None,
            "time_stop": float(x[-1]) if n else None,
            "voltage_min": float(np.min(y)) if n else None,
            "voltage_max": float(np.max(y)) if n else None,
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
        inst.write("DATA:MODE VECTOR")
        inst.write(f"DATA:START {start}")
        inst.write(f"DATA:STOP {stop}")
        inst.write("DATA:RESAMPLE 1")

        if encoding == "BINARY":
            # Exact capture uses 16-bit signed samples so GET DATA/SINGLE
            # preserve vertical digitizer resolution better than the fast UI path.
            inst.write("DATA:ENCDG RIBINARY")
            inst.write("DATA:WIDTH 2")
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
            datatype="h",
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

    def _read_preamble_fast(self) -> WaveformPreamble:
        """Read the six display-critical preamble values in one LAN query."""
        command = (
            "WFMOUTPRE:XINCR?;XZERO?;PT_OFF?;"
            "YMULT?;YZERO?;YOFF?"
        )
        try:
            raw = self.query(command)
            parts = [part.strip() for part in raw.split(";") if part.strip()]
            if len(parts) != 6:
                raise ValueError(
                    f"Expected 6 preamble values, received {len(parts)}"
                )

            values = [self._parse_number(part) for part in parts]
            return WaveformPreamble(
                xincr=values[0],
                xzero=values[1],
                pt_off=values[2],
                ymult=values[3],
                yzero=values[4],
                yoff=values[5],
            )
        except Exception:
            return self._read_preamble()

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
