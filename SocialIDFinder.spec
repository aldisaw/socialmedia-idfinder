# -*- mode: python ; coding: utf-8 -*-
"""Reproducible PyInstaller spec for Social ID Finder v1.1.0 (Browser Edition).

The sole official distribution bundles PySide6, HTTPX, Playwright driver,
and Chromium Headless Shell.

Environment controls:
  SIF_BUILD_MODE = 'onedir' (default) | 'onefile'
"""

import os
from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_data_files

BUILD_MODE = os.environ.get("SIF_BUILD_MODE", "onedir").strip().lower()
if BUILD_MODE not in ("onedir", "onefile"):
    raise ValueError(f"Unsupported SIF_BUILD_MODE={BUILD_MODE!r}; expected 'onedir' or 'onefile'.")

PROJECT_ROOT = Path(SPECPATH).resolve()
ICON_FILE = str(PROJECT_ROOT / "resources" / "app_icon.ico")

datas = [
    (str(PROJECT_ROOT / "resources" / "app_icon.ico"), "resources"),
    (str(PROJECT_ROOT / "README.md"), "."),
]
datas += collect_data_files("certifi")

binaries = []
hiddenimports = [
    "lxml.etree",
    "lxml._elementpath",
    "bs4.builder._lxml",
    "sqlite3",
]

excludes = [
    "pytest",
    "_pytest",
    "pytest_asyncio",
    "PyInstaller",
    "pyinstaller",
    "tests",
    "tkinter",
    "doctest",
    "pydoc",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtQuick",
    "PySide6.QtQml",
    "PySide6.Qt3DCore",
    "PySide6.QtMultimedia",
    "PySide6.QtSql",
    "PySide6.QtNetwork",
    "PySide6.QtPdf",
    "PySide6.QtOpenGL",
    "PySide6.QtTest",
]

pw_datas, pw_bins, pw_hidden = collect_all("playwright")
datas += pw_datas
binaries += pw_bins
hiddenimports += pw_hidden + ["greenlet"]

# Bundle Playwright Chromium headless shell runtime under ms-playwright/<version>
ms_pw_root = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or (Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"))
shell_dirs = sorted(ms_pw_root.glob("chromium_headless_shell-*"), reverse=True)
if not shell_dirs:
    raise FileNotFoundError(
        f"Chromium headless shell not found under '{ms_pw_root}'. Run '.venv\\Scripts\\playwright.exe install chromium'."
    )
shell_dir = shell_dirs[0]
for src_file in shell_dir.rglob("*"):
    if not src_file.is_file() or src_file.suffix.lower() == ".log":
        continue
    rel_parent = src_file.parent.relative_to(shell_dir)
    dest_dir = Path("ms-playwright") / shell_dir.name / rel_parent
    datas.append((str(src_file), str(dest_dir)))

collect_name = "SocialIDFinder_Browser"
onefile_name = "SocialIDFinder_Browser"

a = Analysis(
    [str(PROJECT_ROOT / "main.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=1,
)

pyz = PYZ(a.pure)

if BUILD_MODE == "onefile":
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name=onefile_name,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        upx_exclude=[],
        runtime_tmpdir=None,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=ICON_FILE,
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="SocialIDFinder",
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
        icon=ICON_FILE,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name=collect_name,
    )
