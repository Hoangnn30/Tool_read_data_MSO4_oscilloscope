from __future__ import annotations

import socket
import threading
from dataclasses import dataclass
from typing import Optional

import numpy as np

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
    byte_width: int
    byte_order: str


class MSO4Client:
    """Minimal Tektronix 4 Series MSO SCPI client over raw TCP socket."""

    def __init__(self, host: str, port: int = 4000, timeout: float = 3.0):
        self.host = host.strip()
        self.port = int(port)
        self.timeout = float(timeout)
        self._sock: Optional[socket.socket] = None
        self._lock = threading.RLock()

    @property
    def connected(self) -> bool:
        return self._sock is not None

    def connect(self) -> str:
        self.close()
        try:
            sock = socket.create_connection((self.host, self.port), self.timeout)
            sock.settimeout(self.timeout)
            self._sock = sock
            idn = self.query("*IDN?")
            if not idn:
                raise MSO4Error("Connected but *IDN? returned an empty response.")
            return idn
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def write(self, command: str) -> None:
        with self._lock:
            sock = self._require_socket()
            payload = command.rstrip("\r\n").encode("ascii") + b"\n"
            try:
                sock.sendall(payload)
            except OSError as exc:
                self.close()
                raise MSO4Error(f"SCPI write failed: {exc}") from exc

    def query(self, command: str) -> str:
        with self._lock:
            self.write(command)
            return self._readline().decode("ascii", errors="replace").strip()

    def query_float(self, command: str) -> float:
        raw = self.query(command)
        try:
            return float(raw)
        except ValueError as exc:
            raise MSO4Error(f"Invalid numeric response for {command}: {raw!r}") from exc

    def query_int(self, command: str) -> int:
        return int(round(self.query_float(command)))

    def get_waveform(self, channel: str, start: int = 1, stop: int = 10000) -> Waveform:
        ch = channel.upper().strip()
        if ch not in {"CH1", "CH2", "CH3", "CH4"}:
            raise ValueError(f"Unsupported channel: {channel}")

        with self._lock:
            self.write(f"DATA:SOURCE {ch}")
            self.write(f"DATA:START {max(1, int(start))}")
            self.write(f"DATA:STOP {max(int(start), int(stop))}")

            # Signed integer binary is substantially faster than ASCII for live display.
            self.write("DATA:ENCDG RIBINARY")

            byte_width = 1
            try:
                self.write("DATA:WIDTH 1")
                byte_width = self.query_int("WFMOUTPRE:BYT_NR?")
            except Exception:
                # Some firmware variants expose width through the waveform preamble only.
                try:
                    byte_width = self.query_int("WFMOUTPRE:BYT_NR?")
                except Exception:
                    byte_width = 1

            pre = self._read_preamble(byte_width)
            self.write("CURVE?")
            payload = self._read_ieee_block()

        samples = self._decode_samples(payload, pre)
        n = samples.size
        x = pre.xzero + (np.arange(n, dtype=np.float64) - pre.pt_off) * pre.xincr
        y = (samples.astype(np.float64) - pre.yoff) * pre.ymult + pre.yzero
        return Waveform(channel=ch, time_s=x, volts=y)

    def _read_preamble(self, byte_width: int) -> WaveformPreamble:
        xincr = self.query_float("WFMOUTPRE:XINCR?")
        xzero = self.query_float("WFMOUTPRE:XZERO?")
        pt_off = self.query_float("WFMOUTPRE:PT_OFF?")
        ymult = self.query_float("WFMOUTPRE:YMULT?")
        yzero = self.query_float("WFMOUTPRE:YZERO?")
        yoff = self.query_float("WFMOUTPRE:YOFF?")

        byte_order = "MSB"
        try:
            response = self.query("WFMOUTPRE:BYT_OR?")
            upper = response.upper()
            if "LSB" in upper:
                byte_order = "LSB"
        except Exception:
            pass

        return WaveformPreamble(
            xincr=xincr,
            xzero=xzero,
            pt_off=pt_off,
            ymult=ymult,
            yzero=yzero,
            yoff=yoff,
            byte_width=max(1, int(byte_width)),
            byte_order=byte_order,
        )

    def _decode_samples(self, payload: bytes, pre: WaveformPreamble) -> np.ndarray:
        if pre.byte_width <= 1:
            return np.frombuffer(payload, dtype=np.int8)

        if pre.byte_width == 2:
            dtype = ">i2" if pre.byte_order == "MSB" else "<i2"
            usable = len(payload) - (len(payload) % 2)
            return np.frombuffer(payload[:usable], dtype=dtype)

        raise MSO4Error(f"Unsupported waveform byte width: {pre.byte_width}")

    def _read_ieee_block(self) -> bytes:
        sock = self._require_socket()

        first = self._recv_exact(1)
        while first in {b"\n", b"\r", b" "}:
            first = self._recv_exact(1)

        if first != b"#":
            rest = self._readline(prefix=first)
            raise MSO4Error(
                "CURVE? did not return an IEEE-488.2 binary block: "
                + rest[:120].decode("ascii", errors="replace")
            )

        ndigits_raw = self._recv_exact(1)
        if not ndigits_raw.isdigit():
            raise MSO4Error(f"Invalid binary block header: #{ndigits_raw!r}")

        ndigits = int(ndigits_raw)
        if ndigits == 0:
            raise MSO4Error("Indefinite-length binary blocks are not supported.")

        length_raw = self._recv_exact(ndigits)
        try:
            length = int(length_raw.decode("ascii"))
        except ValueError as exc:
            raise MSO4Error(f"Invalid binary block length: {length_raw!r}") from exc

        payload = self._recv_exact(length)

        # Consume optional line terminator without blocking.
        old_timeout = sock.gettimeout()
        try:
            sock.settimeout(0.02)
            try:
                tail = sock.recv(2)
                if tail and tail not in {b"\n", b"\r", b"\r\n"}:
                    # No push-back is available; instruments normally send only EOL here.
                    pass
            except (socket.timeout, BlockingIOError):
                pass
        finally:
            sock.settimeout(old_timeout)

        return payload

    def _readline(self, prefix: bytes = b"") -> bytes:
        sock = self._require_socket()
        chunks = [prefix] if prefix else []
        total = len(prefix)

        while True:
            try:
                b = sock.recv(1)
            except socket.timeout as exc:
                raise MSO4Error("Timed out waiting for oscilloscope response.") from exc
            except OSError as exc:
                self.close()
                raise MSO4Error(f"SCPI read failed: {exc}") from exc

            if not b:
                self.close()
                raise MSO4Error("Oscilloscope closed the TCP connection.")

            if b == b"\n":
                break

            chunks.append(b)
            total += 1
            if total > 4_000_000:
                raise MSO4Error("SCPI line response is unexpectedly large.")

        return b"".join(chunks).rstrip(b"\r")

    def _recv_exact(self, count: int) -> bytes:
        sock = self._require_socket()
        buf = bytearray()

        while len(buf) < count:
            try:
                chunk = sock.recv(count - len(buf))
            except socket.timeout as exc:
                raise MSO4Error("Timed out while receiving waveform data.") from exc
            except OSError as exc:
                self.close()
                raise MSO4Error(f"Waveform receive failed: {exc}") from exc

            if not chunk:
                self.close()
                raise MSO4Error("Oscilloscope closed the connection during waveform transfer.")

            buf.extend(chunk)

        return bytes(buf)

    def _require_socket(self) -> socket.socket:
        if self._sock is None:
            raise MSO4Error("Not connected to oscilloscope.")
        return self._sock

    def __enter__(self) -> "MSO4Client":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
