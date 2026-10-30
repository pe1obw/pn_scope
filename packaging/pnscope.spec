# PyInstaller spec for PNScope. Build with:  pyinstaller packaging/pnscope.spec
# Produces dist/PNScope/ (a folder with PNScope.exe and its libraries).
import os

from PyInstaller.utils.hooks import collect_submodules

root = os.path.dirname(SPECPATH)

a = Analysis(
    [os.path.join(SPECPATH, "pnscope_entry.py")],
    pathex=[root],
    # drivers register themselves on import; make sure all of them are bundled
    hiddenimports=collect_submodules("pnscope"),
    # pyqtgraph supports several Qt bindings; only PySide6 is used
    excludes=["PyQt5", "PyQt6", "PySide2", "tkinter", "pytest", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PNScope",
    console=False,      # GUI application: no console window
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="PNScope", upx=False)
