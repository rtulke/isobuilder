#!/usr/bin/env python3
"""
iso_builder.py — Automated Debian-based ISO builder

Dependencies:
    pip install textual pyyaml
    apt install xorriso isolinux 7zip

Usage:
    python3 iso_builder.py                             # interactive TUI wizard
    python3 iso_builder.py --iso 1001 --profile 1 --output /tmp/out.iso
    python3 iso_builder.py --iso 1001 --profile 1 --output /tmp/out.iso --method file
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse, urljoin

import yaml
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button, DataTable, Footer, Header, Input,
    Label, Log, ProgressBar, Select, Static,
)

# ─── Paths & Constants ────────────────────────────────────────────────────────

BASE_DIR      = Path(__file__).parent
ISO_YAML      = BASE_DIR / "iso.yaml"
PROFILES_DIR  = BASE_DIR / "profiles"
TEMPLATES_DIR = BASE_DIR / "templates"
DOWNLOADS_DIR = BASE_DIR / "downloads"

# Architecture → installer directory + boot mode
ARCH_CONFIG: dict[str, dict] = {
    "amd64":     {"install_dir": "install.amd",       "efi_only": False},
    "i386":      {"install_dir": "install.386",       "efi_only": False},
    "hurd-i386": {"install_dir": "install.hurd-i386", "efi_only": False},
    "arm64":     {"install_dir": "install.a64",       "efi_only": True},
    "armhf":     {"install_dir": "install.armhf",     "efi_only": True},
    "ppc64el":   {"install_dir": "install.ppc64el",   "efi_only": True},
    "riscv64":   {"install_dir": "install.riscv64",   "efi_only": True},
    "s390x":     {"install_dir": "install.s390x",     "efi_only": True},
    "mips64el":  {"install_dir": "install.mips64el",  "efi_only": True},
}

# ─── Data Models ─────────────────────────────────────────────────────────────

@dataclass
class ISOSource:
    id: int
    name: str
    url: str
    arch: str
    sha256: str = ""

    def resolved_path(self) -> Optional[Path]:
        """Returns local Path for path:// or file:// URLs, else None."""
        for prefix in ("path://", "file://"):
            if self.url.startswith(prefix):
                p = Path(self.url[len(prefix):])
                return p if p.is_absolute() else BASE_DIR / p
        return None

    def cached_path(self) -> Path:
        filename = Path(urlparse(self.url).path).name or f"iso_{self.id}.iso"
        return DOWNLOADS_DIR / f"{self.id}_{filename}"

    def is_available(self) -> bool:
        local = self.resolved_path()
        if local:
            return local.exists()
        return self.cached_path().exists()

    def get_iso_path(self) -> Optional[Path]:
        local = self.resolved_path()
        if local and local.exists():
            return local
        cached = self.cached_path()
        return cached if cached.exists() else None


@dataclass
class Profile:
    id: int
    description: str
    preseed: str
    postinstall: str = ""
    source_file: Optional[Path] = field(default=None, repr=False)

    def preseed_path(self) -> Path:
        p = Path(self.preseed)
        return p if p.is_absolute() else BASE_DIR / p

    def postinstall_path(self) -> Optional[Path]:
        if not self.postinstall:
            return None
        p = Path(self.postinstall)
        return p if p.is_absolute() else BASE_DIR / p


# ─── Persistence ─────────────────────────────────────────────────────────────

def load_iso_sources() -> list[ISOSource]:
    if not ISO_YAML.exists():
        return []
    raw = yaml.safe_load(ISO_YAML.read_text()) or []
    return [
        ISOSource(id=e["id"], name=e["name"], url=e["url"], arch=e["arch"], sha256=e.get("sha256", ""))
        for e in raw
    ]


def save_iso_sources(sources: list[ISOSource]) -> None:
    data = []
    for s in sources:
        entry: dict = {"id": s.id, "name": s.name, "url": s.url, "arch": s.arch}
        if s.sha256:
            entry["sha256"] = s.sha256
        data.append(entry)
    ISO_YAML.write_text(
        yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False)
    )


def load_profiles() -> list[Profile]:
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    profiles = []
    for f in sorted(PROFILES_DIR.glob("*.yaml")):
        d = yaml.safe_load(f.read_text()) or {}
        profiles.append(Profile(
            id=d.get("id", 0),
            description=d.get("description", ""),
            preseed=d.get("preseed", ""),
            postinstall=d.get("postinstall", ""),
            source_file=f,
        ))
    return profiles


def save_profile(p: Profile) -> None:
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "id": p.id,
        "description": p.description,
        "preseed": p.preseed,
        "postinstall": p.postinstall,
    }
    path = p.source_file or (PROFILES_DIR / f"profile_{p.id}.yaml")
    path.write_text(yaml.dump(data, default_flow_style=False, allow_unicode=True))
    p.source_file = path


def delete_profile(p: Profile) -> None:
    if p.source_file and p.source_file.exists():
        p.source_file.unlink()


# ─── Dependency Check ─────────────────────────────────────────────────────────

@dataclass
class DepResult:
    name:        str
    description: str
    package:     str
    required:    bool
    found:       bool
    path:        str = ""


def check_dependencies() -> list[DepResult]:
    results: list[DepResult] = []

    tools = [
        ("7z",      "ISO entpacken",            "7zip",      True),
        ("xorriso", "ISO bauen",                "xorriso",   True),
        ("cpio",    "initrd patchen",            "cpio",      True),
        ("gzip",    "initrd (de)komprimieren",   "gzip",      True),
        ("md5sum",  "Prüfsummen berechnen",      "coreutils", True),
    ]
    for cmd, desc, pkg, required in tools:
        found_path = shutil.which(cmd) or ""
        results.append(DepResult(
            name=cmd, description=desc, package=pkg,
            required=required, found=bool(found_path), path=found_path,
        ))

    # isohdpfx.bin — only needed for amd64/i386 BIOS+UEFI hybrid ISOs
    isohdpfx = Path("/usr/lib/ISOLINUX/isohdpfx.bin")
    results.append(DepResult(
        name="isohdpfx.bin",
        description="BIOS-Boot (nur amd64/i386)",
        package="isolinux",
        required=False,
        found=isohdpfx.exists(),
        path=str(isohdpfx) if isohdpfx.exists() else "",
    ))

    return results


# ─── USB Devices ─────────────────────────────────────────────────────────────

@dataclass
class BlockDevice:
    name:      str    # kernel name, e.g. "sdb"
    size_gb:   float
    model:     str
    removable: bool

    @property
    def path(self) -> str:
        return f"/dev/{self.name}"


