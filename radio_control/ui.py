"""
Main application window.

Layout
──────
  ┌──────────────────────────────────────────────────────┐
  │  [Radio model]          [VFO: 14.100 000 MHz] [Mode] │
  ├──────────────────────────────────────────────────────┤
  │                                                      │
  │          Spectrum  (amplitude dB  vs  frequency)     │
  │                                                      │
  ├──────────────────────────────────────────────────────┤
  │                                                      │
  │          Waterfall (time scroll vs  frequency)       │
  │                                                      │
  └──────────────────────────────────────────────────────┘
  │  Status bar                                          │
  └──────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import numpy as np
import pyqtgraph as pg

from PyQt5.QtCore  import Qt, QTimer, pyqtSlot
from PyQt5.QtGui   import QFont, QColor
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QSplitter, QStatusBar,
)

from .civ   import CIVController
from .audio import AudioWorker

# ── tuneable constants ────────────────────────────────────────────────────────
FFT_SIZE       = 2048
WATERFALL_ROWS = 300          # vertical history lines
DB_FLOOR       = -120.0       # bottom of colour scale
DB_CEIL        = -20.0        # top of colour scale (adjust to taste)
CIV_POLL_MS    = 200          # how often to ask radio for its VFO frequency


def _make_waterfall_lut() -> np.ndarray:
    """Build a 256-entry RGB LUT: deep-blue → cyan → green → yellow → red."""
    lut = np.zeros((256, 3), dtype=np.uint8)
    stops = [
        (0,   (0,   0,   60)),
        (64,  (0,   0,  200)),
        (96,  (0,  200, 200)),
        (160, (0,  255,   0)),
        (200, (255, 200,  0)),
        (230, (255,  80,  0)),
        (255, (255, 255, 255)),
    ]
    for i in range(len(stops) - 1):
        x0, c0 = stops[i]
        x1, c1 = stops[i + 1]
        for x in range(x0, x1 + 1):
            t = (x - x0) / (x1 - x0)
            lut[x] = [int(c0[j] + t * (c1[j] - c0[j])) for j in range(3)]
    return lut


class MainWindow(QMainWindow):
    def __init__(
        self,
        radio:        CIVController | None = None,
        audio_device: int | None           = None,
        sample_rate:  int                  = 48000,
        is_iq:        bool                 = False,
    ):
        super().__init__()
        self.radio        = radio
        self.sample_rate  = sample_rate
        self.is_iq        = is_iq
        self.vfo_hz       = 14_100_000
        self.mode_str     = "---"

        # Number of frequency bins produced by the FFT
        self._n_bins = FFT_SIZE if is_iq else FFT_SIZE // 2 + 1

        # Waterfall buffer: rows = time, cols = frequency
        self._wf_buf = np.full((WATERFALL_ROWS, self._n_bins), DB_FLOOR, dtype=np.float32)

        pg.setConfigOptions(antialias=False, background="k", foreground="w")

        self._build_ui()
        self._build_audio(audio_device)
        if radio:
            self._build_civ_timer()
            self._update_info_labels()

        self._update_freq_axis()

    # ─────────────────────────────────────────────── UI construction ──────────

    def _build_ui(self):
        self.setWindowTitle("Icom Radio Control – Spectrum & Waterfall")
        self.setMinimumSize(960, 720)

        root = QWidget()
        self.setCentralWidget(root)
        vbox = QVBoxLayout(root)
        vbox.setContentsMargins(6, 6, 6, 4)
        vbox.setSpacing(3)

        # ── info bar ──────────────────────────────────────────────────────────
        hbar = QHBoxLayout()

        self._lbl_radio = QLabel("Radio: not connected")
        self._lbl_radio.setStyleSheet("color: #aaa;")

        self._lbl_freq = QLabel("--- MHz")
        freq_font = QFont("Monospace", 20, QFont.Bold)
        self._lbl_freq.setFont(freq_font)
        self._lbl_freq.setStyleSheet("color: #0f0;")
        self._lbl_freq.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self._lbl_mode = QLabel("---")
        self._lbl_mode.setStyleSheet("color: #ff0; font-size: 14px;")
        self._lbl_mode.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        hbar.addWidget(self._lbl_radio)
        hbar.addStretch()
        hbar.addWidget(self._lbl_mode)
        hbar.addSpacing(12)
        hbar.addWidget(self._lbl_freq)
        vbox.addLayout(hbar)

        # ── plots ─────────────────────────────────────────────────────────────
        splitter = QSplitter(Qt.Vertical)

        # Spectrum
        self._spec_plot = pg.PlotWidget()
        self._spec_plot.setLabel("left",   "Amplitude", units="dBFS")
        self._spec_plot.setLabel("bottom", "Frequency", units="Hz")
        self._spec_plot.showGrid(x=True, y=True, alpha=0.25)
        self._spec_plot.setYRange(DB_FLOOR, 0, padding=0)
        self._spec_plot.setMouseEnabled(x=True, y=True)
        self._spec_curve = self._spec_plot.plot(
            pen=pg.mkPen(color=(80, 220, 80), width=1)
        )
        # Reference line at noise floor marker
        self._spec_plot.addItem(
            pg.InfiniteLine(pos=DB_CEIL, angle=0,
                            pen=pg.mkPen(color=(255, 80, 0), style=Qt.DashLine))
        )
        splitter.addWidget(self._spec_plot)

        # Waterfall
        wf_widget = pg.PlotWidget()
        wf_widget.setLabel("bottom", "Frequency", units="Hz")
        wf_widget.setLabel("left",   "Time →")
        wf_widget.setMouseEnabled(x=True, y=False)

        self._wf_img = pg.ImageItem()
        self._wf_img.setLookupTable(_make_waterfall_lut())
        self._wf_img.setLevels([DB_FLOOR, DB_CEIL])
        wf_widget.addItem(self._wf_img)
        # Link x-axis of waterfall to spectrum for synchronised pan/zoom
        wf_widget.setXLink(self._spec_plot)

        self._wf_plot_widget = wf_widget
        splitter.addWidget(wf_widget)

        splitter.setSizes([260, 400])
        vbox.addWidget(splitter)

        # ── status bar ────────────────────────────────────────────────────────
        self._status = QStatusBar()
        self.setStatusBar(self._status)
        self._status.showMessage("Starting audio…")

    # ─────────────────────────────────────────────── audio thread ─────────────

    def _build_audio(self, device):
        self._audio = AudioWorker(
            device=device,
            sample_rate=self.sample_rate,
            fft_size=FFT_SIZE,
            is_iq=self.is_iq,
        )
        self._audio.fft_ready.connect(self._on_fft)
        self._audio.start()

        mode = "IQ (stereo)" if self.is_iq else "audio (mono)"
        self._status.showMessage(
            f"Audio: {mode} · {self.sample_rate} Hz · FFT {FFT_SIZE} bins"
        )

    # ─────────────────────────────────────────────── CI-V polling ─────────────

    def _build_civ_timer(self):
        self._civ_timer = QTimer(self)
        self._civ_timer.timeout.connect(self._poll_radio)
        self._civ_timer.start(CIV_POLL_MS)

    @pyqtSlot()
    def _poll_radio(self):
        if not self.radio or not self.radio.is_connected:
            return
        freq = self.radio.read_frequency()
        if freq:
            changed = freq != self.vfo_hz
            self.vfo_hz = freq
            self._lbl_freq.setText(f"{freq / 1e6:.6f} MHz")
            if changed:
                self._update_freq_axis()

        result = self.radio.read_mode()
        if result:
            mode_name, filt = result
            self.mode_str = mode_name
            self._lbl_mode.setText(f"{mode_name}  F{filt}")

    # ─────────────────────────────────────────────── freq axis ────────────────

    def _freq_array(self) -> np.ndarray:
        if self.is_iq:
            return self.vfo_hz + np.linspace(
                -self.sample_rate / 2, self.sample_rate / 2, FFT_SIZE,
                dtype=np.float64
            )
        else:
            return self.vfo_hz + np.linspace(
                0, self.sample_rate / 2, FFT_SIZE // 2 + 1,
                dtype=np.float64
            )

    def _update_freq_axis(self):
        self._freqs = self._freq_array()
        f0, f1 = self._freqs[0], self._freqs[-1]
        self._spec_plot.setXRange(f0, f1, padding=0)
        # Update waterfall image transform to match frequency axis
        rect = pg.QtCore.QRectF(f0, 0, f1 - f0, WATERFALL_ROWS)
        self._wf_img.setRect(rect)

    # ─────────────────────────────────────────────── FFT update ───────────────

    @pyqtSlot(object)
    def _on_fft(self, fft_db: np.ndarray):
        n = min(len(fft_db), self._n_bins)
        freqs = self._freqs[:n] if hasattr(self, "_freqs") else np.arange(n)

        # ── spectrum ──────────────────────────────────────────────────────────
        self._spec_curve.setData(freqs, fft_db[:n])

        # ── waterfall ─────────────────────────────────────────────────────────
        # Roll buffer down (oldest row moves toward the bottom, newest at top)
        self._wf_buf[1:] = self._wf_buf[:-1]
        self._wf_buf[0, :n] = fft_db[:n]

        # ImageItem expects (width=freq, height=time) → transpose
        self._wf_img.setImage(
            self._wf_buf[:, :n].T,
            autoLevels=False,
            autoHistogramRange=False,
        )

    # ─────────────────────────────────────────────── helpers ──────────────────

    def _update_info_labels(self):
        if self.radio:
            self._lbl_radio.setText(
                f"Radio: {self.radio.model}  ·  "
                f"{self.radio.port}  ·  {self.radio.baud_rate} baud"
            )
            self._lbl_radio.setStyleSheet("color: #0cf;")

    # ─────────────────────────────────────────────── cleanup ──────────────────

    def closeEvent(self, event):
        if hasattr(self, "_civ_timer"):
            self._civ_timer.stop()
        if hasattr(self, "_audio"):
            self._audio.stop()
            self._audio.wait(3000)
        if self.radio:
            self.radio.disconnect()
        event.accept()
