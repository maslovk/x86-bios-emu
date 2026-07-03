"""Phase E text tools: FIND, SORT, MORE, COMP, FC.

Redirection (``<`` and ``>``) is supported by this COMMAND.COM build; pipes
(``|``) are not ("Invalid parameter"), so every pipeline is expressed with
explicit redirection.  FC.EXE ships on DISK02, hence the ``dos_b`` fixture and
the ``B:FC`` invocation; FIND/SORT/MORE/COM run from A: (DISK01).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

pytestmark = [pytest.mark.slow, pytest.mark.tools]


def test_sort_ascending(dos_rw):
    """``SORT < IN.TXT`` emits lines in ascending order."""
    dos_rw.create_file('IN.TXT', 'cherry\r\napple\r\nbanana\r\n')
    r = dos_rw.run_command('SORT < IN.TXT', max_steps=4_000_000)
    assert not r.timed_out
    # Consider only the command's own output (after the 'A>SORT' prompt that
    # launched it), not the COPY CON scrollback which contains the unsorted
    # source lines.
    run_out = r.output.split('A>SORT')[-1]
    assert run_out.index('apple') < run_out.index('banana') < run_out.index('cherry')


def test_sort_redirect_to_file(dos_rw):
    """``SORT < IN > OUT`` writes the sorted result to a file (host-verified)."""
    from fat12 import FAT12
    dos_rw.create_file('IN.TXT', 'zebra\r\napple\r\n')
    r = dos_rw.run_command('SORT < IN.TXT > OUT.TXT', max_steps=4_000_000)
    assert not r.timed_out
    fat = FAT12(dos_rw.emu.disk)
    fat.mount()
    data = fat.read_file_by_name('OUT.TXT')
    assert data is not None
    text = data.decode('ascii', errors='replace')
    assert text.index('apple') < text.index('zebra')
    dos_rw.run_command('DEL IN.TXT', probe_errorlevel=False)
    dos_rw.run_command('DEL OUT.TXT', probe_errorlevel=False)


def test_find_counts_lines(dos_rw):
    """``FIND "x" FILE`` reports matching line count and echoes matches."""
    dos_rw.create_file('F.TXT', 'alpha\r\nbravo\r\nalpha\r\ncharlie\r\n')
    r = dos_rw.run_command('FIND "alpha" F.TXT', max_steps=4_000_000)
    assert not r.timed_out
    assert 'F.TXT' in r.output             # ---------- F.TXT header
    assert r.output.count('alpha') >= 2    # two matching lines
    dos_rw.run_command('DEL F.TXT', probe_errorlevel=False)


def test_find_case_switch(dos_rw):
    """``FIND /I`` matches case-insensitively."""
    dos_rw.create_file('F.TXT', 'Hello\r\nHELLO\r\nworld\r\n')
    r = dos_rw.run_command('FIND /I "hello" F.TXT', max_steps=4_000_000)
    assert not r.timed_out
    # Both Hello and HELLO match under /I.
    assert r.output.count('ello') >= 2
    dos_rw.run_command('DEL F.TXT', probe_errorlevel=False)


def test_more_paginates(dos_rw):
    """MORE pauses at "-- More --" and resumes on a space."""
    body = ''.join(f'line{n}\r\n' for n in range(1, 31))
    dos_rw.create_file('LONG.TXT', body)
    r = dos_rw.run_dialog('MORE < LONG.TXT', [('-- More --', ' \r')],
                          max_steps=6_000_000, probe_errorlevel=False)
    assert not r.timed_out
    assert '-- More --' in r.output
    assert 'line30' in r.output
    dos_rw.run_command('DEL LONG.TXT', probe_errorlevel=False)


def test_comp_matching_files(dos_rw):
    """COMP of two identical small files reports 'Files compare ok'."""
    dos_rw.create_file('A.TXT', 'hello\r\n')
    dos_rw.create_file('B.TXT', 'hello\r\n')
    r = dos_rw.run_dialog('COMP', [('Enter primary file name', 'A.TXT\r'),
                                  ('Enter 2nd file name or drive id', 'B.TXT\r'),
                                  ('Compare more files', 'N\r')],
                          max_steps=6_000_000)
    assert not r.timed_out
    assert 'compare ok' in r.output.lower()
    dos_rw.run_command('DEL A.TXT', probe_errorlevel=False)
    dos_rw.run_command('DEL B.TXT', probe_errorlevel=False)


def test_comp_differing_files_reports_difference(dos_rw):
    """COMP of differing files reports the mismatch offset and bytes."""
    dos_rw.create_file('A.TXT', 'abc\r\n')
    dos_rw.create_file('B.TXT', 'abd\r\n')
    r = dos_rw.run_dialog('COMP', [('Enter primary file name', 'A.TXT\r'),
                                  ('Enter 2nd file name or drive id', 'B.TXT\r'),
                                  ('Compare more files', 'N\r')],
                          max_steps=6_000_000)
    assert not r.timed_out
    out = r.output.lower()
    assert 'compare error' in out
    # 'c' (0x63) vs 'd' (0x64) at the differing offset.
    assert '63' in r.output and '64' in r.output
    dos_rw.run_command('DEL A.TXT', probe_errorlevel=False)
    dos_rw.run_command('DEL B.TXT', probe_errorlevel=False)


def test_fc_identical(dos_b):
    """B:FC of identical files reports 'no differences encountered'."""
    dos_b.create_file('A.TXT', 'same\r\n')
    dos_b.create_file('B.TXT', 'same\r\n')
    r = dos_b.run_command('B:FC A.TXT B.TXT', max_steps=6_000_000)
    assert not r.timed_out
    assert 'no differences' in r.output.lower()


def test_fc_differing(dos_b):
    """B:FC of differing files reports a difference."""
    dos_b.create_file('A.TXT', 'line1\r\nabc\r\nline3\r\n')
    dos_b.create_file('B.TXT', 'line1\r\nabd\r\nline3\r\n')
    r = dos_b.run_command('B:FC A.TXT B.TXT', max_steps=6_000_000)
    assert not r.timed_out
    assert 'abc' in r.output and 'abd' in r.output
