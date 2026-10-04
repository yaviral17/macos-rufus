#!/usr/bin/env python3
"""Regression test for copy_wim_direct().

Guards against the ENOTSOCK bug: os.sendfile(2) on macOS/BSD requires the
destination fd to be a socket, so the file->file form used here previously
failed with "[Errno 38] Socket operation on non-socket" for every user.

Run: python -m pytest tests/  (or: python3 tests/test_copy_wim_direct.py)
"""

import hashlib
import importlib.util
import os
import sys
from pathlib import Path

import pytest

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


@pytest.mark.parametrize("size", SIZES)
def test_copy_wim_direct_roundtrip(size, tmp_path):
    """copy_wim_direct() must produce a byte-identical sources/install.wim."""
    src = tmp_path / "install.wim"
    src.write_bytes(os.urandom(size))
    usb_root = tmp_path / "usb"

    rufus.copy_wim_direct(src, usb_root)

    dst = usb_root / "sources" / "install.wim"
    assert dst.exists()
    assert dst.stat().st_size == size
    assert sha256(dst) == sha256(src)


def main() -> int:
    return pytest.main([__file__, "-v"])


if __name__ == "__main__":
    sys.exit(main())
