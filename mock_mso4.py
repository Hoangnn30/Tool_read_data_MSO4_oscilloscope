from __future__ import annotations

import argparse
import math
import socket
import socketserver
import struct


class State:
    source = "CH1"
    start = 1
    stop = 10000


class Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        state = State()
        buf = b""
        while True:
            data = self.request.recv(4096)
            if not data:
                return
            buf += data
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                command = raw.decode("ascii", errors="replace").strip()
                if command:
                    self.process(command, state)

    def process(self, command: str, state: State) -> None:
        upper = command.upper()
        if upper == "*IDN?":
            self.reply("TEKTRONIX,MSO44,MOCK0001,0.1")
        elif upper.startswith("DATA:SOURCE "):
            state.source = command.split()[-1].upper()
        elif upper.startswith("DATA:START "):
            state.start = int(command.split()[-1])
        elif upper.startswith("DATA:STOP "):
            state.stop = int(command.split()[-1])
        elif upper in {"DATA:ENCDG RIBINARY", "DATA:WIDTH 1"}:
            return
        elif upper == "WFMOUTPRE:XINCR?":
            self.reply("1e-6")
        elif upper == "WFMOUTPRE:XZERO?":
            self.reply("0")
        elif upper == "WFMOUTPRE:PT_OFF?":
            self.reply("0")
        elif upper == "WFMOUTPRE:YMULT?":
            self.reply("0.01")
        elif upper == "WFMOUTPRE:YZERO?":
            self.reply("0")
        elif upper == "WFMOUTPRE:YOFF?":
            self.reply("0")
        elif upper == "WFMOUTPRE:BYT_NR?":
            self.reply("1")
        elif upper == "WFMOUTPRE:BYT_OR?":
            self.reply("MSB")
        elif upper == "CURVE?":
            self.send_curve(state)
        elif upper.endswith("?"):
            self.reply("0")

    def reply(self, text: str) -> None:
        self.request.sendall(text.encode("ascii") + b"\n")

    def send_curve(self, state: State) -> None:
        count = max(1, min(50000, state.stop - state.start + 1))
        frequencies = {"CH1": 1000.0, "CH2": 1700.0, "CH3": 420.0, "CH4": 2500.0}
        amplitudes = {"CH1": 60.0, "CH2": 40.0, "CH3": 75.0, "CH4": 30.0}
        f = frequencies.get(state.source, 1000.0)
        amp = amplitudes.get(state.source, 60.0)

        values = bytearray()
        for i in range(count):
            t = i * 1e-6
            sample = int(round(amp * math.sin(2.0 * math.pi * f * t)))
            sample = max(-127, min(127, sample))
            values.extend(struct.pack("b", sample))

        size = str(len(values)).encode("ascii")
        header = b"#" + str(len(size)).encode("ascii") + size
        self.request.sendall(header + values + b"\n")


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> None:
    parser = argparse.ArgumentParser(description="Mock Tektronix MSO4 TCP socket server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4000)
    args = parser.parse_args()

    with Server((args.host, args.port), Handler) as server:
        print(f"Mock MSO4 listening on {args.host}:{args.port}")
        server.serve_forever()


if __name__ == "__main__":
    main()
