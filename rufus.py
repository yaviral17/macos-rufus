#!/usr/bin/env python3
"""macos-rufus: create bootable Windows USB drives on macOS."""

import logging
import os
import plistlib
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.progress import (BarColumn, FileSizeColumn, MofNCompleteColumn,
                           Progress, TextColumn, TimeRemainingColumn,
                           TransferSpeedColumn)
from rich.prompt import Confirm, Prompt
from rich.table import Column, Table
from rich.text import Text

console = Console()
log: logging.Logger = logging.getLogger("rufus")


def setup_logger() -> Path:
    logs_dir = Path(__file__).parent / "logs"
    logs_dir.mkdir(exist_ok=True)
    log_path = logs_dir / f"rufus_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.FileHandler(log_path)],
    )
    return log_path

FAT32_LIMIT = 4 * 1024 ** 3  # 4 GiB


# ── helpers ───────────────────────────────────────────────────────────────────

def run(cmd: list[str], check=True, capture=True, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, capture_output=capture, text=True, **kw)


def escalate_to_root():
    """Re-exec under sudo if not root — macOS will prompt for password."""
    if os.geteuid() != 0:
        console.print("[dim]Root access required — you may be prompted for your password.[/dim]")
        os.execvp("sudo", ["sudo", sys.executable] + sys.argv)


def _brew_run(args: list[str]):
    brew = shutil.which("brew")
    if not brew:
        console.print(
            "[red]Homebrew not found.[/red] Install: [bold]https://brew.sh[/bold]"
        )
        sys.exit(1)
    login_user = os.environ.get("SUDO_USER") or os.environ.get("USER", "")
    if login_user and os.geteuid() == 0:
        subprocess.run(["sudo", "-u", login_user, brew] + args, check=True)
    else:
        subprocess.run([brew] + args, check=True)


def _auto_install(package: str, binary: str, reason: str):
    console.print(f"[yellow]{binary} not found[/yellow] — {reason}")
    if Confirm.ask(f"Auto-install [bold]{package}[/bold] via Homebrew?", default=True):
        _brew_run(["install", package])
        if not shutil.which(binary):
            console.print(f"[red]Install failed. Run manually: brew install {package}[/red]")
            sys.exit(1)
        console.print(f"[green]✓ {package} installed[/green]")
    else:
        console.print(f"[dim]Skipping {package}.[/dim]")


def check_deps():
    for tool in ("hdiutil", "diskutil"):
        if not shutil.which(tool):
            console.print(f"[red]Missing required macOS tool: {tool}[/red]")
            sys.exit(1)

    if not shutil.which("wimlib-imagex"):
        _auto_install(
            "wimlib", "wimlib-imagex",
            "needed for Windows 11 ISOs where install.wim > 4 GiB"
        )


# ── ISO ───────────────────────────────────────────────────────────────────────

def ask_iso_path() -> Path:
    while True:
        raw = Prompt.ask("\n[bold cyan]Path to Windows ISO[/bold cyan]").strip().strip("'\"")
        p = Path(raw).expanduser().resolve()
        if not p.exists():
            console.print(f"[red]Not found:[/red] {p}")
            continue
        if p.suffix.lower() != ".iso":
            console.print("[yellow]Warning: file doesn't end in .iso — continuing anyway.[/yellow]")
        return p


def mount_iso(iso: Path) -> str:
    """Mount ISO read-only via hdiutil, return mount point path."""
    console.print(f"\n[dim]Mounting {iso.name}...[/dim]")
    result = run(["hdiutil", "attach", "-nobrowse", "-readonly", str(iso)])
    for line in reversed(result.stdout.strip().splitlines()):
        parts = line.split("\t")
        if len(parts) >= 3 and parts[-1].strip().startswith("/"):
            return parts[-1].strip()
    raise RuntimeError(f"Could not parse mount point:\n{result.stdout}")


def unmount_iso(mount_point: str):
    run(["hdiutil", "detach", mount_point, "-force"], check=False)


