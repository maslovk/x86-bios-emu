"""Phase E internal commands with wildcards: DIR/COPY/DEL/REN globbing.

Builds on Phase B's per-file round-trip tests (``test_file_io.py``) by exercising
DOS's wildcard expansion.  Drive A: on the distribution DISK01 has only ~5 KB
free, so every test keeps file creation to one or two tiny files.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

pytestmark = [pytest.mark.slow, pytest.mark.tools]


def test_dir_wildcard_com(dos):
    """``DIR *.COM`` lists only .COM files and the summary line."""
    r = dos.run_command('DIR *.COM', max_steps=4_000_000)
    assert not r.timed_out
    assert 'File(s)' in r.output
    # COMMAND.COM is always present on DISK01; its name must appear.
    assert 'COMMAND' in r.output


def test_dir_wide(dos):
    """``DIR *.COM /W`` wide format shows entries across columns."""
    r = dos.run_command('DIR *.COM /W', max_steps=4_000_000)
    assert not r.timed_out
    assert 'File(s)' in r.output


def test_copy_wildcard_bak(dos_rw):
    """``COPY *.TXT *.BAK`` renames the extension via wildcards.

    Creates one .TXT file (DISK01 ships none in the root), then COPY *.TXT
    *.BAK must produce a matching .BAK whose content is identical, verified
    host-side.
    """
    from fat12 import FAT12
    dos_rw.create_file('W.TXT', 'wildcard-body')
    r = dos_rw.run_command('COPY *.TXT *.BAK')
    assert not r.timed_out
    assert '1 File(s) copied' in r.output

    fat = FAT12(dos_rw.emu.disk)
    fat.mount()
    assert fat.read_file_by_name('W.BAK') == b'wildcard-body'
    dos_rw.run_command('DEL W.BAK')   # tidy so free space is restored for the
    dos_rw.run_command('DEL W.TXT')  # errorlevel probe / next command


def test_del_wildcard(dos_rw):
    """``DEL *.BAK`` removes only files matching the glob."""
    from fat12 import FAT12
    dos_rw.create_file('K.BAK', 'bak')
    dos_rw.create_file('K.TXT', 'txt')
    r = dos_rw.run_command('DEL *.BAK')
    assert not r.timed_out

    fat = FAT12(dos_rw.emu.disk)
    fat.mount()
    assert fat.find_file('K.BAK') is None
    assert fat.find_file('K.TXT') is not None
    dos_rw.run_command('DEL K.TXT')


def test_ren_wildcards(dos_rw):
    """``REN *.OLD *.NEW`` renames by extension wildcard."""
    from fat12 import FAT12
    dos_rw.create_file('P.OLD', 'renamed-via-glob')
    r = dos_rw.run_command('REN *.OLD *.NEW')
    assert not r.timed_out

    fat = FAT12(dos_rw.emu.disk)
    fat.mount()
    assert fat.find_file('P.OLD') is None
    ent = fat.find_file('P.NEW')
    assert ent is not None
    assert fat.read_file(ent.first_cluster, ent.size) == b'renamed-via-glob'
    dos_rw.run_command('DEL P.NEW')
