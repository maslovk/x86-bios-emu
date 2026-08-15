"""Phase C/E/F disk-tool tests, including full-disk copy/compare.

FORMAT, SYS, DISKCOPY, and BACKUP/RESTORE are verified host-side; DISKCOMP
covers both identical and differing 360KB images.
"""
import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from dosharness import DOSHarness, DISK01, DISK02  # noqa: E402
from fat12 import FAT12, make_blank_image  # noqa: E402

pytestmark = [pytest.mark.slow, pytest.mark.tools]


def test_format_b_blank_then_host_verify(tmp_path):
    blank = str(tmp_path / 'BLANK.IMG')
    make_blank_image(blank, 360 * 1024)
    h = DOSHarness(image_path=DISK01, image_b=blank, writable=True)
    try:
        h.boot_to_prompt()
        r = h.run_dialog(
            'FORMAT B:',
            [('strike ENTER when ready', '\r'), ('Format another', 'N\r')],
            max_steps=6_000_000)
        assert not r.timed_out
        assert 'Format complete' in r.output
        assert '362496' in r.output              # 360K bytes available

        fat = FAT12(h.emu.disk_b)
        fat.mount()
        assert fat.list_root() == []             # no files after format
        assert fat.free_cluster_count() == fat.total_clusters
    finally:
        h.cleanup()


def test_diskcopy_a_to_b(tmp_path):
    blank = str(tmp_path / 'BLANK.IMG')
    make_blank_image(blank, 360 * 1024)
    h = DOSHarness(image_path=DISK01, image_b=blank, writable=True)
    try:
        h.boot_to_prompt()
        r = h.run_dialog(
            'DISKCOPY A: B:',
            [('Press any key when ready', '\r'), ('Copy another', 'N\r')],
            max_steps=2_000_000, probe_errorlevel=False)
        assert not r.timed_out
        # A 360KB disk is exactly 720 sectors.  Compare the entire copied
        # medium, not just its FAT directory listing.
        assert h.emu.disk_b.sectors[:720] == h.emu.disk.sectors[:720]
    finally:
        h.cleanup()


def test_sys_b_transfers_system(tmp_path):
    """``SYS B:`` writes IO.SYS/MSDOS.SYS as the first two entries (host-verified).

    In DOS 3.3 SYS takes a single destination drive (``SYS d:``), not the
    two-argument ``SYS src: dst:`` form added in DOS 4+.
    """
    blank = str(tmp_path / 'BLANK.IMG')
    make_blank_image(blank, 360 * 1024)
    h = DOSHarness(image_path=DISK01, image_b=blank, writable=True)
    try:
        h.boot_to_prompt()
        h.run_dialog('FORMAT B:', [('strike ENTER when ready', '\r'),
                                    ('Format another', 'N\r')],
                     max_steps=6_000_000, probe_errorlevel=False)
        r = h.run_command('SYS B:', max_steps=6_000_000)
        assert not r.timed_out
        assert 'System transferred' in r.output

        fb = FAT12(h.emu.disk_b)
        fb.mount()
        names = [e.full_name for e in fb.list_root()]
        # The system files must be the first two entries (IO.SYS, MSDOS.SYS).
        assert names[:2] == ['IO.SYS', 'MSDOS.SYS']
    finally:
        h.cleanup()


def test_recover_usage_returns(tmp_path):
    """``RECOVER`` with no args prints usage (no crash) and returns to A>."""
    blank = str(tmp_path / 'BLANK.IMG')
    make_blank_image(blank, 360 * 1024)
    h = DOSHarness(image_path=DISK01, image_b=blank, writable=True)
    try:
        h.boot_to_prompt()
        r = h.run_command('RECOVER', max_steps=4_000_000, probe_errorlevel=False)
        assert not r.timed_out
        # A follow-up command must still work (no emulator hang/crash).
        ok = h.run_command('ECHO alive', max_steps=2_000_000,
                           probe_errorlevel=False)
        assert not ok.timed_out
        assert 'alive' in ok.output
    finally:
        h.cleanup()