def detect_iso(mount_point: str) -> dict:
    """Inspect ISO contents and report boot capabilities."""
    mp = Path(mount_point)
    has_uefi      = (mp / "efi" / "boot" / "bootx64.efi").exists()
    has_bootsect  = (mp / "boot" / "bootsect.dat").exists()
    has_bootmgr   = (mp / "bootmgr").exists()
    is_win7_era   = has_bootmgr and not has_uefi

    t = Table(title="ISO Analysis", show_lines=False, box=None, padding=(0, 2))
    t.add_column("Property", style="dim")
    t.add_column("Value", style="bold")
    t.add_row("UEFI boot (efi/boot/bootx64.efi)",
              "[green]Yes[/green]" if has_uefi else "[red]No[/red]")
    t.add_row("Legacy BIOS boot (bootmgr)",
              "[green]Yes[/green]" if has_bootmgr else "[red]No[/red]")
    t.add_row("VBR source (boot/bootsect.dat)",
              "[green]Yes[/green]" if has_bootsect else "[yellow]Missing[/yellow]")
    t.add_row("Detected type",
              "[yellow]Windows 7 / 8 legacy[/yellow]" if is_win7_era
              else "[cyan]Windows 8.1 / 10 / 11[/cyan]")
    console.print()
    console.print(t)

    if is_win7_era:
        console.print(
            "[yellow]No UEFI boot files found.[/yellow] Will write legacy BIOS boot sector.\n"
            "[dim]Target PC may need CSM / Legacy Boot enabled in BIOS.[/dim]"
        )

    return {"uefi": has_uefi, "has_bootsect": has_bootsect, "is_win7_era": is_win7_era}


# ── USB detection ─────────────────────────────────────────────────────────────

def list_usb_drives() -> list[dict]:
    result = run(["diskutil", "list", "-plist", "external", "physical"])
    data = plistlib.loads(result.stdout.encode())
    disks = []
    for dev in data.get("WholeDisks", []):
        info = plistlib.loads(run(["diskutil", "info", "-plist", dev]).stdout.encode())
        disks.append({
            "node": f"/dev/{dev}",
            "name": info.get("MediaName", "Unknown"),
            "size": info.get("TotalSize", 0),
            "protocol": info.get("BusProtocol", "?"),
        })
    return disks


def fmt_size(b: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} PB"


def ask_usb(drives: list[dict]) -> dict:
    t = Table(title="Detected USB Drives", show_lines=True)
    t.add_column("#", style="bold cyan", width=3)
    t.add_column("Device", style="bold")
    t.add_column("Name")
    t.add_column("Size", justify="right")
    t.add_column("Bus")
    for i, d in enumerate(drives, 1):
        t.add_row(str(i), d["node"], d["name"], fmt_size(d["size"]), d["protocol"])
    console.print()
    console.print(t)

    while True:
        raw = Prompt.ask("[bold cyan]Select USB drive number[/bold cyan]")
        try:
            idx = int(raw) - 1
            if 0 <= idx < len(drives):
                return drives[idx]
        except ValueError:
            pass
        console.print(f"[red]Enter 1–{len(drives)}.[/red]")


# ── disk prep ─────────────────────────────────────────────────────────────────

def format_usb(disk_node: str):
    """Erase and format USB as FAT32 + MBR — widest UEFI + BIOS compat."""
    console.print(f"\n[yellow]Erasing {disk_node} as FAT32 (MBR)...[/yellow]")
    run(["diskutil", "eraseDisk", "FAT32", "WINUSB", "MBRFormat", disk_node], capture=False)


def get_volume_path(disk_node: str) -> Path:
    info = plistlib.loads(run(["diskutil", "info", "-plist", disk_node + "s1"]).stdout.encode())
    mp = info.get("MountPoint", "")
    if not mp:
        raise RuntimeError(f"No mount point for {disk_node}s1")
    return Path(mp)


