"""
IC-7300 style front panel with real-time spectrum and waterfall.

Layout (top → bottom)
─────────────────────
  ┌─ display ──────────────────────────────────────────────────────┐
  │  [VFO A][VFO B][A⇄B][A=B]   14.100.000   [USB][FIL1]  STEP  │
  │  S-meter bar ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░  S7+10dB   │
  └────────────────────────────────────────────────────────────────┘
  ┌─ bands ──────────────────────────────────────────────────────── ┐
  │  [160m][80m][60m][40m][30m][20m][17m][15m][12m][10m][6m]       │
  └────────────────────────────────────────────────────────────────┘
  ┌─ levels ────────────────────────────────────────────────────────┐
  │  RF GAIN  ══════════════  SQL  ══════════  NR Lvl  NB Lvl  DRIVE│
  └────────────────────────────────────────────────────────────────┘
  ┌─ functions ─────────────────────────────────────────────────────┐
  │  AGC[F][M][S]  Pre[−][1][2]  ATT[−][20]│[NR][NB][COMP][VOX]   │
  │  [SPLIT][PTT][TUNE]                                             │
  └────────────────────────────────────────────────────────────────┘
  Spectrum ─────────────────────────────────────────────────────────
  Waterfall ────────────────────────────────────────────────────────
"""

from __future__ import annotations

import numpy as np
import pyqtgraph as pg

from PyQt5.QtCore    import Qt, QThread, QTimer, pyqtSignal, pyqtSlot
from PyQt5.QtGui     import QFont
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QSplitter, QStatusBar, QPushButton, QButtonGroup,
    QSlider, QProgressBar, QFrame, QGroupBox, QInputDialog, QComboBox,
)

from .civ import (
    CIVController, CIVWorker,
    LVL_RF_GAIN, LVL_SQL, LVL_NR, LVL_NB, LVL_DRIVE,
    FUNC_NR, FUNC_NB, FUNC_COMP, FUNC_VOX,
)
from .audio import AudioWorker

# ── tuneable display constants ────────────────────────────────────────────────
FFT_SIZE       = 2048
WATERFALL_ROWS = 300
DB_FLOOR       = -120.0
DB_CEIL        = -20.0

# ── band table (label, centre Hz) ────────────────────────────────────────────
BANDS = [
    ("160m", 1_850_000),  ("80m",  3_700_000),  ("60m",  5_357_000),
    ("40m",  7_100_000),  ("30m", 10_100_000),  ("20m", 14_200_000),
    ("17m", 18_100_000),  ("15m", 21_200_000),  ("12m", 24_940_000),
    ("10m", 28_500_000),  ("6m",  50_200_000),
]

MODES_LIST  = ["LSB", "USB", "CW", "CW-R", "RTTY", "RTTY-R", "AM", "FM"]
FILTER_LIST = ["FIL1", "FIL2", "FIL3"]
AGC_LIST    = [("F", 1), ("M", 2), ("S", 3)]
PRE_LIST    = [("−", 0), ("1", 1), ("2", 2)]
ATT_LIST    = [("−", 0), ("20", 20)]
STEPS       = [("1 Hz", 1), ("100 Hz", 100), ("1 kHz", 1_000),
               ("5 kHz", 5_000), ("10 kHz", 10_000), ("100 kHz", 100_000)]

