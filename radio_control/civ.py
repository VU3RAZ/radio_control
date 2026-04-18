"""
Icom CI-V protocol implementation.

Frame format:  FE FE <dst> <src> <cmd> [data...] FD
Controller address is always 0xE0.
Radio echoes the command on half-duplex buses; responses are addressed to 0xE0.
"""

import time
import serial
from typing import Optional, Tuple

PREAMBLE = bytes([0xFE, 0xFE])
END_BYTE  = 0xFD
CTRL_ADDR = 0xE0   # our (controller) address

CMD_READ_FREQ  = 0x03
CMD_SET_FREQ   = 0x05
CMD_READ_MODE  = 0x04
CMD_SET_MODE   = 0x06
CMD_READ_ID    = 0x19  # sub-command 0x00 returns radio address

# Common baud rates to probe (most Icom defaults are 9600 or 19200)
BAUD_RATES = [9600, 19200, 4800, 38400, 115200]

KNOWN_RADIOS: dict[int, str] = {
    0x94: "IC-7300",
    0xA4: "IC-705",
    0x88: "IC-7100",
    0x98: "IC-7610",
    0xA2: "IC-9700",
    0x76: "IC-7200",
    0x7A: "IC-7600",
    0x70: "IC-7700",
    0x80: "IC-7800",
    0x8A: "IC-7850",
    0x6E: "IC-756PRO3",
    0x3A: "IC-718",
    0x52: "IC-910H",
    0x62: "IC-9100",
    0x7C: "IC-7410",
    0xAC: "IC-R8600",
    0x58: "IC-746",
    0x46: "IC-706MK2",
    0x48: "IC-706MK2G",
}

MODES: dict[int, str] = {
    0x00: "LSB", 0x01: "USB", 0x02: "AM",  0x03: "CW",
    0x04: "RTTY", 0x05: "FM", 0x06: "WFM", 0x07: "CW-R",
    0x08: "RTTY-R", 0x11: "DV",
}


def _encode_bcd_freq(freq_hz: int) -> bytes:
    """Encode Hz integer to 5-byte Icom BCD (LSB-first pairs)."""
    result = []
    for _ in range(5):
        lo = freq_hz % 10
        freq_hz //= 10
        hi = freq_hz % 10
        freq_hz //= 10
        result.append((hi << 4) | lo)
    return bytes(result)


def _decode_bcd_freq(data: bytes) -> int:
    """Decode 5-byte Icom BCD (LSB-first pairs) to Hz integer."""
    freq = 0
    mult = 1
    for byte in data:
        freq += (byte & 0x0F) * mult
        mult *= 10
        freq += ((byte >> 4) & 0x0F) * mult
        mult *= 10
    return freq


class CIVController:
    """Manages a serial connection to an Icom radio using the CI-V protocol."""

    def __init__(self, port: str, baud_rate: int = 9600, radio_addr: int = 0x94):
        self.port = port
        self.baud_rate = baud_rate
        self.radio_addr = radio_addr
        self.model = KNOWN_RADIOS.get(radio_addr, f"Unknown (0x{radio_addr:02X})")
        self._ser: Optional[serial.Serial] = None

    # ------------------------------------------------------------------ connect

    def connect(self) -> bool:
        try:
            self._ser = serial.Serial(
                self.port,
                self.baud_rate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.3,
            )
            return True
        except serial.SerialException:
            return False

    def disconnect(self):
        if self._ser and self._ser.is_open:
            self._ser.close()

    @property
    def is_connected(self) -> bool:
        return self._ser is not None and self._ser.is_open

    # --------------------------------------------------------- low-level framing

    def _build_frame(self, cmd: int, data: bytes = b"") -> bytes:
        return PREAMBLE + bytes([self.radio_addr, CTRL_ADDR, cmd]) + data + bytes([END_BYTE])

    def _transact(self, cmd: int, data: bytes = b"") -> Optional[bytes]:
        """Send a command frame and return the first valid response frame."""
        if not self.is_connected:
            return None
        frame = self._build_frame(cmd, data)
        self._ser.reset_input_buffer()
        self._ser.write(frame)
        return self._read_response()

    def _read_response(self) -> Optional[bytes]:
        buf = b""
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            chunk = self._ser.read(self._ser.in_waiting or 1)
            if chunk:
                buf += chunk
            # Scan buf for complete frames addressed to us
            i = 0
            while i < len(buf) - 4:
                if buf[i] == 0xFE and buf[i + 1] == 0xFE:
                    end = buf.find(END_BYTE, i + 4)
                    if end == -1:
                        break   # incomplete frame – keep reading
                    msg = buf[i:end + 1]
                    # Response frames have dst == CTRL_ADDR (byte index 2)
                    if len(msg) >= 5 and msg[2] == CTRL_ADDR:
                        return msg
                    i = end + 1
                else:
                    i += 1
        return None

    # ----------------------------------------------------------- radio commands

    def read_frequency(self) -> Optional[int]:
        resp = self._transact(CMD_READ_FREQ)
        # Response: FE FE E0 <radio> 03 [5 BCD bytes] FD  (total 11 bytes)
        if resp and len(resp) >= 11 and resp[4] == CMD_READ_FREQ:
            return _decode_bcd_freq(resp[5:10])
        return None

    def set_frequency(self, freq_hz: int) -> bool:
        resp = self._transact(CMD_SET_FREQ, _encode_bcd_freq(freq_hz))
        return resp is not None and len(resp) >= 6 and resp[4] == 0xFB  # 0xFB = OK

    def read_mode(self) -> Optional[Tuple[str, int]]:
        """Returns (mode_name, filter_index) or None."""
        resp = self._transact(CMD_READ_MODE)
        if resp and len(resp) >= 8 and resp[4] == CMD_READ_MODE:
            mode_byte = resp[5]
            filter_byte = resp[6]
            return MODES.get(mode_byte, f"0x{mode_byte:02X}"), filter_byte
        return None
