"""mount_iso falls back to a raw cd9660 mount for hybrid ISOs (Ubuntu etc.).

All subprocess calls are mocked; nothing here touches a real disk or image.
"""

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import rufus


def _fake_run_factory(calls, attach_fails=True, mount_fails=False):
    def fake_run(cmd, check=True, **kw):
        calls.append(cmd)
        if cmd[:2] == ["hdiutil", "attach"] and "-nomount" not in cmd:
            if attach_fails:
                raise subprocess.CalledProcessError(1, cmd)
            return SimpleNamespace(stdout="/dev/disk9s1\tApple_HFS\t/Volumes/X\n")
        if cmd[:2] == ["hdiutil", "attach"]:
            return SimpleNamespace(stdout=(
                "/dev/disk9          \tApple_partition_scheme\t\n"
                "/dev/disk9s2        \tApple_HFS\t\n"))
        if cmd[0] == "mount" and mount_fails:
            raise subprocess.CalledProcessError(1, cmd)
        return SimpleNamespace(stdout="")
    return fake_run


def test_falls_back_to_cd9660_and_cleans_up(monkeypatch):
    calls = []
    monkeypatch.setattr(rufus, "run", _fake_run_factory(calls))

    mp = rufus.mount_iso(Path("/x/ubuntu.iso"))

    assert Path(mp).is_dir()
    assert ["mount", "-t", "cd9660", "-o", "rdonly", "/dev/disk9", mp] in calls

    rufus.unmount_iso(mp)

    assert ["umount", "-f", mp] in calls
    assert ["hdiutil", "detach", "/dev/disk9", "-force"] in calls
    assert not Path(mp).exists()
    assert mp not in rufus._FALLBACK_MOUNTS


def test_normal_attach_does_not_fall_back(monkeypatch):
    calls = []
    monkeypatch.setattr(rufus, "run", _fake_run_factory(calls, attach_fails=False))

    assert rufus.mount_iso(Path("/x/win.iso")) == "/Volumes/X"
    assert not any("-nomount" in c for c in calls)

    rufus.unmount_iso("/Volumes/X")
    assert calls[-1] == ["hdiutil", "detach", "/Volumes/X", "-force"]


def test_failed_fallback_mount_detaches_device(monkeypatch):
    calls = []
    monkeypatch.setattr(rufus, "run", _fake_run_factory(calls, mount_fails=True))

    with pytest.raises(subprocess.CalledProcessError):
        rufus.mount_iso(Path("/x/bad.iso"))

    assert ["hdiutil", "detach", "/dev/disk9", "-force"] in calls
    assert not rufus._FALLBACK_MOUNTS
