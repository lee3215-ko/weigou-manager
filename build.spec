# -*- mode: python ; coding: utf-8 -*-
import os
import certifi

block_cipher = None
SPEC_DIR = os.path.dirname(os.path.abspath(SPEC))

datas = [(certifi.where(), "certifi")]
binaries = []
hiddenimports = [
    "certifi",
    "PIL",
    "PIL.Image",
    "PIL.ImageTk",
    "playwright",
    "websocket",
    "updater",
    "update_ui",
    "catalog_sync",
    "paths",
]

a = Analysis(
    ["run.py"],
    pathex=[SPEC_DIR],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="WeigouManager",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="WeigouManager",
)
