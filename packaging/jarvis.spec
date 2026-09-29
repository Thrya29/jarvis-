# PyInstaller spec - builds dist/jarvis/jarvis.exe (one-folder, fast startup).
# Usage: uv run pyinstaller packaging/jarvis.spec --noconfirm
# ruff: noqa
from PyInstaller.utils.hooks import collect_submodules, copy_metadata

hiddenimports = (
    collect_submodules("uvicorn")
    + collect_submodules("jarvis")
    + ["keyring.backends.Windows"]
)
datas = copy_metadata("jarvis")

a = Analysis(
    ["../src/jarvis/__main__.py"],
    pathex=["../src"],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "pytest", "mypy", "ruff"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="jarvis",
    console=True,
    upx=False,
    version="version_info.txt",
)
coll = COLLECT(exe, a.binaries, a.datas, name="jarvis", upx=False)
