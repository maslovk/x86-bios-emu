"""Integration tests for the MS-DOS 6.22 boot from DOS6_22/disk01.img.

MS-DOS 6.22 ships on EXEPACK-compressed Setup diskettes (1.44 MB, media
0xF0).  SYSINIT's bit-stream decompressor is a dense consumer of DEC/SHR/
TEST flag corners, so this boot pins the four CPU semantics fixed for it
(see tests/test_dos6_flags.py): INC/DEC auxiliary carry, logic-op AF
clearing, SHR overflow from the original MSB, and TEST r/m,r using the
ModRM reg field.  Before those fixes the decompressor terminated early,
leaving the relocated kernel mostly zero-filled.

The Setup disk itself requires a hard disk to proceed past its welcome
flow (it is an Upgrade edition), so the hard-disk test attaches a blank
legacy C/4/17 image.

Slow: each test boots MS-DOS 6.22.  Run with:
    pytest tests/test_dos6_boot.py -v -m slow
"""
import os
import sys
import tempfile
import shutil

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from dosharness import DOSHarness, REPO_ROOT

DOS6_DISK1 = os.path.join(REPO_ROOT, 'DOS6_22', 'disk01.img')

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not os.path.exists(DOS6_DISK1),
                       reason='DOS6_22/disk01.img not present'),
]


class TestDOS6Boot:
    """MS-DOS 6.22 boot from the historical Setup system disk."""

    def test_boot_reaches_setup_welcome(self):
        h = DOSHarness(image_path=DOS6_DISK1)
        h.wait_for('Starting MS-DOS', max_steps=6_000_000)
        h.run_steps(6_000_000)
        screen = h.vga_str()
        assert 'MS-DOS 6.22 Setup' in screen
        # The blank-hardware dialog is the expected first stop without a
        # hard disk; the EXEPACK'd kernel has fully decompressed by here.
        assert 'does not have a hard disk' in screen

    def test_boot_with_hard_disk_reaches_welcome(self):
        tmp = tempfile.mkdtemp()
        try:
            from main import create_hard_disk_image
            hdd = os.path.join(tmp, 'dos6.hdd')
            create_hard_disk_image(hdd, cylinders=306)
            h = DOSHarness(image_path=DOS6_DISK1, hard_disk=hdd,
                            writable=True)
            h.wait_for('Welcome to Setup', max_steps=12_000_000)
            screen = h.vga_str()
            assert 'MS-DOS 6.22 Setup' in screen
            assert 'To set up MS-DOS now, press ENTER' in screen
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
