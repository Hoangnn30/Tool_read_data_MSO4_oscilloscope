# Tool_read_data_MSO4_oscilloscope

Lightweight Python desktop oscilloscope client for Tektronix 4 Series MSO, focused on MSO44/MSO46 over TCPIP LAN.

The GUI uses **Tkinter + Canvas**, so the project no longer depends on PyQt6 or pyqtgraph.

## Features

- Connect to MSO44 through VISA TCPIP/LAN:
  `TCPIP0::<IP>::inst0::INSTR`
- Default target IP in the GUI: `192.168.1.133`
- Read `*IDN?`
- Acquire CH1-CH4 waveforms with Tektronix SCPI
- Display waveforms on a lightweight Tkinter Canvas
- Background acquisition thread so the GUI remains responsive
- Run / Stop
- Channel enable/disable
- Adjustable point count and refresh interval
- Auto-scale
- Mouse cursor time/voltage readout
- Vpp, Vrms, Mean, Min, Max and frequency estimate
- Manual SCPI console

## Dependencies

Python packages:

```text
numpy
PyVISA
PyVISA-py
```

Tkinter is part of standard Python distributions. If you use Homebrew Python on macOS and `import tkinter` fails, install the matching Tk package. Example for Python 3.14:

```bash
brew install python-tk@3.14
```

For Python 3.12:

```bash
brew install python-tk@3.12
```

## Install / update

```bash
git checkout feat/pyqt-mso44-lan
git pull

source .venv/bin/activate
pip uninstall -y PyQt6 PyQt6-Qt6 PyQt6-sip pyqtgraph
pip install -r requirements.txt
```

Check Tkinter:

```bash
python -c "import tkinter; print('Tkinter OK', tkinter.TkVersion)"
```

## Run

```bash
python main.py
```

Enter:

```text
192.168.1.133
```

The application connects using:

```text
TCPIP0::192.168.1.133::inst0::INSTR
```

No HTTP port 80 and no raw socket port 4000 are required for this VISA TCPIP/LAN mode.

## MSO44 waveform flow

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

The returned samples are converted with the waveform preamble:

```text
time = XZERO + (sample_index - PT_OFF) * XINCR
voltage = (raw_sample - YOFF) * YMULT + YZERO
```

## Project structure

```text
.
├── main.py
├── ui_tk.py
├── requirements.txt
└── mso4/
    ├── __init__.py
    ├── scpi.py
    └── waveform.py
```

## Recommended waveform architecture: TekHSI first

For Tektronix MSO44B / 4 Series B, realtime waveform transfer should use TekHSI on port 5000. VISA/SCPI remains the control plane for RUN/STOP, channel state, V/div, Time/div, trigger and setup.

Recommended runtime:

```bash
brew install python@3.13 python-tk@3.13

rm -rf .venv
"$(brew --prefix python@3.13)/bin/python3.13" -m venv .venv
source .venv/bin/activate

python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Verify:

```bash
python --version
python -c "import tekhsi; print('TekHSI OK')"
```

Expected Python version: 3.13.x.

On the oscilloscope enable the High Speed Interface and use the default TekHSI endpoint:

```text
192.168.1.133:5000
```

The application uses:
- TekHSI first for realtime multi-channel waveform data.
- VISA/SCPI for instrument control.
- SCPI CURVE? only as a waveform fallback if TekHSI is unavailable.

