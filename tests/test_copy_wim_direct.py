#!/usr/bin/env python3
"""Regression test for copy_wim_direct().

Guards against the ENOTSOCK bug: os.sendfile(2) on macOS/BSD requires the
destination fd to be a socket, so the file->file form used here previously
failed with "[Errno 38] Socket operation on non-socket" for every user.

Run: python3 tests/test_copy_wim_direct.py
"""

import hashlib
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location("rufus", REPO_ROOT / "rufus.py")
rufus = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rufus)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# Sizes straddle the 4 MB copy chunk boundary: empty, tiny, exact, +1, multi-chunk
SIZES = (0, 1, 4 * 1024 * 1024, 4 * 1024 * 1024 + 1, 10 * 1024 * 1024 + 12345)


def main() -> int:
    failures = []
    for size in SIZES:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "install.wim"
            src.write_bytes(os.urandom(size))
            usb_root = Path(tmp) / "usb"

            try:
                rufus.copy_wim_direct(src, usb_root)
            except OSError as exc:
                failures.append(f"size={size}: OSError errno={exc.errno} ({exc.strerror})")
                continue

            dst = usb_root / "sources" / "install.wim"
            if not dst.exists():
                failures.append(f"size={size}: destination was not created")
            elif dst.stat().st_size != size:
                failures.append(f"size={size}: wrote {dst.stat().st_size} bytes")
            elif sha256(dst) != sha256(src):
                failures.append(f"size={size}: checksum mismatch")
            else:
                print(f"PASS size={size}")

    if failures:
        print("\nFAILED:")
        for failure in failures:
            print(f"  {failure}")
        return 1

    print("\nAll copy_wim_direct tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
