"""Phase E internal-command basics: VER/VOL/CLS/ECHO/SET/PATH/DATE/TIME.

Read-only / settable-state internal commands driven via the module-scoped
read-only `dos` fixture.  DATE/TIME prompt for a new value and are answered
through run_dialog.  (PROMPT is exercised indirectly: changing the prompt
marker would break run_command's A>/B>/C> detection, so it is left to a
later, prompt-marker-flexible pass.)
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

pytestmark = [pytest.mark.slow, pytest.mark.tools]


def test_ver(dos):
    r = dos.run_command('VER')
    assert not r.timed_out
    assert 'Version 3.30' in r.output
    assert r.errorlevel == 0


def test_vol(dos):
    r = dos.run_command('VOL')
    assert not r.timed_out
    assert 'Volume in drive A' in r.output


def test_cls_clears_screen(dos):
    r = dos.run_command('CLS', probe_errorlevel=False)
    assert not r.timed_out
    # The boot banner must be gone after CLS leaves only the prompt.
    assert 'MS-DOS' not in r.screen
    assert 'A>' in r.screen


def test_echo(dos):
    r = dos.run_command('ECHO MarkerLine42')
    assert not r.timed_out
    assert 'MarkerLine42' in r.output


def test_set_and_query_path(dos):
    r = dos.run_command('SET PATH=A:\\')
    assert not r.timed_out and r.errorlevel == 0
    r = dos.run_command('PATH')
    assert not r.timed_out
    assert 'A:\\' in r.output


def test_date_and_time_prompts_answered(dos):
    r = dos.run_dialog('DATE', [('Enter new date', '\r')], max_steps=3_000_000)
    assert not r.timed_out
    assert 'Current date' in r.output
    r = dos.run_dialog('TIME', [('Enter new time', '\r')], max_steps=3_000_000)
    assert not r.timed_out
    assert 'Current time' in r.output
