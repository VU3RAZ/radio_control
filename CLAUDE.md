# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the app

```bash
# Auto-detect radio and audio device
python main.py

# List available audio input devices
python main.py --list

# Override specific devices / settings
python main.py --port /dev/ttyUSB0 --baud 9600 --addr 0x94
python main.py --audio 3 --rate 48000 --iq
python main.py --mono       # force mono audio (no IQ)
```

## Installing dependencies

```bash
pip install pyserial sounddevice numpy PyQt5 pyqtgraph
```

On Linux, add your user to the `dialout` group for serial port access:
```bash
sudo usermod -aG dialout $USER   # then log out / in
```

## Architecture

```
main.py                     – CLI arg parsing, wires together radio + audio + UI
radio_control/
  civ.py                    – CI-V protocol: BCD encode/decode, CIVController class
  detector.py               – Auto-detect serial port (probes baud/address combos)
                              and USB audio device by name matching
  audio.py  (AudioWorker)   – QThread wrapping sounddevice.InputStream; emits
                              fft_ready(np.ndarray) at ~20 Hz
  ui.py     (MainWindow)    – PyQt5 window: spectrum PlotWidget + waterfall
                              ImageItem; polls radio via QTimer every 200 ms
```

### Data flow

```
sounddevice callback → ring buffer → FFT → fft_ready signal (Qt queued)
                                                 ↓
                                      MainWindow._on_fft()
                                         ├── spectrum PlotCurveItem.setData()
                                         └── waterfall buffer roll + ImageItem.setImage()

QTimer (200 ms) → CIVController.read_frequency() → _lbl_freq + freq axis update
```

### CI-V protocol notes

- **Frame**: `FE FE <dst> <src> <cmd> [data…] FD`
- Controller address is always `0xE0`; radio echoes every frame on half-duplex buses
- Frequency is 5-byte BCD, **LSB-first**, each byte `(hi_nibble << 4) | lo_nibble` where hi = more-significant digit of the pair
- `civ.py::_encode_bcd_freq` / `_decode_bcd_freq` implement this exactly
- Known radio addresses are in `KNOWN_RADIOS` dict (IC-7300 = 0x94, IC-705 = 0xA4, etc.)

### Spectrum / waterfall tuning

Constants at the top of `ui.py`:

| Constant | Default | Effect |
|---|---|---|
| `FFT_SIZE` | 2048 | Frequency resolution = Fs / FFT_SIZE |
| `WATERFALL_ROWS` | 300 | Time history depth |
| `DB_FLOOR` | −120 dB | Bottom of colour scale |
| `DB_CEIL` | −20 dB | Top of colour scale |
| `CIV_POLL_MS` | 200 | Radio VFO poll interval |

### IQ vs mono audio

- **Mono**: `rfft` → positive frequencies `0 … Fs/2`; x-axis = `VFO + [0, Fs/2]`
- **IQ (stereo)**: left=I, right=Q → complex FFT shifted to `−Fs/2 … +Fs/2`; x-axis = `VFO + [−Fs/2, +Fs/2]`

Modern Icom radios (IC-7300, IC-705, IC-7610) expose a stereo USB audio device where the pair is in-phase / quadrature; pass `--iq` or let auto-detection pick stereo.