# ── global dark stylesheet ────────────────────────────────────────────────────
_STYLE = """
QMainWindow, QWidget { background: #1a1a1a; color: #cccccc; font-size: 11px; }
QFrame#panel {
    background: #141414; border: 1px solid #2a2a2a; border-radius: 4px;
}
QPushButton {
    background: #2d2d2d; color: #bbbbbb;
    border: 1px solid #3a3a3a; border-radius: 3px;
    padding: 2px 7px; min-height: 24px; min-width: 38px;
}
QPushButton:hover  { background: #383838; border-color: #555; }
QPushButton:checked {
    background: #b84400; color: #fff; border-color: #e05500;
    font-weight: bold;
}
QPushButton#ptt { min-width: 60px; font-weight: bold; }
QPushButton#ptt:checked { background: #cc0000; border-color: #ff2222; }
QPushButton#tune:pressed { background: #0055aa; }
QGroupBox {
    border: 1px solid #333; border-radius: 3px;
    margin-top: 14px; padding: 4px 4px 2px 4px;
    font-size: 10px; color: #666;
}
QGroupBox::title { subcontrol-origin: margin; left: 6px; top: 1px; }
QSlider::groove:horizontal {
    height: 5px; background: #252525; border: 1px solid #333; border-radius: 2px;
}
QSlider::handle:horizontal {
    width: 13px; height: 13px; background: #0077cc;
    border-radius: 6px; margin: -4px 0;
}
QSlider::sub-page:horizontal { background: #005599; border-radius: 2px; }
QProgressBar {
    border: 1px solid #2a2a2a; background: #0a0a0a; border-radius: 2px;
}
QComboBox {
    background: #252525; border: 1px solid #383838; color: #ccc;
    padding: 1px 4px; border-radius: 2px;
}
QComboBox::drop-down { border: none; }
QLabel { color: #aaa; }
QLabel#freq_lbl {
    background: #000d1a; color: #00ff99;
    font-family: Monospace; font-size: 44px; font-weight: bold;
    border: 1px solid #1a3a2a; padding: 4px 14px;
    letter-spacing: 2px;
}
QLabel#mode_lbl {
    background: #000d1a; color: #ffcc00;
    font-family: Monospace; font-size: 16px; font-weight: bold;
    border: 1px solid #3a3a1a; padding: 4px 8px; min-width: 70px;
}
QLabel#filter_lbl {
    background: #000d1a; color: #44aaff;
    font-family: Monospace; font-size: 14px;
    border: 1px solid #1a2a3a; padding: 4px 6px; min-width: 44px;
}
QLabel#radio_lbl { color: #0099cc; font-size: 11px; }
"""


# ── helper widgets ────────────────────────────────────────────────────────────

def _panel() -> QFrame:
    f = QFrame()
    f.setObjectName("panel")
    return f


def _btn(text: str, checkable: bool = False, obj: str = "") -> QPushButton:
    b = QPushButton(text)
    b.setCheckable(checkable)
    if obj:
        b.setObjectName(obj)
    return b


def _group(title: str) -> QGroupBox:
    return QGroupBox(title)


def _exclusive_group(parent_layout, title: str,
                     items: list[tuple[str, object]],
                     on_select) -> tuple[QGroupBox, list[QPushButton]]:
    """Return a labelled QGroupBox of exclusive checkable buttons."""
    grp = _group(title)
    hbox = QHBoxLayout(grp)
    hbox.setContentsMargins(3, 2, 3, 2)
    hbox.setSpacing(2)
    btn_grp = QButtonGroup(grp)
    btn_grp.setExclusive(True)
    btns = []
    for label, value in items:
        b = _btn(label, checkable=True)
        btn_grp.addButton(b)
        hbox.addWidget(b)
        v = value   # capture by value
        b.clicked.connect(lambda checked, val=v: on_select(val))
        btns.append(b)
    return grp, btns


class LevelControl(QWidget):
    """Vertical labelled slider with debounced valueChanged signal."""
    value_changed = pyqtSignal(int)

    def __init__(self, label: str, default: int = 200, parent=None):
        super().__init__(parent)
        vbox = QVBoxLayout(self)
        vbox.setContentsMargins(2, 2, 2, 2)
        vbox.setSpacing(1)

        lbl = QLabel(label)
        lbl.setAlignment(Qt.AlignCenter)
        lbl.setStyleSheet("font-size: 9px; color: #777;")
        vbox.addWidget(lbl)

        self._slider = QSlider(Qt.Horizontal)
        self._slider.setRange(0, 255)
        self._slider.setValue(default)
        self._slider.setFixedHeight(20)
        vbox.addWidget(self._slider)

        self._val_lbl = QLabel(str(default))
        self._val_lbl.setAlignment(Qt.AlignCenter)
        self._val_lbl.setStyleSheet("font-size: 10px; color: #ccc;")
        vbox.addWidget(self._val_lbl)

        # 120 ms debounce so dragging doesn't flood CI-V
        self._debounce = QTimer(singleShot=True)
        self._debounce.timeout.connect(lambda: self.value_changed.emit(self._slider.value()))
        self._slider.valueChanged.connect(self._on_change)

    def _on_change(self, v):
        self._val_lbl.setText(str(v))
        self._debounce.start(120)

    def set_value_quiet(self, v: int):
        self._slider.blockSignals(True)
        self._slider.setValue(v)
        self._val_lbl.setText(str(v))
        self._slider.blockSignals(False)

    def value(self) -> int:
        return self._slider.value()


class SMeterWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        hbox = QHBoxLayout(self)
        hbox.setContentsMargins(2, 0, 2, 0)
        hbox.setSpacing(4)

        hbox.addWidget(QLabel("S"))

        self._bar = QProgressBar()
        self._bar.setRange(0, 241)
        self._bar.setValue(0)
        self._bar.setTextVisible(False)
        self._bar.setFixedHeight(13)
        self._bar.setStyleSheet("""
            QProgressBar { border:1px solid #222; background:#090909; border-radius:2px; }
            QProgressBar::chunk {
                background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                    stop:0.00 #003300, stop:0.40 #00cc00,
                    stop:0.65 #aacc00, stop:0.80 #ffaa00,
                    stop:0.90 #ff4400, stop:1.00 #ff0000);
                border-radius:2px;
            }
        """)
        # S-unit tick marks
        tick_lbl = QLabel("S1  S3  S5  S7  S9  +20  +40  +60")
        tick_lbl.setStyleSheet("font-size: 8px; color: #555;")
        v = QVBoxLayout()
        v.setSpacing(0)
        v.setContentsMargins(0, 0, 0, 0)
        v.addWidget(self._bar)
        v.addWidget(tick_lbl)
        hbox.addLayout(v, 1)

        self._lbl = QLabel("S0")
        self._lbl.setFixedWidth(58)
        self._lbl.setStyleSheet("color:#00ff88; font-size:12px; font-weight:bold;")
        hbox.addWidget(self._lbl)

    def set_value(self, val: int):
        self._bar.setValue(val)
        if val <= 120:
            self._lbl.setText(f"S{val * 9 // 120}")
        else:
            self._lbl.setText(f"S9+{(val - 120) // 2}dB")


# ── main window ───────────────────────────────────────────────────────────────

def _make_waterfall_lut() -> np.ndarray:
    lut = np.zeros((256, 3), dtype=np.uint8)
    stops = [(0,(0,0,50)),(60,(0,0,180)),(100,(0,180,200)),
             (160,(0,255,0)),(200,(255,200,0)),(230,(255,70,0)),(255,(255,255,255))]
    for i in range(len(stops)-1):
        x0,c0 = stops[i]; x1,c1 = stops[i+1]
        for x in range(x0, x1+1):
            t = (x-x0)/(x1-x0)
            lut[x] = [int(c0[j]+t*(c1[j]-c0[j])) for j in range(3)]
    return lut


