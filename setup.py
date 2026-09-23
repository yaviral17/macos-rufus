"""Build macos-rufus.app with: python3 setup.py py2app

Requires py2app: pip install py2app
"""
import sys
from pathlib import Path

from setuptools import setup

sys.path.insert(0, str(Path(__file__).parent))
import rufus  # noqa: E402 — needed after sys.path tweak, for __version__ below

APP = ["gui.py"]
DATA_FILES = ["rufus.py", "rufus_worker.py"]
OPTIONS = {
    "argv_emulation": False,
    "iconfile": None,
    "plist": {
        "CFBundleName": "macos-rufus",
        "CFBundleDisplayName": "macos-rufus",
        "CFBundleIdentifier": "com.macos-rufus.gui",
        "CFBundleShortVersionString": rufus.__version__,
        "NSHighResolutionCapable": True,
        # This app never runs as root itself (see rufus_worker.py) — only the
        # backgrounded disk-writing worker is elevated, via a native macOS
        # password prompt, when the user clicks "Flash Drive".
        "NSHumanReadableCopyright": "MIT License",
    },
    "packages": ["rich", "requests", "playwright"],
}

setup(
    app=APP,
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
