# Icom Radio Control

A PyQt5 desktop app for controlling Icom transceivers via CI-V over USB, with real-time spectrum and waterfall display from the radio's IQ audio output.

Tested with IC-7300. Should work with any Icom radio that supports CI-V (IC-705, IC-7100, IC-7610, IC-9700, …).

---

## Features

- **Real-time spectrum + waterfall** — FFT from the radio's USB audio (IQ stereo or mono)
- **Full front-panel controls** — frequency, mode, filter, band buttons, AGC, ATT, pre-amp, NR, NB, COMP, VOX, split, PTT, tune
- **Bidirectional CI-V sync** — every control reflects the radio's actual state; hardware knob changes update the GUI within 100 ms
- **S-meter** — polled at 10 Hz via CI-V
- **Auto-detection** — finds the radio serial port and Icom USB audio device automatically
- **Tuning** — scroll wheel on frequency display, configurable step (1 Hz – 100 kHz), click to type frequency

---

## Requirements

- Python 3.10+
- Icom transceiver with USB CI-V and USB audio (or a separate CI-V adapter)

```bash
pip install pyserial sounddevice numpy PyQt5 pyqtgraph
```

On Linux, give yourself serial port access (log out/in after):

```bash
sudo usermod -aG dialout $USER
```

---

## Usage

```bash
# Auto-detect everything
python main.py

# List audio input devices
python main.py --list

# Manual overrides
python main.py --port /dev/ttyUSB0 --baud 115200 --addr 0x94
python main.py --audio 3 --rate 48000 --iq
python main.py --mono       # force mono (no IQ)
```

### IC-7300 quick start

1. Connect the radio via USB (creates a virtual COM port and a USB audio device)
2. In the radio menu set **CI-V Baud Rate → Auto** and **CI-V USB → Unlink**
3. Run `python main.py` — the app detects the radio and audio device automatically

---

## Architecture

```
main.py              CLI args → wires radio + audio + UI
radio_control/
  civ.py             CI-V protocol (BCD encode/decode, frame parser, CIVWorker QThread)
  detector.py        Auto-detect serial port and USB audio device
  audio.py           sounddevice capture → ring buffer → FFT (QThread)
  ui.py              PyQt5 main window (front panel + spectrum + waterfall)
```

### CI-V worker

`CIVWorker` is a `QThread` that owns the serial port for the lifetime of the session:

- Reads bytes continuously, parses CI-V frames, emits Qt signals
- Polls S-meter and frequency at **10 Hz**
- Rotates a full-state query list (mode, levels, AGC, ATT, split, …) at **10 Hz** — keeps the GUI in sync even when the radio's Transceive setting is OFF
- UI controls call `send_set_*()` which enqueues pre-built frames; the worker writes them on its next cycle

### Audio / FFT

- IQ (stereo) mode: complex FFT with `fftshift` — spectrum centred on VFO ± Fs/2
- Mono mode: real FFT — spectrum from VFO to VFO + Fs/2
- DC offset subtracted before windowing to remove the centre-frequency carrier spike common with USB audio codecs

---

## Supported radios

Any Icom radio with CI-V. Auto-detection probes these addresses in order:

`IC-7300 (0x94)`, `IC-705 (0xA4)`, `IC-7100 (0x88)`, `IC-7610 (0x98)`, `IC-9700 (0xA2)`, `IC-7200 (0x76)`, `IC-7600 (0x7A)`, `IC-7700 (0x70)`, `IC-7800 (0x80)`, `IC-7850 (0x8A)`, `IC-756PRO (0x6E)`, `IC-718 (0x3A)`, `IC-910H (0x52)`, `IC-9100 (0x62)`, `IC-7410 (0x7C)`, `IC-R8600 (0xAC)`, `IC-746 (0x58)`, `IC-706MK2 (0x46)`, `IC-706MK2G (0x48)`

---

## License

MIT