def test_backup_restore_roundtrip(tmp_path):
    """BACKUP one file to B:, delete it, then RESTORE it byte-for-byte.

    BACKUP.COM and RESTORE.COM ship on DISK02.  Copy them into the writable
    boot image so drive B can be a genuinely blank backup disk.  The two old,
    large A: files removed here are only from the harness's private image copy.
    """
    tools = DOSHarness(image_path=DISK02, writable=False)
    try:
        ft = FAT12(tools.emu.disk)
        ft.mount()
        backup_com = ft.read_file_by_name('BACKUP.COM')
        restore_com = ft.read_file_by_name('RESTORE.COM')
    finally:
        tools.cleanup()

    blank = str(tmp_path / 'BACKUP.IMG')
    make_blank_image(blank, 360 * 1024)
    h = DOSHarness(image_path=DISK01, image_b=blank, writable=True)
    try:
        fa = FAT12(h.emu.disk)
        fa.mount()
        assert fa.delete_file('FDISK.COM')
        assert fa.delete_file('4201.CPI')
        fa.write_file('BACKUP.COM', backup_com)
        fa.write_file('RESTORE.COM', restore_com)
        fa.write_file('BK.TXT', b'backup-body')

        h.boot_to_prompt()

        # BACKUP has two media confirmations.  Wait for the complete first
        # prompt; for the destructive warning, allow its trailing "Strike any
        # key" line to finish before injecting the response.
        prev = h.vga_str()
        h.inject_string('BACKUP A:BK.TXT B:\r')
        h.wait_for('Strike any key when ready', max_steps=3_000_000)
        h.inject_string(' ')
        h.wait_for('Warning! Files in the target drive', max_steps=3_000_000)
        h.run_steps(100_000)
        h.inject_string(' ')
        _, timed_out = h._wait_prompt(prev, 4_000_000)
        assert not timed_out

        fb = FAT12(h.emu.disk_b)
        fb.mount()
        assert fb.read_file_by_name('BACKUP.001') == b'backup-body'
        control = fb.read_file_by_name('CONTROL.001')
        assert control is not None and len(control) > 0

        assert not h.run_command('DEL BK.TXT', probe_errorlevel=False).timed_out
        # Remove BACKUP's old prompt text so RESTORE's identical first prompt
        # cannot make the sequential wait return prematurely.
        assert not h.run_command('CLS', probe_errorlevel=False).timed_out

        prev = h.vga_str()
        h.inject_string('RESTORE B: A:BK.TXT\r')
        h.wait_for('Strike any key when ready', max_steps=3_000_000)
        h.inject_string(' ')
        h.wait_for('Insert restore target in drive A:', max_steps=3_000_000)
        h.run_steps(100_000)
        h.inject_string(' ')
        _, timed_out = h._wait_prompt(prev, 5_000_000)
        assert not timed_out

        fa = FAT12(h.emu.disk)
        fa.mount()
        assert fa.read_file_by_name('BK.TXT') == b'backup-body'
    finally:
        h.cleanup()


def _run_diskcomp(h):
    """Run DISKCOMP after each complete media prompt is ready for input."""
    h.run_steps(20_000)
    scroll_start = len(h._scrollback)
    prev = h.vga_str()
    h.inject_string('DISKCOMP A: B:\r')
    h.wait_for('Press any key when ready . . .', max_steps=2_000_000)
    h.inject_string('\r')
    h.wait_for('Compare another diskette (Y/N) ?', max_steps=2_000_000)
    # On identical disks the final prompt follows Compare OK immediately;
    # allow the program to reach its input loop before sending N.
    h.run_steps(100_000)
    h.inject_string('N\r')
    _, timed_out = h._wait_prompt(prev, 2_000_000)
    return timed_out, h._transcript(scroll_start)


def test_diskcomp_a_b(tmp_path):
    identical = str(tmp_path / 'IDENTICAL.IMG')
    shutil.copy2(DISK01, identical)
    h = DOSHarness(image_path=DISK01, image_b=identical, writable=True)
    try:
        h.boot_to_prompt()
        timed_out, output = _run_diskcomp(h)
        assert not timed_out
        assert 'Compare OK' in output
    finally:
        h.cleanup()


def test_diskcomp_reports_differences(tmp_path):
    blank = str(tmp_path / 'DIFFERENT.IMG')
    make_blank_image(blank, 360 * 1024)
    h = DOSHarness(image_path=DISK01, image_b=blank, writable=True)
    try:
        h.boot_to_prompt()
        timed_out, output = _run_diskcomp(h)
        assert not timed_out
        assert 'Compare error on side' in output
        assert 'Compare OK' not in output
    finally:
        h.cleanup()
