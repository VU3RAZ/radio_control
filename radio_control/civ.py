"""
Icom CI-V protocol implementation.

Frame format:  FE FE <dst> <src> <cmd> [data…] FD
Controller address is always 0xE0.

CIVWorker uses a stream-reader architecture:
  • One loop: read bytes → parse frames → dispatch signals
  • Commands are pre-built frames queued as raw bytes (no lambdas, no closures)
  • Handles both polled responses (dst=0xE0) and transceive broadcasts (dst=0x00)
  • Fallback frequency poll every 0.5 s covers radios with transceive OFF
"""

import queue
import time
import serial
from typing import Optional, Tuple

from PyQt5.QtCore import QThread, pyqtSignal

# ── protocol constants ────────────────────────────────────────────────────────
PREAMBLE  = bytes([0xFE, 0xFE])
END_BYTE  = 0xFD
CTRL_ADDR = 0xE0        # controller (our) CI-V address
OK_BYTE   = 0xFB
NG_BYTE   = 0xFA

BAUD_RATES = [9600, 19200, 4800, 38400, 115200]

KNOWN_RADIOS: dict[int, str] = {
    0x94: "IC-7300",  0xA4: "IC-705",   0x88: "IC-7100",  0x98: "IC-7610",
    0xA2: "IC-9700",  0x76: "IC-7200",  0x7A: "IC-7600",  0x70: "IC-7700",
    0x80: "IC-7800",  0x8A: "IC-7850",  0x6E: "IC-756PRO", 0x3A: "IC-718",
    0x52: "IC-910H",  0x62: "IC-9100",  0x7C: "IC-7410",  0xAC: "IC-R8600",
    0x58: "IC-746",   0x46: "IC-706MK2", 0x48: "IC-706MK2G",
}

MODES: dict[int, str] = {
    0x00: "LSB",  0x01: "USB",    0x02: "AM",    0x03: "CW",
    0x04: "RTTY", 0x05: "FM",     0x06: "WFM",   0x07: "CW-R",
    0x08: "RTTY-R", 0x11: "DV",
}
MODES_BY_NAME: dict[str, int] = {v: k for k, v in MODES.items()}

# ── CI-V commands ─────────────────────────────────────────────────────────────
CMD_READ_FREQ  = 0x03
CMD_SET_FREQ   = 0x05
CMD_READ_MODE  = 0x04
CMD_SET_MODE   = 0x06
CMD_VFO        = 0x07   # sub: 00=A, 01=B, 90=swap, A0=copy A→B
CMD_SPLIT      = 0x0F   # data: 00=off, 01=on
CMD_ATT        = 0x11   # data: 00=off, 20=20 dB
CMD_SMETER     = 0x15   # sub 0x02 → receive level
CMD_LEVEL      = 0x14   # sub-commands: see LVL_* below
CMD_FUNC       = 0x16   # sub-commands: see FUNC_* below
CMD_TX         = 0x1C   # sub 00: 00=RX 01=TX; sub 01/02: tuner

# Level sub-commands (CMD_LEVEL = 0x14)
LVL_AF      = 0x01
LVL_RF_GAIN = 0x02
LVL_SQL     = 0x03
LVL_NR      = 0x06
LVL_NB      = 0x09
LVL_DRIVE   = 0x0E
LVL_MIC     = 0x0F

# Function sub-commands (CMD_FUNC = 0x16)
FUNC_PREAMP = 0x02   # data: 00=off 01=Pre1 02=Pre2
FUNC_AGC    = 0x12   # data: 00=off 01=fast 02=mid 03=slow
FUNC_NR     = 0x40
FUNC_NB     = 0x41
FUNC_COMP   = 0x44
FUNC_VOX    = 0x46


# ── BCD / level helpers ───────────────────────────────────────────────────────

def _encode_bcd_freq(freq_hz: int) -> bytes:
    result = []
    for _ in range(5):
        lo = freq_hz % 10;  freq_hz //= 10
        hi = freq_hz % 10;  freq_hz //= 10
        result.append((hi << 4) | lo)
    return bytes(result)


def _decode_bcd_freq(data: bytes) -> int:
    freq = 0;  mult = 1
    for byte in data:
        freq += (byte & 0x0F) * mult;   mult *= 10
        freq += ((byte >> 4) & 0x0F) * mult; mult *= 10
    return freq


def _encode_level(value: int) -> bytes:
    """Encode 0–255 as 4-digit BCD, 2 bytes LSB-first."""
    v = max(0, min(255, int(value)))
    d0 = v % 10; v //= 10
    d1 = v % 10; v //= 10
    d2 = v % 10; d3 = v // 10
    return bytes([(d1 << 4) | d0, (d3 << 4) | d2])


