#!/usr/bin/env python3
"""Automate the retail MS-DOS 6.22 floppy-disk Setup program."""
import argparse
import hashlib
import os
import shutil
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from cpu_backend import CPUBackendError
from dosharness import DOSHarness, REPO_ROOT
from fat12 import FAT12Error, FAT16
from main import create_hard_disk_image
from video import Disk, DiskView

DEFAULT_CYLINDERS = 615
HEADS, SECTORS_PER_TRACK, PARTITION_START = 4, 17, 17
WELCOME_MARKER = "F7=Install to a Floppy Disk"
REPLACE_MARKER = "Continue Setup and replace your current version of DOS."
RESTART_MARKER = "Setup will restart your computer now"
REMOVE_DISKS_MARKER = "Remove disks from all floppy disk drives"
EXPECTED_MEDIA_SHA256 = {
    "Disk1.img": "b88030401122d234ea6aafba3cfed7de2b7b1782700a67be5498edca6f9fec5d",
    "Disk2.img": "e1d48a415495a17d65316d5328a91d7df0910fb1e42b0b07e7dbf8a4b4df305a",
    "Disk3.img": "52a3b4e7f5973c38f2517dbb20426bf5c8bd62f202e84a85d3d7283401d5e63c",
}


class InstallError(RuntimeError):
    pass


@dataclass(frozen=True)
class InstallMedia:
    disk1: Path
    disk2: Path
    disk3: Path

    @property
    def paths(self):
        return (self.disk1, self.disk2, self.disk3)


@dataclass(frozen=True)
class SetupAction:
    name: str
    marker: str
    disk_number: int = 0


