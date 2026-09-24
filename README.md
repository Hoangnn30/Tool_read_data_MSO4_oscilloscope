# Tool_read_data_MSO4_oscilloscope

Python/PyQt desktop oscilloscope client for Tektronix 4 Series MSO, focused on MSO44/MSO46 over LAN.

## Current MVP

- Connect to MSO44 by IP over raw TCP socket.
- Default Tektronix Socket Server port: `4000`.
- Read `*IDN?`.
- Acquire CH1-CH4 waveforms using Tektronix SCPI.
- Decode IEEE-488.2 binary waveform blocks.
- Display waveforms in a PyQt6 + pyqtgraph oscilloscope-style UI.
- Run/Stop acquisition without freezing the UI.
- Enable/disable CH1-CH4.
- Adjustable record length and refresh interval.
- Cursor crosshair.
- Auto-scale display.
- Local measurements:
  - Vpp
  - Vrms
  - Mean
  - Min
  - Max
  - Frequency estimate
- Manual SCPI query console.
- Mock MSO4 server for development without physical hardware.

## Project structure

```text
.
├── main.py
├── mock_mso4.py
├── requirements.txt
├── mso4/
│   ├── __init__.py
│   ├── scpi.py
│   └── waveform.py
└── ui/
    ├── __init__.py
    └── main_window.py
```

## MSO44 LAN setup

On the MSO44:

1. Connect the oscilloscope LAN port to the same network as the computer.
2. Open the instrument I/O/LAN settings and confirm the oscilloscope has an IP address.
3. Enable the instrument Socket Server.
4. Use TCP port `4000` unless you configured a different port.
5. Verify the PC and oscilloscope are on reachable IP networks.

Example:

```text
PC:    192.168.1.100
MSO44: 192.168.1.120
Port:  4000
```

Basic connectivity check:

```bash
ping 192.168.1.120
nc -vz 192.168.1.120 4000
```

## Install

Python 3.11 or 3.12 is recommended.

### macOS / Linux

```bash
git clone https://github.com/Hoangnn30/Tool_read_data_MSO4_oscilloscope.git
cd Tool_read_data_MSO4_oscilloscope

python3 -m venv .venv
source .venv/bin/activate

python -m pip install --upgrade pip
pip install -r requirements.txt
```

### Windows PowerShell

```powershell
git clone https://github.com/Hoangnn30/Tool_read_data_MSO4_oscilloscope.git
cd Tool_read_data_MSO4_oscilloscope

py -m venv .venv
.\.venv\Scripts\Activate.ps1

python -m pip install --upgrade pip
pip install -r requirements.txt
```

## Run with the real MSO44

```bash
python main.py
```

Then:

1. Enter the MSO44 IP address.
2. Keep port `4000` unless the instrument uses another Socket Server port.
3. Click **Connect**.
4. A successful connection should show the instrument response to `*IDN?`.
5. Enable the desired channels.
6. Click **RUN**.

## Run without hardware

Terminal 1:

```bash
python mock_mso4.py
```

Terminal 2:

```bash
python main.py
```

Use:

```text
IP:   127.0.0.1
Port: 4000
```

The mock server generates sine waves on CH1-CH4 so the GUI and acquisition pipeline can be tested locally.

## Waveform acquisition flow

The client uses the Tektronix SCPI waveform-transfer flow:

```text
DATA:SOURCE CH1
DATA:START 1
DATA:STOP 10000
DATA:ENCDG RIBINARY
DATA:WIDTH 1

WFMOUTPRE:XINCR?
WFMOUTPRE:XZERO?
WFMOUTPRE:PT_OFF?
WFMOUTPRE:YMULT?
WFMOUTPRE:YZERO?
WFMOUTPRE:YOFF?

CURVE?
```

The returned integer samples are converted to engineering units using the waveform preamble:

```text
time = XZERO + (sample_index - PT_OFF) * XINCR
voltage = (raw_sample - YOFF) * YMULT + YZERO
```

## Notes

This first version intentionally keeps instrument writes limited. The main acquisition path reads the oscilloscope waveform and does not modify acquisition/trigger/channel configuration except for selecting the waveform source and transfer format.

Possible next features:

- Read instrument-side vertical scale and offset.
- Read horizontal time/div and sample rate.
- Trigger status and trigger controls.
- Single acquisition.
- Run/Stop the physical MSO44 from the application.
- FFT view.
- Measurement table using instrument-native measurements.
- Save waveform CSV/NPY.
- Screenshot capture.
- Measurement logging.
- LAN discovery/LXI discovery.
- Multi-device support.