def _decode_level(data: bytes) -> int:
    lo = (data[0] & 0x0F) + ((data[0] >> 4) & 0x0F) * 10
    hi = ((data[1] & 0x0F) + ((data[1] >> 4) & 0x0F) * 10) if len(data) > 1 else 0
    return lo + hi * 100


# ── CIVController (synchronous — used by detector and build_frame) ───────────

class CIVController:
    """Manages a serial connection.  Used synchronously by the detector;
    within the app all I/O goes through CIVWorker."""

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

    def _build_frame(self, cmd: int, data: bytes = b"") -> bytes:
        return PREAMBLE + bytes([self.radio_addr, CTRL_ADDR, cmd]) + data + bytes([END_BYTE])

    # Synchronous transact — only used by the detector (port scanning)
    def _transact(self, cmd: int, data: bytes = b"") -> Optional[bytes]:
        if not self.is_connected:
            return None
        try:
            self._ser.reset_input_buffer()
            self._ser.write(self._build_frame(cmd, data))
            buf = b""
            deadline = time.monotonic() + 0.2
            while time.monotonic() < deadline:
                chunk = self._ser.read(self._ser.in_waiting or 1)
                if chunk:
                    buf += chunk
                i = 0
                while i < len(buf) - 4:
                    if buf[i] == 0xFE and buf[i+1] == 0xFE:
                        end = buf.find(bytes([END_BYTE]), i + 4)
                        if end < 0:
                            break
                        msg = buf[i:end+1]
                        if len(msg) >= 5 and msg[2] == CTRL_ADDR:
                            return msg
                        i = end + 1
                    else:
                        i += 1
        except serial.SerialException:
            pass
        return None

    def read_frequency(self) -> Optional[int]:
        resp = self._transact(CMD_READ_FREQ)
        if resp and len(resp) >= 11 and resp[4] == CMD_READ_FREQ:
            return _decode_bcd_freq(resp[5:10])
        return None


# ── CIVWorker — stream-reader CI-V thread ─────────────────────────────────────

