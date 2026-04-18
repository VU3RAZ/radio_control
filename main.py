#!/usr/bin/env python3
"""
Icom Radio Control – main entry point.

  python main.py               # auto-detect radio + audio device
  python main.py --list        # list available audio devices
  python main.py --audio 3     # use audio device index 3
  python main.py --port /dev/ttyUSB0 --baud 9600 --addr 0x94
"""

import sys
import argparse

from PyQt5.QtWidgets import QApplication, QMessageBox

from radio_control.civ      import CIVController, KNOWN_RADIOS
from radio_control.detector import find_serial_port, find_audio_device
from radio_control.ui       import MainWindow


def parse_args():
    p = argparse.ArgumentParser(description="Icom Radio Control")
    p.add_argument("--list",   action="store_true", help="List audio devices and exit")
    p.add_argument("--port",   help="Serial port (e.g. /dev/ttyUSB0)")
    p.add_argument("--baud",   type=int, default=9600, help="Baud rate (default 9600)")
    p.add_argument("--addr",   default=None,
                   help="Radio CI-V address in hex (e.g. 0x94 for IC-7300)")
    p.add_argument("--audio",  type=int, default=None,
                   help="Audio input device index (skip auto-detection)")
    p.add_argument("--rate",   type=int, default=None,
                   help="Sample rate override (e.g. 48000)")
    p.add_argument("--iq",     action="store_true",
                   help="Force IQ (stereo) mode regardless of auto-detection")
    p.add_argument("--mono",   action="store_true",
                   help="Force mono audio mode")
    return p.parse_args()


def list_audio_devices():
    import sounddevice as sd
    print("\nAvailable audio input devices:")
    print(f"  {'idx':>4}  {'name':<40}  {'ch':>3}  {'rate':>7}")
    print("  " + "-" * 60)
    for idx, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] > 0:
            print(f"  {idx:>4}  {dev['name']:<40}  "
                  f"{dev['max_input_channels']:>3}  "
                  f"{int(dev['default_samplerate']):>7}")
    print()


def main():
    args = parse_args()

    if args.list:
        list_audio_devices()
        return

    app = QApplication(sys.argv)
    app.setApplicationName("Icom Radio Control")

    # ── serial / CI-V ─────────────────────────────────────────────────────────
    radio = None
    if args.port:
        addr = int(args.addr, 16) if args.addr else 0x94
        radio = CIVController(args.port, args.baud, addr)
        if not radio.connect():
            QMessageBox.warning(
                None, "Serial Error",
                f"Could not open {args.port} at {args.baud} baud.\n"
                "Running without radio control."
            )
            radio = None
        else:
            print(f"[main] Connected to {radio.model} on {args.port}")
    else:
        print("[main] Auto-detecting radio…")
        radio = find_serial_port()
        if radio is None:
            print("[main] No Icom radio detected – running in audio-only mode.")

    # ── audio device ──────────────────────────────────────────────────────────
    if args.audio is not None:
        import sounddevice as sd
        dev   = args.audio
        devinfo = sd.query_devices(dev)
        sr    = args.rate or int(devinfo["default_samplerate"])
        is_iq = args.iq or (devinfo["max_input_channels"] >= 2 and not args.mono)
    else:
        print("[main] Auto-detecting audio device…")
        dev, sr, is_iq = find_audio_device()
        if args.rate:
            sr = args.rate
        if args.iq:
            is_iq = True
        if args.mono:
            is_iq = False

    print(f"[main] Audio: device={dev} rate={sr} iq={is_iq}")

    # ── launch UI ─────────────────────────────────────────────────────────────
    win = MainWindow(radio=radio, audio_device=dev, sample_rate=sr, is_iq=is_iq)
    win.show()

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
