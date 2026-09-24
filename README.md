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
