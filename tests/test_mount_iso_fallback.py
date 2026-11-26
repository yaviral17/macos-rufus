"""mount_iso falls back to a raw cd9660 mount for hybrid ISOs (Ubuntu etc.).

All subprocess calls are mocked; nothing here touches a real disk or image.
"""

import subprocess
from pathlib import Path

import pytest

import rufus

NO_FS = ("hdiutil: WARNING: 'hdiutil attach -nobrowse -readonly ...' is deprecated. "
         "Please use 'diskutil image attach ...' instead.\n"
         "hdiutil: attach failed - no mountable file systems\n")


def _done(cmd, rc=0, out="", err=""):
    return subprocess.CompletedProcess(cmd, rc, out, err)


def _fake_run_factory(calls, attach_fails=True, nomount_fails=False, mount_fails=False):
    def fake_run(cmd, check=True, **kw):
        calls.append(cmd)
        if cmd[:2] == ["hdiutil", "attach"] and "-nomount" not in cmd:
            if attach_fails:
                return _done(cmd, 1, err=NO_FS)
            return _done(cmd, out="/dev/disk9s1\tApple_HFS\t/Volumes/X\n")
        if cmd[:2] == ["hdiutil", "attach"]:
            if nomount_fails:
                return _done(cmd, 1, err="hdiutil: attach failed - image not recognized")
            return _done(cmd, out=(
                "/dev/disk9          \tGUID_partition_scheme\t\n"
                "/dev/disk9s2        \tEFI\t\n"))
        if cmd[0] == "mount" and mount_fails:
            return _done(cmd, 1, err="mount_cd9660: Invalid argument")
        return _done(cmd)
    return fake_run


def _write_iso(path: Path, label: str = "ARCH_202611", boot_record_first: bool = False):
    """Minimal ISO 9660 volume descriptor set — just enough to read the label."""
    data = bytearray(20 * 2048)
    pvd_sector = 16
    if boot_record_first:
        data[16 * 2048:16 * 2048 + 7] = b"\x00CD001\x01"
        pvd_sector = 17
    pvd = pvd_sector * 2048
    data[pvd:pvd + 7] = b"\x01CD001\x01"
    data[pvd + 40:pvd + 72] = label.encode().ljust(32)
    term = (pvd_sector + 1) * 2048
    data[term:term + 7] = b"\xffCD001\x01"
    path.write_bytes(data)


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


def test_failed_fallback_mount_detaches_device_and_reports_hdiutil_error(monkeypatch):
    calls = []
    monkeypatch.setattr(rufus, "run", _fake_run_factory(calls, mount_fails=True))

    with pytest.raises(RuntimeError, match="no mountable file systems"):
        rufus.mount_iso(Path("/x/bad.iso"))

    assert ["hdiutil", "detach", "/dev/disk9", "-force"] in calls
    assert not rufus._FALLBACK_MOUNTS


def test_unreadable_image_reports_hdiutil_error_without_mounting(monkeypatch):
    calls = []
    monkeypatch.setattr(rufus, "run", _fake_run_factory(calls, nomount_fails=True))

    with pytest.raises(RuntimeError) as err:
        rufus.mount_iso(Path("/x/bad.iso"))
    assert str(err.value) == "Couldn't mount bad.iso: hdiutil: attach failed - no mountable file systems"

    assert not any(c[0] == "mount" for c in calls)
    assert not any(c[:2] == ["hdiutil", "detach"] for c in calls)


def test_iso_volume_label_reads_primary_volume_descriptor(tmp_path):
    iso = tmp_path / "arch.iso"
    _write_iso(iso, "ARCH_202611")
    assert rufus._iso_volume_label(iso) == "ARCH_202611"

    _write_iso(iso, "Ubuntu 24.04 LTS amd64", boot_record_first=True)
    assert rufus._iso_volume_label(iso) == "Ubuntu 24.04 LTS amd64"

    iso.write_bytes(b"\0" * 40000)
    assert rufus._iso_volume_label(iso) == ""
    assert rufus._iso_volume_label(tmp_path / "missing.iso") == ""


def test_exists_ci_ignores_case(tmp_path):
    (tmp_path / "EFI" / "boot").mkdir(parents=True)
    (tmp_path / "EFI" / "boot" / "bootx64.efi").write_bytes(b"")

    assert rufus._exists_ci(tmp_path, "efi/boot/bootx64.efi")
    assert rufus._exists_ci(tmp_path, "EFI/BOOT/BOOTX64.EFI")
    assert not rufus._exists_ci(tmp_path, "efi/boot/bootia32.efi")
    assert not rufus._exists_ci(tmp_path, "sources/install.wim")


def test_probe_of_fallback_mount_uses_iso_label_and_finds_mixed_case_efi(tmp_path, monkeypatch):
    iso = tmp_path / "ubuntu-24.04-desktop-amd64.iso"
    _write_iso(iso, "Ubuntu 24.04 LTS amd64")
    mount = tmp_path / "rufus-iso-abc123"
    (mount / "EFI" / "boot").mkdir(parents=True)
    (mount / "EFI" / "boot" / "bootx64.efi").write_bytes(b"")
    (mount / "casper").mkdir()

    monkeypatch.setattr(rufus, "mount_iso", lambda p: str(mount))
    monkeypatch.setattr(rufus, "unmount_iso", lambda mp: rufus._FALLBACK_MOUNTS.pop(mp, None))
    monkeypatch.setitem(rufus._FALLBACK_MOUNTS, str(mount), "/dev/disk9")

    info = rufus.probe_iso(iso)

    assert info["kind"] == "linux"
    assert info["uefi"] is True
    assert info["label"] == "Ubuntu 24.04 LTS amd64"


def test_probe_of_fallback_mount_without_label_uses_file_name(tmp_path, monkeypatch):
    iso = tmp_path / "custom.iso"
    iso.write_bytes(b"\0" * 40000)
    mount = tmp_path / "rufus-iso-abc123"
    (mount / "casper").mkdir(parents=True)

    monkeypatch.setattr(rufus, "mount_iso", lambda p: str(mount))
    monkeypatch.setattr(rufus, "unmount_iso", lambda mp: rufus._FALLBACK_MOUNTS.pop(mp, None))
    monkeypatch.setitem(rufus._FALLBACK_MOUNTS, str(mount), "/dev/disk9")

    assert rufus.probe_iso(iso)["label"] == "custom"
