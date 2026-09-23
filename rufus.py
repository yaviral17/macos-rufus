#!/usr/bin/env python3
"""macos-rufus: create bootable Windows USB drives on macOS."""

import json
import logging
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import requests
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


# ── OS catalog & ISO download ────────────────────────────────────────────────

# Microsoft only exposes the "no Media Creation Tool" ISO download API for
# Windows 10 and 11 ("dynamic"). Windows 7 / 8.1 are fetched from fixed
# archive.org URLs ("direct"). Anything else is manual-path-only.
OS_CATALOG = [
    {"name": "Windows 11", "slug": "windows11", "dynamic": True},
    {"name": "Windows 10", "slug": "windows10ISO", "dynamic": True},
    {"name": "Windows 8.1", "slug": None, "dynamic": False, "direct": [
        {"label": "64-bit (x64)", "arch": "x64",
         "url": "https://archive.org/download/win-8.1-english-x-64_20211019/Win8.1_English_x64.iso"},
        {"label": "32-bit (x86)", "arch": "x86",
         "url": "https://archive.org/download/win-8.1-english-x-64_20211019/Win8.1_English_x32.iso"},
    ]},
    {"name": "Windows 7", "slug": None, "dynamic": False, "direct": [
        {"label": "Professional 32-bit (x86)", "arch": "x86",
         "url": "https://archive.org/download/windows-7-iso-/Win7Pro32bit.iso"},
    ]},
    {"name": "Ubuntu", "slug": None, "dynamic": False, "linux": True, "direct": [
        {"label": "24.04 LTS Desktop (64-bit)", "tag": "lts-desktop-amd64",
         "resolver": "_resolve_ubuntu"},
    ]},
    {"name": "Arch Linux", "slug": None, "dynamic": False, "linux": True, "direct": [
        {"label": "Latest (64-bit)", "tag": "latest-x86_64",
         "url": "https://geo.mirror.pkgbuild.com/iso/latest/archlinux-x86_64.iso"},
    ]},
    {"name": "Kali Linux", "slug": None, "dynamic": False, "linux": True, "direct": [
        {"label": "Installer (64-bit)", "tag": "installer-amd64",
         "resolver": "_resolve_kali_installer"},
    ]},
    {"name": "Pop!_OS", "slug": None, "dynamic": False, "linux": True, "direct": [
        {"label": "24.04 — Intel/AMD graphics", "tag": "24.04-intel", "resolver": "_resolve_pop_2404_intel"},
        {"label": "24.04 — NVIDIA graphics", "tag": "24.04-nvidia", "resolver": "_resolve_pop_2404_nvidia"},
        {"label": "22.04 — Intel/AMD graphics", "tag": "22.04-intel", "resolver": "_resolve_pop_2204_intel"},
        {"label": "22.04 — NVIDIA graphics", "tag": "22.04-nvidia", "resolver": "_resolve_pop_2204_nvidia"},
    ]},
    {"name": "Omarchy", "slug": None, "dynamic": False, "linux": True, "direct": [
        {"label": "Latest (64-bit)", "tag": "latest-x86_64", "resolver": "_resolve_omarchy"},
    ]},
    {"name": "I already have an ISO file", "slug": None, "dynamic": False},
]


def direct_filename(entry: dict, option: dict) -> str:
    """Matches the prefix find_existing_isos() globs for."""
    prefix = entry['name'].replace(' ', '').replace('.', '').replace('!', '')
    return f"{prefix}_{option.get('tag') or 'en-us_' + option['arch']}.iso"


def _scrape_latest(index_url: str, pattern: str) -> str:
    """Newest href in an HTTP directory listing matching `pattern` (a regex).
    Distros rename ISOs every point release, so links can't be hard-coded."""
    resp = requests.get(index_url, timeout=30, headers={"User-Agent": _DOWNLOAD_UA})
    resp.raise_for_status()
    names = sorted(set(re.findall(pattern, resp.text)),
                   key=lambda n: [int(x) for x in re.findall(r"\d+", n)])
    if not names:
        raise RuntimeError(f"No ISO matching {pattern!r} found at {index_url}")
    return names[-1]


def _resolve_ubuntu() -> str:
    base = "https://releases.ubuntu.com/noble/"
    return base + _scrape_latest(base, r'href="(ubuntu-[\d.]+-desktop-amd64\.iso)"')


def _resolve_kali_installer() -> str:
    base = "https://cdimage.kali.org/current/"
    return base + _scrape_latest(base, r'href="(kali-linux-[\d.]+-installer-amd64\.iso)"')


def _resolve_pop(version: str, channel: str) -> str:
    resp = requests.get(f"https://api.pop-os.org/builds/{version}/{channel}?arch=amd64", timeout=30)
    resp.raise_for_status()
    return resp.json()["url"]


def _resolve_pop_2404_intel() -> str:  return _resolve_pop("24.04", "intel")
def _resolve_pop_2404_nvidia() -> str: return _resolve_pop("24.04", "nvidia")
def _resolve_pop_2204_intel() -> str:  return _resolve_pop("22.04", "intel")
def _resolve_pop_2204_nvidia() -> str: return _resolve_pop("22.04", "nvidia")


def _resolve_omarchy() -> str:
    resp = requests.get("https://omarchy.org", timeout=30, headers={"User-Agent": _DOWNLOAD_UA})
    resp.raise_for_status()
    m = re.search(r"https://iso\.omarchy\.org/omarchy-[\d.]+\.iso", resp.text)
    if not m:
        raise RuntimeError("Couldn't find the Omarchy ISO link on omarchy.org")
    return m.group(0)


def resolve_option_url(option: dict) -> str:
    """Fixed URL, or one looked up at download time by the named resolver."""
    return option.get("url") or globals()[option["resolver"]]()


def _plain_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": _DOWNLOAD_UA})
    return s

# Spoofing a non-Windows browser is required — Microsoft's download API
# redirects real Windows user agents to the Media Creation Tool instead of
# handing back a direct ISO link.
_DOWNLOAD_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)
_MS_ORG_ID = "y6jn8c31"
_MS_PROFILE = "606624d44113"