class CIVWorker(QThread):
    """
    All serial I/O in one thread:
      run-loop  →  read bytes  →  parse frames  →  dispatch signals
                →  write queued command frames
                →  periodic S-meter + fallback freq polls

    UI controls call send_set_xxx() which pre-builds the frame in the UI
    thread and puts raw bytes on the write queue — no lambdas, no closures.
    """

    freq_updated     = pyqtSignal(int)        # Hz
    mode_updated     = pyqtSignal(str, int)   # mode_name, filter 1-3
    smeter_updated   = pyqtSignal(int)        # 0-241
    level_updated    = pyqtSignal(int, int)   # sub_cmd, value
    function_updated = pyqtSignal(int, bool)  # sub_cmd, on/off
    agc_updated      = pyqtSignal(int)        # 0-3
    att_updated      = pyqtSignal(int)        # 0 or 20
    preamp_updated   = pyqtSignal(int)        # 0, 1, 2
    split_updated    = pyqtSignal(bool)       # split on/off
    tx_updated       = pyqtSignal(bool)       # TX on/off
    status_msg       = pyqtSignal(str)

    def __init__(self, ctrl: CIVController, parent=None):
        super().__init__(parent)
        self._ctrl    = ctrl
        self._wq: queue.SimpleQueue = queue.SimpleQueue()   # frames to write
        self._running = True
        self._buf: bytes = b""

    # ── public send API (called from UI thread) ───────────────────────────────

    def _enqueue(self, cmd: int, data: bytes = b""):
        """Build frame in the calling thread and queue raw bytes for writing."""
        self._wq.put(self._ctrl._build_frame(cmd, data))

    def send_set_freq(self, hz: int):
        self._enqueue(CMD_SET_FREQ, _encode_bcd_freq(hz))

    def send_set_mode(self, mode_name: str, filt: int):
        self._enqueue(CMD_SET_MODE, bytes([MODES_BY_NAME.get(mode_name, 0x01), filt]))

    def send_set_level(self, sub: int, val: int):
        self._enqueue(CMD_LEVEL, bytes([sub]) + _encode_level(val))

    def send_set_function(self, sub: int, on: bool):
        self._enqueue(CMD_FUNC, bytes([sub, 0x01 if on else 0x00]))

    def send_set_agc(self, mode: int):
        self._enqueue(CMD_FUNC, bytes([FUNC_AGC, mode]))

    def send_set_att(self, val: int):
        self._enqueue(CMD_ATT, bytes([0x20 if val >= 20 else 0x00]))

    def send_set_preamp(self, val: int):
        self._enqueue(CMD_FUNC, bytes([FUNC_PREAMP, val & 0xFF]))

    def send_set_split(self, on: bool):
        self._enqueue(CMD_SPLIT, bytes([0x01 if on else 0x00]))

    def send_set_vfo(self, vfo: str):
        self._enqueue(CMD_VFO, bytes([0x00 if vfo.upper() == "A" else 0x01]))

    def send_swap_vfo(self):
        self._enqueue(CMD_VFO, bytes([0x90]))

    def send_copy_vfo(self):
        self._enqueue(CMD_VFO, bytes([0xA0]))

    def send_set_tx(self, tx: bool):
        self._enqueue(CMD_TX, bytes([0x00, 0x01 if tx else 0x00]))

    def send_start_tune(self):
        self._enqueue(CMD_TX, bytes([0x01, 0x02]))

    def stop(self):
        self._running = False

    # ── main loop ─────────────────────────────────────────────────────────────

    def run(self):
        ser = self._ctrl._ser
        if ser is None or not ser.is_open:
            self.status_msg.emit("CI-V: serial port not open")
            return

        # Short timeout: read() returns promptly when data arrives,
        # or after 50 ms when idle — allows queue draining without spin.
        ser.timeout = 0.05

        self.status_msg.emit(f"CI-V: syncing {self._ctrl.model}…")
        self._initial_sync(ser)
        self.status_msg.emit(f"CI-V: {self._ctrl.model}  {self._ctrl.port}  {self._ctrl.baud_rate} baud")

        t_smeter   = 0.0
        t_fallback = 0.0
        t_state    = 0.0    # periodic full-state poll for radios with transceive OFF
        _state_queries = [
            self._ctrl._build_frame(CMD_READ_FREQ),
            self._ctrl._build_frame(CMD_READ_MODE),
            self._ctrl._build_frame(CMD_FUNC,  bytes([FUNC_AGC])),
            self._ctrl._build_frame(CMD_ATT),
            self._ctrl._build_frame(CMD_FUNC,  bytes([FUNC_PREAMP])),
            self._ctrl._build_frame(CMD_SPLIT),
        ]
        for _sub in (LVL_AF, LVL_RF_GAIN, LVL_SQL, LVL_NR, LVL_NB, LVL_DRIVE):
            _state_queries.append(self._ctrl._build_frame(CMD_LEVEL, bytes([_sub])))
        for _sub in (FUNC_NR, FUNC_NB, FUNC_COMP, FUNC_VOX):
            _state_queries.append(self._ctrl._build_frame(CMD_FUNC, bytes([_sub])))
        _sq_idx = 0     # index into _state_queries — one frame per poll cycle

        while self._running:
            # ── 1. Read available bytes and parse frames ──────────────────────
            try:
                data = ser.read(256)
                if data:
                    self._buf += data
                    self._parse()
            except serial.SerialException as exc:
                self.status_msg.emit(f"CI-V error: {exc}")
                self.msleep(500)
                continue

            # ── 2. Write queued command frames ────────────────────────────────
            while True:
                try:
                    frame = self._wq.get_nowait()
                    ser.write(frame)
                except queue.Empty:
                    break
                except serial.SerialException:
                    break

            now = time.monotonic()

            # ── 3. S-meter poll (10 Hz) ──────────────────────────────────────
            if now - t_smeter >= 0.1:
                try:
                    ser.write(self._ctrl._build_frame(CMD_SMETER, bytes([0x02])))
                except Exception:
                    pass
                t_smeter = now

            # ── 4. Freq poll (10 Hz) ──────────────────────────────────────────
            if now - t_fallback >= 0.1:
                try:
                    ser.write(self._ctrl._build_frame(CMD_READ_FREQ))
                except Exception:
                    pass
                t_fallback = now

            # ── 5. Full-state poll (one frame per 100 ms cycle) ───────────────
            #     Keeps GUI in sync when transceive is disabled on the radio.
            if now - t_state >= 0.1:
                try:
                    ser.write(_state_queries[_sq_idx])
                except Exception:
                    pass
                _sq_idx = (_sq_idx + 1) % len(_state_queries)
                t_state = now

    # ── initial sync ──────────────────────────────────────────────────────────

    def _initial_sync(self, ser: serial.Serial):
        """Send read queries for all radio state; responses handled by _dispatch."""
        queries = [
            self._ctrl._build_frame(CMD_READ_FREQ),
            self._ctrl._build_frame(CMD_READ_MODE),
            self._ctrl._build_frame(CMD_FUNC,  bytes([FUNC_AGC])),
            self._ctrl._build_frame(CMD_ATT),
            self._ctrl._build_frame(CMD_FUNC,  bytes([FUNC_PREAMP])),
            self._ctrl._build_frame(CMD_SPLIT),
            self._ctrl._build_frame(CMD_TX,    bytes([0x00])),
        ]
        for sub in (LVL_AF, LVL_RF_GAIN, LVL_SQL, LVL_NR, LVL_NB, LVL_DRIVE, LVL_MIC):
            queries.append(self._ctrl._build_frame(CMD_LEVEL, bytes([sub])))
        for sub in (FUNC_NR, FUNC_NB, FUNC_COMP, FUNC_VOX):
            queries.append(self._ctrl._build_frame(CMD_FUNC, bytes([sub])))

        try:
            for frame in queries:
                ser.write(frame)
                time.sleep(0.03)        # brief gap so radio isn't overwhelmed
        except Exception as exc:
            print(f"[CIV] sync write error: {exc}")
            return

        # Collect responses for 1.5 seconds
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            try:
                data = ser.read(256)
                if data:
                    self._buf += data
                    self._parse()
            except Exception:
                break
            time.sleep(0.01)

    # ── frame parser ──────────────────────────────────────────────────────────

    def _parse(self):
        """Extract and dispatch all complete frames from self._buf."""
        while True:
            idx = self._buf.find(b"\xfe\xfe")
            if idx < 0:
                # No preamble; keep trailing byte in case it's start of 0xFE pair
                self._buf = self._buf[-1:] if self._buf else b""
                return
            if idx > 0:
                self._buf = self._buf[idx:]     # discard junk before preamble

            if len(self._buf) < 6:
                return                          # need more data

            end = self._buf.find(bytes([END_BYTE]), 4)
            if end < 0:
                if len(self._buf) > 512:
                    self._buf = b""             # overflow protection
                return

            self._dispatch(self._buf[:end + 1])
            self._buf = self._buf[end + 1:]

    # ── frame dispatcher ──────────────────────────────────────────────────────

    def _dispatch(self, frame: bytes):
        if len(frame) < 6:
            return

        dst  = frame[2]
        src  = frame[3]
        cmd  = frame[4]
        data = frame[5:-1]      # between cmd byte and FD

        # Only accept frames FROM our radio
        if src != self._ctrl.radio_addr:
            return

        # Accept frames addressed to us (E0) or transceive broadcasts (00)
        if dst not in (CTRL_ADDR, 0x00):
            return

        # ── frequency ─────────────────────────────────────────────────────────
        # cmd 0x00 = CI-V transceive freq change; cmd 0x03 = polled response
        if cmd in (0x00, CMD_READ_FREQ) and len(data) >= 5:
            freq = _decode_bcd_freq(data[:5])
            if freq > 0:
                self.freq_updated.emit(freq)

        # ── mode ──────────────────────────────────────────────────────────────
        # cmd 0x01 = transceive mode change; cmd 0x04 = polled response
        elif cmd in (0x01, CMD_READ_MODE) and len(data) >= 2:
            self.mode_updated.emit(
                MODES.get(data[0], f"0x{data[0]:02X}"), data[1]
            )

        # ── levels (0x14) ─────────────────────────────────────────────────────
        elif cmd == CMD_LEVEL and len(data) >= 3:
            # data = [sub_cmd, lo_bcd, hi_bcd]
            self.level_updated.emit(data[0], _decode_level(data[1:3]))

        # ── functions (0x16) ──────────────────────────────────────────────────
        elif cmd == CMD_FUNC and len(data) >= 2:
            sub, val = data[0], data[1]
            if sub == FUNC_AGC:
                self.agc_updated.emit(val)
            elif sub == FUNC_PREAMP:
                self.preamp_updated.emit(val)
            elif sub in (FUNC_NR, FUNC_NB, FUNC_COMP, FUNC_VOX):
                self.function_updated.emit(sub, bool(val))

        # ── ATT (0x11) ────────────────────────────────────────────────────────
        elif cmd == CMD_ATT and len(data) >= 1:
            self.att_updated.emit(20 if data[0] == 0x20 else 0)

        # ── S-meter (0x15, sub 0x02) ──────────────────────────────────────────
        # len >= 2 because some firmware returns only 1 payload byte after sub-cmd
        elif cmd == CMD_SMETER and len(data) >= 2 and data[0] == 0x02:
            self.smeter_updated.emit(_decode_level(data[1:3]))

        # ── split (0x0F) ──────────────────────────────────────────────────────
        elif cmd == CMD_SPLIT and len(data) >= 1:
            self.split_updated.emit(bool(data[0]))

        # ── TX/RX state (0x1C, sub 0x00) ─────────────────────────────────────
        elif cmd == CMD_TX and len(data) >= 2 and data[0] == 0x00:
            self.tx_updated.emit(bool(data[1]))

        # OK / NG — silently ignored (confirmations for our set commands)
        elif cmd in (OK_BYTE, NG_BYTE):
            pass
