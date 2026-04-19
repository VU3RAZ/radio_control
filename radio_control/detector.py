"""
Auto-detect Icom radio serial port and USB audio device.
"""

import serial.tools.list_ports
import sounddevice as sd
from typing import Optional, Tuple

from .civ import CIVController, BAUD_RATES, KNOWN_RADIOS

# Probe order: most common Icom defaults first
_PROBE_ADDRS = [0x94, 0xA4, 0x88, 0x98, 0xA2, 0x76, 0x7A, 0x70, 0x80,
                0x8A, 0x6E, 0x3A, 0x52, 0x62, 0x7C, 0xAC, 0x58, 0x46, 0x48]

# USB VID for Silicon Labs (CP210x) and FTDI – commonly used by Icom USB cables
_ICOM_VIDS = {0x10C4, 0x0403, 0x067B, 0x2341}


def find_serial_port() -> Optional[CIVController]:
    """
    Scan serial ports for an Icom radio.
    Returns a connected CIVController, or None if not found.
    """
    ports = serial.tools.list_ports.comports()

    # Sort: prefer ports whose USB VID matches known Icom/FTDI vendors
    def port_priority(p):
        return 0 if p.vid in _ICOM_VIDS else 1

    ports = sorted(ports, key=port_priority)

    for port_info in ports:
        device = port_info.device
        for baud in BAUD_RATES:
            opened = False
            for addr in _PROBE_ADDRS:
                ctrl = CIVController(device, baud, addr)
                if not ctrl.connect():
                    break           # port not openable at this baud
                opened = True
                freq = ctrl.read_frequency()
                if freq is not None:
                    print(f"[detector] Found {ctrl.model} on {device} "
                          f"at {baud} baud (addr 0x{addr:02X})")
                    return ctrl     # return CONNECTED controller
                ctrl.disconnect()   # wrong address – try next

            if not opened:
                break               # port refused open entirely – skip other bauds

    return None


def find_audio_device(prefer_stereo: bool = True) -> Tuple[Optional[int], int, bool]:
    """
    Locate the Icom USB audio input device.

    Returns (device_index, sample_rate, is_iq) where:
      - device_index is None if no match found (caller should use default)
      - is_iq is True when a stereo device was found (I/Q capable)
    """
    devices = sd.query_devices()
    icom_keywords = ["icom", "usb audio codec", "usb audio device",
                     "usb sound", "line"]

    candidates = []
    for idx, dev in enumerate(devices):
        if dev["max_input_channels"] < 1:
            continue
        name = dev["name"].lower()
        if any(kw in name for kw in icom_keywords):
            candidates.append((idx, dev))

    # Prefer stereo (IQ) > mono, higher sample rate > lower
    def score(item):
        idx, dev = item
        ch_score = 2 if dev["max_input_channels"] >= 2 else 1
        sr = dev["default_samplerate"]
        return (ch_score, sr)

    if candidates:
        candidates.sort(key=score, reverse=True)
        idx, dev = candidates[0]
        sr = int(dev["default_samplerate"])
        is_iq = prefer_stereo and dev["max_input_channels"] >= 2
        print(f"[detector] Audio device [{idx}] '{dev['name']}' "
              f"ch={dev['max_input_channels']} sr={sr} iq={is_iq}")
        return idx, sr, is_iq

    # Fallback: use default input device
    try:
        default_idx = sd.default.device[0]
        dev = sd.query_devices(default_idx)
        sr = int(dev["default_samplerate"])
        is_iq = prefer_stereo and dev["max_input_channels"] >= 2
        print(f"[detector] Using default audio device [{default_idx}] '{dev['name']}'")
        return default_idx, sr, is_iq
    except Exception:
        return None, 48000, False
