"""Phase D/E EDLIN — currently xfail (insert-mode termination).

EDLIN enters insert mode on ``1i`` and accepts lines terminated by CR; to end
insert mode it expects a Ctrl-C (0x03) or Ctrl-Break.  The harness injects the
raw 0x03 byte directly into the keyboard buffer (Phase D control-char path),
but EDLIN reads its input through DOS console functions that intercept Ctrl-C
as the break character (INT 23h) before EDLIN sees it as a plain end-of-insert
byte, so insert mode never terminates and the edited lines are never written
to disk.  Flipping this needs the Ctrl-Break → INT 1Bh/23h break-injection
semantics from Phase D item 2 fully wired through the keyboard controller /
DOS console input path.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from fat12 import FAT12  # noqa: E402

pytestmark = [pytest.mark.slow, pytest.mark.tools]


@pytest.mark.xfail(strict=True, reason='EDLIN insert-mode needs Ctrl-C to fire '
                                       'INT 23h break semantics through DOS '
                                       'console input (Phase D item 2)')
def test_edlin_insert_save(dos_rw):
    dos_rw.run_command('DEL NEW.TXT', probe_errorlevel=False)
    dos_rw.inject_string('EDLIN NEW.TXT\r')
    dos_rw.run_steps(120000)
    dos_rw.inject_string('1i\r')
    dos_rw.run_steps(60000)
    dos_rw.inject_string('line one\rline two\r')
    dos_rw.run_steps(60000)
    dos_rw.inject_string('\x03')       # Ctrl-C ends insert mode
    dos_rw.run_steps(120000)
    dos_rw.inject_string('e\r')         # e = end + save
    dos_rw.run_steps(400000)

    fat = FAT12(dos_rw.emu.disk)
    fat.mount()
    data = fat.read_file_by_name('NEW.TXT')
    assert data is not None
    assert b'line one' in data and b'line two' in data