def file_hashes(paths):
    result = {}
    for path in paths:
        digest = hashlib.sha256()
        with open(path, "rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        result[str(path)] = digest.hexdigest()
    return result


def resolve_media(media_dir=None):
    root = Path(media_dir or Path(REPO_ROOT) / "DOS6_22").resolve()
    paths = tuple(root / f"Disk{i}.img" for i in range(1, 4))
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise InstallError("retail MS-DOS 6.22 media is incomplete; missing: "
                           + ", ".join(missing))
    for path in paths:
        if path.stat().st_size != 1_474_560:
            raise InstallError(f"expected a 1.44 MB image: {path}")
    hashes = file_hashes(paths)
    bad = [path.name for path in paths
           if hashes[str(path)] != EXPECTED_MEDIA_SHA256[path.name]]
    if bad:
        raise InstallError("media does not match the supported retail "
                           "MS-DOS 6.22 set: " + ", ".join(bad))
    return InstallMedia(*paths)


def choose_backend(requested="auto"):
    if requested != "auto":
        return requested
    try:
        import c_cpu_native  # noqa: F401
    except ImportError:
        return "python"
    return "c"


def classify_screen(screen):
    for marker in (RESTART_MARKER, REMOVE_DISKS_MARKER):
        if marker in screen:
            return SetupAction("restart", marker)
    if REPLACE_MARKER in screen:
        return SetupAction("replace", REPLACE_MARKER)
    for number in (2, 3):
        for marker in (f"Setup Disk {number}", f"Setup Disk #{number}"):
            if marker in screen and "drive A" in screen:
                return SetupAction("swap", marker, number)
    for marker in ("Setup is complete", "MS-DOS 6.22 is now installed"):
        if marker in screen:
            return SetupAction("complete", marker)
    if "Setup will place your MS-DOS files" in screen and "C:\\DOS" in screen:
        return SetupAction("enter", "Setup will place your MS-DOS files")
    marker = "Configure unallocated disk space (recommended)."
    if marker in screen:
        return SetupAction("enter", marker)
    if ("The following settings will be used" in screen or
            "Setup will use the following settings" in screen or
            "Setup will use the following system settings" in screen):
        return SetupAction("enter", "Setup will use")
    if WELCOME_MARKER in screen:
        return SetupAction("enter", WELCOME_MARKER)
    if "does not have a hard disk" in screen:
        return SetupAction("error", "does not have a hard disk")
    if "Setup cannot continue" in screen or "Error" in screen:
        return SetupAction("error", "Setup cannot continue")
    lines = [line.strip() for line in screen.splitlines() if line.strip()]
    if lines and lines[-1].endswith("A:\\>"):
        return SetupAction("error", "A:\\>")
    return None


def _raise_timeout(label, harness):
    raise InstallError(f"timed out waiting for {label}; VGA screen:\n"
                       f"{harness.vga_str()}")


def wait_for_action(harness, max_steps):
    used = 0
    while used < max_steps:
        action = classify_screen(harness.vga_str())
        if action:
            return action
        chunk = min(100_000, max_steps - used)
        harness.run_steps(chunk)
        used += chunk
    _raise_timeout("the next Setup screen", harness)


def _inject_until_transition(harness, marker, *, ascii_byte=None,
                             scan_code=None, max_steps=4_000_000):
    stop = threading.Event()

    def inject():
        while not stop.wait(0.03):
            if scan_code is not None:
                harness.emu.kbd_ctrl.inject_extended_key(scan_code)
            else:
                harness.emu.kbd_ctrl.inject_key(ascii_byte)

    worker = threading.Thread(target=inject, daemon=True)
    worker.start()
    used = 0
    try:
        while used < max_steps and marker in harness.vga_str():
            harness.run_steps(50_000)
            used += 50_000
    finally:
        stop.set()
        worker.join(timeout=1)
    if marker in harness.vga_str():
        _raise_timeout(f"prompt transition from {marker!r}", harness)


def press_enter(harness, marker):
    _inject_until_transition(harness, marker, ascii_byte=0x0D)


def select_replace_existing_dos(harness):
    stop = threading.Event()

    def inject_down():
        while not stop.wait(0.03):
            harness.emu.kbd_ctrl.inject_extended_key(0x50)

    worker = threading.Thread(target=inject_down, daemon=True)
    worker.start()
    harness.run_steps(500_000)
    stop.set()
    worker.join(timeout=1)
    harness.run_steps(100_000)
    press_enter(harness, REPLACE_MARKER)


def write_disk(path, disk):
    with open(path, "wb") as image:
        for sector in disk.sectors:
            image.write(sector)
        image.flush()
        os.fsync(image.fileno())


def _new_harness(media, work_image, backend, boot_drive=0x00):
    harness = DOSHarness(
        image_path=str(media.disk1), hard_disk=str(work_image),
        boot_drive=boot_drive, cpu_backend=backend)
    harness.cpu.max_insns = max(harness.cpu.max_insns, 1_000_000_000)
    return harness


def _run_partition_stage(media, work_image, backend, max_steps, progress):
    progress("Booting Setup Disk 1 to create the DOS partition")
    harness = _new_harness(media, work_image, backend)
    try:
        harness.wait_for(WELCOME_MARKER, max_steps=max_steps)
        if WELCOME_MARKER not in harness.vga_str():
            _raise_timeout("the complete Setup welcome screen", harness)
        press_enter(harness, WELCOME_MARKER)
        for _ in range(8):
            action = wait_for_action(harness, max_steps)
            progress(f"Partitioning state: {action.name}")
            if action.name == "enter":
                press_enter(harness, action.marker)
            elif action.name == "replace":
                select_replace_existing_dos(harness)
            elif action.name == "restart":
                write_disk(work_image, harness.emu.hard_disk)
                return
            else:
                raise InstallError(
                    f"unexpected partitioning state {action.name!r}; "
                    f"VGA screen:\n{harness.vga_str()}")
        raise InstallError("partitioning exceeded the supported dialog count")
    finally:
        harness.cleanup()


def _run_setup_stage(media, work_image, backend, max_steps, progress):
    """Resume Microsoft Setup and perform its authentic three-disk install."""
    progress("Restarting Microsoft Setup to install MS-DOS onto C:")
    harness = _new_harness(media, work_image, backend)
    current_disk = 1
    try:
        for _ in range(32):
            action = wait_for_action(harness, max_steps * 12)
            progress(f"Setup state: {action.name}")
            if action.name == "replace":
                select_replace_existing_dos(harness)
            elif action.name == "enter":
                press_enter(harness, action.marker)
            elif action.name == "swap":
                if action.disk_number < current_disk:
                    raise InstallError("Setup requested disks out of order")
                harness.swap_disk(str(media.paths[action.disk_number - 1]))
                current_disk = action.disk_number
                harness.run_steps(100_000)
                press_enter(harness, action.marker)
            elif action.name == "complete":
                press_enter(harness, action.marker)
            elif action.name == "restart":
                write_disk(work_image, harness.emu.hard_disk)
                return
            else:
                raise InstallError(
                    f"Setup stopped at {action.marker!r}; "
                    f"VGA screen:\n{harness.vga_str()}")
        raise InstallError("Setup exceeded the supported dialog count")
    finally:
        harness.cleanup()


def _disk_from_file(path, cylinders):
    data = Path(path).read_bytes()
    expected = cylinders * HEADS * SECTORS_PER_TRACK * 512
    if len(data) != expected:
        raise InstallError(
            f"hard-disk size is {len(data)} bytes, expected {expected}")
    disk = Disk(len(data) // 512, cylinders=cylinders, heads=HEADS,
                sectors_per_track=SECTORS_PER_TRACK, hard_disk=True)
    for index, sector in enumerate(disk.sectors):
        sector[:] = data[index * 512:(index + 1) * 512]
    disk.dirty = False
    return disk


def verify_host_image(path, cylinders=DEFAULT_CYLINDERS):
    disk = _disk_from_file(path, cylinders)
    mbr = bytes(disk.sectors[0])
    if mbr[510:512] != b"\x55\xaa":
        raise InstallError("installed image has no MBR signature")
    part = mbr[446:462]
    start = int.from_bytes(part[8:12], "little")
    count = int.from_bytes(part[12:16], "little")
    if part[0] != 0x80 or part[4] not in (0x04, 0x06):
        raise InstallError("installed image has no active FAT16 partition")
    if start != PARTITION_START or count <= 0 or start + count > len(disk.sectors):
        raise InstallError("installed image has invalid partition bounds")
    try:
        fat = FAT16(DiskView(disk, start, count)).mount()
        root = fat.list_root()
    except (FAT12Error, ValueError) as exc:
        raise InstallError(f"cannot mount installed FAT16: {exc}") from exc
    entries = {entry.full_name: entry for entry in root if not entry.deleted}
    required = {"IO.SYS", "MSDOS.SYS", "COMMAND.COM", "CONFIG.SYS",
                "AUTOEXEC.BAT", "DOS"}
    missing = sorted(required - entries.keys())
    if missing:
        raise InstallError("installed filesystem is missing: " + ", ".join(missing))
    if not entries["DOS"].is_dir:
        raise InstallError("C:\\DOS is not a directory")
    dos_entries = [entry for entry in fat.read_dir(entries["DOS"].first_cluster)
                   if not entry.deleted]
    names = {entry.full_name for entry in dos_entries}
    core = {"SETUP.EXE", "FORMAT.COM", "FDISK.EXE", "HIMEM.SYS",
            "EXPAND.EXE", "MSAV.EXE", "UNDELETE.EXE"}
    if not core.issubset(names) or len(names) < 120:
        raise InstallError("C:\\DOS does not contain the complete utility set")
    compressed = sorted(name for name in names if name.endswith("_"))
    if compressed:
        raise InstallError("C:\\DOS still contains compressed files: "
                           + ", ".join(compressed[:5]))
    return {"partition_start": start, "partition_sectors": count,
            "dos_files": len(names)}


def verify_guest_boot(media, path, backend, max_steps):
    harness = _new_harness(media, path, backend, boot_drive=0x80)
    try:
        harness.wait_for("C:\\>", max_steps=max_steps * 2)
        if "C:\\>" not in harness.vga_str():
            _raise_timeout("the installed C> prompt", harness)
        result = harness.run_command(
            "VER", max_steps=max_steps, probe_errorlevel=False)
        if result.timed_out or "6.22" not in result.output:
            raise InstallError(
                "installed guest did not report MS-DOS 6.22; output:\n"
                + result.output)
    finally:
        harness.cleanup()


def _publish_atomic(source, destination, force=True):
    destination = Path(destination).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp",
        dir=str(destination.parent))
    try:
        with os.fdopen(fd, "wb") as target, open(source, "rb") as src:
            shutil.copyfileobj(src, target, 1024 * 1024)
            target.flush()
            os.fsync(target.fileno())
        if not force and destination.exists():
            raise InstallError(f"refusing to overwrite existing output: {destination}")
        os.replace(temp_name, destination)
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def install_dos622(output, media_dir=None, cylinders=DEFAULT_CYLINDERS,
                   cpu_backend="auto", force=False, max_steps=20_000_000,
                   keep_failed_state=False, progress=print):
    output = Path(output).resolve()
    if output.exists() and not force:
        raise InstallError(f"refusing to overwrite existing output: {output}")
    if output.exists() and not output.is_file():
        raise InstallError(f"output is not a regular file: {output}")
    if not 1 <= cylinders <= 1024:
        raise InstallError("cylinders must be between 1 and 1024")
    if cylinders < DEFAULT_CYLINDERS:
        raise InstallError("MS-DOS 6.22 automation requires at least 615 cylinders")
    if max_steps <= 0:
        raise InstallError("max_steps must be positive")
    media = resolve_media(media_dir)
    before = file_hashes(media.paths)
    backend = choose_backend(cpu_backend)
    work_dir = Path(tempfile.mkdtemp(prefix="dos622-install-"))
    work_image = work_dir / "dos622-working.hdd"
    try:
        create_hard_disk_image(str(work_image), cylinders=cylinders)
        progress(f"CPU backend: {backend}")
        _run_partition_stage(media, work_image, backend, max_steps, progress)
        _run_setup_stage(media, work_image, backend, max_steps, progress)
        details = verify_host_image(work_image, cylinders)
        progress("Host-side FAT16 verification passed")
        verify_guest_boot(media, work_image, backend, max_steps)
        progress("Guest boot verification passed")
        if file_hashes(media.paths) != before:
            raise InstallError("one or more source installation disks changed")
        _publish_atomic(work_image, output, force=force)
        progress(f"Installed MS-DOS 6.22 image: {output}")
        return details
    except Exception:
        if keep_failed_state and work_image.exists():
            failed = output.with_suffix(output.suffix + ".failed")
            if failed.exists():
                print(f"Did not overwrite existing failed image: {failed}",
                      file=sys.stderr)
            else:
                failed.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(work_image, failed)
                print(f"Preserved failed working image: {failed}", file=sys.stderr)
        raise
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def build_parser():
    parser = argparse.ArgumentParser(
        description="Install retail MS-DOS 6.22 through its floppy Setup")
    parser.add_argument("output", help="new raw hard-disk image to create")
    parser.add_argument("--media-dir", default=str(Path(REPO_ROOT) / "DOS6_22"),
                        help="directory containing Disk1.img through Disk3.img")
    parser.add_argument("--cylinders", type=int, default=DEFAULT_CYLINDERS,
                        help="C/4/17 hard-disk cylinders (default: 615)")
    parser.add_argument("--cpu-backend", choices=("auto", "python", "c"),
                        default="auto", help="CPU backend for the full install")
    parser.add_argument("--max-steps", type=int, default=20_000_000,
                        help="instruction budget per Setup state")
    parser.add_argument("--force", action="store_true",
                        help="replace an existing output only after verification")
    parser.add_argument("--keep-failed-state", action="store_true",
                        help="save OUTPUT.failed when installation fails")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        install_dos622(
            args.output, media_dir=args.media_dir, cylinders=args.cylinders,
            cpu_backend=args.cpu_backend, force=args.force,
            max_steps=args.max_steps, keep_failed_state=args.keep_failed_state)
    except (CPUBackendError, InstallError, OSError, ValueError) as exc:
        print(f"install_dos622: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
