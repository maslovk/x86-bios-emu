"""Phase H DOS filesystem coverage for the fixed-disk partition."""

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
PARTITION_SECTORS = TOTAL_SECTORS - PARTITION_START


def _make_partitioned_image(path):
    """Create the exact active FAT12 partition layout emitted by FDISK."""
    with path.open('wb') as image:
        image.truncate(TOTAL_SECTORS * 512)
    mbr = bytearray(512)
    mbr[446:462] = bytes.fromhex(
        '80010100010351311100000037510000')
    mbr[510:512] = b'\x55\xaa'
    with path.open('r+b') as image:
        image.write(mbr)


def test_format_c_and_file_roundtrip(tmp_path):
    """A fresh boot mounts the partition as C: and FORMAT makes it writable."""
    source = tmp_path / 'PARTITIONED-HD.IMG'
    _make_partitioned_image(source)

    h = DOSHarness(
        image_path=DISK01, hard_disk=str(source), writable=True)
    try:
        h.boot_to_prompt()
        visible = h.run_command(
            'DIR C:', max_steps=4_000_000, probe_errorlevel=False)
        assert not visible.timed_out
        assert 'Directory of  C:\\' in visible.output

        previous_screen = h.vga_str()
        format_scroll_start = len(h._scrollback)
        h.inject_string('FORMAT C:\r')
        h.wait_for('Proceed with Format', max_steps=3_000_000)
        h.run_steps(20_000)
        h.inject_string('Y\r')
        _steps, timed_out = h._wait_prompt(previous_screen, 12_000_000)
        assert not timed_out
        assert 'Format complete' in h._transcript(format_scroll_start)

        written = h.run_command(
            'ECHO phase-h>C:\\HELLO.TXT', max_steps=4_000_000)
        assert not written.timed_out
        assert written.errorlevel == 0
        read_back = h.run_command(
            'TYPE C:\\HELLO.TXT', max_steps=4_000_000)
        assert not read_back.timed_out
        assert 'phase-h' in read_back.output

        partition = DiskView(
            h.emu.hard_disk, PARTITION_START, PARTITION_SECTORS)
        fat = FAT12(partition)
        fat.mount()
        assert fat.hidden_sectors == PARTITION_START
        assert fat.total_sectors == PARTITION_SECTORS
        assert fat.cluster_size == 4096
        assert fat.read_file_by_name('HELLO.TXT') == b'phase-h\r\n'

        # The harness formatted only its private materialised copy.
        with source.open('rb') as image:
            image.seek(PARTITION_START * 512)
            assert image.read(512) == bytes(512)
    finally:
        h.cleanup()
