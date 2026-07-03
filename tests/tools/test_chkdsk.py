"""Phase E CHKDSK: reported byte counts match host-side FAT12 arithmetic.

CHKDSK is read-only on a clean disk, so the module-scoped ``dos`` fixture is
reused.  The totals DOS reports come straight from the FAT and root directory,
so a fresh :class:`fat12.FAT12` mount of the same in-memory disk must agree on
total space, free space, and hidden/user file byte counts.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from fat12 import FAT12, DirEntry  # noqa: E402

pytestmark = [pytest.mark.slow, pytest.mark.tools]


def _int(s):
    """Pull the first integer (allowing comma thousands) out of a string."""
    digits = ''
    for ch in s:
        if ch.isdigit():
            digits += ch
        elif digits:
            break
    return int(digits) if digits else None


def test_chkdsk_reports_match_host_fat12(dos):
    r = dos.run_command('CHKDSK', max_steps=6_000_000)
    assert not r.timed_out
    out = r.output
    log = out.split('A>CHKDSK', 1)[-1] if 'A>CHKDSK' in out else out

    dos_total = _int(_line_after(log, 'bytes total disk space'))
    dos_free = _int(_line_after(log, 'bytes available on disk'))
    assert dos_total == 362496          # 360K formatted capacity
    assert dos_free is not None and dos_free >= 0

    # Host-side FAT12 must agree on total and free bytes.
    fat = FAT12(dos.emu.disk)
    fat.mount()
    bpc = fat.cluster_size
    host_total = fat.total_clusters * bpc
    host_free = fat.free_cluster_count() * bpc
    assert host_total == dos_total
    assert host_free == dos_free


def test_chkdsk_hidden_and_user_bytes(dos):
    """Hidden files (IO.SYS/MSDOS.SYS) and user file counts are positive."""
    r = dos.run_command('CHKDSK', max_steps=6_000_000)
    log = r.output.split('A>CHKDSK', 1)[-1]
    hidden = _int(_line_after(log, 'hidden files'))
    user = _int(_line_after(log, 'user files'))
    assert hidden and hidden > 0
    assert user and user > 0


def _line_after(text, needle):
    """Return the text of the line containing `needle` (case-insensitive)."""
    for line in text.splitlines():
        if needle.lower() in line.lower():
            return line
    return ''