_ISO_STATE_SUFFIX = ".json"  # sidecar next to a "*.iso.part" file


# The whole script re-execs itself under `sudo` (see escalate_to_root), which
# resets $HOME to root's. Resolve the actual logged-in user's home so
# downloads land in *their* ~/Downloads, not /var/root's.
def get_user_home() -> Path:
    login_user = os.environ.get("SUDO_USER")
    if login_user:
        try:
            import pwd
            return Path(pwd.getpwnam(login_user).pw_dir)
        except KeyError:
            pass
    return Path.home()


def _chown_to_login_user(path: Path):
    login_user = os.environ.get("SUDO_USER")
    if not login_user or os.geteuid() != 0:
        return
    try:
        import pwd
        pw = pwd.getpwnam(login_user)
    except KeyError:
        return
    for p in (path, *path.rglob("*")) if path.is_dir() else (path,):
        try:
            os.chown(p, pw.pw_uid, pw.pw_gid)
        except OSError:
            pass


def get_downloads_dir() -> Path:
    d = get_user_home() / "Downloads" / "macos-rufus" / "isos"
    d.mkdir(parents=True, exist_ok=True)
    _chown_to_login_user(get_user_home() / "Downloads" / "macos-rufus")
    return d


def find_existing_isos(entry: dict, downloads_dir: Path) -> list[Path]:
    prefix = entry["name"].replace(" ", "").replace(".", "")
    matches = sorted(downloads_dir.glob(f"{prefix}_*.iso"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
    return matches


def find_incomplete_downloads(downloads_dir: Path) -> list[dict]:
    """Partial downloads left behind by an interrupted run — each ``*.iso.part``
    has a JSON sidecar with everything needed to resume or regenerate the link."""
    found = []
    for part in downloads_dir.glob("*.iso.part"):
        state_path = part.with_name(part.name + _ISO_STATE_SUFFIX)
        if not state_path.exists():
            continue
        try:
            state = json.loads(state_path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        found.append({"state": state, "part_path": part, "state_path": state_path})
    return found


def _write_download_state(tmp_dest: Path, state: dict):
    state_path = tmp_dest.with_name(tmp_dest.name + _ISO_STATE_SUFFIX)
    state_path.write_text(json.dumps(state, indent=2))
    _chown_to_login_user(state_path)


def _ms_download_session(slug: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": _DOWNLOAD_UA,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": f"https://www.microsoft.com/en-us/software-download/{slug}",
        "X-Requested-With": "XMLHttpRequest",
    })
    try:
        s.get("https://vlscppe.microsoft.com/tags",
              params={"org_id": _MS_ORG_ID, "session_id": str(uuid.uuid4())},
              timeout=15)
    except requests.RequestException:
        pass  # best-effort warm-up; not fatal if it fails
    return s


def fetch_product_editions(session: requests.Session, slug: str) -> list[tuple[str, str]]:
    """Scrape the download page's <select> options for edition IDs — avoids
    hardcoding IDs that Microsoft rotates with every release."""
    resp = session.get(f"https://www.microsoft.com/en-us/software-download/{slug}",
                        headers={"Accept": "text/html"}, timeout=20)
    resp.raise_for_status()
    options = re.findall(
        r'<option value="(\d+)"[^>]*>\s*([^<]+?)\s*</option>', resp.text
    )
    if not options:
        raise RuntimeError("Could not find a downloadable edition on Microsoft's page.")
    return options


def fetch_skus(session: requests.Session, edition_id: str, session_id: str) -> list[dict]:
    resp = session.get(
        "https://www.microsoft.com/software-download-connector/api/getskuinformationbyproductedition",
        params={
            "profile": _MS_PROFILE,
            "ProductEditionId": edition_id,
            "SKU": "undefined",
            "friendlyFileName": "undefined",
            "Locale": "en-US",
            "sessionID": session_id,
        },
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("Errors"):
        msgs = ", ".join(e.get("Value", "") for e in data["Errors"])
        raise RuntimeError(f"Microsoft rejected the request: {msgs}")
    skus = data.get("Skus") or []
    if not skus:
        raise RuntimeError(
            "Microsoft returned no language editions — this usually means the "
            "download API is rate-limiting this network/session."
        )
    return skus


def _ensure_playwright_ready():
    """GetProductDownloadLinksBySku (the call that returns the actual ISO URL)
    sits behind Microsoft's bot detection ("Sentinel") — it rejects plain HTTP
    clients even with identical headers/params to a real browser, because it
    checks a JS-computed device-fingerprint token that can't be forged without
    running the real page. A real (headless) browser passes it every time, so
    we drive one instead. This just makes sure Chromium is installed."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError(
            "The 'playwright' package is required for automatic ISO downloads. "
            "Install it with: pip install playwright"
        )
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            browser.close()
        return
    except Exception as e:
        if "Executable doesn't exist" not in str(e):
            raise
    console.print(
        "\n[yellow]Playwright's Chromium browser isn't installed yet[/yellow] — "
        "it's needed to get past Microsoft's bot-check on the download page."
    )
    if not Confirm.ask("Install it now (one-time download, ~150-300 MB)?", default=True):
        raise RuntimeError("Playwright's Chromium browser is required for automatic downloads.")
    result = subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"])
    if result.returncode != 0:
        raise RuntimeError(
            "Failed to install Playwright's Chromium browser. "
            "Run manually: python3 -m playwright install chromium"
        )
    console.print("[green]✓ Chromium installed[/green]")


def fetch_download_links(slug: str, edition_id: str, sku_id: str, sku_language: str) -> list[dict]:
    """Drive a real headless browser through Microsoft's own download page to
    get the ISO link, instead of calling GetProductDownloadLinksBySku
    directly — see _ensure_playwright_ready for why."""
    from playwright.sync_api import sync_playwright

    option_value = json.dumps({"id": sku_id, "language": sku_language}, separators=(",", ":"))
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(user_agent=_DOWNLOAD_UA)
            page.goto(f"https://www.microsoft.com/en-us/software-download/{slug}",
                      wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(500)
            page.select_option("#product-edition", edition_id)
            with page.expect_response(lambda r: "getskuinformationbyproductedition" in r.url, timeout=30000):
                page.click("#submit-product-edition")
            page.select_option("#product-languages", option_value)
            with page.expect_response(lambda r: "GetProductDownloadLinksBySku" in r.url, timeout=30000) as ri:
                page.click("#submit-sku")
            resp = ri.value
            if resp.status != 200:
                raise RuntimeError(f"Microsoft returned HTTP {resp.status} for the download link request.")
            data = resp.json()
        finally:
            browser.close()

    errs = (data.get("ValidationContainer") or {}).get("Errors") or data.get("Errors")
    if errs:
        msgs = ", ".join(e.get("Value", "") for e in errs)
        raise RuntimeError(f"Microsoft rejected the request: {msgs}")
    options = data.get("ProductDownloadOptions") or []
    if not options:
        raise RuntimeError(
            "Microsoft returned no download links — the ISO may no longer be "
            "offered for this edition/language."
        )
    return options


def _describe_download_option(opt: dict) -> str:
    name = opt.get("Name") or opt.get("LocalizedProductDisplayName") or "Download"
    m = re.search(r"(x64|x86|x32|arm64)", opt.get("Uri", ""), re.IGNORECASE)
    return f"{name} ({m.group(1).lower()})" if m else name


def _extract_arch(url: str) -> str:
    m = re.search(r"(x64|x86|x32|arm64)", url, re.IGNORECASE)
    return m.group(1).lower() if m else "unknown"


def ask_download_option_choice(options: list[dict]) -> dict:
    """Microsoft can return more than one download (e.g. different
    architectures) — show them all so the user picks the right one."""
    t = Table(title="Select Download", show_lines=True)
    t.add_column("#", style="bold cyan", width=3)
    t.add_column("Option")
    for i, opt in enumerate(options, 1):
        t.add_row(str(i), _describe_download_option(opt))
    console.print()
    console.print(t)
    while True:
        raw = Prompt.ask("[bold cyan]Select download option number[/bold cyan]")
        try:
            idx = int(raw) - 1
            if 0 <= idx < len(options):
                return options[idx]
        except ValueError:
            pass
        console.print(f"[red]Enter 1–{len(options)}.[/red]")


def _pick_download_option(options: list[dict], previous_name: str | None = None) -> dict:
    if len(options) == 1:
        return options[0]
    if previous_name:
        match = next((o for o in options if o.get("Name") == previous_name), None)
        if match:
            return match
    return ask_download_option_choice(options)


def download_iso_with_progress(session: requests.Session, url: str, dest: Path, resume: bool = False,
                                progress_cb=None, should_pause=None):
    """progress_cb(downloaded_bytes, total_bytes) is called after every chunk
    when given — used by the GUI instead of the Rich progress bar.
    should_pause() is polled between chunks when given; if it returns True,
    the download stops cleanly (like a Ctrl+C pause) instead of continuing —
    used by the GUI's Pause button."""
    tmp_dest = dest.with_suffix(".iso.part")
    headers = {}
    initial = 0
    if resume and tmp_dest.exists():
        initial = tmp_dest.stat().st_size
        headers["Range"] = f"bytes={initial}-"

    with session.get(url, stream=True, timeout=30, headers=headers) as resp:
        if resume and resp.status_code == 416:
            resp.close()
            console.print(f"[green]✓ {dest.name} was already fully downloaded[/green]")
        else:
            resp.raise_for_status()
            mode = "wb"
            if resp.status_code == 206:
                mode = "ab"
                content_range = resp.headers.get("Content-Range", "")
                total = int(content_range.rsplit("/", 1)[-1]) if "/" in content_range \
                    else initial + int(resp.headers.get("Content-Length", 0))
            else:
                # Server ignored the Range request (or this is a fresh download) — start over.
                initial = 0
                total = int(resp.headers.get("Content-Length", 0))

            if progress_cb is None:
                console.print(
                    f"\n[dim]{'Resuming' if mode == 'ab' else 'Downloading'} {dest.name}... "
                    "(Ctrl+C to pause — progress is saved and can be resumed later)[/dim]"
                )
            try:
                if progress_cb is not None:
                    downloaded = initial
                    progress_cb(downloaded, total)
                    with open(tmp_dest, mode) as f:
                        for chunk in resp.iter_content(chunk_size=1024 * 1024):
                            if chunk:
                                f.write(chunk)
                                downloaded += len(chunk)
                                progress_cb(downloaded, total)
                            if should_pause is not None and should_pause():
                                raise KeyboardInterrupt
                else:
                    with Progress(
                        TextColumn("[cyan]{task.fields[filename]}[/cyan]",
                                   table_column=Column(width=35, no_wrap=True)),
                        BarColumn(),
                        FileSizeColumn(),
                        TransferSpeedColumn(),
                        TimeRemainingColumn(),
                        console=console,
                    ) as progress:
                        task = progress.add_task("dl", total=total or None, filename=dest.name, completed=initial)
                        with open(tmp_dest, mode) as f:
                            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                                if chunk:
                                    f.write(chunk)
                                    progress.advance(task, len(chunk))
            except KeyboardInterrupt:
                so_far = tmp_dest.stat().st_size if tmp_dest.exists() else 0
                if progress_cb is None:
                    pct = f" ({so_far * 100 // total}%)" if total else ""
                    console.print(
                        f"\n[yellow]Paused[/yellow] — {fmt_size(so_far)}{pct} of {dest.name} saved. "
                        "Run macos-rufus again to resume from here."
                    )
                    raise SystemExit(0)
                raise  # let the GUI's caller decide how to report the pause

    tmp_dest.rename(dest)
    tmp_dest.with_name(tmp_dest.name + _ISO_STATE_SUFFIX).unlink(missing_ok=True)
    _chown_to_login_user(dest)


def ask_os_choice() -> dict:
    t = Table(title="Select Operating System", show_lines=True)
    t.add_column("#", style="bold cyan", width=3)
    t.add_column("OS")
    t.add_column("Auto-download")
    for i, entry in enumerate(OS_CATALOG, 1):
        t.add_row(str(i), entry["name"],
                   "[green]Yes[/green]" if entry["dynamic"] or entry.get("direct")
                   else "[dim]No — manual ISO only[/dim]")
    console.print()
    console.print(t)

    while True:
        raw = Prompt.ask("[bold cyan]Select an OS[/bold cyan]")
        try:
            idx = int(raw) - 1
            if 0 <= idx < len(OS_CATALOG):
                return OS_CATALOG[idx]
        except ValueError:
            pass
        console.print(f"[red]Enter 1–{len(OS_CATALOG)}.[/red]")


def ask_existing_iso_choice(entry: dict, found: list[Path]) -> Path | None:
    """Reuse an ISO already in the managed downloads folder. A single match is
    picked automatically; with several, the newest is the default."""
    if len(found) == 1:
        console.print(f"\n[green]✓ Found existing {entry['name']} ISO[/green] — using {found[0].name}")
        return found[0]
    t = Table(title=f"Existing {entry['name']} ISOs found", show_lines=True)
    t.add_column("#", style="bold cyan", width=3)
    t.add_column("File")
    t.add_column("Size", justify="right")
    t.add_column("Downloaded")
    for i, p in enumerate(found, 1):
        stat = p.stat()
        t.add_row(str(i), p.name, fmt_size(stat.st_size),
                   datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"))
    console.print()
    console.print(t)
    while True:
        raw = Prompt.ask("[bold cyan]Select file number (0 to download a new one)[/bold cyan]", default="1")
        try:
            idx = int(raw)
            if idx == 0:
                return None
            if 1 <= idx <= len(found):
                return found[idx - 1]
        except ValueError:
            pass
        console.print(f"[red]Enter 0–{len(found)}.[/red]")


def download_windows_iso(entry: dict, downloads_dir: Path) -> Path:
    session = _ms_download_session(entry["slug"])
    session_id = str(uuid.uuid4())

    console.print(f"\n[dim]Contacting Microsoft for {entry['name']} download options...[/dim]")
    editions = fetch_product_editions(session, entry["slug"])
    if len(editions) == 1:
        edition_id, edition_name = editions[0]
    else:
        t = Table(title="Select Edition", show_lines=True)
        t.add_column("#", style="bold cyan", width=3)
        t.add_column("Edition")
        for i, (_, name) in enumerate(editions, 1):
            t.add_row(str(i), name)
        console.print(t)
        while True:
            raw = Prompt.ask("[bold cyan]Select edition number[/bold cyan]")
            try:
                idx = int(raw) - 1
                if 0 <= idx < len(editions):
                    edition_id, edition_name = editions[idx]
                    break
            except ValueError:
                pass
            console.print(f"[red]Enter 1–{len(editions)}.[/red]")

    skus = fetch_skus(session, edition_id, session_id)
    t = Table(title="Select Language", show_lines=True)
    t.add_column("#", style="bold cyan", width=3)
    t.add_column("Language")
    for i, sku in enumerate(skus, 1):
        t.add_row(str(i), sku.get("LocalizedLanguage", sku.get("Language", "?")))
    console.print()
    console.print(t)
    while True:
        raw = Prompt.ask("[bold cyan]Select language number[/bold cyan]")
        try:
            idx = int(raw) - 1
            if 0 <= idx < len(skus):
                sku = skus[idx]
                break
        except ValueError:
            pass
        console.print(f"[red]Enter 1–{len(skus)}.[/red]")

    _ensure_playwright_ready()
    console.print("[dim]Opening a headless browser to get past Microsoft's download-page bot-check...[/dim]")
    links = fetch_download_links(entry["slug"], edition_id, sku["Id"], sku["Language"])
    chosen = _pick_download_option(links)
    url = chosen["Uri"]

    arch = _extract_arch(url)
    lang = sku.get("Language", "en-us")
    filename = f"{entry['name'].replace(' ', '').replace('.', '')}_{lang}_{arch}.iso"
    dest = downloads_dir / filename

    state = {
        "entry_name": entry["name"],
        "slug": entry["slug"],
        "edition_id": edition_id,
        "sku_id": sku["Id"],
        "sku_language": sku.get("LocalizedLanguage"),
        "sku_language_raw": sku["Language"],
        "option_name": chosen.get("Name"),
        "url": url,
        "dest": str(dest),
        "created": datetime.now().isoformat(),
    }
    _write_download_state(dest.with_suffix(".iso.part"), state)

    console.print(f"[green]✓ Got download link[/green] — saving to {dest}")
    download_iso_with_progress(session, url, dest)
    console.print(f"[green]✓ Downloaded {dest.name}[/green]")
    return dest


def download_direct_iso(entry: dict, downloads_dir: Path) -> Path:
    options = entry["direct"]
    if len(options) == 1:
        chosen = options[0]
    else:
        t = Table(title="Select Download", show_lines=True)
        t.add_column("#", style="bold cyan", width=3)
        t.add_column("Option")
        for i, o in enumerate(options, 1):
            t.add_row(str(i), o["label"])
        console.print()
        console.print(t)
        while True:
            raw = Prompt.ask("[bold cyan]Select download option number[/bold cyan]")
            try:
                idx = int(raw) - 1
                if 0 <= idx < len(options):
                    chosen = options[idx]
                    break
            except ValueError:
                pass
            console.print(f"[red]Enter 1–{len(options)}.[/red]")

    dest = downloads_dir / direct_filename(entry, chosen)
    console.print(f"[dim]Looking up the latest {entry['name']} ISO...[/dim]")
    url = resolve_option_url(chosen)
    state = {
        "entry_name": entry["name"], "direct": True, "sku_language": chosen["label"],
        "url": url, "dest": str(dest), "created": datetime.now().isoformat(),
    }
    _write_download_state(dest.with_suffix(".iso.part"), state)
    console.print(f"[green]✓ Saving {chosen['label']}[/green] to {dest}")
    download_iso_with_progress(_plain_session(), url, dest)
    console.print(f"[green]✓ Downloaded {dest.name}[/green]")
    return dest


def resume_incomplete_download(item: dict) -> Path:
    state, part_path = item["state"], item["part_path"]
    dest = Path(state["dest"])
    if state.get("direct"):
        download_iso_with_progress(_plain_session(), state["url"], dest, resume=True)
        console.print(f"[green]✓ Downloaded {dest.name}[/green]")
        return dest
    session = _ms_download_session(state["slug"])

    try:
        download_iso_with_progress(session, state["url"], dest, resume=True)
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else None
        # Expired signed URLs come back as an edge "Access Denied" — usually 403,
        # but treat any auth/not-found-shaped status as expired and regenerate.
        if status not in (400, 401, 403, 404, 410):
            raise
        console.print("[yellow]Download link expired — requesting a fresh one from Microsoft...[/yellow]")
        _ensure_playwright_ready()
        links = fetch_download_links(
            state["slug"], state["edition_id"], state["sku_id"],
            state.get("sku_language_raw", state.get("sku_language")),
        )
        chosen = _pick_download_option(links, state.get("option_name"))
        state["url"] = chosen["Uri"]
        _write_download_state(part_path, state)
        download_iso_with_progress(session, state["url"], dest, resume=True)

    console.print(f"[green]✓ Downloaded {dest.name}[/green]")
    return dest


def incomplete_for_entry(entry: dict, downloads_dir: Path) -> list[dict]:
    """Interrupted downloads belonging to this OS, newest first. A partial whose
    finished ISO already exists is stale and gets cleaned up."""
    items = []
    for item in find_incomplete_downloads(downloads_dir):
        state = item["state"]
        if state.get("entry_name") != entry["name"]:
            continue
        if Path(state.get("dest", "")).exists():
            item["part_path"].unlink(missing_ok=True)
            item["state_path"].unlink(missing_ok=True)
            continue
        items.append(item)
    return sorted(items, key=lambda i: i["part_path"].stat().st_mtime, reverse=True)


def auto_resume_downloads(entry: dict, downloads_dir: Path) -> Path | None:
    """Pick up a half-finished download for this OS where it left off."""
    for item in incomplete_for_entry(entry, downloads_dir):
        state, part_path = item["state"], item["part_path"]
        console.print(
            f"\n[yellow]Found an incomplete {entry['name']} download[/yellow] "
            f"({state.get('sku_language', '?')}, {fmt_size(part_path.stat().st_size)} saved) — resuming..."
        )
        try:
            return resume_incomplete_download(item)
        except (requests.RequestException, RuntimeError, KeyError, ValueError) as e:
            console.print(f"[red]Resume failed:[/red] {e}")
    return None


def ask_os_and_iso() -> Path:
    """Top-level flow: pick an OS, then automatically resume its interrupted
    download or reuse a finished one; otherwise download it or fall back to a
    manual path."""
    downloads_dir = get_downloads_dir()

    entry = ask_os_choice()
    downloadable = entry["dynamic"] or entry.get("direct")

    if downloadable:
        resumed = auto_resume_downloads(entry, downloads_dir)
        if resumed:
            return resumed
        found = find_existing_isos(entry, downloads_dir)
        if found:
            chosen = ask_existing_iso_choice(entry, found)
            if chosen:
                return chosen

    if entry.get("direct"):
        source = "the official mirror" if entry.get("linux") else "archive.org"
        if Confirm.ask(f"Download {entry['name']} now from {source}?", default=True):
            try:
                return download_direct_iso(entry, downloads_dir)
            except (requests.RequestException, RuntimeError, KeyError, OSError) as e:
                console.print(f"[red]Download failed:[/red] {e}")
        console.print("[dim]Provide the path to an existing ISO instead.[/dim]")
        return ask_iso_path()

    if not entry["dynamic"]:
        if entry["name"] != "I already have an ISO file":
            console.print(
                f"\n[yellow]{entry['name']} isn't available as a direct download.[/yellow] "
                "Please download it manually and provide the path below."
            )
        return ask_iso_path()

    if Confirm.ask(f"Download {entry['name']} now from Microsoft?", default=True):
        try:
            return download_windows_iso(entry, downloads_dir)
        except (requests.RequestException, RuntimeError, KeyError, ValueError) as e:
            console.print(
                f"[red]Automatic download failed:[/red] {e}\n"
                "[dim]Microsoft may be rate-limiting this network, or changed their API. "
                "Download manually from https://www.microsoft.com/software-download/"
                f"{entry['slug']} and provide the ISO path below.[/dim]"
            )
            return ask_iso_path()

    console.print("[dim]Provide the path to an existing ISO instead.[/dim]")
    return ask_iso_path()


# ── ISO ───────────────────────────────────────────────────────────────────────

def ask_iso_path() -> Path:
    while True:
        raw = Prompt.ask("\n[bold cyan]Path to ISO file[/bold cyan]").strip().strip("'\"")
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


# ── ISO probing (Windows vs Linux) ───────────────────────────────────────────

def _is_hybrid_iso(iso: Path) -> bool:
    """True if the ISO is an isohybrid image — i.e. the first sector is a valid
    MBR (0x55AA) that also carries boot code or partition entries. Such images
    boot from USB when written raw (dd), on both BIOS and UEFI."""
    with open(iso, "rb") as f:
        sector = f.read(512)
    if len(sector) < 512 or sector[510:512] != b"\x55\xaa":
        return False
    has_code = any(sector[:446])
    has_parts = any(sector[446:510])
    return has_code or has_parts


def probe_iso(iso: Path) -> dict:
    """Quietly mount the ISO and classify it. Returns
    {kind: windows|linux|unknown, hybrid, uefi, label, size}."""
    mount_point = mount_iso(iso)
    try:
        mp = Path(mount_point)
        is_windows = (mp / "bootmgr").exists() or (mp / "sources" / "install.wim").exists() \
            or (mp / "sources" / "install.esd").exists()
        has_uefi = any((mp / "EFI" / "BOOT" / n).exists() or (mp / "efi" / "boot" / n).exists()
                       for n in ("BOOTx64.EFI", "bootx64.efi", "BOOTX64.EFI"))
        linux_markers = ("casper", "live", "arch", "isolinux", "syslinux", "boot/grub",
                         "loader/entries", "LiveOS", "boot/syslinux")
        is_linux = not is_windows and any((mp / m).exists() for m in linux_markers)
        return {
            "kind": "windows" if is_windows else "linux" if is_linux else "unknown",
            "hybrid": _is_hybrid_iso(iso),
            "uefi": has_uefi,
            "label": mp.name,
            "size": iso.stat().st_size,
        }
    finally:
        unmount_iso(mount_point)


def ask_linux_options(info: dict, disk_size: int) -> dict:
    """Interactive Rufus-style settings for a Linux ISO."""
    console.print()
    t = Table(title="Linux ISO detected", box=None, padding=(0, 2))
    t.add_column(style="dim")
    t.add_column(style="bold")
    t.add_row("Volume label", info["label"])
    t.add_row("Size", fmt_size(info["size"]))
    t.add_row("Hybrid (raw-writable)", "[green]Yes[/green]" if info["hybrid"] else "[yellow]No[/yellow]")
    t.add_row("UEFI boot files", "[green]Yes[/green]" if info["uefi"] else "[yellow]Not found[/yellow]")
    console.print(t)

    opts = {"mode": "copy", "scheme": "MBR", "label": _fat_label(info["label"]), "verify": False}

    if info["hybrid"]:
        console.print(
            "\n[bold]Live bootable USB[/bold] — writes the ISO to the drive exactly as-is (\"DD mode\"). "
            "Boots on both BIOS and UEFI machines and works for live sessions and installers."
        )
        if Confirm.ask("Create a live bootable USB this way?", default=True):
            opts["mode"] = "dd"
        else:
            console.print("[dim]Falling back to file-copy mode (UEFI only).[/dim]")
    elif not info["uefi"]:
        raise RuntimeError(
            "This ISO isn't hybrid and has no UEFI boot files, so it can't be made bootable "
            "from macOS. Try a different ISO image."
        )
    else:
        console.print("\n[yellow]This ISO isn't a hybrid image[/yellow] — using file-copy mode (UEFI boot only).")

    if opts["mode"] == "dd":
        if disk_size and info["size"] > disk_size:
            raise RuntimeError(f"ISO ({fmt_size(info['size'])}) is larger than the drive ({fmt_size(disk_size)}).")
        opts["verify"] = Confirm.ask("Verify the drive after writing? (slower, catches bad sticks)", default=False)
    else:
        console.print("\n[bold]Advanced settings[/bold]")
        opts["scheme"] = Prompt.ask("Partition scheme", choices=["MBR", "GPT"], default="MBR")
        console.print("[dim]Some distros (e.g. Arch) locate their files by volume label — keep the default unless you know otherwise.[/dim]")
        opts["label"] = _fat_label(Prompt.ask("Volume label (max 11 chars)", default=opts["label"]))
    return opts


def _fat_label(label: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_ -]", "_", label).upper().strip()
    return (cleaned or "LINUXUSB")[:11]


# ── Linux flashing ────────────────────────────────────────────────────────────

_DD_CHUNK = 4 * 1024 * 1024  # multiple of 512 — required for raw device writes


def dd_write_iso(iso: Path, disk_node: str, progress_cb=None, verify: bool = False, verify_cb=None):
    """Raw-write an isohybrid ISO to the whole disk (like `dd bs=4m`).

    progress_cb(done_bytes, total_bytes); verify_cb(done_bytes, total_bytes).
    """
    raw = disk_node.replace("/dev/disk", "/dev/rdisk")
    total = iso.stat().st_size
    run(["diskutil", "unmountDisk", "force", disk_node], check=False)

    rich_progress = None
    if progress_cb is None:
        rich_progress = Progress(
            TextColumn("[cyan]{task.description}[/cyan]", table_column=Column(width=12, no_wrap=True)),
            BarColumn(), FileSizeColumn(), TransferSpeedColumn(), TimeRemainingColumn(), console=console)
        rich_progress.start()
        task = rich_progress.add_task("Writing", total=total)
        progress_cb = lambda done, _t: rich_progress.update(task, completed=done)

    try:
        done = 0
        with open(iso, "rb") as src, open(raw, "r+b", buffering=0) as dst:
            while True:
                chunk = src.read(_DD_CHUNK)
                if not chunk:
                    break
                if len(chunk) % 512:
                    chunk += b"\0" * (512 - len(chunk) % 512)
                dst.write(chunk)
                done += min(len(chunk), total - done)
                progress_cb(done, total)
            os.fsync(dst.fileno())
    finally:
        if rich_progress:
            rich_progress.stop()

    if verify:
        log.info("Verifying %s", disk_node)
        done = 0
        if rich_progress is not None:
            console.print("[dim]Verifying...[/dim]")
        with open(iso, "rb") as src, open(raw, "rb", buffering=0) as dst:
            while True:
                want = src.read(_DD_CHUNK)
                if not want:
                    break
                got = dst.read(len(want) + (-len(want) % 512))[:len(want)]
                if got != want:
                    raise RuntimeError(f"Verification failed near byte {done} — the drive may be faulty.")
                done += len(want)
                if verify_cb:
                    verify_cb(done, total)
        if rich_progress is not None:
            console.print("[green]✓ Verified[/green]")
    run(["diskutil", "unmountDisk", "force", disk_node], check=False)


def linux_copy_to_usb(iso_mount: str, disk_node: str, scheme: str, label: str, progress_cb=None):
    """File-copy mode for non-hybrid Linux ISOs: FAT32, UEFI boot only."""
    if scheme not in ("MBR", "GPT"):
        raise ValueError(f"bad partition scheme {scheme!r}")
    fmt = "MBRFormat" if scheme == "MBR" else "GPTFormat"
    run(["diskutil", "unmountDisk", disk_node], check=False)
    log.info("Formatting %s as FAT32 %s label=%s", disk_node, scheme, label)
    run(["diskutil", "eraseDisk", "FAT32", label, fmt, disk_node], capture=progress_cb is not None)

    part = disk_node + ("s1" if scheme == "MBR" else "s2")  # GPT: s1 is the EFI system partition
    run(["diskutil", "mount", part], check=False)
    mp = plistlib.loads(run(["diskutil", "info", "-plist", part]).stdout.encode()).get("MountPoint", "")
    if not mp:
        raise RuntimeError(f"No mount point for {part}")

    too_big = [p for p in Path(iso_mount).rglob("*") if p.is_file() and p.stat().st_size > FAT32_LIMIT]
    if too_big:
        raise RuntimeError(f"{too_big[0].name} is larger than 4 GiB and can't be stored on FAT32.")
    copy_files_except_wim(iso_mount, Path(mp), progress_cb=progress_cb)
    run(["diskutil", "unmountDisk", disk_node], check=False)


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
            "[dim]Normal for Windows 8.1+ ISOs — UEFI boot is unaffected; only Legacy BIOS boot may not work.[/dim]"
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


def copy_files_except_wim(src: str, dst: Path, progress_cb=None):
    """progress_cb(done_count, total_count, current_filename) is called after
    each file when given, instead of drawing the Rich progress bar."""
    src_path = Path(src)
    skip = {src_path / "sources" / "install.wim", src_path / "sources" / "install.esd"}
    files = [p for p in src_path.rglob("*") if p.is_file() and p not in skip]
    # Large files first — they take longest, so start them while small files fill in
    files.sort(key=lambda p: p.stat().st_size, reverse=True)

    # Pre-create all dirs before threads start — avoids mkdir contention
    for d in {(dst / f.relative_to(src_path)).parent for f in files}:
        d.mkdir(parents=True, exist_ok=True)

    if progress_cb is None:
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
    else:
        done = 0
        progress_cb(done, len(files), "")
        with ThreadPoolExecutor(max_workers=_COPY_WORKERS) as pool:
            futures = {
                pool.submit(_copy_one, f, dst / f.relative_to(src_path)): f
                for f in files
            }
            for future in as_completed(futures):
                src_file = futures[future]
                future.result()  # re-raise any copy error
                done += 1
                progress_cb(done, len(files), src_file.relative_to(src_path).name)


_WIM_PCT_RE = re.compile(r"\((\d+)%\)")
_WIM_PART_MB = 3800  # FAT32-safe part size
_WIM_ERR_UNSUPPORTED = 68  # WIMLIB_ERR_UNSUPPORTED — e.g. splitting a solid (LZMS) WIM


def _run_wimlib(cmd: list[str], progress_cb=None) -> tuple[int, str]:
    """Run a wimlib-imagex command, streaming its \\r-updated progress output.

    progress_cb(percent) is called whenever wimlib reports a new percentage.
    Returns (exit_code, tail_of_output).
    """
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    tail: list[str] = []
    buf = ""
    last_pct = -1

    def flush(line: str):
        nonlocal last_pct
        m = _WIM_PCT_RE.search(line)
        if m:
            pct = int(m.group(1))
            if pct != last_pct and progress_cb:
                progress_cb(pct)
            last_pct = pct
        elif line.strip():
            tail.append(line.strip())
            del tail[:-10]

    while True:
        ch = proc.stdout.read(1)
        if not ch:
            break
        if ch in "\r\n":
            flush(buf)
            buf = ""
        else:
            buf += ch
    flush(buf)
    proc.stdout.close()
    return proc.wait(), "\n".join(tail)


def wim_may_be_solid(wim_path: Path) -> bool:
    """True if the WIM uses LZMS compression (what solid WIMs use).

    Non-solid LZMS WIMs also match, so this is only a hint for pre-flight
    checks; the authoritative signal is `wimlib-imagex split` failing.
    """
    if not shutil.which("wimlib-imagex"):
        return False
    res = run(["wimlib-imagex", "info", str(wim_path)], check=False)
    return re.search(r"^Compression:\s*LZMS", res.stdout, re.M) is not None


def check_wim_export_space(wim_path: Path):
    """Fail early (before the USB is erased) if a solid WIM can't be re-exported."""
    if wim_path.stat().st_size <= FAT32_LIMIT or not wim_may_be_solid(wim_path):
        return
    needed = wim_path.stat().st_size
    free = shutil.disk_usage(tempfile.gettempdir()).free
    if free < needed:
        raise RuntimeError(
            f"This ISO's install.wim is compressed in a format that must be "
            f"re-compressed before it fits on FAT32, which needs about "
            f"{fmt_size(needed)} of free temporary space on this Mac, but only "
            f"{fmt_size(free)} is free. Free up disk space and try again."
        )


def split_and_copy_wim(wim_path: Path, dst: Path, progress_cb=None):
    """Split install.wim into <=3800 MB chunks (FAT32 safe) via wimlib.

    Windows 11 24H2+ ISOs ship a solid (LZMS) WIM which wimlib cannot split.
    In that case it is first exported to a non-solid WIM in a temp dir, then split.

    progress_cb(phase, percent) is called during long steps; when None, a Rich
    progress bar is drawn instead.
    """
    if not shutil.which("wimlib-imagex"):
        raise RuntimeError("wimlib-imagex missing. Run: brew install wimlib")
    out_dir = dst / "sources"
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "install.swm"

    if progress_cb is None:
        console.print("\n[yellow]install.wim > 4 GiB — splitting into .swm chunks...[/yellow]")
        progress = Progress(TextColumn("[cyan]{task.description}[/cyan]"),
                            BarColumn(), TextColumn("{task.percentage:>3.0f}%"),
                            console=console)
        task = progress.add_task("", total=100)

        def cb(phase, pct):
            progress.update(task, description=phase, completed=pct)

        with progress:
            _split_wim(wim_path, target, cb)
    else:
        _split_wim(wim_path, target, progress_cb)


def _split_wim(wim_path: Path, target: Path, cb):
    def cleanup_parts():
        for f in target.parent.glob("install*.swm"):
            f.unlink(missing_ok=True)

    cb("Splitting", 0)
    code, out = _run_wimlib(["wimlib-imagex", "split", str(wim_path), str(target), str(_WIM_PART_MB)],
                            lambda p: cb("Splitting", p))
    if code == 0:
        return
    if code != _WIM_ERR_UNSUPPORTED:
        cleanup_parts()
        raise RuntimeError(f"wimlib-imagex split failed (exit {code}):\n{out}")

    log.info("WIM is solid — exporting to non-solid before splitting")
    cleanup_parts()
    with tempfile.TemporaryDirectory(prefix="macos-rufus-") as tmp:
        flat = Path(tmp) / "install.wim"
        cb("Re-compressing (solid WIM)", 0)
        code, out = _run_wimlib(
            ["wimlib-imagex", "export", str(wim_path), "all", str(flat), "--compress=LZX"],
            lambda p: cb("Re-compressing (solid WIM)", p))
        if code != 0:
            raise RuntimeError(f"wimlib-imagex export failed (exit {code}):\n{out}")
        cb("Splitting", 0)
        code, out = _run_wimlib(["wimlib-imagex", "split", str(flat), str(target), str(_WIM_PART_MB)],
                                lambda p: cb("Splitting", p))
        if code != 0:
            cleanup_parts()
            raise RuntimeError(f"wimlib-imagex split failed (exit {code}):\n{out}")


def copy_wim_direct(wim_path: Path, dst: Path, progress_cb=None):
    """progress_cb(bytes_done, total_bytes) is called per chunk when given,
    instead of drawing the Rich progress bar."""
    dst_dir = dst / "sources"
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst_file = dst_dir / wim_path.name
    size = wim_path.stat().st_size
    chunk = 4 * 1024 * 1024  # 4 MB — good balance for USB sequential write

    if progress_cb is None:
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
            with open(wim_path, "rb") as fsrc, open(dst_file, "wb") as fdst:
                in_fd, out_fd, offset = fsrc.fileno(), fdst.fileno(), 0
                while offset < size:
                    sent = os.sendfile(out_fd, in_fd, offset, min(chunk, size - offset))
                    if sent == 0:
                        break
                    offset += sent
                    progress.advance(task, sent)
    else:
        progress_cb(0, size)
        with open(wim_path, "rb") as fsrc, open(dst_file, "wb") as fdst:
            in_fd, out_fd, offset = fsrc.fileno(), fdst.fileno(), 0
            while offset < size:
                sent = os.sendfile(out_fd, in_fd, offset, min(chunk, size - offset))
                if sent == 0:
                    break
                offset += sent
                progress_cb(offset, size)
    shutil.copystat(wim_path, dst_file)


# ── main ──────────────────────────────────────────────────────────────────────

def fmt_duration(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s}s" if m else f"{s}s"


def flash_linux_cli(iso_path: Path, selected: dict, probed: dict, opts: dict, log_path):
    disk_node = selected["node"]
    start_time = time.monotonic()
    mount_point = None
    try:
        if opts["mode"] == "dd":
            log.info("DD-writing %s -> %s", iso_path, disk_node)
            dd_write_iso(iso_path, disk_node, verify=opts["verify"])
            boot_note = "[green]Live USB — UEFI + Legacy BIOS[/green]"
        else:
            mount_point = mount_iso(iso_path)
            linux_copy_to_usb(mount_point, disk_node, opts["scheme"], opts["label"])
            boot_note = "[yellow]File-copy mode — UEFI boot only[/yellow]"
        elapsed = time.monotonic() - start_time

        summary = Table.grid(padding=(0, 2))
        summary.add_column(style="dim")
        summary.add_column(style="bold white")
        summary.add_row("ISO", iso_path.name)
        summary.add_row("Drive", f"{disk_node}  ({selected['name']}, {fmt_size(selected['size'])})")
        summary.add_row("Boot mode", boot_note)
        summary.add_row("Time taken", f"[bold cyan]{fmt_duration(elapsed)}[/bold cyan]")
        summary.add_row("Log saved", f"[dim]{log_path}[/dim]")
        console.print()
        console.print(Panel(
            Text.assemble(("  Done! ", "bold green"), ("Bootable Linux USB is ready.\n\n", "white")),
            style="bold green", subtitle="[dim]Safe to unplug[/dim]"))
        console.print(summary)
        log.info("Linux flash finished in %.1fs", elapsed)
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted — the drive is in an incomplete state.[/yellow]")
        sys.exit(1)
    except Exception as e:
        console.print(f"\n[bold red]Error:[/bold red] {e}")
        log.error("Linux flash failed: %s", e, exc_info=True)
        sys.exit(1)
    finally:
        if mount_point:
            unmount_iso(mount_point)


def main():
    console.print(Panel(
        "[bold white]macos-rufus[/bold white]  —  Windows & Linux bootable USB creator",
        subtitle="macOS · UEFI + Legacy BIOS · Win7/8/8.1/10/11 · Ubuntu/Arch/Kali/Pop!_OS/Omarchy",
        style="bold blue",
    ))

    escalate_to_root()
    log_path = setup_logger()
    log.info("macos-rufus started")
    check_deps()

    # 1. Pick OS + get ISO (download fresh, reuse a managed download, or supply a path)
    try:
        iso_path = ask_os_and_iso()
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")
        log.warning("Interrupted by user during OS/ISO selection")
        sys.exit(1)
    log.info("ISO: %s", iso_path)

    # 2. USB selection
    drives = list_usb_drives()
    if not drives:
        console.print("[red]No external USB drives detected.[/red]")
        sys.exit(1)
    selected = ask_usb(drives)
    disk_node = selected["node"]
    log.info("Target USB: %s (%s, %s)", disk_node, selected["name"], fmt_size(selected["size"]))

    # 2b. Windows or Linux? Linux gets its own settings + write pipeline.
    try:
        probed = probe_iso(iso_path)
        log.info("ISO probe: %s", probed)
        linux_opts = None
        if probed["kind"] == "linux":
            linux_opts = ask_linux_options(probed, selected["size"])
        elif probed["kind"] == "unknown":
            console.print("[yellow]Couldn't recognise this ISO as Windows or Linux — trying the Windows flow.[/yellow]")
    except KeyboardInterrupt:
        sys.exit(1)
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        log.error("ISO probe failed: %s", e, exc_info=True)
        sys.exit(1)

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

    if linux_opts:
        flash_linux_cli(iso_path, selected, probed, linux_opts, log_path)
        return

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

        # 4b. Pre-flight: make sure a solid WIM can be re-exported before we erase anything
        preflight_wim = get_wim_path(mount_point)
        if preflight_wim:
            check_wim_export_space(preflight_wim)

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
