"""Phase F fast differential smoke check.

Runs the checked-in snapshot trigger (snapshot.bin + snapshot.regs) through
diff_trace.py's lockstep (our CPU vs Unicorn) for a few thousand instructions
and asserts zero register/flag divergences.  This is the SDM-correctness
backstop: any CPU change that silently breaks instruction semantics fails
here before a slow DOS tool test would.  Skipped if unicorn is unavailable or
the checked-in snapshot artifacts are missing.
"""
import os
import subprocess
import sys

import pytest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(THIS_DIR, '..'))
SNAPSHOT = os.path.join(REPO, 'snapshot.bin')
REGS = os.path.join(REPO, 'snapshot.regs')
DIFF_TRACE = os.path.join(REPO, 'diff_trace.py')

pytestmark = pytest.mark.tools  # no DOS boot; runs a few thousand CPU steps (~1s)


def _have_unicorn():
    try:
        import unicorn  # noqa: F401
        return True
    except Exception:
        return False


def test_lockstep_snapshot_no_divergence():
    if not _have_unicorn():
        pytest.skip("unicorn not installed")
    if not (os.path.exists(SNAPSHOT) and os.path.exists(REGS)):
        pytest.skip("snapshot.bin / snapshot.regs not present")
    env = dict(os.environ, DIFF_STEPS='5000', PYTHONPATH=REPO)
    # Run from the repo root so diff_trace.py finds snapshot.bin / the emulator.
    proc = subprocess.run(
        [sys.executable, DIFF_TRACE],
        cwd=REPO, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        timeout=180, text=True)
    out = proc.stdout
    assert 'FIRST DIVERGENCE' not in out, \
        "CPU diverged from Unicorn on the snapshot trace:\n" + out[-2000:]
    assert 'trace ended at step' in out, \
        "diff_trace did not complete its run:\n" + out[-2000:]
