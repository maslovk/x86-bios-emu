"""Phase E/F TREE / XCOPY / REPLACE.

TREE.EXE, XCOPY.EXE and REPLACE.COM all ship on DISK02 (drive B), so they are
invoked as ``B:TREE`` etc. on a harness with B:=DISK02.  XCOPY/REPLACE need
both the *executable* (on DISK02) and free space on the *destination* (DISK02
has ~2 KB free, enough for one small file), so TREE uses the shared read-mostly
``dos_b`` fixture and XCOPY/REPLACE each boot their own isolated harness with
DISK02 as drive B (writing one tiny file, verified host-side).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from dosharness import DOSHarness, DISK01, DISK02  # noqa: E402
from fat12 import FAT12  # noqa: E402

pytestmark = [pytest.mark.slow, pytest.mark.tools]


def test_tree_lists_volume(dos_b):
    """B:TREE lists the volume with no sub-directories on the stock disk."""
    r = dos_b.run_command('B:TREE', max_steps=5_000_000)
    assert not r.timed_out
    assert 'DIRECTORY PATH LISTING' in r.output
    assert 'No sub-directories' in r.output


def _dos_b_harness():
    h = DOSHarness(image_path=DISK01, image_b=DISK02, writable=True)
    h.boot_to_prompt()
    return h


def test_xcopy_a_to_b(dos_b):
    """B:XCOPY A:SRC.TXT B: copies one file to drive B (host-verified)."""
    dos_b.create_file('SRC.TXT', 'xcopy-body')
    r = dos_b.run_command('B:XCOPY A:SRC.TXT B:', max_steps=6_000_000)
    assert not r.timed_out
    assert '1 File(s) copied' in r.output

    fb = FAT12(dos_b.emu.disk_b)
    fb.mount()
    assert fb.read_file_by_name('SRC.TXT') == b'xcopy-body'
    # tidy drive B so the module-scoped session keeps space for later tests.
    dos_b.run_command('DEL B:SRC.TXT', max_steps=3_000_000, probe_errorlevel=False)
    dos_b.run_command('DEL SRC.TXT', probe_errorlevel=False)


def test_replace_updates_destination(dos_b):
    """B:REPLACE overwrites a newer source file onto the destination on B.

    Phase F fixed the memory shift/rotate displacement double-decode that
    corrupted control flow before REPLACE initialized its DOS list head.
    """
    dos_b.create_file('R.TXT', 'old-version')
    dos_b.run_command('COPY R.TXT B:', max_steps=4_000_000, probe_errorlevel=False)
    dos_b.run_command('DEL R.TXT', probe_errorlevel=False)
    dos_b.create_file('R.TXT', 'new-version')
    r = dos_b.run_command('B:REPLACE A:R.TXT B:', max_steps=6_000_000)
    assert not r.timed_out
    # REPLACE prints 'replacing' on an existing destination file.
    assert 'replacing' in r.output.lower()

    fb = FAT12(dos_b.emu.disk_b)
    fb.mount()
    assert fb.read_file_by_name('R.TXT') == b'new-version'
    dos_b.run_command('DEL B:R.TXT', max_steps=3_000_000, probe_errorlevel=False)
    dos_b.run_command('DEL R.TXT', probe_errorlevel=False)
