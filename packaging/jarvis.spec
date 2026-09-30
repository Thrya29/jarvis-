# PyInstaller spec - builds dist/jarvis/ (one-folder, fast startup) with two launchers
# sharing the same runtime:
#   jarvis.exe   console app (CLI: jarvis doctor / chat / voice / ...)
#   jarvisw.exe  windowless app (Start menu, tray, autostart: `jarvisw app`)
# Usage: uv run python packaging/make_version_info.py
#        uv run python packaging/make_icon.py
#        uv run pyinstaller packaging/jarvis.spec --noconfirm
# ruff: noqa
from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
    copy_metadata,
)

hiddenimports = (
    collect_submodules("uvicorn")
    + collect_submodules("jarvis")
    + collect_submodules("comtypes")
    + collect_submodules("uiautomation")
    + collect_submodules("piper")
    + collect_submodules("pystray")
    + ["keyring.backends.Windows", "win32com.client", "pythoncom", "mss", "PIL.PngImagePlugin"]
    + ["sounddevice", "soxr", "ctranslate2", "livekit.rtc"]
)
datas = (
    # The desktop UI (HTML/CSS/JS), served by the daemon.
    [("../src/jarvis/ui", "jarvis/ui")]
    + copy_metadata("jarvis")
    + copy_metadata("anthropic")
    # python-docx ships its default template as package data.
    + collect_data_files("docx")
    # uiautomation ships helper DLLs next to its modules.
    + collect_data_files("uiautomation", include_py_files=False)
    # Voice: Silero VAD model, Piper's espeak-ng data, WebRTC audio processing.
    + collect_data_files("faster_whisper")
    + collect_data_files("piper")
    + collect_data_files("livekit")
    + copy_metadata("faster_whisper")
)
binaries = (
    collect_dynamic_libs("ctranslate2")
    + collect_dynamic_libs("onnxruntime")
    + collect_dynamic_libs("livekit")
    + collect_dynamic_libs("sounddevice")
)

a = Analysis(
    ["../src/jarvis/__main__.py"],
    pathex=["../src"],
    datas=datas,
    binaries=binaries,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "pytest", "mypy", "ruff", "hf_xet"],
    noarchive=False,
)
pyz = PYZ(a.pure)


def launcher(name, console):
    return EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name=name,
        console=console,
        upx=False,
        icon="jarvis.ico",
        version="version_info.txt",
    )


coll = COLLECT(
    launcher("jarvis", console=True),
    launcher("jarvisw", console=False),
    a.binaries,
    a.datas,
    name="jarvis",
    upx=False,
)
