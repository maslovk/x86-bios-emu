"""Phase E EXE2BIN / LINK (Tier 2).

EXE2BIN.EXE ships on DISK01 (A:); LINK.EXE ships on DISK02 (B:).  EXE2BIN with
no arguments prints usage and returns.  LINK is interactive: it prompts for
Object/Run/List/Libraries modules — the test drives the first prompts and
asserts the linker banner and first prompt appear (loads without crashing the
emulator), which is the Tier-2 bar.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

pytestmark = [pytest.mark.slow, pytest.mark.tools]


def test_exe2bin_usage(dos_rw):
    """EXE2BIN with no args prints usage and returns to the prompt."""
    r = dos_rw.run_command('EXE2BIN', max_steps=4_000_000, probe_errorlevel=False)
    assert not r.timed_out
    # A follow-up command must still work (no hang/crash).
    ok = dos_rw.run_command('ECHO alive', max_steps=2_000_000,
                            probe_errorlevel=False)
    assert not ok.timed_out
    assert 'alive' in ok.output


def test_link_loads_banner(dos_b):
    """B:LINK prints its banner and reaches the 'Object Modules' prompt."""
    r = dos_b.run_dialog('B:LINK',
                         [('Object Modules', 'NOFILE\r'),
                          ('Run File', '\r'),
                          ('List File', '\r')],
                         max_steps=5_000_000, probe_errorlevel=False)
    assert 'Personal Computer Linker' in r.output
    # The link object prompt must have appeared (LINK initialised its UI).
    assert 'Object Modules' in r.output
