#!/usr/bin/env python3
"""Privileged flash worker for the macos-rufus GUI.

Runs the disk-writing pipeline (mount, format, boot sectors, file copy) as
root, reusing every function from rufus.py — the GUI never talks to disks
directly. It's launched by the GUI via `osascript ... with administrator
privileges`, backgrounded so the GUI's password prompt returns immediately.

Progress is reported by appending one JSON object per line to a file the
(unprivileged) GUI polls, since a backgrounded privileged process can't just
write to the GUI's own stdout.

Usage: rufus_worker.py <args.json> <progress.jsonl>
    args.json: {"iso_path": "...", "disk_node": "/dev/diskN"}
"""

import json
import sys
import time
from pathlib import Path

import rufus


def emit(progress_file, **fields):
    with open(progress_file, "a") as f:
        f.write(json.dumps(fields) + "\n")
        f.flush()


def main():
    if len(sys.argv) != 3:
        print("Usage: rufus_worker.py <args.json> <progress.jsonl>", file=sys.stderr)
        sys.exit(2)

    args = json.loads(Path(sys.argv[1]).read_text())
    progress_file = sys.argv[2]
    iso_path = Path(args["iso_path"])
    disk_node = args["disk_node"]

    log_path = rufus.setup_logger()
    rufus.log.info("rufus_worker started for %s -> %s", iso_path, disk_node)

    start_time = time.monotonic()
    mount_point = None
    try:
        emit(progress_file, stage="mount", status="start")
        mount_point = rufus.mount_iso(iso_path)
        iso_info = rufus.detect_iso(mount_point)
        preflight_wim = rufus.get_wim_path(mount_point)
        if preflight_wim:
            rufus.check_wim_export_space(preflight_wim)
        emit(progress_file, stage="mount", status="done", uefi=iso_info["uefi"])

        emit(progress_file, stage="format", status="start")
        rufus.run(["diskutil", "unmountDisk", disk_node], check=False)
        rufus.format_usb(disk_node)
        emit(progress_file, stage="format", status="done")

        emit(progress_file, stage="bootsectors", status="start")
        rufus.set_mbr_active_partition(disk_node)
        rufus.write_windows_vbr(disk_node, mount_point)
        emit(progress_file, stage="bootsectors", status="done")

        rufus.run(["diskutil", "mount", disk_node + "s1"], check=False)
        usb_volume = rufus.get_volume_path(disk_node)

        wim_path = rufus.get_wim_path(mount_point)

        emit(progress_file, stage="copy_files", status="start")
        rufus.copy_files_except_wim(
            mount_point, usb_volume,
            progress_cb=lambda done, total, name: emit(
                progress_file, stage="copy_files", status="progress",
                done=done, total=total, filename=name,
            ),
        )
        emit(progress_file, stage="copy_files", status="done")

        if wim_path:
            if wim_path.stat().st_size > rufus.FAT32_LIMIT:
                emit(progress_file, stage="copy_wim", status="start", split=True)
                rufus.split_and_copy_wim(
                    wim_path, usb_volume,
                    progress_cb=lambda phase, pct: emit(
                        progress_file, stage="copy_wim", status="progress",
                        phase=phase, percent=pct,
                    ),
                )
                emit(progress_file, stage="copy_wim", status="done", split=True)
            else:
                emit(progress_file, stage="copy_wim", status="start", split=False,
                     total=wim_path.stat().st_size)
                rufus.copy_wim_direct(
                    wim_path, usb_volume,
                    progress_cb=lambda done, total: emit(
                        progress_file, stage="copy_wim", status="progress",
                        done=done, total=total,
                    ),
                )
                emit(progress_file, stage="copy_wim", status="done", split=False)
        else:
            emit(progress_file, stage="copy_wim", status="skipped")

        emit(progress_file, stage="eject", status="start")
        rufus.run(["diskutil", "unmountDisk", disk_node], check=False)
        emit(progress_file, stage="eject", status="done")

        elapsed = time.monotonic() - start_time
        emit(progress_file, stage="complete", uefi=iso_info["uefi"],
             elapsed=elapsed, log_path=str(log_path))
        rufus.log.info("rufus_worker finished successfully in %.1fs", elapsed)

    except Exception as e:
        rufus.log.error("rufus_worker fatal error: %s", e, exc_info=True)
        emit(progress_file, stage="error", message=str(e))
        sys.exit(1)
    finally:
        if mount_point:
            rufus.unmount_iso(mount_point)


if __name__ == "__main__":
    main()
