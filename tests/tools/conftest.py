"""Shared fixtures for the DOS tool integration suite.

``dos`` boots DISK01 once per test *module* and is shared by read-only tests;
``dos_rw`` boots a fresh writable temp copy per test for write scenarios.
Both are slow (a real MS-DOS cold boot).  Mark every tool test with
``@pytest.mark.slow`` and ``@pytest.mark.tools``.
"""
import hashlib
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from dosharness import DOSHarness, DISK01  # noqa: E402

_REPO_IMG_DIR = os.path.join(
    os.path.dirname(__file__), '..', '..', 'DOS3_3_525')


def _image_hashes():
    """Map of image basename → sha256 for every .IMG under DOS3_3_525/."""
    hashes = {}
    img_dir = os.path.abspath(_REPO_IMG_DIR)
    for name in sorted(os.listdir(img_dir)):
        if name.upper().endswith('.IMG'):
            path = os.path.join(img_dir, name)
            h = hashlib.sha256()
            with open(path, 'rb') as f:
                for chunk in iter(lambda: f.read(65536), b''):
                    h.update(chunk)
            hashes[name] = h.hexdigest()
    return hashes


@pytest.fixture(scope='module')
def dos():
    """Booted read-only DISK01 harness, shared across one test module."""
    h = DOSHarness(image_path=DISK01, writable=False)
    try:
        h.boot_to_prompt()
        yield h
    finally:
        h.cleanup()


@pytest.fixture
def dos_rw():
    """Fresh writable DISK01 harness (temp image copy) per test."""
    h = DOSHarness(image_path=DISK01, writable=True)
    try:
        h.boot_to_prompt()
        yield h
    finally:
        h.cleanup()


@pytest.fixture(scope='session', autouse=True)
def _guard_repo_images(request):
    """Fail the session if any shipped DOS image was mutated by a test.

    Records the sha256 of every ``DOS3_3_525/*.IMG`` at session start and
    re-checks at session end.  This is the ground-rule backstop: tests must
    always work on in-memory or temp copies and never write the repo images.
    """
    before = _image_hashes()
    yield
    after = _image_hashes()
    if before != after:
        diffs = []
        for name in sorted(set(before) | set(after)):
            if before.get(name) != after.get(name):
                diffs.append(f"{name}: {before.get(name)} -> {after.get(name)}")
        pytest.fail(
            "Shipped DOS images were mutated during the test session:\n  "
            + "\n  ".join(diffs))
