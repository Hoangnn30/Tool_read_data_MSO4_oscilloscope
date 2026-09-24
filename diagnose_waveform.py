from __future__ import annotations

import argparse
import sys

from mso4 import MSO4Client


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Diagnose Tektronix MSO44/MSO44B waveform acquisition over VISA TCPIP/LAN."
    )
    parser.add_argument("--ip", default="192.168.1.133")
    parser.add_argument("--channel", default="CH1", choices=["CH1", "CH2", "CH3", "CH4"])
    parser.add_argument("--points", type=int, default=10000)
    args = parser.parse_args()

    resource = f"TCPIP0::{args.ip}::inst0::INSTR"
    print(f"Resource: {resource}")

    client = MSO4Client(args.ip, timeout=8.0)
    try:
        idn = client.connect()
        print(f"*IDN?: {idn}")

        record_length = client.get_record_length()
        print(f"Record length: {record_length}")

        client.prepare_acquisition([args.channel])
        print(f"Acquisition: RUN, {args.channel} enabled")

        waveform = client.get_waveform(args.channel, 1, args.points)
        info = client.get_last_transfer_info(args.channel)
        m = waveform.measurements()

        print(f"Transfer mode: {info.get('mode')}")
        print(f"Received points: {waveform.points}")
        print(f"Reported points: {info.get('reported_points')}")
        print(f"X increment: {info.get('xincr')}")
        print(f"Time range: {waveform.time_s[0]} .. {waveform.time_s[-1]} s")
        print(f"Voltage min/max: {m['min']} .. {m['max']} V")
        print(f"Vpp: {m['pkpk']} V")
        print(f"Vrms: {m['rms']} V")
        print(f"Frequency estimate: {m['frequency']} Hz")

        if waveform.points < 2:
            print("FAIL: fewer than 2 waveform points received.")
            return 2

        print("PASS: waveform data received successfully.")
        return 0

    except Exception as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
