"""
Icom CI-V protocol implementation + non-blocking CIVWorker thread.

Frame format:  FE FE <dst> <src> <cmd> [data...] FD
Controller address is always 0xE0.
Radio echoes the command on half-duplex buses; responses are addressed to 0xE0.

Level values (command 0x14) use 4-digit BCD in the range 0000–0255.
All CI-V sub-commands below are verified against the IC-7300 manual;
other Icom models share most commands but exact sub-commands may differ.
"""

import queue
import time
import serial
from typing import Optional, Tuple, Callable

from PyQt5.QtCore import QThread, pyqtSignal

# ── protocol constants ────────────────────────────────────────────────────────
PREAMBLE  = bytes([0xFE, 0xFE])
END_BYTE  = 0xFD
CTRL_ADDR = 0xE0        # controller (our) CI-V address
OK_BYTE   = 0xFB        # response: command OK
NG_BYTE   = 0xFA        # response: command NG

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
    0x00: "LSB",  0x01: "USB",   0x02: "AM",   0x03: "CW",
    0x04: "RTTY", 0x05: "FM",    0x06: "WFM",  0x07: "CW-R",
    0x08: "RTTY-R", 0x11: "DV",
}
MODES_BY_NAME: dict[str, int] = {v: k for k, v in MODES.items()}

# ── CI-V command bytes ────────────────────────────────────────────────────────
CMD_READ_FREQ  = 0x03
CMD_SET_FREQ   = 0x05
CMD_READ_MODE  = 0x04
CMD_SET_MODE   = 0x06
CMD_VFO        = 0x07   # sub: 00=A, 01=B, 90=swap, A0=main, B0=sub
CMD_MEM_WRITE  = 0x09
CMD_MEM_TO_VFO = 0x0A
CMD_SPLIT      = 0x0F   # data: 00=off, 01=on
CMD_ATT        = 0x11   # data: 00=off, 0x20=20 dB (IC-7300)
CMD_PREAMP     = 0x16   # sub 0x02, data: 00=off, 01=Pre1, 02=Pre2
CMD_SMETER     = 0x15   # sub 0x02 → receive level 0000–0241
CMD_LEVEL      = 0x14   # sub-commands below
CMD_FUNC       = 0x16   # sub-commands below
CMD_TX         = 0x1C   # sub 0x00: 00=RX, 01=TX
CMD_TUNE_START = 0x1C   # sub 0x02: start auto-tuner

# Level sub-commands (CMD_LEVEL = 0x14)
LVL_AF         = 0x01
LVL_RF_GAIN    = 0x02
LVL_SQL        = 0x03
LVL_NR         = 0x06
LVL_NB         = 0x09
LVL_DRIVE      = 0x0E
LVL_MIC        = 0x0F
LVL_COMP       = 0x0C
LVL_VOX_GAIN   = 0x10

# Function sub-commands (CMD_FUNC = 0x16)
FUNC_NR        = 0x40   # NR  on/off
FUNC_NB        = 0x41   # NB  on/off
FUNC_COMP      = 0x44   # COMP on/off
FUNC_VOX       = 0x46   # VOX  on/off
FUNC_AGC       = 0x12   # AGC: data 00=off, 01=fast, 02=mid, 03=slow
FUNC_PREAMP    = 0x02   # pre-amp: data 00=off, 01=Pre1, 02=Pre2


# ── BCD helpers ───────────────────────────────────────────────────────────────

def _encode_bcd_freq(freq_hz: int) -> bytes:
    """Encode Hz to 5-byte Icom BCD, LSB-first (hi nibble = upper digit of each pair)."""
    result = []
    for _ in range(5):
        lo = freq_hz % 10;  freq_hz //= 10
        hi = freq_hz % 10;  freq_hz //= 10
        result.append((hi << 4) | lo)
    return bytes(result)


def _decode_bcd_freq(data: bytes) -> int:
    freq = 0; mult = 1
    for byte in data:
        freq += (byte & 0x0F) * mult;   mult *= 10
        freq += ((byte >> 4) & 0x0F) * mult; mult *= 10
    return freq


