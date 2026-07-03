"""Phase E CONFIG.SYS device drivers (Tier 2): boot-smoke.

Each driver is loaded via an injected ``CONFIG.SYS`` (written host-side into the
in-memory disk with :class:`fat12.FAT12` *before* the boot steps run, so DOS
finds it during IO.SYS/MSDOS.SYS startup).  The Tier-2 bar is "boots to ``A>``
with the driver loaded"; the ANSI escape-attribute effect and the RAMDRIVE
``DIR C:`` functional probe are stretches documented as xfail (ANSI.SYS hooks
INT 29h, which the emulator always services with its built-in putchar, so
escape sequences print literally rather than altering VRAM attributes; RAMDRIVE
loads but does not register a visible ``C:`` drive).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from dosharness import DOSHarness, DISK01  # noqa: E402
from fat12 import FAT12  # noqa: E402

pytestmark = [pytest.mark.slow, pytest.mark.tools]


def _boot_with_config(config):
    """Boot DISK01 (writable copy) with the given CONFIG.SYS injected pre-boot."""
    h = DOSHarness(image_path=DISK01, writable=True)
    fat = FAT12(h.emu.disk)
    fat.mount()
    fat.write_file('CONFIG.SYS', config.encode())
    h.boot_to_prompt()
    return h


def test_ansi_sys_boots():
    """Booting with DEVICE=ANSI.SYS reaches the A> prompt."""
    h = _boot_with_config('DEVICE=ANSI.SYS\r\n')
    try:
        assert 'A>' in h.vga_str()
    finally:
        h.cleanup()


def test_ramdrive_sys_boots():
    """Booting with DEVICE=RAMDRIVE.SYS reaches the A> prompt."""
    h = _boot_with_config('DEVICE=RAMDRIVE.SYS\r\n')
    try:
        assert 'A>' in h.vga_str()
    finally:
        h.cleanup()


@pytest.mark.xfail(strict=True, reason='RAMDRIVE loads but does not register a '
                                       'visible C: drive (DIR C: -> Invalid drive '
                                       'specification); functional probe pending')
def test_ramdrive_dir_c():
    h = _boot_with_config('DEVICE=RAMDRIVE.SYS\r\n')
    try:
        r = h.run_command('DIR C:', max_steps=4_000_000, probe_errorlevel=False)
        assert not r.timed_out
        assert 'File(s)' in r.output
    finally:
        h.cleanup()


def test_driver_sys_boots():
    """Booting with DEVICE=DRIVER.SYS reaches the A> prompt."""
    h = _boot_with_config('DEVICE=DRIVER.SYS /D:1\r\n')
    try:
        assert 'A>' in h.vga_str()
    finally:
        h.cleanup()
