# 🖥️ macos-rufus

> **The Rufus alternative for macOS** — create bootable Windows 7 / 8 / 8.1 / 10 / 11 USB drives directly from your Mac. No VMs, no Boot Camp, no nonsense.

[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![macOS](https://img.shields.io/badge/macOS-12%2B-000000?style=flat-square&logo=apple&logoColor=white)](https://apple.com/macos)
[![License: MIT](https://img.shields.io/badge/License-MIT-green?style=flat-square)](LICENSE)
[![Windows 7](https://img.shields.io/badge/Windows-7-0078D6?style=flat-square&logo=windows&logoColor=white)](https://microsoft.com)
[![Windows 10](https://img.shields.io/badge/Windows-10-0078D6?style=flat-square&logo=windows&logoColor=white)](https://microsoft.com)
[![Windows 11](https://img.shields.io/badge/Windows-11-0078D6?style=flat-square&logo=windows11&logoColor=white)](https://microsoft.com)

---

## 📌 What Is This?

**macos-rufus** is an open-source command-line tool for macOS that does exactly one thing, perfectly: it turns a Windows ISO file into a bootable USB drive — for every Windows version from 7 to 11.

If you've ever searched for:

- *"Rufus for Mac"*
- *"How to create bootable Windows USB on macOS"*
- *"Make Windows 11 bootable USB from Mac"*
- *"Rufus alternative macOS"*
- *"Create Windows 7 bootable USB on Mac"*
- *"dd command Windows ISO Mac"* (hint: `dd` doesn't work for Windows ISOs)
- *"How to install Windows from a Mac without Boot Camp"*

…you found the right tool.

---

## ✨ Features

| Feature | Details |
|---|---|
| **Windows 7, 8, 8.1, 10, 11 support** | All versions, UEFI and legacy BIOS |
| **Built-in Windows 10/11 ISO downloader** | Pick an OS from a menu — fetches the real Microsoft ISO link directly, no Media Creation Tool |
| **Managed download folder** | Downloaded ISOs are saved to `~/Downloads/macos-rufus/isos` and reused on later runs instead of re-downloading |
| **Resumable downloads** | Kill the script mid-download and it'll offer to pick up where it left off next run — even regenerating an expired link automatically |
| **Auto self-elevation** | Prompts for your password itself — no `sudo` prefix needed |
| **Auto-installs dependencies** | Missing `wimlib`? Script offers to install via Homebrew automatically |
| **Handles install.wim > 4 GB** | Splits oversized WIM files — the #1 cause of failure on other tools |
| **Writes Windows VBR boot sector** | Patches `boot/bootsect.dat` from the ISO onto the USB — required for Win7 legacy BIOS boot |
| **Sets MBR active partition flag** | Pure Python struct write — no `ms-sys` or external tool required |
| **ISO auto-detection** | Detects UEFI vs legacy-only ISOs, warns when CSM is needed on target PC |
| **FAT32 + MBR format** | Maximum compatibility across UEFI and legacy BIOS systems |
| **Interactive USB picker** | Lists all connected USB drives with size and name — no guessing device paths |
| **Safe by design** | External-only disk listing, explicit warning, manual confirm before any write |
| **Parallel file copy** | 4-thread concurrent copy with 1 MB buffers — saturates USB write bandwidth |
| **Beautiful terminal UI** | Powered by `rich` — color, tables, progress bars with speed + ETA |
| **Drag-and-drop ISO path** | Drag your `.iso` file into the terminal — quoted paths handled automatically |

---

## 🚀 Quick Start

### Option 1 — Homebrew (recommended)

```bash
brew install yaviral17/macos-rufus/macos-rufus
```

Then run from anywhere:

```bash
macos-rufus
```

> `wimlib` is installed automatically as a dependency.

---

### Option 2 — Run from source

**1. Prerequisites**

Install [Homebrew](https://brew.sh) if you haven't already. That's it — the script handles the rest.

> **Optional pre-install** (script will offer this automatically if missing):
> ```bash
> brew install wimlib   # needed for Windows 11 ISOs where install.wim > 4 GB
> ```

**2. Clone the repo**

```bash
git clone https://github.com/yaviral17/macos-rufus.git
cd macos-rufus
```

**3. Set up the Python virtual environment**

```bash
python3 -m venv venv
pip install -r requirements.txt
```

> The first time you use the automatic Windows 10/11 ISO downloader, the
> script will offer to run `playwright install chromium` for you (one-time,
> ~150–300 MB) — needed to get past Microsoft's download-page bot detection.

**4. Run it**

```bash
venv/bin/python3 rufus.py
```

> No `sudo` needed — the script detects it needs root access and prompts for your password automatically.

---

## 🖱️ GUI App

Prefer a point-and-click app over the terminal? `gui.py` is a PySide6
(Qt) wizard built on top of the exact same `rufus.py` logic — OS selection,
resumable downloads, disk formatting, and boot-sector writing are unchanged;
this just adds a windowed front-end.

**Run it from source:**

```bash
pip install -r requirements.txt   # includes PySide6
python3 gui.py
```

**Build it into a real double-clickable `macos-rufus.app`:**

```bash
pip install py2app
python3 setup.py py2app
open dist/macos-rufus.app
```

**How privileges work in the GUI:** unlike the CLI (which re-execs itself as
root immediately), the GUI app itself never runs as root. Only the final
"Flash Drive" step launches `rufus_worker.py` — a small script that does the
actual mounting/formatting/writing — as root, via macOS's native
administrator-password dialog (the same one any signed installer uses). The
GUI window stays a normal, unprivileged process the whole time and just
watches that worker's progress.

The wizard flow: pick an OS → download or browse for an ISO (pause/resume
supported, same as the CLI) → pick a USB drive → confirm → watch it flash →
done. An incomplete download from a previous session is detected on launch
and offered for resume automatically.

---

## 📖 Step-by-Step Usage (CLI)

```
╭──────────────────────────────────────────────────────────────╮
│  macos-rufus  —  Windows bootable USB creator                │
│          macOS · UEFI + Legacy BIOS · Win7/8/8.1/10/11       │
╰──────────────────────────────────────────────────────────────╯
```

**Step 1 — Pick an OS**

```
                  Select Operating System
┏━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━┓
┃ #   ┃ OS                         ┃ Auto-download        ┃
┡━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━┩
│ 1   │ Windows 11                 │ Yes                  │
│ 2   │ Windows 10                 │ Yes                  │
│ 3   │ Windows 8.1                │ No — manual ISO only │
│ 4   │ Windows 7                  │ No — manual ISO only │
│ 5   │ I already have an ISO file │ No — manual ISO only │
└─────┴────────────────────────────┴──────────────────────┘
Select an OS: 1
```

- **Windows 10 / 11** — pick an edition and language, and the script fetches
  a real, direct ISO link (the same one behind the "no Media Creation Tool"
  trick) and downloads it straight into `~/Downloads/macos-rufus/isos`.
  - Getting the actual download link requires briefly driving a real
    (headless) Chromium browser, because Microsoft's link-generation API
    blocks plain HTTP requests with bot detection. The first time you use
    this, you'll be prompted to let Playwright download Chromium
    (one-time, ~150–300 MB).
  - If a matching ISO was already downloaded before, you'll be offered it
    first instead of downloading again.
  - If the script is killed mid-download, the next run detects the partial
    file and offers to resume it — even regenerating the link automatically
    if it expired in the meantime.
- **Windows 8.1 / 7 / "I already have an ISO"** — Microsoft no longer serves
  these through the API, so you'll be prompted for a path instead:

```
Path to Windows ISO: /Users/you/Downloads/Win11_23H2_English_x64.iso
```

Drag and drop the `.iso` file directly from Finder into the terminal — quoted paths are handled automatically.

**Step 2 — ISO analysis**

```
  Property                              Value
  UEFI boot (efi/boot/bootx64.efi)     Yes
  Legacy BIOS boot (bootmgr)           Yes
  VBR source (boot/bootsect.dat)       Yes
  Detected type                        Windows 8.1 / 10 / 11
```

For Windows 7 ISOs, you'll see a warning to enable CSM/Legacy Boot on the target PC.

**Step 3 — Pick your USB drive**

```
┌───────────────────────────────────────────────────────────┐
│                    Detected USB Drives                    │
├───┬──────────────┬────────────────────┬────────┬─────────┤
│ # │ Device       │ Name               │   Size │ Bus     │
├───┼──────────────┼────────────────────┼────────┼─────────┤
│ 1 │ /dev/disk4   │ SanDisk Ultra USB  │ 32.0 GB│ USB     │
│ 2 │ /dev/disk5   │ Kingston DataTrav… │ 64.0 GB│ USB     │
└───┴──────────────┴────────────────────┴────────┴─────────┘

Select USB drive number: 1
```

**Step 4 — Confirm**

```
WARNING: /dev/disk4 (SanDisk Ultra USB, 32.0 GB) will be completely erased.
Proceed? [y/N]:
```

**Step 5 — Sit back**

The tool will:
1. Mount the ISO read-only via `hdiutil`
2. Erase and format the USB as FAT32 + MBR
3. Set the MBR active partition flag (legacy BIOS boot requirement)
4. Write the Windows FAT32 VBR from `boot/bootsect.dat` in the ISO (Win7 support)
5. Copy all boot files in parallel (4 threads, 1 MB buffers) with live progress bar
6. Copy `install.wim` in 4 MB chunks with speed + ETA display — or split automatically if > 4 GiB
7. Flush writes and unmount cleanly

---

## ⚙️ How It Works (Technical)

### Why not just use `dd`?

Unlike Linux ISOs (hybrid ISOs that `dd` writes byte-for-byte), **Windows ISOs are not `dd`-compatible**. Writing a Windows ISO with `dd` produces a non-bootable USB. You need to:

1. Format the USB with a real partition table (MBR) and filesystem (FAT32)
2. Copy the ISO *contents*, not the ISO image itself
3. Write the correct boot sector code so BIOS knows how to start Windows

### The 4 GiB problem

FAT32 has a hard per-file limit of 4,294,967,295 bytes (~4 GiB). Windows 11's `install.wim` often exceeds this. Most tools fail silently here.

**macos-rufus** detects this automatically and uses `wimlib-imagex split` to break the WIM into 3,800 MB chunks (`install.swm`, `install2.swm`, …). The Windows installer natively reassembles split WIM files — no extra steps on the target machine.

Newer Windows 11 ISOs (24H2+) store `install.wim` as a *solid* WIM, which wimlib cannot split directly. In that case macos-rufus first re-exports it to a regular (LZX) WIM in a temporary folder, then splits it. This takes a few extra minutes and needs free space on your Mac roughly equal to the size of `install.wim` — it is checked before the USB is erased.

### Windows 7 legacy BIOS boot — the VBR problem

This is what breaks every other "copy files to USB" approach for Windows 7:

1. **MBR active partition flag** — the MBR partition table entry must have byte 0 set to `0x80` (bootable). The script writes this directly into the raw MBR using Python's `struct` — no external tool needed.

2. **Windows FAT32 VBR** — `diskutil` formats the partition with a generic FAT32 Volume Boot Record. Legacy BIOS reads the VBR to find `bootmgr`, but the generic VBR doesn't know about Windows. Every Windows ISO ships `boot/bootsect.dat` — the exact VBR code Windows needs. The script reads it, merges it with the existing FAT32 BPB (bytes 3–89, which hold filesystem metadata and must be preserved), and writes the merged sector back to the raw partition device.

   ```
   FAT32 boot sector layout (512 bytes):
     [0-2]    jump instruction     ← from bootsect.dat
     [3-89]   FAT32 BPB            ← preserved from diskutil format
     [90-511] Windows boot code    ← from bootsect.dat
   ```

This requires no `ms-sys`, no Wine, no Windows machine — just the ISO itself.

### Partition scheme: MBR vs GPT

MBR + FAT32 is chosen deliberately:

- **UEFI systems** — boot via `efi/boot/bootx64.efi` (all modern Windows ISOs) — works on FAT32 + MBR
- **Legacy BIOS systems** — boot via MBR → VBR → `bootmgr` — requires MBR scheme
- GPT + FAT32 works on pure UEFI but breaks on BIOS — MBR is the universally safe default

### Windows version compatibility

| Version | UEFI boot | Legacy BIOS boot | Notes |
|---|---|---|---|
| Windows 7 | ❌ most ISOs | ✅ via VBR write | Enable CSM on modern PCs |
| Windows 8 | ✅ | ✅ | Full support |
| Windows 8.1 | ✅ | ✅ | Full support |
| Windows 10 | ✅ | ✅ | Full support |
| Windows 11 | ✅ | ✅ | install.wim auto-split if > 4 GiB |

### Tools used under the hood

| Tool | Source | Purpose |
|---|---|---|
| `hdiutil` | macOS built-in | Mount ISO as read-only volume |
| `diskutil` | macOS built-in | List disks, erase, format, mount/unmount |
| `wimlib-imagex` | Homebrew (`wimlib`) | Split install.wim > 4 GiB |
| `rich` | pip | Terminal UI — progress bars, tables, color |
| Python `readinto` | stdlib | Chunked WIM copy into a reused buffer — no per-chunk allocation |
| Python `ThreadPoolExecutor` | stdlib | Parallel boot file copy across 4 threads |
| Python `struct` | stdlib | Write MBR active flag + merge VBR sectors |

---

## 🔧 Requirements

| Requirement | Version |
|---|---|
| macOS | 12 Monterey or later (tested on Ventura, Sonoma, Sequoia) |
| Python | 3.11+ |
| Homebrew | Any recent version (for wimlib auto-install) |
| USB drive | 8 GB minimum for Windows 7/8/10, 16 GB for Windows 11 |

---

## 📥 Getting the Windows ISO

Download official ISOs directly from Microsoft:

- **Windows 11**: [microsoft.com/software-download/windows11](https://www.microsoft.com/software-download/windows11)
- **Windows 10**: [microsoft.com/software-download/windows10ISO](https://www.microsoft.com/software-download/windows10ISO)
- **Windows 7**: [microsoft.com/software-download/windows7](https://www.microsoft.com/software-download/windows7) *(requires product key)*

> **Tip for macOS users:** Microsoft's download page sometimes redirects to the Media Creation Tool (Windows-only exe). In Safari, go to **Develop → User Agent → change to any non-Windows browser** — the direct ISO download link appears.

---

## ❓ Troubleshooting

### "No external USB drives detected"

- Confirm the drive appears in Finder or Disk Utility
- Try a different USB port or cable
- Run `diskutil list` to verify macOS sees it

### "install.wim splitting fails"

- Run `brew install wimlib` manually and retry
- Check free disk space — the split writes chunks to the USB as it goes, and solid WIMs (Win 11 24H2+) need temporary space on your Mac too
- Garbled box-drawing characters (`â macos-rufus â`) mean your terminal isn't using UTF-8; run `export LANG=en_US.UTF-8` first

### "USB not booting on target machine"

1. Enter the BIOS/UEFI boot menu (`F12`, `F2`, `Esc`, or `Del` on boot)
2. For Windows 8.1/10/11 — select USB under **UEFI** boot entries
3. Disable **Secure Boot** if Windows Setup won't launch (re-enable after install)
4. For Windows 7 or older PCs — enable **CSM / Legacy Boot** in BIOS

### "Automatic download failed" / "Sentinel marked this request as rejected"

Microsoft's link-generation API is protected by bot detection that blocks
plain HTTP requests — the script works around this by driving a real
headless Chromium browser through the actual download page instead. If you
still see this error:

- Make sure Playwright's Chromium is installed: `python3 -m playwright install chromium`
- Check your internet connection can reach `microsoft.com` normally
- As a last resort, download the ISO manually (see below) and provide the path when prompted

### "hdiutil: attach failed"

- Verify the ISO isn't corrupted — check SHA256 against Microsoft's published hash
- Check the ISO isn't already mounted (look in Finder sidebar or `diskutil list`)

### Password prompt doesn't appear

Run directly — don't use `sudo` yourself:
```bash
# Homebrew install
macos-rufus

# Source install
venv/bin/python3 rufus.py
```
The script calls `sudo` internally via `os.execvp` and will prompt for your password.

---

## 🛠️ Project Structure

```
macos-rufus/
├── rufus.py          # CLI tool + all shared logic (OS catalog, downloads, disk ops)
├── rufus_worker.py   # Privileged flash worker used by the GUI (runs as root)
├── gui.py            # PySide6 GUI wizard, built on top of rufus.py
├── setup.py          # py2app config to build macos-rufus.app
├── requirements.txt  # Python dependencies
├── venv/             # Python virtual environment (not committed)
└── README.md         # This file
```

---

## 🔒 Security & Safety

- **External-only disk listing** — internal drives are never shown as targets
- **Explicit confirmation** before any destructive operation
- **Read-only ISO mount** — source ISO is never modified
- **Self-elevation only when needed** — the CLI requests root via `sudo` transparently; no silent privilege escalation
- **GUI never runs as root** — only the disk-writing worker it launches does, via macOS's native administrator-password dialog, for just the flash step

---

## 🤝 Contributing

Pull requests welcome. Focus areas:

- [ ] GPT + ExFAT/NTFS support for pure-UEFI installs (removes 4 GiB limit entirely)
- [ ] Progress bar for WIM split operation (`wimlib-imagex split`)
- [ ] ISO SHA256 verification against Microsoft checksums
- [ ] Windows Server ISO support
- [ ] GUI wrapper (Tkinter or native macOS via PyObjC)

---

## 📄 License

MIT — see [LICENSE](LICENSE).

---

## 🙏 Acknowledgements

- [Rufus](https://rufus.ie) — the original Windows tool this is inspired by
- [wimlib](https://wimlib.net) — open-source WIM library that makes Windows 11 USB creation possible on non-Windows systems
- [rich](https://github.com/Textualize/rich) — beautiful terminal output for Python

---

<p align="center">
  Made for every Mac user who just wants to install Windows without booting into a Windows machine first.
</p>

<p align="center">
  <strong>macos-rufus</strong> · Rufus for Mac · Bootable Windows USB · macOS ISO Tool · Windows 7 USB Mac · Create Windows USB from macOS
</p>
