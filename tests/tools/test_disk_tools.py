"""Phase C/E/F disk-tool tests.

FORMAT, SYS, and BACKUP/RESTORE are verified host-side with FAT12;
DISKCOPY/DISKCOMP remain xfailed because full-disk operation is too slow at
the current emulated instruction rate.
"""
import os
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


@pytest.mark.xfail(
    strict=True,
    reason='DISKCOPY of a full 360KB disk reads+writes 720 sectors via INT 13h '
           'and exceeds a practical step budget at the current ~40k inst/s '
           'emulated instruction rate (>10M steps, watchdog timeout). '
           'Revisit after a CPU performance / Phase-F pass.')
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
        from fat12 import FAT12 as _F
        a = _F(h.emu.disk); a.mount()
        b = _F(h.emu.disk_b); b.mount()
        assert (sorted(e.full_name for e in a.list_root()) ==
                sorted(e.full_name for e in b.list_root()))
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


@pytest.mark.xfail(
    strict=True,
    reason='DISKCOMP compares the full 360KB disk track-by-track and exceeds '
           'the step budget at the current instruction rate; companion to the '
           'DISKCOPY xfail.')
def test_diskcomp_a_b(tmp_path):
    blank = str(tmp_path / 'BLANK.IMG')
    make_blank_image(blank, 360 * 1024)
    h = DOSHarness(image_path=DISK01, image_b=blank, writable=True)
    try:
        h.boot_to_prompt()
        r = h.run_dialog(
            'DISKCOMP A: B:',
            [('Press any key when ready', '\r'), ('Compare another', 'N\r')],
            max_steps=2_000_000, probe_errorlevel=False)
        assert not r.timed_out
        assert 'Compare OK' in r.output
    finally:
        h.cleanup()
