"""Generate the Windows VERSIONINFO resource for the PyInstaller build from pyproject.toml."""

from __future__ import annotations

import tomllib
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent

TEMPLATE = """VSVersionInfo(
  ffi=FixedFileInfo(filevers={t}, prodvers={t}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'Thrya29'),
      StringStruct('FileDescription', 'JARVIS - AI control layer'),
      StringStruct('FileVersion', '{v}'),
      StringStruct('InternalName', 'jarvis'),
      StringStruct('OriginalFilename', 'jarvis.exe'),
      StringStruct('ProductName', 'JARVIS'),
      StringStruct('ProductVersion', '{v}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""


def main() -> None:
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]
    parts = [int(p) for p in version.split(".")[:3]]
    tup = tuple([*parts, 0])
    (HERE / "version_info.txt").write_text(TEMPLATE.format(t=tup, v=version), encoding="utf-8")
    print(version)


if __name__ == "__main__":
    main()
