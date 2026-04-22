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
  civ.py                    – CI-V protocol: BCD encode/decode, CIVController (sync,
                              used by detector) and CIVWorker (QThread, all runtime I/O)
  detector.py               – Auto-detect serial port (probes baud/address combos)
                              and USB audio device by name matching
  audio.py  (AudioWorker)   – QThread wrapping sounddevice.InputStream; emits
                              fft_ready(np.ndarray) at ~20 Hz
  ui.py     (MainWindow)    – PyQt5 window: IC-7300 style front panel, spectrum
                              PlotWidget + waterfall ImageItem
```

### Data flow

```
sounddevice callback → ring buffer → DC removal → FFT → fft_ready signal (Qt queued)
                                                              ↓
                                                   MainWindow._on_fft()
                                                      ├── spectrum PlotCurveItem.setData()
                                                      └── waterfall buffer roll + ImageItem.setImage()

CIVWorker (QThread)
  ├── polls S-meter every 100 ms  → smeter_updated  → SMeterWidget.set_value()
  ├── polls frequency every 100 ms → freq_updated   → _on_freq()  (also updates waterfall axis)
  ├── rotates full-state poll      → level_updated / mode_updated / agc_updated / …
  └── dispatches transceive broadcasts when radio hardware is changed
```

### Bidirectional CI-V sync

Every radio parameter has a signal (radio → GUI) and a send_set_*() method (GUI → radio):

| Parameter | Signal | Send method | Notes |
|---|---|---|---|
| Frequency | `freq_updated(int)` | `send_set_freq(hz)` | Polled 10 Hz + transceive |
| Mode / filter | `mode_updated(str, int)` | `send_set_mode(name, filt)` | Transceive + state poll |
| S-meter | `smeter_updated(int)` | — | Polled 10 Hz only |
| AF volume | `level_updated(LVL_AF, v)` | `send_set_level(LVL_AF, v)` | |
| RF gain | `level_updated(LVL_RF_GAIN, v)` | `send_set_level(LVL_RF_GAIN, v)` | |
| Squelch | `level_updated(LVL_SQL, v)` | `send_set_level(LVL_SQL, v)` | |
| NR / NB level | `level_updated(LVL_NR/NB, v)` | `send_set_level(…)` | |
| Drive / Mic | `level_updated(LVL_DRIVE/MIC, v)` | `send_set_level(…)` | |
| AGC | `agc_updated(int)` | `send_set_agc(mode)` | 0=off 1=F 2=M 3=S |
| ATT | `att_updated(int)` | `send_set_att(val)` | 0 or 20 dB |
| Pre-amp | `preamp_updated(int)` | `send_set_preamp(val)` | 0/1/2 |
| NR / NB / COMP / VOX | `function_updated(sub, bool)` | `send_set_function(sub, on)` | |
| Split | `split_updated(bool)` | `send_set_split(on)` | |
| TX / PTT | `tx_updated(bool)` | `send_set_tx(on)` | |

### CI-V protocol notes

- **Frame**: `FE FE <dst> <src> <cmd> [data…] FD`
- Controller address is always `0xE0`; radio echoes every frame on half-duplex buses
- Frequency is 5-byte BCD, **LSB-first**: each byte `(hi_nibble << 4) | lo_nibble` where hi = more-significant digit of the pair
- Level values (0–255) are 4-digit BCD in 2 bytes LSB-first; `_encode_level` / `_decode_level` implement this
- `_initial_sync()` queries all state at startup; full-state poll rotates one query per 100 ms for radios with transceive disabled
- Known radio addresses in `KNOWN_RADIOS` dict (IC-7300 = 0x94, IC-705 = 0xA4, etc.)
- **Detector**: `find_serial_port()` returns a **connected** `CIVController`; the `finally: disconnect()` anti-pattern must not be reintroduced

### Spectrum / waterfall tuning

Constants at the top of `ui.py`:

| Constant | Default | Effect |
|---|---|---|
| `FFT_SIZE` | 2048 | Frequency resolution = Fs / FFT_SIZE |
| `WATERFALL_ROWS` | 300 | Time history depth |
| `DB_FLOOR` | −120 dB | Bottom of colour scale |
| `DB_CEIL` | −20 dB | Top of colour scale |

### IQ vs mono audio

- **Mono (default)**: `rfft` → positive frequencies `0 … Fs/2`; x-axis = `VFO + [0, Fs/2]`
- **IQ (stereo)**: left=I, right=Q → complex FFT shifted to `−Fs/2 … +Fs/2`; x-axis = `VFO + [−Fs/2, +Fs/2]`
- DC offset is removed (`sig -= sig.mean()`) before windowing to suppress the centre-frequency carrier spike

**Default is always mono.** The IC-7300 USB audio device is stereo but outputs regular AF audio
by default (not IQ). IQ mode requires the radio to be explicitly configured for I/Q USB output
AND the app to be launched with `--iq`. Never assume stereo device = IQ signal.

To enable IQ: `python main.py --iq`

### S-meter CI-V notes

- Polled at 10 Hz via command `0x15` sub-command `0x02`
- Scale: 0 = S0, 120 = S9, 241 = S9+60 dB
- `_dispatch` handles both response formats: `[0x02, lo, hi]` (sub-command echoed) and `[lo, hi]` (sub-command omitted by some firmware)
