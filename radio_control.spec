# -*- mode: python ; coding: utf-8 -*-
# Local build: pyinstaller radio_control.spec

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        'PyQt5.sip',
        'serial.tools.list_ports_windows',
        'serial.tools.list_ports_posix',
        'serial.tools.list_ports_linux',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    collect_all=['sounddevice', 'pyqtgraph'],
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='IcomRadioControl',
    debug=False,
    strip=False,
    upx=True,
    console=False,
    icon=None,
)