def _encode_level(value: int) -> bytes:
    """Encode 0–255 integer as 4-digit BCD, 2 bytes LSB-first."""
    v = max(0, min(255, int(value)))
    d0 = v % 10; v //= 10
    d1 = v % 10; v //= 10
    d2 = v % 10; d3 = v // 10
    return bytes([(d1 << 4) | d0, (d3 << 4) | d2])


def _decode_level(data: bytes) -> int:
    lo = (data[0] & 0x0F) + ((data[0] >> 4) & 0x0F) * 10
    hi = ((data[1] & 0x0F) + ((data[1] >> 4) & 0x0F) * 10) if len(data) > 1 else 0
    return lo + hi * 100


# ── CIVController ─────────────────────────────────────────────────────────────

class CIVController:
    """Synchronous Icom CI-V controller over a serial port."""

    def __init__(self, port: str, baud_rate: int = 9600, radio_addr: int = 0x94):
        self.port       = port
        self.baud_rate  = baud_rate
        self.radio_addr = radio_addr
        self.model      = KNOWN_RADIOS.get(radio_addr, f"Unknown (0x{radio_addr:02X})")
        self._ser: Optional[serial.Serial] = None

    def connect(self) -> bool:
        try:
            self._ser = serial.Serial(
                self.port, self.baud_rate,
                bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE, timeout=0.15,
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

    # ── low-level ─────────────────────────────────────────────────────────────

    def _build_frame(self, cmd: int, data: bytes = b"") -> bytes:
        return PREAMBLE + bytes([self.radio_addr, CTRL_ADDR, cmd]) + data + bytes([END_BYTE])

    def _transact(self, cmd: int, data: bytes = b"") -> Optional[bytes]:
        if not self.is_connected:
            return None
        try:
            self._ser.reset_input_buffer()
            self._ser.write(self._build_frame(cmd, data))
            return self._read_response()
        except serial.SerialException:
            return None

    def _read_response(self) -> Optional[bytes]:
        buf = b""
        deadline = time.monotonic() + 0.2
        while time.monotonic() < deadline:
            chunk = self._ser.read(self._ser.in_waiting or 1)
            if chunk:
                buf += chunk
            i = 0
            while i < len(buf) - 4:
                if buf[i] == 0xFE and buf[i + 1] == 0xFE:
                    end = buf.find(END_BYTE, i + 4)
                    if end == -1:
                        break
                    msg = buf[i:end + 1]
                    if len(msg) >= 5 and msg[2] == CTRL_ADDR:
                        return msg
                    i = end + 1
                else:
                    i += 1
        return None

    # ── frequency ─────────────────────────────────────────────────────────────

    def read_frequency(self) -> Optional[int]:
        resp = self._transact(CMD_READ_FREQ)
        if resp and len(resp) >= 11 and resp[4] == CMD_READ_FREQ:
            return _decode_bcd_freq(resp[5:10])
        return None

    def set_frequency(self, freq_hz: int) -> bool:
        resp = self._transact(CMD_SET_FREQ, _encode_bcd_freq(freq_hz))
        return resp is not None and resp[4] == OK_BYTE

    # ── mode ──────────────────────────────────────────────────────────────────

    def read_mode(self) -> Optional[Tuple[str, int]]:
        """Returns (mode_name, filter_index 1–3) or None."""
        resp = self._transact(CMD_READ_MODE)
        if resp and len(resp) >= 8 and resp[4] == CMD_READ_MODE:
            return MODES.get(resp[5], f"0x{resp[5]:02X}"), resp[6]
        return None

    def set_mode(self, mode_name: str, filter_num: int = 1) -> bool:
        code = MODES_BY_NAME.get(mode_name.upper())
        if code is None:
            return False
        resp = self._transact(CMD_SET_MODE, bytes([code, filter_num]))
        return resp is not None and resp[4] == OK_BYTE

    # ── VFO ───────────────────────────────────────────────────────────────────

    def set_vfo(self, vfo: str) -> bool:
        sub = 0x00 if vfo.upper() == "A" else 0x01
        resp = self._transact(CMD_VFO, bytes([sub]))
        return resp is not None

    def swap_vfo(self) -> bool:
        resp = self._transact(CMD_VFO, bytes([0x90]))
        return resp is not None

    def copy_vfo_a_to_b(self) -> bool:
        resp = self._transact(CMD_VFO, bytes([0xA0]))
        return resp is not None

    # ── split ─────────────────────────────────────────────────────────────────

    def set_split(self, on: bool) -> bool:
        resp = self._transact(CMD_SPLIT, bytes([0x01 if on else 0x00]))
        return resp is not None and resp[4] == OK_BYTE

    # ── ATT / pre-amp ─────────────────────────────────────────────────────────

    def set_att(self, value: int) -> bool:
        """value: 0 = off, 20 = 20 dB."""
        att_byte = 0x20 if value >= 20 else 0x00
        resp = self._transact(CMD_ATT, bytes([att_byte]))
        return resp is not None

    def read_att(self) -> Optional[int]:
        resp = self._transact(CMD_ATT)
        if resp and len(resp) >= 7 and resp[4] == CMD_ATT:
            return 20 if resp[5] == 0x20 else 0
        return None

    def set_preamp(self, value: int) -> bool:
        """value: 0 = off, 1 = Pre1, 2 = Pre2."""
        resp = self._transact(CMD_FUNC, bytes([FUNC_PREAMP, value & 0xFF]))
        return resp is not None

    def read_preamp(self) -> Optional[int]:
        resp = self._transact(CMD_FUNC, bytes([FUNC_PREAMP]))
        if resp and len(resp) >= 7 and resp[4] == CMD_FUNC:
            return resp[6]
        return None

    # ── AGC ───────────────────────────────────────────────────────────────────

    def set_agc(self, mode: int) -> bool:
        """mode: 0=off, 1=fast, 2=mid, 3=slow."""
        resp = self._transact(CMD_FUNC, bytes([FUNC_AGC, mode & 0xFF]))
        return resp is not None

    def read_agc(self) -> Optional[int]:
        resp = self._transact(CMD_FUNC, bytes([FUNC_AGC]))
        if resp and len(resp) >= 7 and resp[4] == CMD_FUNC:
            return resp[6]
        return None

    # ── levels ────────────────────────────────────────────────────────────────

    def set_level(self, sub_cmd: int, value: int) -> bool:
        """Set a level (0–255). sub_cmd from LVL_* constants."""
        resp = self._transact(CMD_LEVEL, bytes([sub_cmd]) + _encode_level(value))
        return resp is not None and resp[4] == OK_BYTE

    def read_level(self, sub_cmd: int) -> Optional[int]:
        resp = self._transact(CMD_LEVEL, bytes([sub_cmd]))
        # Response: FE FE E0 addr 14 sub lo hi FD
        if resp and len(resp) >= 9 and resp[4] == CMD_LEVEL and resp[5] == sub_cmd:
            return _decode_level(resp[6:8])
        return None

    # ── functions ─────────────────────────────────────────────────────────────

    def set_function(self, sub_cmd: int, on: bool) -> bool:
        resp = self._transact(CMD_FUNC, bytes([sub_cmd, 0x01 if on else 0x00]))
        return resp is not None and resp[4] == OK_BYTE

    def read_function(self, sub_cmd: int) -> Optional[bool]:
        resp = self._transact(CMD_FUNC, bytes([sub_cmd]))
        if resp and len(resp) >= 7 and resp[4] == CMD_FUNC and resp[5] == sub_cmd:
            return bool(resp[6])
        return None

    # ── TX / tuner ────────────────────────────────────────────────────────────

    def set_tx(self, tx: bool) -> bool:
        resp = self._transact(CMD_TX, bytes([0x00, 0x01 if tx else 0x00]))
        return resp is not None

    def start_tune(self) -> bool:
        resp = self._transact(CMD_TUNE_START, bytes([0x01, 0x02]))
        return resp is not None

    # ── meters ────────────────────────────────────────────────────────────────

    def read_smeter(self) -> Optional[int]:
        """Read receive level. Returns 0–241 (0=S0, 120=S9, 241=S9+60dB)."""
        resp = self._transact(CMD_SMETER, bytes([0x02]))
        if resp and len(resp) >= 8 and resp[4] == CMD_SMETER:
            return _decode_level(resp[6:8])
        return None


# ── CIVWorker — non-blocking CI-V thread ─────────────────────────────────────

class CIVWorker(QThread):
    """
    Runs all serial I/O in a dedicated thread so the Qt UI stays responsive.
    Commands are posted via send(); results arrive as Qt signals (queued
    connections to the main thread are automatic).
    """
    freq_updated    = pyqtSignal(int)         # Hz
    mode_updated    = pyqtSignal(str, int)    # mode_name, filter 1-3
    smeter_updated  = pyqtSignal(int)         # 0-241
    level_updated   = pyqtSignal(int, int)    # sub_cmd, value
    function_updated= pyqtSignal(int, bool)   # sub_cmd, on
    agc_updated     = pyqtSignal(int)         # 0-3
    att_updated     = pyqtSignal(int)         # 0 or 20
    preamp_updated  = pyqtSignal(int)         # 0, 1, or 2

    def __init__(self, ctrl: CIVController, parent=None):
        super().__init__(parent)
        self._ctrl    = ctrl
        self._q: queue.SimpleQueue = queue.SimpleQueue()
        self._running = True

    def send(self, fn: Callable):
        """Queue a zero-argument callable to be executed in the CI-V thread."""
        self._q.put(fn)

    def stop(self):
        self._running = False

    def run(self):
        self._initial_sync()
        t_freq = t_mode = t_smeter = 0.0
        while self._running:
            # Drain queued commands first (user actions have priority)
            while True:
                try:
                    fn = self._q.get_nowait()
                    try:
                        fn()
                    except Exception as e:
                        print(f"[civ worker] command error: {e}")
                except queue.Empty:
                    break

            now = time.monotonic()

            # Auto-poll: frequency at ~6 Hz
            if now - t_freq >= 0.15:
                f = self._ctrl.read_frequency()
                if f:
                    self.freq_updated.emit(f)
                t_freq = time.monotonic()

            # Mode at ~1 Hz
            if now - t_mode >= 1.0:
                r = self._ctrl.read_mode()
                if r:
                    self.mode_updated.emit(*r)
                t_mode = time.monotonic()

            # S-meter at ~4 Hz
            if now - t_smeter >= 0.25:
                s = self._ctrl.read_smeter()
                if s is not None:
                    self.smeter_updated.emit(s)
                t_smeter = time.monotonic()

            self.msleep(20)

    def _initial_sync(self):
        """Read all radio state on connect to seed the UI."""
        r = self._ctrl.read_frequency()
        if r: self.freq_updated.emit(r)

        r = self._ctrl.read_mode()
        if r: self.mode_updated.emit(*r)

        r = self._ctrl.read_agc()
        if r is not None: self.agc_updated.emit(r)

        r = self._ctrl.read_att()
        if r is not None: self.att_updated.emit(r)

        r = self._ctrl.read_preamp()
        if r is not None: self.preamp_updated.emit(r)

        for sub in (LVL_RF_GAIN, LVL_SQL, LVL_NR, LVL_NB, LVL_DRIVE):
            v = self._ctrl.read_level(sub)
            if v is not None:
                self.level_updated.emit(sub, v)

        for sub in (FUNC_NR, FUNC_NB, FUNC_COMP, FUNC_VOX):
            v = self._ctrl.read_function(sub)
            if v is not None:
                self.function_updated.emit(sub, v)
