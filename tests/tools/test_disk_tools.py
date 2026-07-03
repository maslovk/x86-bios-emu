"""Phase C disk-to-disk tool tests: FORMAT (passing); DISKCOPY/DISKCOMP
(full-disk, too slow at the current emulated instruction rate) xfailed.

FORMAT B: drives the interactive prompts via the sequential run_dialog
helper and is then verified host-side with FAT12.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from dosharness import DOSHarness, DISK01  # noqa: E402
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
