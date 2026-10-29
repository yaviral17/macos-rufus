"""Raw image CLI options must not allow switching to ISO file-copy mode."""

from pathlib import Path

import rufus


def test_raw_img_cli_always_uses_dd(monkeypatch):
    monkeypatch.setattr(rufus.Confirm, "ask", lambda *_args, **_kwargs: False)
    info = {"kind": "linux", "hybrid": True, "uefi": False, "raw": True,
            "label": "tails", "size": 3}
    opts = rufus.ask_linux_options(info, 1024)
    assert opts["mode"] == "dd"
    assert not opts["verify"]
