"""
Audio capture thread with real-time FFT.

Runs sounddevice.InputStream in a QThread and emits fft_ready
with a numpy array of dB magnitudes (ready for the spectrum/waterfall UI).

Mono mode  – real FFT,  positive frequencies only (0 … Fs/2).
IQ mode    – complex FFT shifted to (−Fs/2 … +Fs/2), centred on VFO.
"""

import numpy as np
import sounddevice as sd

from PyQt5.QtCore import QThread, pyqtSignal


class AudioWorker(QThread):
    fft_ready = pyqtSignal(object)   # numpy array of float32 dB values

    def __init__(self,
                 device=None,
                 sample_rate: int = 48000,
                 fft_size: int = 2048,
                 is_iq: bool = False,
                 parent=None):
        super().__init__(parent)
        self.device      = device
        self.sample_rate = sample_rate
        self.fft_size    = fft_size
        self.is_iq       = is_iq
        self._stop_flag  = False

        # Pre-compute window once
        self._window = np.hanning(fft_size).astype(np.float32)
        # Normalisation factor so 0 dBFS ≈ 0 dB
        self._win_norm = np.sum(self._window)

        # Ring buffer for overlap-add (accumulate until we have fft_size samples)
        channels = 2 if is_iq else 1
        self._buf    = np.zeros((fft_size, channels), dtype=np.float32)
        self._buf_pos = 0

    # ---------------------------------------------------------------------- run

    def run(self):
        channels = 2 if self.is_iq else 1
        blocksize = self.fft_size // 4   # 75 % overlap

        try:
            with sd.InputStream(
                device=self.device,
                channels=channels,
                samplerate=self.sample_rate,
                dtype="float32",
                blocksize=blocksize,
                callback=self._callback,
            ):
                while not self._stop_flag:
                    self.msleep(20)
        except Exception as exc:
            print(f"[audio] Stream error: {exc}")

    def stop(self):
        self._stop_flag = True

    # ---------------------------------------------------------------- callback

    def _callback(self, indata, frames, time_info, status):
        # Append new samples to ring buffer
        n = min(frames, self.fft_size)
        self._buf = np.roll(self._buf, -n, axis=0)
        self._buf[-n:] = indata[:n]

        if self.is_iq:
            # Combine channels as complex I+jQ, remove DC offset before FFT
            sig = (self._buf[:, 0] + 1j * self._buf[:, 1]).astype(np.complex64)
            sig -= sig.mean()   # kills the centre-frequency carrier spike
            windowed = sig * self._window
            spectrum  = np.fft.fftshift(np.abs(np.fft.fft(windowed)))
            spectrum /= self._win_norm
        else:
            sig = self._buf[:, 0] - self._buf[:, 0].mean()   # remove DC
            windowed = sig * self._window
            # rfft gives positive frequencies only (fft_size//2 + 1 bins)
            spectrum = np.abs(np.fft.rfft(windowed))
            spectrum /= self._win_norm

        # Convert to dB (floor at -140 dB to avoid log(0))
        spectrum = np.maximum(spectrum, 1e-7)
        fft_db = (20.0 * np.log10(spectrum)).astype(np.float32)

        self.fft_ready.emit(fft_db)
