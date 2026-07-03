"""Phase C tool tests: drive B access (DIR B:, COPY B:->A:, prompt switching)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

pytestmark = [pytest.mark.slow, pytest.mark.tools]


def test_dir_b_lists_disk02(dos_b):
    r = dos_b.run_command('DIR B:', max_steps=6_000_000)
    assert not r.timed_out
    # DISK02 ships DEBUG.COM, TREE.COM, GWBASIC.EXE among 15 files.
    assert 'TREE' in r.output or 'DEBUG' in r.output
    assert 'File(s)' in r.output


def test_copy_from_b_to_a(dos_b):
    r = dos_b.run_command('COPY B:TREE.COM A:', max_steps=8_000_000)
    assert not r.timed_out
    assert '1 File(s) copied' in r.output
    r2 = dos_b.run_command('DIR TREE.COM')
    assert 'TREE' in r2.output


def test_drive_letter_switching(dos_b):
    r = dos_b.run_command('B:')
    assert not r.timed_out
    assert 'B>' in r.screen
    r = dos_b.run_command('DIR', probe_errorlevel=False)
    assert not r.timed_out
    r = dos_b.run_command('A:')
    assert not r.timed_out
    assert 'A>' in r.screen
