"""Phase J end-to-end FAT16 hard-disk coverage."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from dosharness import DISK01, DOSHarness  # noqa: E402
from fat12 import FAT16  # noqa: E402
from video import DiskView  # noqa: E402

pytestmark = [pytest.mark.slow, pytest.mark.tools]

CYLINDERS = 615
HEADS = 4
SECTORS_PER_TRACK = 17
TOTAL_SECTORS = CYLINDERS * HEADS * SECTORS_PER_TRACK
PARTITION_START = 17
PARTITION_SECTORS = TOTAL_SECTORS - PARTITION_START


def _write_disk(path, disk):
    with path.open('wb') as image:
        for sector in disk.sectors:
            image.write(sector)


def test_fdisk_format_and_boot_fat16_hard_disk(tmp_path):
    """DOS creates, formats, and boots a >16 MB FAT16 partition."""
    blank = tmp_path / 'BLANK-FAT16.IMG'
    with blank.open('wb') as image:
        image.truncate(TOTAL_SECTORS * 512)
    partitioned = tmp_path / 'PARTITIONED-FAT16.IMG'
    bootable = tmp_path / 'BOOTABLE-FAT16.IMG'

    fdisk = DOSHarness(
        image_path=DISK01, hard_disk=str(blank), writable=True)
    try:
        fdisk.boot_to_prompt()
        fdisk.inject_string('FDISK\r')
        fdisk.wait_for('Enter choice:', max_steps=3_000_000)
        fdisk.run_steps(20_000)
        fdisk.inject_string('1\r')
        fdisk.wait_for('Create Primary DOS partition', max_steps=3_000_000)
        fdisk.run_steps(20_000)
        fdisk.inject_string('1\r')
        fdisk.wait_for('maximum size', max_steps=3_000_000)
        fdisk.run_steps(20_000)
        fdisk.inject_string('Y\r')
        fdisk.wait_for('Press any key when ready', max_steps=5_000_000)

        mbr = bytes(fdisk.emu.hard_disk.sectors[0])
        entry = mbr[446:462]
        assert mbr[510:512] == b'\x55\xaa'
        assert entry[0] == 0x80
        assert entry[4] == 0x04
        assert int.from_bytes(entry[8:12], 'little') == PARTITION_START
        assert int.from_bytes(entry[12:16], 'little') == PARTITION_SECTORS
        _write_disk(partitioned, fdisk.emu.hard_disk)
    finally:
        fdisk.cleanup()

    formatter = DOSHarness(
        image_path=DISK01, hard_disk=str(partitioned), writable=True)
    try:
        formatter.boot_to_prompt()
        previous_screen = formatter.vga_str()
        scroll_start = len(formatter._scrollback)
        formatter.inject_string('FORMAT C: /S\r')
        formatter.wait_for('Proceed with Format', max_steps=3_000_000)
        formatter.run_steps(20_000)
        formatter.inject_string('Y\r')
        _steps, timed_out = formatter._wait_prompt(
            previous_screen, 25_000_000)
        assert not timed_out
        transcript = formatter._transcript(scroll_start)
        assert 'Format complete' in transcript
        assert 'System transferred' in transcript

        result = formatter.run_command(
            'ECHO phase-j-fat16>C:\\FAT16.TXT', max_steps=4_000_000)
        assert not result.timed_out
        assert result.errorlevel == 0

        partition = DiskView(
            formatter.emu.hard_disk, PARTITION_START, PARTITION_SECTORS)
        fat = FAT16(partition).mount()
        assert fat.hidden_sectors == PARTITION_START
        assert fat.total_sectors == PARTITION_SECTORS
        assert fat.sectors_per_cluster == 4
        assert fat.sectors_per_fat == 41
        names = [entry.full_name for entry in fat.list_root()]
        assert names[:3] == ['IO.SYS', 'MSDOS.SYS', 'COMMAND.COM']
        assert fat.read_file_by_name('FAT16.TXT') == b'phase-j-fat16\r\n'
        _write_disk(bootable, formatter.emu.hard_disk)
    finally:
        formatter.cleanup()

    boot = DOSHarness(
        image_path=DISK01, hard_disk=str(bootable), boot_drive=0x80,
        writable=False)
    try:
        boot.boot_to_prompt()
        assert boot.vga_str().rstrip().endswith('C>')
        result = boot.run_command(
            'TYPE C:\\FAT16.TXT', max_steps=4_000_000)
        assert not result.timed_out
        assert result.errorlevel == 0
        assert 'phase-j-fat16' in result.output
    finally:
        boot.cleanup()