class MainWindow(QMainWindow):
    def __init__(self, radio: CIVController | None = None,
                 audio_device=None, sample_rate: int = 48000, is_iq: bool = False):
        super().__init__()
        self.radio       = radio
        self.sample_rate = sample_rate
        self.is_iq       = is_iq
        self.vfo_hz      = 14_200_000
        self._tune_step  = 1_000
        self._n_bins     = FFT_SIZE if is_iq else FFT_SIZE // 2 + 1
        self._wf_buf     = np.full((WATERFALL_ROWS, self._n_bins), DB_FLOOR, dtype=np.float32)
        self._last_f0    = None
        self._civ: CIVWorker | None = None

        self.setStyleSheet(_STYLE)
        pg.setConfigOptions(antialias=False, background="#0a0a0a", foreground="#888888")

        self._build_ui()
        self._build_audio(audio_device)
        if radio and radio.is_connected:
            self._start_civ()

    # ═════════════════════════════════════════════ build UI ═══════════════════

    def _build_ui(self):
        self.setWindowTitle("Icom Radio Control")
        self.setMinimumSize(1020, 800)

        root = QWidget()
        self.setCentralWidget(root)
        vbox = QVBoxLayout(root)
        vbox.setContentsMargins(6, 6, 6, 4)
        vbox.setSpacing(4)

        vbox.addWidget(self._build_display_panel())
        vbox.addWidget(self._build_band_panel())
        vbox.addWidget(self._build_level_panel())
        vbox.addWidget(self._build_function_panel())
        vbox.addWidget(self._build_plots(), stretch=1)

        self._status = QStatusBar()
        self.setStatusBar(self._status)
        self._status.showMessage("Initialising…")

    # ── display panel ─────────────────────────────────────────────────────────

    def _build_display_panel(self) -> QFrame:
        p = _panel()
        vbox = QVBoxLayout(p)
        vbox.setContentsMargins(8, 6, 8, 6)
        vbox.setSpacing(4)

        # ── row 1: VFO controls + frequency display + mode/filter ────────────
        row1 = QHBoxLayout()
        row1.setSpacing(6)

        # VFO buttons
        vfo_box = QVBoxLayout()
        vfo_box.setSpacing(2)

        top_vfo = QHBoxLayout()
        top_vfo.setSpacing(2)
        self._btn_vfoa = _btn("VFO A", checkable=True)
        self._btn_vfob = _btn("VFO B", checkable=True)
        self._btn_vfoa.setChecked(True)
        top_vfo.addWidget(self._btn_vfoa)
        top_vfo.addWidget(self._btn_vfob)

        bot_vfo = QHBoxLayout()
        bot_vfo.setSpacing(2)
        btn_swap = _btn("A⇄B")
        btn_copy = _btn("A=B")
        bot_vfo.addWidget(btn_swap)
        bot_vfo.addWidget(btn_copy)

        vfo_box.addLayout(top_vfo)
        vfo_box.addLayout(bot_vfo)
        row1.addLayout(vfo_box)

        # Frequency display (clickable, scrollable)
        self._lbl_freq = QLabel("14.200.000")
        self._lbl_freq.setObjectName("freq_lbl")
        self._lbl_freq.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._lbl_freq.setToolTip("Click to enter frequency • Scroll to tune")
        self._lbl_freq.mousePressEvent = self._freq_click
        self._lbl_freq.wheelEvent      = self._freq_wheel
        row1.addWidget(self._lbl_freq, stretch=1)

        # Mode indicator
        self._lbl_mode = QLabel("USB")
        self._lbl_mode.setObjectName("mode_lbl")
        self._lbl_mode.setAlignment(Qt.AlignCenter)
        row1.addWidget(self._lbl_mode)

        # Filter indicator
        self._lbl_filter = QLabel("FIL1")
        self._lbl_filter.setObjectName("filter_lbl")
        self._lbl_filter.setAlignment(Qt.AlignCenter)
        row1.addWidget(self._lbl_filter)

        # Tuning step
        self._step_combo = QComboBox()
        for label, _ in STEPS:
            self._step_combo.addItem(label)
        self._step_combo.setCurrentIndex(2)     # 1 kHz default
        self._step_combo.setFixedWidth(82)
        self._step_combo.setToolTip("Tuning step")
        row1.addWidget(self._step_combo)

        vbox.addLayout(row1)

        # ── row 2: S-meter ────────────────────────────────────────────────────
        self._smeter = SMeterWidget()
        vbox.addWidget(self._smeter)

        # ── radio info label ──────────────────────────────────────────────────
        self._lbl_radio = QLabel("Radio: not connected")
        self._lbl_radio.setObjectName("radio_lbl")
        vbox.addWidget(self._lbl_radio)

        # Connect VFO/swap/copy buttons
        self._btn_vfoa.clicked.connect(lambda: self._civ_send(lambda: self.radio.set_vfo("A")))
        self._btn_vfob.clicked.connect(lambda: self._civ_send(lambda: self.radio.set_vfo("B")))
        btn_swap.clicked.connect(lambda: self._civ_send(lambda: self.radio.swap_vfo()))
        btn_copy.clicked.connect(lambda: self._civ_send(lambda: self.radio.copy_vfo_a_to_b()))
        self._step_combo.currentIndexChanged.connect(self._on_step_change)

        return p

    # ── band panel ────────────────────────────────────────────────────────────

    def _build_band_panel(self) -> QFrame:
        p = _panel()
        hbox = QHBoxLayout(p)
        hbox.setContentsMargins(6, 4, 6, 4)
        hbox.setSpacing(3)
        hbox.addWidget(QLabel("Band:"))
        for label, freq in BANDS:
            b = _btn(label)
            f = freq
            b.clicked.connect(lambda checked, hz=f: self._set_freq(hz))
            hbox.addWidget(b)
        hbox.addStretch()

        # Mode buttons
        hbox.addWidget(QLabel("Mode:"))
        self._mode_grp = QButtonGroup(p)
        self._mode_grp.setExclusive(True)
        self._mode_btns: dict[str, QPushButton] = {}
        for m in MODES_LIST:
            b = _btn(m, checkable=True)
            self._mode_grp.addButton(b)
            hbox.addWidget(b)
            self._mode_btns[m] = b
            name = m
            b.clicked.connect(lambda checked, mn=name: self._set_mode(mn))
        self._mode_btns["USB"].setChecked(True)

        # Filter buttons
        hbox.addWidget(QLabel("FIL:"))
        self._fil_grp = QButtonGroup(p)
        self._fil_grp.setExclusive(True)
        self._fil_btns: list[QPushButton] = []
        for i, fl in enumerate(FILTER_LIST):
            b = _btn(fl, checkable=True)
            self._fil_grp.addButton(b)
            hbox.addWidget(b)
            fn = i + 1
            b.clicked.connect(lambda checked, n=fn: self._set_filter(n))
            self._fil_btns.append(b)
        self._fil_btns[0].setChecked(True)

        return p

    # ── level panel ───────────────────────────────────────────────────────────

    def _build_level_panel(self) -> QFrame:
        p = _panel()
        hbox = QHBoxLayout(p)
        hbox.setContentsMargins(6, 4, 6, 4)
        hbox.setSpacing(8)

        self._sl_rfgain = LevelControl("RF GAIN", 200)
        self._sl_sql    = LevelControl("SQL",       0)
        self._sl_nr     = LevelControl("NR LVL",  128)
        self._sl_nb     = LevelControl("NB LVL",  128)
        self._sl_drive  = LevelControl("DRIVE",   200)

        for widget, sub in [
            (self._sl_rfgain, LVL_RF_GAIN),
            (self._sl_sql,    LVL_SQL),
            (self._sl_nr,     LVL_NR),
            (self._sl_nb,     LVL_NB),
            (self._sl_drive,  LVL_DRIVE),
        ]:
            hbox.addWidget(widget, stretch=1)
            s = sub
            widget.value_changed.connect(lambda v, sc=s: self._civ_send(lambda val=v, c=sc: self.radio.set_level(c, val)))

        return p

    # ── function panel ────────────────────────────────────────────────────────

    def _build_function_panel(self) -> QFrame:
        p = _panel()
        hbox = QHBoxLayout(p)
        hbox.setContentsMargins(6, 4, 6, 4)
        hbox.setSpacing(10)

        # AGC exclusive group
        agc_grp, self._agc_btns = _exclusive_group(
            hbox, "AGC", AGC_LIST,
            lambda v: self._civ_send(lambda val=v: self.radio.set_agc(val))
        )
        hbox.addWidget(agc_grp)

        # Pre-amp exclusive group
        pre_grp, self._pre_btns = _exclusive_group(
            hbox, "Pre", PRE_LIST,
            lambda v: self._civ_send(lambda val=v: self.radio.set_preamp(val))
        )
        self._pre_btns[0].setChecked(True)
        hbox.addWidget(pre_grp)

        # ATT exclusive group
        att_grp, self._att_btns = _exclusive_group(
            hbox, "ATT", ATT_LIST,
            lambda v: self._civ_send(lambda val=v: self.radio.set_att(val))
        )
        self._att_btns[0].setChecked(True)
        hbox.addWidget(att_grp)

        hbox.addWidget(self._make_separator())

        # Toggle function buttons
        self._btn_nr   = self._toggle_btn("NR",   FUNC_NR)
        self._btn_nb   = self._toggle_btn("NB",   FUNC_NB)
        self._btn_comp = self._toggle_btn("COMP", FUNC_COMP)
        self._btn_vox  = self._toggle_btn("VOX",  FUNC_VOX)
        for b in (self._btn_nr, self._btn_nb, self._btn_comp, self._btn_vox):
            hbox.addWidget(b)

        hbox.addWidget(self._make_separator())

        # Split
        self._btn_split = _btn("SPLIT", checkable=True)
        self._btn_split.clicked.connect(
            lambda checked: self._civ_send(lambda c=checked: self.radio.set_split(c))
        )
        hbox.addWidget(self._btn_split)

        # PTT
        self._btn_ptt = _btn("TX", checkable=True, obj="ptt")
        self._btn_ptt.clicked.connect(
            lambda checked: self._civ_send(lambda c=checked: self.radio.set_tx(c))
        )
        hbox.addWidget(self._btn_ptt)

        # Tune
        btn_tune = _btn("TUNE", obj="tune")
        btn_tune.clicked.connect(lambda: self._civ_send(lambda: self.radio.start_tune()))
        hbox.addWidget(btn_tune)

        hbox.addStretch()

        # DB range controls (display-only)
        db_grp = _group("Display")
        db_layout = QHBoxLayout(db_grp)
        db_layout.setContentsMargins(4, 2, 4, 2)
        db_layout.setSpacing(4)
        db_layout.addWidget(QLabel("Ref:"))
        self._ref_slider = QSlider(Qt.Horizontal)
        self._ref_slider.setRange(-60, 0)
        self._ref_slider.setValue(int(DB_CEIL))
        self._ref_slider.setFixedWidth(80)
        self._ref_slider.valueChanged.connect(self._on_ref_change)
        db_layout.addWidget(self._ref_slider)
        self._ref_lbl = QLabel(f"{DB_CEIL:.0f}")
        self._ref_lbl.setFixedWidth(28)
        db_layout.addWidget(self._ref_lbl)
        hbox.addWidget(db_grp)

        return p

    def _make_separator(self) -> QFrame:
        sep = QFrame()
        sep.setFrameShape(QFrame.VLine)
        sep.setStyleSheet("color: #333;")
        return sep

    def _toggle_btn(self, label: str, sub_cmd: int) -> QPushButton:
        b = _btn(label, checkable=True)
        sc = sub_cmd
        b.clicked.connect(
            lambda checked, s=sc: self._civ_send(lambda c=checked, sub=s: self.radio.set_function(sub, c))
        )
        return b

    # ── spectrum + waterfall ──────────────────────────────────────────────────

    def _build_plots(self) -> QSplitter:
        splitter = QSplitter(Qt.Vertical)

        # Spectrum
        self._spec_plot = pg.PlotWidget()
        self._spec_plot.setLabel("left",   "dBFS")
        self._spec_plot.setLabel("bottom", "Frequency", units="Hz")
        self._spec_plot.showGrid(x=True, y=True, alpha=0.2)
        self._spec_plot.setYRange(DB_FLOOR, 0, padding=0)
        self._spec_plot.setMouseEnabled(x=True, y=True)
        self._spec_curve = self._spec_plot.plot(
            pen=pg.mkPen(color=(70, 210, 70), width=1)
        )
        self._ref_line = pg.InfiniteLine(
            pos=DB_CEIL, angle=0,
            pen=pg.mkPen(color=(255, 80, 0), style=Qt.DashLine, width=1)
        )
        self._spec_plot.addItem(self._ref_line)
        splitter.addWidget(self._spec_plot)

        # Waterfall
        wf = pg.PlotWidget()
        wf.setLabel("bottom", "Frequency", units="Hz")
        wf.setLabel("left",   "Time ↓")
        wf.setMouseEnabled(x=True, y=False)
        wf.setXLink(self._spec_plot)

        self._wf_img = pg.ImageItem()
        self._wf_img.setLookupTable(_make_waterfall_lut())
        self._wf_img.setLevels([DB_FLOOR, DB_CEIL])
        wf.addItem(self._wf_img)
        splitter.addWidget(wf)

        splitter.setSizes([280, 380])
        return splitter

    # ═════════════════════════════════════════════ audio thread ═══════════════

    def _build_audio(self, device):
        self._audio = AudioWorker(
            device=device, sample_rate=self.sample_rate,
            fft_size=FFT_SIZE, is_iq=self.is_iq,
        )
        self._audio.fft_ready.connect(self._on_fft)
        self._audio.start()
        mode = "IQ stereo" if self.is_iq else "audio mono"
        self._status.showMessage(
            f"Audio: {mode} · {self.sample_rate} Hz · FFT {FFT_SIZE}")

    # ═════════════════════════════════════════════ CI-V worker ════════════════

    def _start_civ(self):
        self._civ = CIVWorker(self.radio)
        self._civ.freq_updated.connect(self._on_freq)
        self._civ.mode_updated.connect(self._on_mode)
        self._civ.smeter_updated.connect(self._smeter.set_value)
        self._civ.level_updated.connect(self._on_level)
        self._civ.function_updated.connect(self._on_function)
        self._civ.agc_updated.connect(self._on_agc)
        self._civ.att_updated.connect(self._on_att)
        self._civ.preamp_updated.connect(self._on_preamp)
        self._civ.start()
        self._lbl_radio.setText(
            f"Radio: {self.radio.model}  ·  {self.radio.port}  ·  {self.radio.baud_rate} baud"
        )

    def _civ_send(self, fn):
        if self._civ:
            self._civ.send(fn)

    # ═════════════════════════════════════════════ CI-V slots ════════════════

    @pyqtSlot(int)
    def _on_freq(self, hz: int):
        self.vfo_hz = hz
        mhz = hz // 1_000_000
        khz = (hz % 1_000_000) // 1000
        rem = hz % 1000
        self._lbl_freq.setText(f"{mhz}.{khz:03d}.{rem:03d}")

    @pyqtSlot(str, int)
    def _on_mode(self, mode_name: str, filter_num: int):
        self._lbl_mode.setText(mode_name)
        self._lbl_filter.setText(f"FIL{filter_num}")
        if mode_name in self._mode_btns:
            self._mode_btns[mode_name].setChecked(True)
        idx = filter_num - 1
        if 0 <= idx < len(self._fil_btns):
            self._fil_btns[idx].setChecked(True)

    @pyqtSlot(int, int)
    def _on_level(self, sub_cmd: int, value: int):
        mapping = {
            LVL_RF_GAIN: self._sl_rfgain,
            LVL_SQL:     self._sl_sql,
            LVL_NR:      self._sl_nr,
            LVL_NB:      self._sl_nb,
            LVL_DRIVE:   self._sl_drive,
        }
        if sub_cmd in mapping:
            mapping[sub_cmd].set_value_quiet(value)

    @pyqtSlot(int, bool)
    def _on_function(self, sub_cmd: int, on: bool):
        mapping = {
            FUNC_NR:   self._btn_nr,
            FUNC_NB:   self._btn_nb,
            FUNC_COMP: self._btn_comp,
            FUNC_VOX:  self._btn_vox,
        }
        if sub_cmd in mapping:
            mapping[sub_cmd].setChecked(on)

    @pyqtSlot(int)
    def _on_agc(self, mode: int):
        # mode: 1=fast, 2=mid, 3=slow
        for i, (_, v) in enumerate(AGC_LIST):
            self._agc_btns[i].setChecked(v == mode)

    @pyqtSlot(int)
    def _on_att(self, value: int):
        for i, (_, v) in enumerate(ATT_LIST):
            self._att_btns[i].setChecked(v == value)

    @pyqtSlot(int)
    def _on_preamp(self, value: int):
        for i, (_, v) in enumerate(PRE_LIST):
            self._pre_btns[i].setChecked(v == value)

    # ═════════════════════════════════════════════ user interactions ══════════

    def _set_freq(self, hz: int):
        """Tune to hz (optimistic: update display immediately, then send CI-V)."""
        self.vfo_hz = hz
        self._on_freq(hz)
        self._civ_send(lambda f=hz: self.radio.set_frequency(f))

    def _set_mode(self, mode_name: str):
        filt = self._fil_btns.index(
            next(b for b in self._fil_btns if b.isChecked()), 0
        ) + 1
        self._lbl_mode.setText(mode_name)
        self._civ_send(lambda m=mode_name, f=filt: self.radio.set_mode(m, f))

    def _set_filter(self, filt_num: int):
        mode = self._lbl_mode.text()
        self._lbl_filter.setText(f"FIL{filt_num}")
        self._civ_send(lambda m=mode, f=filt_num: self.radio.set_mode(m, f))

    def _freq_click(self, event):
        if event.button() == Qt.LeftButton:
            current = f"{self.vfo_hz / 1e6:.6f}"
            text, ok = QInputDialog.getText(
                self, "Enter Frequency", "Frequency (MHz):", text=current
            )
            if ok and text:
                try:
                    hz = round(float(text.replace(",", ".")) * 1_000_000)
                    self._set_freq(hz)
                except ValueError:
                    pass

    def _freq_wheel(self, event):
        delta = 1 if event.angleDelta().y() > 0 else -1
        self._set_freq(self.vfo_hz + delta * self._tune_step)

    def _on_step_change(self, idx: int):
        self._tune_step = STEPS[idx][1]

    def _on_ref_change(self, val: int):
        self._ref_lbl.setText(str(val))
        self._ref_line.setValue(val)
        self._wf_img.setLevels([DB_FLOOR, val])

    # ═════════════════════════════════════════════ FFT update ════════════════

    @pyqtSlot(object)
    def _on_fft(self, fft_db: np.ndarray):
        n = min(len(fft_db), self._n_bins)

        # FIX: always recompute freq axis from current vfo_hz each frame
        if self.is_iq:
            f0 = self.vfo_hz - self.sample_rate / 2
            f1 = self.vfo_hz + self.sample_rate / 2
        else:
            f0 = float(self.vfo_hz)
            f1 = self.vfo_hz + self.sample_rate / 2
        freqs = np.linspace(f0, f1, n)

        # Update spectrum curve
        self._spec_curve.setData(freqs, fft_db[:n])

        # Update waterfall image rect only when frequency changes
        if self._last_f0 is None or abs(self._last_f0 - f0) > 1:
            self._last_f0 = f0
            self._wf_img.setRect(pg.QtCore.QRectF(f0, 0, f1 - f0, WATERFALL_ROWS))

        # Roll waterfall buffer (newest row at top)
        self._wf_buf[1:] = self._wf_buf[:-1]
        self._wf_buf[0, :n] = fft_db[:n]
        self._wf_img.setImage(
            self._wf_buf[:, :n].T,
            autoLevels=False,
            autoHistogramRange=False,
        )

    # ═════════════════════════════════════════════ cleanup ═══════════════════

    def closeEvent(self, event):
        if self._civ:
            self._civ.stop()
            self._civ.wait(2000)
        if hasattr(self, "_audio"):
            self._audio.stop()
            self._audio.wait(3000)
        if self.radio:
            self.radio.disconnect()
        event.accept()
