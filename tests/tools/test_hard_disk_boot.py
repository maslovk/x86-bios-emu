"""Phase I end-to-end MS-DOS hard-disk boot coverage."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from dosharness import DISK01, DOSHarness  # noqa: E402
from fat12 import FAT12  # noqa: E402
from video import DiskView  # noqa: E402

pytestmark = [pytest.mark.slow, pytest.mark.tools]

CYLINDERS = 306
HEADS = 4
SECTORS_PER_TRACK = 17
TOTAL_SECTORS = CYLINDERS * HEADS * SECTORS_PER_TRACK
PARTITION_START = 17


def _write_disk(path, disk):
    with path.open('wb') as image:
        for sector in disk.sectors:
            image.write(sector)


def _partition_with_fdisk(source, output):
    h = DOSHarness(
        image_path=DISK01, hard_disk=str(source), writable=True)
    try:
        h.boot_to_prompt()
        h.inject_string('FDISK\r')
        h.wait_for('Enter choice:', max_steps=3_000_000)
        h.run_steps(20_000)
        h.inject_string('1\r')
        h.wait_for('Create Primary DOS partition', max_steps=3_000_000)
        h.run_steps(20_000)
        h.inject_string('1\r')
        h.wait_for('maximum size', max_steps=3_000_000)
        h.run_steps(20_000)
        h.inject_string('Y\r')
        h.wait_for('Press any key when ready', max_steps=5_000_000)
        _write_disk(output, h.emu.hard_disk)
    finally:
        h.cleanup()


def _format_system_partition(source, output):
    h = DOSHarness(
        image_path=DISK01, hard_disk=str(source), writable=True)
    try:
        h.boot_to_prompt()
        previous_screen = h.vga_str()
        format_scroll_start = len(h._scrollback)
        h.inject_string('FORMAT C: /S\r')
        h.wait_for('Proceed with Format', max_steps=3_000_000)
        h.run_steps(20_000)
        h.inject_string('Y\r')
        _steps, timed_out = h._wait_prompt(previous_screen, 14_000_000)
        assert not timed_out
        transcript = h._transcript(format_scroll_start)
        assert 'Format complete' in transcript
        assert 'System transferred' in transcript

        written = h.run_command(
            'ECHO hard-disk-boot>C:\\BOOTED.TXT', max_steps=4_000_000)
        assert not written.timed_out
        assert written.errorlevel == 0

        partition = DiskView(
            h.emu.hard_disk, PARTITION_START,
            TOTAL_SECTORS - PARTITION_START)
        fat = FAT12(partition)
        fat.mount()
        names = [entry.full_name for entry in fat.list_root()]
        assert names[:3] == ['IO.SYS', 'MSDOS.SYS', 'COMMAND.COM']
        assert fat.read_file_by_name('BOOTED.TXT') == b'hard-disk-boot\r\n'
        _write_disk(output, h.emu.hard_disk)
    finally:
        h.cleanup()


def test_fdisk_format_system_then_boot_from_hard_disk(tmp_path):
    """An FDISK + FORMAT /S image boots through its MBR to C>."""
    blank = tmp_path / 'BLANK-HD.IMG'
    with blank.open('wb') as image:
        image.truncate(TOTAL_SECTORS * 512)
    partitioned = tmp_path / 'PARTITIONED-HD.IMG'
    bootable = tmp_path / 'BOOTABLE-HD.IMG'

    _partition_with_fdisk(blank, partitioned)
    _format_system_partition(partitioned, bootable)

    boot = DOSHarness(
        image_path=DISK01, hard_disk=str(bootable), boot_drive=0x80,
        writable=False)
    try:
        boot.boot_to_prompt()
        assert boot.vga_str().rstrip().endswith('C>')
        result = boot.run_command(
            'TYPE C:\\BOOTED.TXT', max_steps=4_000_000)
        assert not result.timed_out
        assert result.errorlevel == 0
        assert 'hard-disk-boot' in result.output
    finally:
        boot.cleanup()
