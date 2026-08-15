"""Phase E/F CONFIG.SYS device-driver coverage (Tier 2).

Each driver is loaded via an injected ``CONFIG.SYS`` (written host-side into the
in-memory disk with :class:`fat12.FAT12` *before* the boot steps run, so DOS
finds it during IO.SYS/MSDOS.SYS startup).  RAMDRIVE.SYS ships on DISK02, so
its tests mount that image as B: and use ``DEVICE=B:\\RAMDRIVE.SYS``.  ANSI.SYS
can own its hooked INT 29h vector, but complete cursor/attribute rendering
still needs an end-to-end regression.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from dosharness import DOSHarness, DISK01, DISK02  # noqa: E402
from fat12 import FAT12  # noqa: E402

pytestmark = [pytest.mark.slow, pytest.mark.tools]


def _boot_with_config(config, image_b=None):
    """Boot DISK01 (writable copy) with the given CONFIG.SYS injected pre-boot."""
    h = DOSHarness(image_path=DISK01, image_b=image_b, writable=True)
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
    """Booting RAMDRIVE from B: registers virtual disk C:."""
    h = _boot_with_config('DEVICE=B:\\RAMDRIVE.SYS\r\n', image_b=DISK02)
    try:
        assert 'A>' in h.vga_str()
        assert 'Microsoft RAMDrive' in h.vga_str()
        assert 'Bad or missing' not in h.vga_str()
    finally:
        h.cleanup()


def test_ramdrive_dir_c():
    """Create, list, and read a file on the RAMDRIVE C: block device."""
    h = _boot_with_config('DEVICE=B:\\RAMDRIVE.SYS\r\n', image_b=DISK02)
    try:
        h.create_file('C:\\RAM.TXT', 'ram-body')
        r = h.run_command('DIR C:', max_steps=4_000_000, probe_errorlevel=False)
        assert not r.timed_out
        assert 'MS-RAMDRIVE' in r.output
        assert 'RAM      TXT' in r.output

        r = h.run_command('TYPE C:\\RAM.TXT', max_steps=4_000_000,
                          probe_errorlevel=False)
        assert not r.timed_out
        assert 'ram-body' in r.output
    finally:
        h.cleanup()


def test_driver_sys_boots():
    """Booting with DEVICE=DRIVER.SYS reaches the A> prompt."""
    h = _boot_with_config('DEVICE=DRIVER.SYS /D:1\r\n')
    try:
        assert 'A>' in h.vga_str()
    finally:
        h.cleanup()
