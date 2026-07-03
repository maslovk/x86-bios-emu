"""Phase E ATTRIB / LABEL / VOL.

ATTRIB +R/-R toggles the read-only attribute (host-verified) and makes DEL
refuse; LABEL writes the volume label (host-verified + VOL round-trip).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from fat12 import FAT12, DirEntry  # noqa: E402

pytestmark = [pytest.mark.slow, pytest.mark.tools]


def _mount(dos_rw):
    fat = FAT12(dos_rw.emu.disk)
    fat.mount()
    return fat


def _volume_label(fat):
    """Return the root volume-label entry's name, or None."""
    for e in fat.list_root():
        if e.attributes & DirEntry.ATTR_VOLUME_ID:
            return e.full_name
    return None


def test_attrib_set_and_clear_readonly(dos_rw):
    """ATTRIB +R sets the read-only bit; ATTRIB -R clears it (host-verified)."""
    dos_rw.create_file('Z.TXT', 'ro-body')
    dos_rw.run_command('ATTRIB +R Z.TXT', max_steps=3_000_000)
    r = dos_rw.run_command('ATTRIB Z.TXT', max_steps=3_000_000)
    assert not r.timed_out
    assert 'R' in r.output            # the 'R' attribute marker is shown

    fat = _mount(dos_rw)
    ent = fat.find_file('Z.TXT')
    assert ent is not None and (ent.attributes & DirEntry.ATTR_READ_ONLY)

    dos_rw.run_command('ATTRIB -R Z.TXT', max_steps=3_000_000)
    fat2 = _mount(dos_rw)
    ent2 = fat2.find_file('Z.TXT')
    assert ent2 is not None and not (ent2.attributes & DirEntry.ATTR_READ_ONLY)
    dos_rw.run_command('DEL Z.TXT', probe_errorlevel=False)


def test_readonly_blocks_del(dos_rw):
    """A read-only file cannot be deleted; clearing the bit allows it."""
    dos_rw.create_file('Z.TXT', 'ro-body')
    dos_rw.run_command('ATTRIB +R Z.TXT', max_steps=3_000_000)
    dos_rw.run_command('DEL Z.TXT', max_steps=3_000_000)
    # The file must still exist (delete refused on read-only).
    fat = _mount(dos_rw)
    assert fat.find_file('Z.TXT') is not None

    dos_rw.run_command('ATTRIB -R Z.TXT', max_steps=3_000_000)
    dos_rw.run_command('DEL Z.TXT', max_steps=3_000_000)
    fat2 = _mount(dos_rw)
    assert fat2.find_file('Z.TXT') is None


def test_label_roundtrip(dos_rw):
    """LABEL A:NAME writes the label; VOL reports it; host-side FAT12 sees it."""
    r = dos_rw.run_command('LABEL A:TESTDISK', max_steps=3_000_000)
    assert not r.timed_out
    r = dos_rw.run_command('VOL', max_steps=3_000_000)
    assert not r.timed_out
    assert 'TESTDISK' in r.output

    fat = _mount(dos_rw)
    label = _volume_label(fat)
    assert label is not None and 'TESTDISK' in label


def test_vol_default_label(dos):
    """VOL on the stock image reports the original distribution label."""
    r = dos.run_command('VOL', max_steps=3_000_000)
    assert not r.timed_out
    assert 'Volume in drive A' in r.output
