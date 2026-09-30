"""Generate packaging/jarvis.ico from the icon drawn in code (no binary files in git)."""

from __future__ import annotations

from pathlib import Path

from jarvis.app.icon import write_ico

if __name__ == "__main__":
    out = Path(__file__).parent / "jarvis.ico"
    write_ico(str(out))
    print(out)