def set_mbr_active_partition(disk_node: str):
    """
    Set first MBR partition entry as active (bootable flag = 0x80).
    Required for legacy BIOS to hand off to the VBR.
    Partition table starts at MBR byte 446; each entry is 16 bytes.
    """
    raw = disk_node.replace("/dev/disk", "/dev/rdisk")
    # Must unmount before opening raw disk device — macOS blocks rdisk while mounted
    run(["diskutil", "unmountDisk", disk_node], check=False)
    with open(raw, "rb") as f:
        mbr = bytearray(f.read(512))
    for i, off in enumerate((446, 462, 478, 494)):
        mbr[off] = 0x80 if i == 0 else 0x00
    with open(raw, "r+b") as f:
        f.seek(0)
        f.write(mbr)
    console.print("[green]✓ MBR active partition flag set[/green]")


def write_windows_vbr(disk_node: str, iso_mount: str):
    """
    Patch the FAT32 VBR on the USB partition with Windows boot code.

    Windows ISOs ship boot/bootsect.dat — the exact VBR code Windows needs.
    We merge it with the BPB (bytes 3-89) that diskutil wrote during format
    so the FAT32 filesystem metadata stays intact while the boot code becomes
    Windows-compatible. Without this, legacy BIOS boot silently fails on Win7.

    Byte layout of a FAT32 boot sector:
      0-2   : jump instruction          ← take from bootsect.dat
      3-89  : BIOS Parameter Block      ← keep from diskutil (filesystem data)
      90-509: boot code                 ← take from bootsect.dat
      510-511: 0x55AA signature         ← take from bootsect.dat
    """
    bootsect = Path(iso_mount) / "boot" / "bootsect.dat"
    if not bootsect.exists():
        console.print(
            "[yellow]boot/bootsect.dat not found in ISO — skipping VBR write.[/yellow]\n"
            "[dim]Legacy BIOS boot (Win7) may not work.[/dim]"
        )
        return

    partition_node = disk_node + "s1"
    raw_part = partition_node.replace("/dev/disk", "/dev/rdisk")

    with open(bootsect, "rb") as f:
        new_vbr = bytearray(f.read(512))

    # Unmount partition so macOS doesn't interfere with raw sector write
    run(["diskutil", "unmount", partition_node], check=False)

    with open(raw_part, "rb") as f:
        current_vbr = bytearray(f.read(512))

    merged = bytearray(512)
    merged[0:3]    = new_vbr[0:3]        # jump
    merged[3:90]   = current_vbr[3:90]   # BPB preserved
    merged[90:512] = new_vbr[90:512]     # Windows boot code + signature

    with open(raw_part, "r+b") as f:
        f.seek(0)
        f.write(merged)

    console.print("[green]✓ Windows FAT32 VBR written[/green]")

    # Remount so file copy can proceed
    run(["diskutil", "mount", partition_node], check=True)
    console.print("[green]✓ Partition remounted[/green]")


# ── file copy ─────────────────────────────────────────────────────────────────

def get_wim_path(mount_point: str) -> Path | None:
    for name in ("install.wim", "install.esd"):
        p = Path(mount_point) / "sources" / name
        if p.exists():
            return p
    return None


_COPY_BUF = 1024 * 1024  # 1 MB buffer — faster than shutil default 16 KB
_COPY_WORKERS = 4        # 4 threads: good I/O parallelism without hammering USB


def _copy_one(src_file: Path, dst_file: Path):
    with src_file.open("rb") as fsrc, dst_file.open("wb") as fdst:
        shutil.copyfileobj(fsrc, fdst, length=_COPY_BUF)
    shutil.copystat(src_file, dst_file)
    return src_file


