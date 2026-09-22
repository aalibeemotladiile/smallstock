# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller recipe — Cattle & Small Stock System, Windows.

Builds a folder (not a single file): Streamlit reads its own package data off
disk at run time, and a onefile build unpacks that to a temp folder on every
start, which is slow and occasionally locked by antivirus. A folder starts in
a second and is what the Inno Setup installer packages.

    pyinstaller build_windows.spec

Output: dist/CattleSmallStock/CattleSmallStock.exe
"""
from PyInstaller.utils.hooks import collect_all, copy_metadata

APP_NAME = "CattleSmallStock"

datas, binaries, hiddenimports = [], [], []

# Whole packages, data files and all. Streamlit in particular ships its
# static front-end and a pile of metadata it reads at import time.
#
# numpy is listed first and in its own right, not left to arrive as one of
# pandas' dependencies. Since numpy 2 its C extensions live under numpy._core,
# and a PyInstaller older than the numpy it is packing collects the old
# numpy.core instead — the build succeeds and the installed program dies on
# "No module named 'numpy._core._exceptions'". collect_all takes every
# submodule and leaves nothing to the hook's opinion.
for package in ("numpy", "streamlit", "altair", "pandas", "reportlab", "PIL",
                "openpyxl", "pyarrow", "webview", "clr_loader", "pythonnet"):
    try:
        package_datas, package_binaries, package_hidden = collect_all(package)
    except Exception as exc:                       # optional on this machine
        print(f"build_windows.spec: skipping {package} — {exc}")
        continue
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden

# Version metadata Streamlit looks up with importlib.metadata. Missing
# metadata shows up as a crash on start, not at build time, so collect it.
for distribution in ("streamlit", "altair", "pandas", "numpy", "reportlab",
                     "pillow", "openpyxl", "pyarrow", "click", "tornado",
                     "protobuf", "packaging", "pydeck", "watchdog",
                     "gitpython", "tenacity", "toml", "typing_extensions",
                     "rich", "blinker", "cachetools", "jsonschema",
                     "narwhals", "pywebview"):
    try:
        datas += copy_metadata(distribution)
    except Exception:
        pass

hiddenimports += [
    # numpy's private core, named outright. collect_all above should already
    # have these; naming them means a future hook change cannot quietly drop
    # the ones pandas reaches for first.
    "numpy._core._exceptions",
    "numpy._core._multiarray_umath",
    "numpy._core._multiarray_tests",
    "numpy._core.multiarray",
    "numpy._core.umath",
    "numpy._core._methods",
    "numpy._core._dtype",
    "numpy._core._dtype_ctypes",
    "numpy._core._internal",
    "numpy._core.arrayprint",
    "numpy.core._exceptions",              # numpy 1.x, harmless on numpy 2
    "streamlit.web.cli",
    "streamlit.runtime.scriptrunner.magic_funcs",
    "sqlite3",
    "reportlab.graphics.barcode",
    "reportlab.pdfbase._fontdata_enc_winansi",
    "reportlab.pdfbase._fontdata_enc_macroman",
    "reportlab.pdfbase._fontdata_widths_helvetica",
    "reportlab.pdfbase._fontdata_widths_helveticabold",
    "openpyxl.cell._writer",
    "pandas._libs.tslibs.base",
]

# The app itself, the generator it is written from, and the theme, all read
# from beside the .exe at run time.
datas += [
    ("cattlemanagementapp.py", "."),
    ("cattlemanagement_generator1.py", "."),
    ("logo.ico", "."),
    (".streamlit/config.toml", ".streamlit"),
    ("README.txt", "."),
]

analysis = Analysis(
    ["run_desktop.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Nothing here draws with tkinter or matplotlib — the window is WebView2
    # and the charts are Altair, drawn in the page. Leaving them out keeps a
    # few hundred megabytes off the installer. PIL._tkinter_finder is
    # deliberately NOT a hidden import: asking for it while excluding tkinter
    # is a contradiction PyInstaller resolves by warning and moving on, and
    # the app never uses ImageTk.
    excludes=["tkinter", "matplotlib", "pytest", "IPython", "notebook",
              "PIL.ImageTk", "PIL._tkinter_finder"],
    noarchive=False,
)

pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                       # UPX and antivirus do not get along
    console=False,                   # no black console window behind the app
    disable_windowed_traceback=False,
    icon="logo.ico",
    version=None,
)

collection = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)
