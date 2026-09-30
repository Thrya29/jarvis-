# PyInstaller spec - builds dist/jarvis/jarvis.exe (one-folder, fast startup).
# Usage: uv run pyinstaller packaging/jarvis.spec --noconfirm
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
    + collect_submodules("jarvis.voice")
    + collect_submodules("piper")
    + ["keyring.backends.Windows", "win32com.client", "pythoncom", "mss", "PIL.PngImagePlugin"]
    + ["sounddevice", "soxr", "ctranslate2", "livekit.rtc"]
)
# python-docx ships its default template as package data; anthropic reads its own metadata.
datas = (
    copy_metadata("jarvis")
    + copy_metadata("anthropic")
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
