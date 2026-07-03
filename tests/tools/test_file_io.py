"""Phase B tool tests: guest file operations verified host-side via FAT12.

Each test runs the real DOS command and then mounts the in-memory disk with
:class:`fat12.FAT12` to confirm the guest write (INT 13h AH=03) produced what
DOS reported — the two-sided truth check.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from fat12 import FAT12  # noqa: E402

pytestmark = [pytest.mark.slow, pytest.mark.tools]


def _mount(dos_rw):
    fat = FAT12(dos_rw.emu.disk)
    fat.mount()
    return fat


def test_copy_con_roundtrip_host_verified(dos_rw):
    dos_rw.create_file('SRC.TXT', 'hello world')
    r = dos_rw.run_command('TYPE SRC.TXT')
    assert 'hello world' in r.screen

    fat = _mount(dos_rw)
    assert fat.read_file_by_name('SRC.TXT') == b'hello world'


def test_copy_file_to_file_host_verified(dos_rw):
    dos_rw.create_file('A.TXT', 'aaa-content')
    r = dos_rw.run_command('COPY A.TXT B.TXT')
    assert not r.timed_out
    assert '1 File(s) copied' in r.output

    r = dos_rw.run_command('TYPE B.TXT')
    assert 'aaa-content' in r.screen

    fat = _mount(dos_rw)
    assert fat.read_file_by_name('B.TXT') == b'aaa-content'


def test_del_removes_file_host_verified(dos_rw):
    dos_rw.create_file('GONE.TXT', 'bye')
    dos_rw.run_command('DEL GONE.TXT')
    r = dos_rw.run_command('DIR GONE.TXT')
    assert 'File not found' in r.output

    fat = _mount(dos_rw)
    assert fat.find_file('GONE.TXT') is None


def test_ren_preserves_content_host_verified(dos_rw):
    dos_rw.create_file('OLD.TXT', 'renamed-body')
    dos_rw.run_command('REN OLD.TXT NEW.TXT')
    r = dos_rw.run_command('TYPE NEW.TXT')
    assert 'renamed-body' in r.screen

    fat = _mount(dos_rw)
    assert fat.find_file('OLD.TXT') is None
    ent = fat.find_file('NEW.TXT')
    assert ent is not None
    assert fat.read_file(ent.first_cluster, ent.size) == b'renamed-body'


def test_mkdir_cddir_rd_refusal_and_success(dos_rw):
    dos_rw.run_command('MD SUB')
    r = dos_rw.run_command('DIR SUB')
    assert '<DIR>' in r.output          # SUB appears as a directory

    # Host side: the SUB directory entry is a dir with . and ..
    fat = _mount(dos_rw)
    sub = fat.find_file('SUB')
    assert sub is not None and sub.is_dir
    names = {e.full_name for e in fat.read_dir(sub.first_cluster)}
    assert '.' in names and '..' in names

    # Put a file inside SUB, then RD must refuse (non-empty).
    dos_rw.run_command('CD SUB')
    dos_rw.create_file('F.TXT', 'inside-sub')
    dos_rw.run_command('CD \\')
    r = dos_rw.run_command('RD SUB')
    assert 'not empty' in r.output      # refused

    dos_rw.run_command('DEL SUB\\F.TXT')
    r = dos_rw.run_command('RD SUB')     # now empty -> succeeds silently
    assert not r.timed_out
    fat2 = _mount(dos_rw)
    assert fat2.find_file('SUB') is None
