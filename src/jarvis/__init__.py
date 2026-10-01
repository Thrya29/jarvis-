"""JARVIS - an AI control layer for Windows."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("jarvis")
except PackageNotFoundError:  # running from a frozen build or source tree
    __version__ = "2.2.0"

__all__ = ["__version__"]
