"""Phase E Tier-3 tools: must fail *gracefully* (clean error / prompt returns,
no emulator crash or hang).  KEYB / NLSFUNC / DISPLAY / GRAPHICS (codepage /
printer hardware) either print an error or load a driver and return to the
prompt; SELECT prompts Y/N and is declined. FDISK is covered functionally in
``test_fdisk.py`` when a temporary hard disk is attached.

All loaded in one fresh writable session (small resident footprints); the
guard is that each returns to ``A>`` *and* a follow-up command still runs.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

pytestmark = [pytest.mark.slow, pytest.mark.tools]

# Tools that, with no (or harmless) arguments, return cleanly to the prompt.
_RETURNING = ['KEYB', 'NLSFUNC', 'DISPLAY', 'GRAPHICS']


def test_tier3_tools_return_gracefully(dos_rw):
    """Each Tier-3 tool returns to the prompt; the system stays responsive."""
    for cmd in _RETURNING:
        r = dos_rw.run_command(cmd, max_steps=4_000_000, probe_errorlevel=False)
        assert not r.timed_out, f'{cmd!r} did not return to the prompt'
        # And a follow-up command must still work (no hung/crashed emulator).
        ok = dos_rw.run_command('ECHO alive', max_steps=2_000_000,
                                probe_errorlevel=False)
        assert not ok.timed_out
        assert 'alive' in ok.output


def test_select_declined_returns(dos_rw):
    """SELECT prompts 'Do you want to continue (Y/N)?'; declining returns to A>."""
    r = dos_rw.run_dialog('SELECT country=001 keyboard=us',
                          [('continue (Y/N)', 'N\r')],
                          max_steps=4_000_000, probe_errorlevel=False)
    assert not r.timed_out
    ok = dos_rw.run_command('ECHO alive', max_steps=2_000_000,
                            probe_errorlevel=False)
    assert not ok.timed_out
    assert 'alive' in ok.output