def copy_files_except_wim(src: str, dst: Path):
    src_path = Path(src)
    skip = {src_path / "sources" / "install.wim", src_path / "sources" / "install.esd"}
    files = [p for p in src_path.rglob("*") if p.is_file() and p not in skip]
    # Large files first — they take longest, so start them while small files fill in
    files.sort(key=lambda p: p.stat().st_size, reverse=True)

    # Pre-create all dirs before threads start — avoids mkdir contention
    for d in {(dst / f.relative_to(src_path)).parent for f in files}:
        d.mkdir(parents=True, exist_ok=True)

    console.print("\n[dim]Copying boot files...[/dim]")
    with Progress(
        TextColumn("[cyan]{task.fields[filename]}[/cyan]", justify="left",
                   table_column=Column(width=35, no_wrap=True)),
        BarColumn(),
        MofNCompleteColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("copy", total=len(files), filename="")
        with ThreadPoolExecutor(max_workers=_COPY_WORKERS) as pool:
            futures = {
                pool.submit(_copy_one, f, dst / f.relative_to(src_path)): f
                for f in files
            }
            for future in as_completed(futures):
                src_file = futures[future]
                future.result()  # re-raise any copy error
                progress.update(task, filename=src_file.relative_to(src_path).name)
                progress.advance(task)


def split_and_copy_wim(wim_path: Path, dst: Path):
    """Split install.wim into ≤3800 MB chunks (FAT32 safe) via wimlib."""
    if not shutil.which("wimlib-imagex"):
        raise RuntimeError("wimlib-imagex missing. Run: brew install wimlib")
    out_dir = dst / "sources"
    out_dir.mkdir(parents=True, exist_ok=True)
    console.print(f"\n[yellow]install.wim > 4 GiB — splitting into .swm chunks...[/yellow]")
    run(["wimlib-imagex", "split", str(wim_path), str(out_dir / "install.swm"), "3800"],
        capture=False)


def copy_wim_direct(wim_path: Path, dst: Path):
    dst_dir = dst / "sources"
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst_file = dst_dir / wim_path.name
    size = wim_path.stat().st_size
    chunk = 4 * 1024 * 1024  # 4 MB — good balance for USB sequential write

    console.print(f"\n[dim]Copying {wim_path.name} ({fmt_size(size)})...[/dim]")
    with Progress(
        TextColumn("[cyan]{task.fields[filename]}[/cyan]",
                   table_column=Column(width=35, no_wrap=True)),
        BarColumn(),
        FileSizeColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("wim", total=size, filename=wim_path.name)
        # Deliberately not os.sendfile: on macOS/BSD sendfile(2) requires the
        # destination fd to be a socket and fails with ENOTSOCK for file->file.
        # readinto on a reused buffer keeps this allocation-free per chunk.
        buf = bytearray(chunk)
        view = memoryview(buf)
        with open(wim_path, "rb") as fsrc, open(dst_file, "wb") as fdst:
            while True:
                n = fsrc.readinto(buf)
                if not n:
                    break
                fdst.write(view[:n])
                progress.advance(task, n)
    shutil.copystat(wim_path, dst_file)


# ── main ──────────────────────────────────────────────────────────────────────

def fmt_duration(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s}s" if m else f"{s}s"


def main():
    console.print(Panel(
        "[bold white]macos-rufus[/bold white]  —  Windows bootable USB creator",
        subtitle="macOS · UEFI + Legacy BIOS · Win7/8/8.1/10/11",
        style="bold blue",
    ))

    escalate_to_root()
    log_path = setup_logger()
    log.info("macos-rufus started")
    check_deps()

    # 1. ISO path
    iso_path = ask_iso_path()
    log.info("ISO: %s", iso_path)

    # 2. USB selection
    drives = list_usb_drives()
    if not drives:
        console.print("[red]No external USB drives detected.[/red]")
        sys.exit(1)
    selected = ask_usb(drives)
    disk_node = selected["node"]
    log.info("Target USB: %s (%s, %s)", disk_node, selected["name"], fmt_size(selected["size"]))

    # 3. Confirm destructive action
    console.print(
        f"\n[bold red]WARNING:[/bold red] "
        f"[white]{disk_node}[/white] ([yellow]{selected['name']}[/yellow], "
        f"{fmt_size(selected['size'])}) will be [bold red]completely erased[/bold red]."
    )
    if not Confirm.ask("[bold]Proceed?[/bold]", default=False):
        console.print("Aborted.")
        log.info("User aborted at confirmation.")
        sys.exit(0)

    start_time = time.monotonic()
    mount_point = None
    try:
        # 4. Mount ISO + detect type
        mount_point = mount_iso(iso_path)
        console.print(f"[green]ISO mounted at:[/green] {mount_point}")
        log.info("ISO mounted at %s", mount_point)
        iso_info = detect_iso(mount_point)
        log.info("ISO type: uefi=%s bootsect=%s win7era=%s",
                 iso_info["uefi"], iso_info["has_bootsect"], iso_info["is_win7_era"])

        # 5. Format USB
        run(["diskutil", "unmountDisk", disk_node], check=False)
        log.info("Formatting %s as FAT32 MBR", disk_node)
        format_usb(disk_node)

        # 6. Write Windows boot sector (legacy BIOS support — Win7 + fallback for all)
        console.print("\n[dim]Writing boot sectors...[/dim]")
        log.info("Writing MBR active partition flag")
        set_mbr_active_partition(disk_node)
        log.info("Writing Windows VBR")
        write_windows_vbr(disk_node, mount_point)

        # 7. Ensure partition mounted before file copy
        run(["diskutil", "mount", disk_node + "s1"], check=False)
        usb_volume = get_volume_path(disk_node)
        console.print(f"[green]USB volume:[/green] {usb_volume}")
        log.info("USB volume: %s", usb_volume)

        # 8. Copy files
        wim_path = get_wim_path(mount_point)
        log.info("Copying boot files (parallel)")
        copy_files_except_wim(mount_point, usb_volume)
        log.info("Boot file copy done")

        if wim_path:
            if wim_path.stat().st_size > FAT32_LIMIT:
                log.info("install.wim > 4 GiB — splitting")
                split_and_copy_wim(wim_path, usb_volume)
                log.info("WIM split done")
            else:
                log.info("Copying %s (%s)", wim_path.name, fmt_size(wim_path.stat().st_size))
                copy_wim_direct(wim_path, usb_volume)
                log.info("WIM copy done")
        else:
            console.print("[yellow]No install.wim/install.esd found in ISO.[/yellow]")
            log.warning("No install.wim/install.esd found in ISO")

        # 9. Flush and eject
        console.print("\n[dim]Flushing writes...[/dim]")
        run(["diskutil", "unmountDisk", disk_node], check=False)

        elapsed = time.monotonic() - start_time
        boot_mode = "UEFI + Legacy BIOS" if iso_info["uefi"] else "Legacy BIOS only"
        boot_note = (
            "[green]UEFI + Legacy BIOS[/green] boot supported."
            if iso_info["uefi"]
            else "[yellow]Legacy BIOS only[/yellow] — enable CSM in target PC BIOS."
        )

        summary = Table.grid(padding=(0, 2))
        summary.add_column(style="dim")
        summary.add_column(style="bold white")
        summary.add_row("ISO",        iso_path.name)
        summary.add_row("Drive",      f"{disk_node}  ({selected['name']}, {fmt_size(selected['size'])})")
        summary.add_row("Boot mode",  boot_note)
        summary.add_row("Time taken", f"[bold cyan]{fmt_duration(elapsed)}[/bold cyan]")
        summary.add_row("Log saved",  f"[dim]{log_path}[/dim]")

        console.print()
        console.print(Panel(
            Text.assemble(("  Done! ", "bold green"), ("Bootable Windows USB is ready.\n\n", "white")),
            style="bold green",
            subtitle="[dim]Safe to unplug[/dim]",
        ))
        console.print(summary)
        console.print()

        log.info("Finished successfully in %s (%.1fs)", fmt_duration(elapsed), elapsed)

    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")
        log.warning("Interrupted by user")
        sys.exit(1)
    except Exception as e:
        console.print(f"\n[bold red]Error:[/bold red] {e}")
        log.error("Fatal error: %s", e, exc_info=True)
        sys.exit(1)
    finally:
        if mount_point:
            console.print("[dim]Unmounting ISO...[/dim]")
            unmount_iso(mount_point)
            log.info("ISO unmounted")


if __name__ == "__main__":
    main()