def list_block_devices() -> list[BlockDevice]:
    """Return removable block devices found in /sys/block/."""
    devices: list[BlockDevice] = []
    sys_block = Path("/sys/block")
    if not sys_block.exists():
        return devices

    skip_prefixes = ("loop", "ram", "dm", "zram", "sr", "nvme", "md")

    for dev_dir in sorted(sys_block.iterdir()):
        name = dev_dir.name
        if any(name.startswith(p) for p in skip_prefixes):
            continue

        removable_file = dev_dir / "removable"
        try:
            removable = removable_file.read_text().strip() == "1"
        except OSError:
            continue

        try:
            size_sectors = int((dev_dir / "size").read_text().strip())
            size_gb = size_sectors * 512 / (1024 ** 3)
        except (OSError, ValueError):
            size_gb = 0.0

        try:
            model = (dev_dir / "device" / "model").read_text().strip()
        except OSError:
            model = "—"

        devices.append(BlockDevice(name=name, size_gb=size_gb, model=model, removable=removable))

    return [d for d in devices if d.removable and d.size_gb > 0]


# ─── Download ────────────────────────────────────────────────────────────────

def download_iso(
    iso: ISOSource,
    log: Callable[[str], None],
    sources: Optional[list[ISOSource]] = None,
) -> Path:
    """Resolve or download ISO. Returns path to local file.

    If `sources` is provided, verifies the ISO checksum and stores it in iso.yaml.
    """
    local = iso.resolved_path()
    if local and local.exists():
        log(f"Using local file: {local}")
        if sources is not None:
            verify_and_store_checksum(iso, local, log, sources)
        return local

    cached = iso.cached_path()
    if cached.exists():
        log(f"Using cached: {cached}")
        if sources is not None:
            verify_and_store_checksum(iso, cached, log, sources)
        return cached

    scheme = urlparse(iso.url).scheme
    if scheme in ("sftp", "ssh"):
        raise NotImplementedError(
            f"{scheme}:// is not yet supported. "
            "Download manually and use path:// in iso.yaml."
        )

    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    log(f"Downloading {iso.url} ...")
    last_pct: list[int] = [-1]

    def hook(block: int, block_size: int, total: int) -> None:
        if total > 0:
            pct = min(100, block * block_size * 100 // total)
            if pct % 5 == 0 and pct != last_pct[0]:
                log(f"  {pct}%")
                last_pct[0] = pct

    urllib.request.urlretrieve(iso.url, cached, reporthook=hook)
    log(f"Download complete: {cached}")
    if sources is not None:
        verify_and_store_checksum(iso, cached, log, sources)
    return cached


# ─── Checksum Verification ────────────────────────────────────────────────────

def _compute_sha256(path: Path) -> str:
    """Compute SHA256 of a file in streaming chunks."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _fetch_remote_sha256(iso_url: str, iso_filename: str, log: Callable[[str], None]) -> Optional[str]:
    """Try to fetch SHA256SUMS from the same directory as the ISO URL.

    Tries several common filenames used by Debian, Ubuntu, Kali, etc.
    Returns the expected hex digest for iso_filename, or None if not found.
    """
    parsed = urlparse(iso_url)
    base_dir = parsed.scheme + "://" + parsed.netloc + "/".join(parsed.path.split("/")[:-1]) + "/"

    for checksums_name in ("SHA256SUMS", "sha256sum.txt", "SHA256SUMS.txt"):
        url = urljoin(base_dir, checksums_name)
        try:
            log(f"Fetching checksums: {url}")
            with urllib.request.urlopen(url, timeout=15) as resp:
                content = resp.read().decode("utf-8", errors="replace")
            for line in content.splitlines():
                parts = line.split()
                if len(parts) >= 2:
                    digest, fname = parts[0], parts[-1].lstrip("*").lstrip("./")
                    if fname == iso_filename or fname.endswith("/" + iso_filename):
                        return digest
            log(f"  '{iso_filename}' not listed in {checksums_name}")
        except Exception:
            pass  # try next filename

    return None


def verify_and_store_checksum(
    iso: ISOSource,
    iso_path: Path,
    log: Callable[[str], None],
    sources: list[ISOSource],
) -> None:
    """Verify ISO checksum and persist it to iso.yaml.

    - If iso.sha256 is already set: verifies locally only (fast).
    - If not set and URL is http/https: fetches SHA256SUMS from remote, verifies, stores.
    - If not set and URL is local (path/file): computes and stores without remote verification.
    - Raises RuntimeError on checksum mismatch.
    """
    log("Computing SHA256 checksum ...")
    actual = _compute_sha256(iso_path)

    if iso.sha256:
        # Known checksum — just compare
        if actual.lower() != iso.sha256.lower():
            raise RuntimeError(
                f"Checksum mismatch!\n"
                f"  Expected: {iso.sha256}\n"
                f"  Got:      {actual}"
            )
        log(f"✓ Checksum verified: {actual[:16]}…")
        return

    # No stored checksum yet
    scheme = urlparse(iso.url).scheme
    if scheme in ("http", "https", "ftp"):
        iso_filename = Path(urlparse(iso.url).path).name
        expected = _fetch_remote_sha256(iso.url, iso_filename, log)
        if expected:
            if actual.lower() != expected.lower():
                raise RuntimeError(
                    f"Checksum mismatch!\n"
                    f"  Expected: {expected}\n"
                    f"  Got:      {actual}"
                )
            log(f"✓ Checksum verified against remote SHA256SUMS: {actual[:16]}…")
        else:
            log(f"⚠ No remote SHA256SUMS found — storing computed checksum.")
    else:
        log(f"Local ISO — storing computed checksum.")

    # Persist to iso.yaml
    iso.sha256 = actual
    save_iso_sources(sources)
    log(f"  SHA256 saved to iso.yaml")


# ─── Build Engine ─────────────────────────────────────────────────────────────

class BuildEngine:

    def __init__(
        self,
        iso: ISOSource,
        profile: Profile,
        output: Path,
        method: str,
        log: Callable[[str], None],
    ) -> None:
        self.iso = iso
        self.profile = profile
        self.output = output
        self.method = method   # "initrd" | "file"
        self.log = log

    def run(self) -> None:
        iso_path = download_iso(self.iso, self.log)
        self._check_disk_space(iso_path)

        with tempfile.TemporaryDirectory(prefix="iso_build_") as tmp:
            work_dir = Path(tmp) / "iso"
            self._extract(iso_path, work_dir)

            if self.method == "initrd":
                self._patch_initrd(work_dir)
            else:
                self._copy_preseed_to_root(work_dir)
                self._patch_grub_cmdline(work_dir)

            self._recalculate_md5(work_dir)
            self._build_xorriso(work_dir)

        self._write_sha256()
        self.log(f"\n✓ Done: {self.output}")

    # ── private helpers ───────────────────────────────────────────────────────

    def _extract(self, iso_path: Path, work_dir: Path) -> None:
        self.log(f"Extracting {iso_path.name} ...")
        work_dir.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(
            ["7z", "x", f"-o{work_dir}", str(iso_path)],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            raise RuntimeError(f"7z failed:\n{r.stderr}")
        self.log("ISO extracted.")

    def _patch_initrd(self, work_dir: Path) -> None:
        arch_cfg = ARCH_CONFIG.get(self.iso.arch, ARCH_CONFIG["arm64"])
        install_dir = work_dir / arch_cfg["install_dir"]

        if not install_dir.exists():
            raise FileNotFoundError(
                f"Install directory not found: {arch_cfg['install_dir']}\n"
                f"Available: {[d.name for d in work_dir.iterdir() if d.is_dir()]}"
            )

        initrd_gz  = install_dir / "initrd.gz"
        initrd_raw = install_dir / "initrd"

        self.log(f"Setting write permissions on {arch_cfg['install_dir']}/ ...")
        subprocess.run(["chmod", "+w", "-R", str(install_dir)], check=True)

        self.log("Decompressing initrd ...")
        with gzip.open(initrd_gz, "rb") as f_in:
            initrd_raw.write_bytes(f_in.read())
        initrd_gz.unlink()

        with tempfile.TemporaryDirectory(prefix="cpio_") as cpio_tmp:
            cpio_dir = Path(cpio_tmp)
            shutil.copy2(self.profile.preseed_path(), cpio_dir / "preseed.cfg")

            files = ["preseed.cfg"]
            pi = self.profile.postinstall_path()
            if pi and pi.exists():
                shutil.copy2(pi, cpio_dir / "postinstall.sh")
                files.append("postinstall.sh")

            self.log(f"Injecting into initrd: {', '.join(files)}")
            r = subprocess.run(
                ["cpio", "-H", "newc", "-o", "-A", "-F", str(initrd_raw)],
                input="\n".join(files),
                capture_output=True, text=True,
                cwd=str(cpio_dir),
            )
            if r.returncode != 0:
                raise RuntimeError(f"cpio failed:\n{r.stderr}")

        self.log("Recompressing initrd ...")
        with open(initrd_raw, "rb") as f_in:
            with gzip.open(initrd_gz, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
        initrd_raw.unlink()
        self.log("initrd patched successfully.")

    def _copy_preseed_to_root(self, work_dir: Path) -> None:
        self.log("Copying preseed.cfg to ISO root ...")
        shutil.copy2(self.profile.preseed_path(), work_dir / "preseed.cfg")
        pi = self.profile.postinstall_path()
        if pi and pi.exists():
            shutil.copy2(pi, work_dir / "postinstall.sh")
            self.log("postinstall.sh copied to ISO root.")

    def _patch_grub_cmdline(self, work_dir: Path) -> None:
        param = "auto=true priority=critical preseed/file=/cdrom/preseed.cfg"
        self.log("Patching GRUB cmdlines ...")
        for cfg in work_dir.rglob("grub.cfg"):
            content = cfg.read_text()
            patched = re.sub(
                r"(^\s*linux\s+\S+\s+)(.*?)\s*(---)",
                lambda m: f"{m.group(1)}{m.group(2)} {param} {m.group(3)}",
                content,
                flags=re.MULTILINE,
            )
            if patched != content:
                cfg.write_text(patched)
                self.log(f"  → {cfg.relative_to(work_dir)}")
        self.log("GRUB cmdlines updated.")

    def _recalculate_md5(self, work_dir: Path) -> None:
        self.log("Recalculating MD5 checksums ...")
        r = subprocess.run(
            "find -follow -type f -print0 | xargs --null md5sum > md5sum.txt",
            shell=True, cwd=str(work_dir), capture_output=True, text=True,
        )
        if r.returncode != 0:
            raise RuntimeError(f"MD5 recalculation failed:\n{r.stderr}")
        self.log("MD5 checksums updated.")

    def _build_xorriso(self, work_dir: Path) -> None:
        arch_cfg = ARCH_CONFIG.get(self.iso.arch, ARCH_CONFIG["arm64"])
        efi_img   = work_dir / "boot" / "grub" / "efi.img"
        label     = self.iso.name[:32]

        cmd = ["xorriso", "-as", "mkisofs", "-r", "-V", label]

        if not arch_cfg["efi_only"]:
            # BIOS + UEFI hybrid (amd64 / i386)
            cmd += [
                "-isohybrid-mbr", "/usr/lib/ISOLINUX/isohdpfx.bin",
                "-c", "isolinux/boot.cat",
                "-b", "isolinux/isolinux.bin",
                "-no-emul-boot", "-boot-load-size", "4", "-boot-info-table",
                "-eltorito-alt-boot",
            ]

        if efi_img.exists():
            cmd += ["-e", "boot/grub/efi.img", "-no-emul-boot"]
            if arch_cfg["efi_only"]:
                cmd += [
                    "-J", "-joliet-long", "-cache-inodes",
                    "-append_partition", "2", "0xef", str(efi_img),
                    "-partition_cyl_align", "all",
                ]
            else:
                cmd += ["-isohybrid-gpt-basdat"]

        self.output.parent.mkdir(parents=True, exist_ok=True)
        cmd += ["-o", str(self.output), str(work_dir)]

        self.log("Running xorriso ...")
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"xorriso failed:\n{r.stderr}")
        self.log(f"ISO built: {self.output}")

    def _check_disk_space(self, iso_path: Path) -> None:
        iso_size  = iso_path.stat().st_size
        # Rough estimate: original ISO (kept) + extracted tree + rebuilt ISO
        needed_tmp = iso_size * 2
        needed_out = iso_size

        tmp_dir = Path(tempfile.gettempdir())
        self.output.parent.mkdir(parents=True, exist_ok=True)

        free_tmp = shutil.disk_usage(tmp_dir).free
        free_out = shutil.disk_usage(self.output.parent).free

        def fmt(b: int) -> str:
            return f"{b / (1024 ** 3):.1f} GB"

        if free_tmp < needed_tmp:
            raise RuntimeError(
                f"Nicht genug Platz in {tmp_dir} für die Extraktion.\n"
                f"  Benötigt: ~{fmt(needed_tmp)}  |  Verfügbar: {fmt(free_tmp)}"
            )
        if free_out < needed_out:
            raise RuntimeError(
                f"Nicht genug Platz in {self.output.parent} für das Output-ISO.\n"
                f"  Benötigt: ~{fmt(needed_out)}  |  Verfügbar: {fmt(free_out)}"
            )
        self.log(
            f"Disk space OK — tmp: {fmt(free_tmp)} free, output: {fmt(free_out)} free"
        )

    def _write_sha256(self) -> None:
        self.log("Calculating SHA256 checksum ...")
        digest = hashlib.sha256(self.output.read_bytes()).hexdigest()
        sha_file = self.output.with_suffix(".sha256")
        sha_file.write_text(f"{digest}  {self.output.name}\n")
        self.log(f"SHA256: {digest[:16]}…  →  {sha_file.name}")


# ─── TUI: Screens ─────────────────────────────────────────────────────────────

class ConfirmModal(ModalScreen[bool]):

    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Label(self.message, id="confirm-msg")
            with Horizontal(id="confirm-btns"):
                yield Button("Ja", variant="error", id="yes")
                yield Button("Nein", variant="primary", id="no")

    @on(Button.Pressed, "#yes")
    def on_yes(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#no")
    def on_no(self) -> None:
        self.dismiss(False)


class ISOEditScreen(Screen):
    """Add or edit an ISO source entry in iso.yaml."""

    BINDINGS = [Binding("escape", "action_cancel", "Abbrechen")]

    def __init__(self, source: Optional[ISOSource] = None, next_id: int = 1) -> None:
        super().__init__()
        self._source  = source
        self._next_id = next_id

    def compose(self) -> ComposeResult:
        s     = self._source
        title = "Neue ISO Quelle" if s is None else f"ISO bearbeiten — {s.name}"
        yield Header(show_clock=False)
        yield Label(title, classes="screen-title")
        with Vertical(id="edit-form"):
            yield Label("ID:")
            yield Input(
                value=str(s.id) if s else str(self._next_id),
                placeholder="z.B. 1001", id="f-id",
            )
            yield Label("Name:")
            yield Input(
                value=s.name if s else "",
                placeholder="z.B. Debian 13 arm64 NETINST", id="f-name",
            )
            yield Label("URL  (https://, ftp://, path://, file://):")
            yield Input(
                value=s.url if s else "",
                placeholder="https://cdimage.debian.org/...", id="f-url",
            )
            yield Label("Arch:")
            yield Select(
                options=[(a, a) for a in sorted(ARCH_CONFIG.keys())],
                value=s.arch if s else "amd64",
                id="f-arch",
            )
        with Horizontal(classes="btn-row"):
            yield Button("Speichern", variant="primary", id="save")
            yield Button("Abbrechen", id="cancel-btn")
        yield Footer()

    @on(Button.Pressed, "#save")
    def on_save(self) -> None:
        id_val = self.query_one("#f-id",   Input).value.strip()
        name   = self.query_one("#f-name", Input).value.strip()
        url    = self.query_one("#f-url",  Input).value.strip()
        arch   = str(self.query_one("#f-arch", Select).value)

        if not id_val.isdigit():
            self.notify("ID muss eine Zahl sein.", severity="error")
            return
        if not name:
            self.notify("Name darf nicht leer sein.", severity="error")
            return
        if not url:
            self.notify("URL darf nicht leer sein.", severity="error")
            return

        new_id  = int(id_val)
        sources = load_iso_sources()

        if self._source is None:
            if any(s.id == new_id for s in sources):
                self.notify(f"ID {new_id} ist bereits vergeben.", severity="error")
                return
            sources.append(ISOSource(id=new_id, name=name, url=url, arch=arch))
        else:
            for s in sources:
                if s.id == self._source.id:
                    s.id = new_id; s.name = name; s.url = url; s.arch = arch
                    break

        save_iso_sources(sources)
        self.notify("Gespeichert.")
        self.app.pop_screen()

    @on(Button.Pressed, "#cancel-btn")
    def action_cancel(self) -> None:
        self.app.pop_screen()


class ISOSelectScreen(Screen):

    BINDINGS = [Binding("escape", "action_back", "Zurück")]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Label("ISO Quellen", classes="screen-title")
        table = DataTable(id="iso-table", cursor_type="row")
        table.add_columns("ID", "Name", "Arch", "Verfügbar")
        yield table
        with Horizontal(classes="btn-row"):
            yield Button("Auswählen", variant="primary", id="select")
            yield Button("Neu",        id="iso-new")
            yield Button("Bearbeiten", id="iso-edit")
            yield Button("Löschen",    variant="error", id="iso-delete")
            yield Button("Zurück",     id="back-btn")
        yield Footer()

    def on_mount(self) -> None:
        self._refresh()

    def on_screen_resume(self) -> None:
        self._refresh()

    def _refresh(self) -> None:
        self._sources = load_iso_sources()
        self.app.iso_sources = self._sources  # type: ignore[attr-defined]
        table = self.query_one(DataTable)
        table.clear()
        for s in self._sources:
            avail = "✓ lokal" if s.is_available() else "—"
            table.add_row(str(s.id), s.name, s.arch, avail)

    @on(DataTable.RowSelected)
    def on_row_selected(self, event: DataTable.RowSelected) -> None:
        self.app.selected_iso = self._sources[event.cursor_row]  # type: ignore[attr-defined]
        self.app.pop_screen()

    @on(Button.Pressed, "#select")
    def on_select(self) -> None:
        table = self.query_one(DataTable)
        if table.row_count:
            self.app.selected_iso = self._sources[table.cursor_row]  # type: ignore[attr-defined]
            self.app.pop_screen()

    @on(Button.Pressed, "#iso-new")
    def on_new(self) -> None:
        next_id = max((s.id for s in self._sources), default=0) + 1
        self.app.push_screen(ISOEditScreen(next_id=next_id))

    @on(Button.Pressed, "#iso-edit")
    def on_edit(self) -> None:
        table = self.query_one(DataTable)
        if not table.row_count:
            return
        self.app.push_screen(ISOEditScreen(self._sources[table.cursor_row]))

    @on(Button.Pressed, "#iso-delete")
    def on_delete(self) -> None:
        table = self.query_one(DataTable)
        if not table.row_count:
            return
        s = self._sources[table.cursor_row]
        self.app.push_screen(
            ConfirmModal(f"'{s.name}' aus iso.yaml löschen?"),
            callback=lambda confirmed: self._do_delete(s, confirmed),
        )

    def _do_delete(self, source: ISOSource, confirmed: bool) -> None:
        if confirmed:
            save_iso_sources([s for s in load_iso_sources() if s.id != source.id])
            if self.app.selected_iso and self.app.selected_iso.id == source.id:  # type: ignore[attr-defined]
                self.app.selected_iso = None  # type: ignore[attr-defined]
            self.notify(f"Gelöscht: {source.name}")
            self._refresh()

    @on(Button.Pressed, "#back-btn")
    def action_back(self) -> None:
        self.app.pop_screen()


class ProfileEditScreen(Screen):

    BINDINGS = [Binding("escape", "action_cancel", "Abbrechen")]

    def __init__(self, profile: Optional[Profile] = None) -> None:
        super().__init__()
        self._profile = profile

    def compose(self) -> ComposeResult:
        p = self._profile
        title = "Neues Profil" if p is None else f"Profil bearbeiten — {p.description}"
        yield Header(show_clock=False)
        yield Label(title, classes="screen-title")
        with Vertical(id="edit-form"):
            yield Label("ID:")
            yield Input(value=str(p.id) if p else "", placeholder="z.B. 1001", id="f-id")
            yield Label("Beschreibung:")
            yield Input(value=p.description if p else "", placeholder="Kurzbeschreibung", id="f-desc")
            yield Label("Preseed-Pfad (relativ zu Projektroot oder absolut):")
            yield Input(value=p.preseed if p else "", placeholder="templates/preseed.cfg", id="f-preseed")
            yield Label("Postinstall-Skript (optional, leer lassen wenn nicht benötigt):")
            yield Input(value=p.postinstall if p else "", placeholder="templates/postinstall.sh", id="f-post")
        with Horizontal(classes="btn-row"):
            yield Button("Speichern", variant="primary", id="save")
            yield Button("Abbrechen", id="cancel-btn")
        yield Footer()

    @on(Button.Pressed, "#save")
    def on_save(self) -> None:
        id_val   = self.query_one("#f-id",      Input).value.strip()
        desc     = self.query_one("#f-desc",    Input).value.strip()
        preseed  = self.query_one("#f-preseed", Input).value.strip()
        post     = self.query_one("#f-post",    Input).value.strip()

        if not id_val.isdigit():
            self.notify("ID muss eine Zahl sein.", severity="error")
            return
        if not preseed:
            self.notify("Preseed-Pfad darf nicht leer sein.", severity="error")
            return

        if self._profile is None:
            p = Profile(id=int(id_val), description=desc, preseed=preseed, postinstall=post)
        else:
            self._profile.id          = int(id_val)
            self._profile.description = desc
            self._profile.preseed     = preseed
            self._profile.postinstall = post
            p = self._profile

        save_profile(p)
        self.notify(f"Gespeichert: {p.source_file.name if p.source_file else 'profil'}")  # type: ignore[union-attr]
        self.app.pop_screen()

    @on(Button.Pressed, "#cancel-btn")
    def action_cancel(self) -> None:
        self.app.pop_screen()


class ProfileListScreen(Screen):

    BINDINGS = [Binding("escape", "action_back", "Zurück")]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Label("Profile verwalten", classes="screen-title")
        table = DataTable(id="profile-table", cursor_type="row")
        table.add_columns("ID", "Beschreibung", "Preseed", "Postinstall")
        yield table
        with Horizontal(classes="btn-row"):
            yield Button("Auswählen", variant="primary", id="select")
            yield Button("Neu",       id="new")
            yield Button("Bearbeiten",id="edit")
            yield Button("Löschen",   variant="error", id="delete")
            yield Button("Zurück",    id="back-btn")
        yield Footer()

    def on_mount(self) -> None:
        self._refresh()

    def on_screen_resume(self) -> None:
        self._refresh()

    def _refresh(self) -> None:
        self._profiles = load_profiles()
        table = self.query_one(DataTable)
        table.clear()
        for p in self._profiles:
            table.add_row(
                str(p.id),
                p.description,
                p.preseed or "—",
                p.postinstall or "—",
            )

    @on(DataTable.RowSelected)
    def on_row_selected(self, event: DataTable.RowSelected) -> None:
        self.app.selected_profile = self._profiles[event.cursor_row]  # type: ignore[attr-defined]
        self.app.pop_screen()

    @on(Button.Pressed, "#select")
    def on_select(self) -> None:
        table = self.query_one(DataTable)
        if table.row_count:
            self.app.selected_profile = self._profiles[table.cursor_row]  # type: ignore[attr-defined]
            self.app.pop_screen()

    @on(Button.Pressed, "#new")
    def on_new(self) -> None:
        self.app.push_screen(ProfileEditScreen())

    @on(Button.Pressed, "#edit")
    def on_edit(self) -> None:
        table = self.query_one(DataTable)
        if not table.row_count:
            return
        self.app.push_screen(ProfileEditScreen(self._profiles[table.cursor_row]))

    @on(Button.Pressed, "#delete")
    def on_delete(self) -> None:
        table = self.query_one(DataTable)
        if not table.row_count:
            return
        p = self._profiles[table.cursor_row]
        self.app.push_screen(
            ConfirmModal(f"Profil '{p.description}' wirklich löschen?"),
            callback=lambda confirmed: self._do_delete(p, confirmed),
        )

    def _do_delete(self, p: Profile, confirmed: bool) -> None:
        if confirmed:
            delete_profile(p)
            self.notify("Profil gelöscht.")
            self._refresh()

    @on(Button.Pressed, "#back-btn")
    def action_back(self) -> None:
        self.app.pop_screen()


class BuildScreen(Screen):

    BINDINGS = [Binding("escape", "action_back", "Zurück")]

    def compose(self) -> ComposeResult:
        iso  = self.app.selected_iso      # type: ignore[attr-defined]
        prof = self.app.selected_profile  # type: ignore[attr-defined]

        yield Header(show_clock=False)
        yield Label("ISO bauen", classes="screen-title")
        with Vertical(id="build-config"):
            yield Static(f"ISO:    {iso.name if iso else '—'}")
            yield Static(f"Profil: {prof.description if prof else '—'}")
            yield Label("Build-Methode:")
            yield Select(
                options=[
                    ("Methode 1 — Preseed in initrd einbetten (nur Text-Installer)", "initrd"),
                    ("Methode 2 — preseed/file= via GRUB-Cmdline",                  "file"),
                ],
                value="initrd",
                id="method-select",
            )
            yield Label("Ausgabe-ISO (Pfad):")
            yield Input(
                value=str(BASE_DIR / "output" / "custom.iso"),
                id="output-path",
            )
        with Horizontal(classes="btn-row"):
            yield Button("Build starten", variant="success", id="build-btn")
            yield Button("Zurück", id="back-btn")
        yield Log(id="build-log", auto_scroll=True)
        yield Footer()

    @on(Button.Pressed, "#build-btn")
    def on_build(self) -> None:
        iso  = self.app.selected_iso      # type: ignore[attr-defined]
        prof = self.app.selected_profile  # type: ignore[attr-defined]

        if not iso:
            self.notify("Kein ISO ausgewählt.", severity="error")
            return
        if not prof:
            self.notify("Kein Profil ausgewählt.", severity="error")
            return
        if not prof.preseed_path().exists():
            self.notify(f"Preseed nicht gefunden: {prof.preseed_path()}", severity="error")
            return

        method = str(self.query_one("#method-select", Select).value)
        output = Path(self.query_one("#output-path", Input).value.strip())

        self.query_one("#build-btn", Button).disabled = True
        self.query_one("#back-btn",  Button).disabled = True
        self._run_build(iso, prof, output, method)

    @work(thread=True, exclusive=True)
    def _run_build(
        self, iso: ISOSource, prof: Profile, output: Path, method: str
    ) -> None:
        log_widget = self.query_one("#build-log", Log)

        def log(msg: str) -> None:
            self.app.call_from_thread(log_widget.write_line, msg)

        try:
            engine = BuildEngine(
                iso=iso, profile=prof, output=output, method=method, log=log
            )
            engine.run()
        except Exception as exc:
            log(f"\n✗ FEHLER: {exc}")
        finally:
            self.app.call_from_thread(self._enable_buttons)

    def _enable_buttons(self) -> None:
        self.query_one("#build-btn", Button).disabled = False
        self.query_one("#back-btn",  Button).disabled = False

    @on(Button.Pressed, "#back-btn")
    def action_back(self) -> None:
        self.app.pop_screen()


class DependencyScreen(Screen):
    """Startup screen — checks that all required system tools are present."""

    BINDINGS = [Binding("escape", "action_exit_app", "Beenden")]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Label("Abhängigkeiten prüfen", classes="screen-title")
        table = DataTable(id="dep-table", cursor_type="none")
        table.add_columns("Tool", "Status", "Paket (apt)", "Verwendung")
        yield table
        yield Static("", id="dep-summary")
        with Horizontal(classes="btn-row"):
            yield Button("Weiter",  variant="primary", id="dep-continue")
            yield Button("Beenden", variant="error",   id="dep-exit")
        yield Footer()

    def on_mount(self) -> None:
        results = check_dependencies()
        table   = self.query_one(DataTable)

        missing_required: list[str] = []
        missing_optional: list[str] = []

        for r in results:
            if r.found:
                status = Text("✓ OK", style="bold green")
            elif r.required:
                status = Text("✗ Fehlt", style="bold red")
                missing_required.append(r.package)
            else:
                status = Text("⚠ Fehlt", style="bold yellow")
                missing_optional.append(r.package)
            table.add_row(r.name, status, r.package, r.description)

        lines: list[str] = []
        if missing_required:
            pkgs = " ".join(sorted(set(missing_required)))
            lines.append(f"[bold red]✗ Fehlende Pflicht-Pakete:[/]  apt install {pkgs}")
        if missing_optional:
            pkgs = " ".join(sorted(set(missing_optional)))
            lines.append(f"[bold yellow]⚠ Fehlende optionale Pakete:[/]  apt install {pkgs}")
        if not missing_required and not missing_optional:
            lines.append("[bold green]✓ Alle Abhängigkeiten erfüllt.[/]")

        self.query_one("#dep-summary", Static).update("\n".join(lines))

        if missing_required:
            self.query_one("#dep-continue", Button).disabled = True

    @on(Button.Pressed, "#dep-continue")
    def on_continue(self) -> None:
        self.app.pop_screen()

    @on(Button.Pressed, "#dep-exit")
    def action_exit_app(self) -> None:
        self.app.exit()


class DownloadScreen(Screen):
    """Pre-download ISO files to the local downloads/ cache."""

    BINDINGS = [Binding("escape", "action_back", "Zurück")]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Label("ISO herunterladen", classes="screen-title")
        table = DataTable(id="dl-table", cursor_type="row")
        table.add_columns("ID", "Name", "Arch", "Status")
        yield table
        with Horizontal(classes="btn-row"):
            yield Button("Herunterladen", variant="primary", id="dl-btn")
            yield Button("Cache löschen", variant="error",   id="del-btn")
            yield Button("Zurück",                           id="back-btn")
        yield ProgressBar(total=100, show_eta=False, id="dl-progress")
        yield Log(id="dl-log", auto_scroll=True)
        yield Footer()

    def on_mount(self) -> None:
        self._sources: list[ISOSource] = self.app.iso_sources  # type: ignore[attr-defined]
        self._refresh_table()

    def _refresh_table(self) -> None:
        table = self.query_one(DataTable)
        table.clear()
        for s in self._sources:
            if not s.is_available():
                status = Text("—", style="dim")
            elif s.resolved_path():
                status = Text("✓ lokal", style="bold green")
            else:
                status = Text("✓ gecacht", style="bold green")
            table.add_row(str(s.id), s.name, s.arch, status)

    @on(Button.Pressed, "#dl-btn")
    def on_download(self) -> None:
        table = self.query_one(DataTable)
        if not table.row_count:
            return
        iso = self._sources[table.cursor_row]

        if iso.resolved_path():
            self.notify("Lokale Datei — kein Download nötig.", severity="information")
            return

        if iso.cached_path().exists():
            self.app.push_screen(
                ConfirmModal(f"'{iso.name}' ist bereits gecacht.\nErneut herunterladen?"),
                callback=lambda confirmed: self._start_download(iso) if confirmed else None,
            )
            return

        self._start_download(iso)

    def _start_download(self, iso: ISOSource) -> None:
        self.query_one("#dl-btn", Button).disabled = True
        self.query_one("#del-btn", Button).disabled = True
        self.query_one(ProgressBar).update(progress=0)
        self._download_worker(iso)

    @work(thread=True, exclusive=True)
    def _download_worker(self, iso: ISOSource) -> None:
        log_widget = self.query_one("#dl-log", Log)
        dest = iso.cached_path()

        def log(msg: str) -> None:
            self.app.call_from_thread(log_widget.write_line, msg)

        last_pct: list[int] = [-1]

        def hook(block: int, block_size: int, total: int) -> None:
            if total > 0:
                pct = min(100, block * block_size * 100 // total)
                if pct != last_pct[0]:
                    last_pct[0] = pct
                    self.app.call_from_thread(self._update_progress, pct)
                    if pct % 10 == 0:
                        log(f"  {pct}%")

        try:
            scheme = urlparse(iso.url).scheme
            if scheme in ("sftp", "ssh"):
                raise NotImplementedError(
                    f"{scheme}:// nicht unterstützt. "
                    "Bitte manuell herunterladen und path:// in iso.yaml verwenden."
                )
            DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
            log(f"URL:  {iso.url}")
            log(f"Ziel: {dest}")
            urllib.request.urlretrieve(iso.url, dest, reporthook=hook)
            size_mb = dest.stat().st_size // (1024 * 1024)
            log(f"\n✓ Fertig: {dest.name}  ({size_mb} MB)")
            self.app.call_from_thread(self._refresh_table)
        except Exception as exc:
            log(f"\n✗ FEHLER: {exc}")
            if dest.exists():
                dest.unlink()
        finally:
            self.app.call_from_thread(self._enable_buttons)

    def _update_progress(self, pct: int) -> None:
        self.query_one(ProgressBar).update(progress=pct)

    def _enable_buttons(self) -> None:
        self.query_one("#dl-btn", Button).disabled = False
        self.query_one("#del-btn", Button).disabled = False

    @on(Button.Pressed, "#del-btn")
    def on_delete_cache(self) -> None:
        table = self.query_one(DataTable)
        if not table.row_count:
            return
        iso = self._sources[table.cursor_row]
        cached = iso.cached_path()
        if not cached.exists():
            self.notify("Kein Download-Cache vorhanden.", severity="warning")
            return
        self.app.push_screen(
            ConfirmModal(f"Cache für '{iso.name}' löschen?"),
            callback=lambda confirmed: self._do_delete(iso, confirmed),
        )

    def _do_delete(self, iso: ISOSource, confirmed: bool) -> None:
        if confirmed:
            cached = iso.cached_path()
            if cached.exists():
                cached.unlink()
                self.notify(f"Cache gelöscht: {cached.name}")
                self._refresh_table()

    @on(Button.Pressed, "#back-btn")
    def action_back(self) -> None:
        self.app.pop_screen()


class USBWriteScreen(Screen):
    """Write a built ISO to a USB stick using dd."""

    BINDINGS = [Binding("escape", "action_back", "Zurück")]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Label("Auf USB schreiben", classes="screen-title")
        yield Static(
            "[bold red]⚠  ACHTUNG: Das gewählte Gerät wird vollständig und "
            "unwiderruflich überschrieben![/]",
            id="usb-warning",
        )
        with Vertical(id="usb-config"):
            yield Label("ISO-Datei (Pfad):")
            yield Input(
                value=str(BASE_DIR / "output" / "custom.iso"),
                id="usb-iso-path",
            )
        yield Label("Verfügbare Wechselmedien:", classes="section-label")
        table = DataTable(id="usb-table", cursor_type="row")
        table.add_columns("Gerät", "Grösse", "Modell")
        yield table
        with Horizontal(id="usb-table-btns"):
            yield Button("Aktualisieren", id="refresh-btn")
        with Vertical(id="usb-confirm-box"):
            yield Label(
                "Zur Bestätigung Gerätepfad eintippen (z.B. /dev/sdb):",
                classes="section-label",
            )
            yield Input(placeholder="/dev/sdX", id="confirm-dev")
        with Horizontal(classes="btn-row"):
            yield Button("Schreiben", variant="error", id="write-btn", disabled=True)
            yield Button("Zurück",                     id="back-btn")
        yield ProgressBar(total=100, show_eta=False, id="usb-progress")
        yield Log(id="usb-log", auto_scroll=True)
        yield Footer()

    def on_mount(self) -> None:
        self._devices: list[BlockDevice] = []
        self._selected_device: str = ""
        self._refresh_devices()

    def _refresh_devices(self) -> None:
        self._devices = list_block_devices()
        table = self.query_one(DataTable)
        table.clear()
        for d in self._devices:
            table.add_row(d.path, f"{d.size_gb:.1f} GB", d.model)
        if not self._devices:
            self.query_one("#usb-log", Log).write_line(
                "Keine Wechselmedien gefunden. USB-Stick einstecken und Aktualisieren drücken."
            )

    # ── selection & validation ────────────────────────────────────────────────

    @on(DataTable.RowSelected)
    def on_row_selected(self, event: DataTable.RowSelected) -> None:
        self._selected_device = self._devices[event.cursor_row].path
        self._validate()

    @on(Input.Changed, "#confirm-dev")
    def on_confirm_changed(self) -> None:
        self._validate()

    @on(Input.Changed, "#usb-iso-path")
    def on_iso_path_changed(self) -> None:
        self._validate()

    def _validate(self) -> None:
        confirm  = self.query_one("#confirm-dev",  Input).value.strip()
        iso_path = Path(self.query_one("#usb-iso-path", Input).value.strip())
        enabled  = (
            bool(self._selected_device)
            and confirm == self._selected_device
            and iso_path.is_file()
        )
        self.query_one("#write-btn", Button).disabled = not enabled

    # ── actions ───────────────────────────────────────────────────────────────

    @on(Button.Pressed, "#refresh-btn")
    def on_refresh(self) -> None:
        self._refresh_devices()
        self.notify("Geräteliste aktualisiert.")

    @on(Button.Pressed, "#write-btn")
    def on_write(self) -> None:
        iso_path = Path(self.query_one("#usb-iso-path", Input).value.strip())
        device   = self._selected_device
        size_gb  = iso_path.stat().st_size / (1024 ** 3)
        self.app.push_screen(
            ConfirmModal(
                f"ISO:   {iso_path.name}  ({size_gb:.1f} GB)\n"
                f"Gerät: {device}\n\n"
                f"Alle Daten auf {device} gehen unwiderruflich verloren!"
            ),
            callback=lambda ok: self._start_write(iso_path, device) if ok else None,
        )

    def _start_write(self, iso_path: Path, device: str) -> None:
        if os.geteuid() != 0:
            self.query_one("#usb-log", Log).write_line(
                "✗ Root-Rechte erforderlich — bitte mit 'sudo python3 iso_builder.py' starten."
            )
            return
        self.query_one("#write-btn", Button).disabled = True
        self.query_one("#back-btn",  Button).disabled = True
        self.query_one(ProgressBar).update(progress=0)
        self._write_worker(iso_path, device)

    @work(thread=True, exclusive=True)
    def _write_worker(self, iso_path: Path, device: str) -> None:
        log_widget = self.query_one("#usb-log", Log)
        iso_size   = iso_path.stat().st_size

        def log(msg: str) -> None:
            self.app.call_from_thread(log_widget.write_line, msg)

        try:
            cmd = [
                "dd",
                f"if={iso_path}",
                f"of={device}",
                "bs=4M",
                "conv=fdatasync",
                "status=progress",
            ]
            log(f"Schreibe: {iso_path.name} → {device}")
            log(f"Befehl:   {' '.join(cmd)}")

            process = subprocess.Popen(
                cmd,
                stderr=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                text=True,
            )

            line_buf   = ""
            last_pct: list[int] = [-1]

            for char in iter(lambda: process.stderr.read(1), ""):  # type: ignore[arg-type]
                if char in ("\r", "\n"):
                    line = line_buf.strip()
                    line_buf = ""
                    if not line:
                        continue
                    m = re.match(r"(\d+) bytes", line)
                    if m and iso_size > 0:
                        pct = min(100, int(m.group(1)) * 100 // iso_size)
                        if pct != last_pct[0]:
                            last_pct[0] = pct
                            self.app.call_from_thread(self._update_usb_progress, pct)
                            if pct % 10 == 0:
                                log(f"  {pct}%  — {line}")
                    else:
                        log(line)
                else:
                    line_buf += char

            process.wait()
            if process.returncode != 0:
                raise RuntimeError(f"dd fehlgeschlagen (exit {process.returncode})")
            log(f"\n✓ Fertig — {iso_path.name} erfolgreich auf {device} geschrieben.")
        except Exception as exc:
            log(f"\n✗ FEHLER: {exc}")
        finally:
            self.app.call_from_thread(self._enable_buttons)

    def _update_usb_progress(self, pct: int) -> None:
        self.query_one(ProgressBar).update(progress=pct)

    def _enable_buttons(self) -> None:
        self.query_one("#write-btn", Button).disabled = False
        self.query_one("#back-btn",  Button).disabled = False

    @on(Button.Pressed, "#back-btn")
    def action_back(self) -> None:
        self.app.pop_screen()


class MainScreen(Screen):

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Label("Debian ISO Builder", id="main-title")
        with Vertical(id="selection-panel"):
            yield Label("Aktuelle Auswahl", classes="panel-title")
            with Horizontal(classes="sel-row"):
                yield Static("ISO:",    classes="sel-label")
                yield Static("— (nicht gewählt)", id="iso-display", classes="sel-value")
                yield Button("Wählen", id="pick-iso", classes="sel-btn")
            with Horizontal(classes="sel-row"):
                yield Static("Profil:", classes="sel-label")
                yield Static("— (nicht gewählt)", id="profile-display", classes="sel-value")
                yield Button("Wählen", id="pick-profile", classes="sel-btn")
        with Horizontal(id="action-row"):
            yield Button("Build starten",     variant="success", id="build-btn")
            yield Button("ISO herunterladen",                    id="downloads-btn")
            yield Button("USB schreiben",     variant="warning", id="usb-btn")
            yield Button("Profile verwalten",                    id="profiles-btn")
        yield Footer()

    def on_mount(self) -> None:
        self._update_display()

    def on_screen_resume(self) -> None:
        self._update_display()

    def _update_display(self) -> None:
        iso  = self.app.selected_iso      # type: ignore[attr-defined]
        prof = self.app.selected_profile  # type: ignore[attr-defined]
        self.query_one("#iso-display",     Static).update(iso.name  if iso  else "— (nicht gewählt)")
        self.query_one("#profile-display", Static).update(prof.description if prof else "— (nicht gewählt)")

    @on(Button.Pressed, "#pick-iso")
    def on_pick_iso(self) -> None:
        self.app.push_screen(ISOSelectScreen())

    @on(Button.Pressed, "#pick-profile")
    def on_pick_profile(self) -> None:
        self.app.push_screen(ProfileListScreen())

    @on(Button.Pressed, "#downloads-btn")
    def on_downloads(self) -> None:
        self.app.push_screen(DownloadScreen())

    @on(Button.Pressed, "#usb-btn")
    def on_usb(self) -> None:
        self.app.push_screen(USBWriteScreen())

    @on(Button.Pressed, "#profiles-btn")
    def on_profiles(self) -> None:
        self.app.push_screen(ProfileListScreen())

    @on(Button.Pressed, "#build-btn")
    def on_build(self) -> None:
        if not self.app.selected_iso or not self.app.selected_profile:  # type: ignore[attr-defined]
            self.notify("ISO und Profil müssen ausgewählt sein.", severity="warning")
            return
        self.app.push_screen(BuildScreen())


# ─── App ─────────────────────────────────────────────────────────────────────

class ISOBuilderApp(App):

    TITLE   = "Debian ISO Builder"
    BINDINGS = [Binding("ctrl+q", "quit", "Beenden")]

    CSS = """
    Screen { background: $surface; }

    #main-title {
        text-align: center;
        text-style: bold;
        padding: 1 2;
        color: $accent;
        height: auto;
    }

    .screen-title {
        text-style: bold;
        padding: 1 2;
        color: $accent;
        height: auto;
    }

    #selection-panel {
        border: round $primary;
        margin: 1 2;
        padding: 1 2;
        height: auto;
    }

    .panel-title {
        text-style: bold underline;
        margin-bottom: 1;
        height: auto;
    }

    .sel-row {
        height: 3;
        align: left middle;
    }

    .sel-label { width: 10; text-style: bold; }
    .sel-value { width: 1fr; }
    .sel-btn   { width: 12; }

    #action-row {
        margin: 1 2;
        height: auto;
    }

    #action-row Button { margin-right: 2; }

    DataTable {
        height: 1fr;
        margin: 0 2;
    }

    .btn-row {
        height: auto;
        margin: 1 2;
    }

    .btn-row Button { margin-right: 1; }

    #edit-form {
        margin: 0 2;
        height: auto;
    }

    #edit-form Label {
        margin-top: 1;
        text-style: bold;
        height: auto;
    }

    #build-config {
        margin: 0 2;
        height: auto;
    }

    #build-config Label {
        margin-top: 1;
        text-style: bold;
        height: auto;
    }

    #build-config Static { height: auto; margin-bottom: 0; }

    #build-log {
        height: 1fr;
        margin: 1 2;
        border: round $primary;
    }

    #usb-warning {
        margin: 1 2;
        padding: 1 2;
        height: auto;
        border: round $error;
    }

    #usb-config {
        margin: 0 2;
        height: auto;
    }

    #usb-config Label { margin-top: 1; text-style: bold; height: auto; }

    .section-label {
        margin: 1 2 0 2;
        text-style: bold;
        height: auto;
    }

    #usb-table {
        height: auto;
        max-height: 8;
        margin: 0 2;
    }

    #usb-table-btns {
        height: auto;
        margin: 0 2 1 2;
    }

    #usb-confirm-box {
        margin: 0 2;
        height: auto;
        border: round $warning;
        padding: 1 2;
    }

    #usb-confirm-box Label { height: auto; margin-bottom: 1; }

    #usb-progress {
        margin: 1 2 0 2;
        height: auto;
    }

    #usb-log {
        height: 1fr;
        margin: 0 2 1 2;
        border: round $primary;
    }

    #dl-progress {
        margin: 1 2 0 2;
        height: auto;
    }

    #dl-log {
        height: 1fr;
        margin: 0 2 1 2;
        border: round $primary;
    }

    #dep-table {
        height: auto;
        max-height: 12;
        margin: 0 2;
    }

    #dep-summary {
        margin: 1 2;
        padding: 1 2;
        height: auto;
        border: round $primary;
    }

    #confirm-box {
        width: 60;
        height: auto;
        border: round $error;
        padding: 2 3;
        background: $surface;
        align: center middle;
    }

    #confirm-msg  { text-align: center; margin-bottom: 2; height: auto; }
    #confirm-btns { align: center middle; height: auto; }
    #confirm-btns Button { margin: 0 1; }
    """

    def __init__(self) -> None:
        super().__init__()
        self.selected_iso:     Optional[ISOSource] = None
        self.selected_profile: Optional[Profile]   = None
        self.iso_sources:      list[ISOSource]     = []

    def on_mount(self) -> None:
        self.iso_sources = load_iso_sources()
        self.push_screen(MainScreen())
        self.push_screen(DependencyScreen())


# ─── CLI ─────────────────────────────────────────────────────────────────────

def cli_build(args: argparse.Namespace) -> None:
    sources  = load_iso_sources()
    profiles = load_profiles()

    iso = next((s for s in sources if s.id == args.iso), None)
    if not iso:
        print(f"Fehler: ISO-ID {args.iso} nicht in iso.yaml gefunden.", file=sys.stderr)
        sys.exit(1)

    profile = next((p for p in profiles if p.id == args.profile), None)
    if not profile:
        print(f"Fehler: Profil-ID {args.profile} nicht in profiles/ gefunden.", file=sys.stderr)
        sys.exit(1)

    output = Path(args.output)
    print(f"ISO:     {iso.name}")
    print(f"Profil:  {profile.description}")
    print(f"Methode: {args.method}")
    print(f"Ausgabe: {output}")
    print("─" * 60)

    engine = BuildEngine(
        iso=iso, profile=profile, output=output,
        method=args.method, log=print,
    )
    engine.run()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Debian ISO Builder — Custom ISOs mit eingebettetem Preseed",
    )
    parser.add_argument("--iso",     type=int, metavar="ID",   help="ISO-ID aus iso.yaml")
    parser.add_argument("--profile", type=int, metavar="ID",   help="Profil-ID aus profiles/")
    parser.add_argument("--output",  metavar="PFAD",           help="Ausgabepfad für das ISO")
    parser.add_argument(
        "--method",
        choices=["initrd", "file"],
        default="initrd",
        help="initrd = in initrd einbetten (Standard) | file = preseed/file= via GRUB",
    )
    args = parser.parse_args()

    if args.iso and args.profile and args.output:
        cli_build(args)
    else:
        ISOBuilderApp().run()


if __name__ == "__main__":
    main()
