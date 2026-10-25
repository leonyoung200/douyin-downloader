# -*- mode: python ; coding: utf-8 -*-
"""uninstall.spec — 卸载器打包配置。

onefile / windowed / icon=app_icon.ico，输出 ``uninstall.exe``。
卸载器只依赖标准库（GUI 用 ctypes 调 user32.MessageBoxW 原生对话框，
不引入 tkinter / PySide6），显式排除重依赖，保持小体积。
"""
from PyInstaller.utils.hooks import collect_all

datas = [('app_icon.ico', '.')]
binaries = []
hiddenimports = []
# tkinter 为标准库，PyInstaller 会自动收集；这里不引入 PySide6。

a = Analysis(
    ['uninstall_app.py'],
    pathex=[SPECPATH],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['PySide6', 'playwright', 'numpy', 'PIL', 'cv2'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='uninstall',
    icon='app_icon.ico',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
