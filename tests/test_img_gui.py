"""GUI regression tests for raw images (headless Qt)."""

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QFileDialog

import gui


def test_browse_accepts_img(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    image = tmp_path / "tails.img"
    image.write_bytes(b"raw")
    page = gui.IsoAcquirePage(lambda *_: None, lambda *_: None, lambda *_: None)

    def choose(_parent, _title, _home, file_filter):
        assert "*.img" in file_filter
        return str(image), file_filter

    monkeypatch.setattr(QFileDialog, "getOpenFileName", choose)
    page._browse()
    assert page.selected_path == image
    assert page.next_btn.isEnabled()


def test_img_confirmation_only_offers_raw_write(tmp_path):
    app = QApplication.instance() or QApplication([])
    page = gui.ConfirmPage(lambda: None, lambda: None)
    page.show()
    info = {"kind": "linux", "hybrid": True, "uefi": False, "label": "tails", "size": 3}
    drive = {"node": "/dev/disk2", "name": "USB", "size": 1024}
    page.load(tmp_path / "tails.img", drive, info)
    assert page.live_radio.isChecked()
    assert not page.copy_radio.isEnabled()
    assert page.linux_opts()["mode"] == "dd"
    page.close()
