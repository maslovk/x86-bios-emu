"""Phase G FDISK coverage against an isolated legacy-CHS hard disk."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from dosharness import DISK01, DOSHarness  # noqa: E402

pytestmark = [pytest.mark.slow, pytest.mark.tools]

CYLINDERS = 306
HEADS = 4
SECTORS_PER_TRACK = 17
TOTAL_SECTORS = CYLINDERS * HEADS * SECTORS_PER_TRACK


def test_fdisk_creates_active_primary_partition(tmp_path):
    """FDISK creates a maximum-size active partition on BIOS drive 80h."""
    source = tmp_path / 'BLANK-HD.IMG'
    with source.open('wb') as image:
        image.truncate(TOTAL_SECTORS * 512)

    h = DOSHarness(
        image_path=DISK01, hard_disk=str(source), writable=True)
    try:
        h.boot_to_prompt()
        h.inject_string('FDISK\r')

        h.wait_for('Enter choice:', max_steps=3_000_000)
        assert 'FDISK Options' in h.vga_str()
        h.run_steps(20_000)
        h.inject_string('1\r')

        h.wait_for('Create Primary DOS partition', max_steps=3_000_000)
        h.run_steps(20_000)
        h.inject_string('1\r')

        h.wait_for('maximum size', max_steps=3_000_000)
        h.run_steps(20_000)
        h.inject_string('Y\r')

        h.wait_for('Press any key when ready', max_steps=5_000_000)
        assert 'System will now restart' in h.vga_str()

        mbr = bytes(h.emu.hard_disk.sectors[0])
        assert mbr[510:512] == b'\x55\xaa'

        primary = mbr[446:462]
        assert primary[0] == 0x80       # active/bootable
        assert primary[4] == 0x01       # FAT12 (partition is below 16 MB)
        assert int.from_bytes(primary[8:12], 'little') == 17
        assert int.from_bytes(primary[12:16], 'little') == TOTAL_SECTORS - 17
        assert mbr[462:510] == bytes(48)  # remaining three entries unused
        assert h.emu.hard_disk.dirty

        # writable=True materialises a private copy before the emulator sees
        # the disk, so even FDISK's sector-zero write cannot touch the caller's
        # source image.
        with source.open('rb') as image:
            assert image.read(512) == bytes(512)
    finally:
        h.cleanup()
