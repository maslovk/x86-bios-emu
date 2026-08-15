"""Phase F EDLIN insert/save regression.

EDLIN enters insert mode on ``1i`` and accepts lines terminated by CR; to end
insert mode it expects Ctrl-C (0x03).  The harness's exact-byte keyboard path
delivers that character successfully.  The former failure happened earlier:
a memory shift decoded its displacement twice, skipped into the following
CALL operand, and sent EDLIN into zero-filled memory.  The CPU regression is
covered by ``tests/test_shift_flags.py``.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from fat12 import FAT12  # noqa: E402

pytestmark = [pytest.mark.slow, pytest.mark.tools]


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
