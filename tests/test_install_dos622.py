"""Fast tests for the MS-DOS 6.22 installation driver."""

from pathlib import Path

import pytest
import install_dos622 as installer

from install_dos622 import (
    InstallError, WELCOME_MARKER, _publish_atomic, choose_backend,
    classify_screen, install_dos622, resolve_media,
)
from dosharness import DOSHarness


def _media_set(path):
    for number in range(1, 4):
        image = path / f"Disk{number}.img"
        with image.open("wb") as output:
            output.truncate(1_474_560)


def _accept_test_media(monkeypatch, path):
    hashes = installer.file_hashes(
        [path / f"Disk{number}.img" for number in range(1, 4)])
    for name in installer.EXPECTED_MEDIA_SHA256:
        monkeypatch.setitem(
            installer.EXPECTED_MEDIA_SHA256, name,
            hashes[str(path / name)])


def test_resolve_media_requires_exact_retail_names(tmp_path, monkeypatch):
    _media_set(tmp_path)
    _accept_test_media(monkeypatch, tmp_path)
    media = resolve_media(tmp_path)
    assert [path.name for path in media.paths] == [
        "Disk1.img", "Disk2.img", "Disk3.img"]

    (tmp_path / "Disk2.img").unlink()
    with pytest.raises(InstallError, match="Disk2.img"):
        resolve_media(tmp_path)


def test_resolve_media_rejects_wrong_image_size(tmp_path, monkeypatch):
    _media_set(tmp_path)
    _accept_test_media(monkeypatch, tmp_path)
    (tmp_path / "Disk3.img").write_bytes(b"short")
    with pytest.raises(InstallError, match="1.44 MB"):
        resolve_media(tmp_path)


@pytest.mark.parametrize(("screen", "name", "disk"), [
    (WELCOME_MARKER, "enter", 0),
    ("Continue Setup and replace your current version of DOS.", "replace", 0),
    ("Configure unallocated disk space (recommended).", "enter", 0),
    ("Setup will place your MS-DOS files in C:\\DOS", "enter", 0),
    ("Setup will use the following system settings", "enter", 0),
    ("Insert Setup Disk 2 in drive A", "swap", 2),
    ("Insert Setup Disk 3 in drive A", "swap", 3),
    ("Setup will restart your computer now", "restart", 0),
    ("MS-DOS 6.22 is now installed", "complete", 0),
])
def test_classify_setup_screens(screen, name, disk):
    action = classify_screen(screen)
    assert action.name == name
    assert action.disk_number == disk


def test_classify_rejects_setup_error_and_command_prompt():
    assert classify_screen("Setup cannot continue").name == "error"
    assert classify_screen("some output\nA:\\>").name == "error"
    assert classify_screen("copying files, please wait") is None


def test_atomic_publish_replaces_destination(tmp_path):
    source = tmp_path / "working.hdd"
    destination = tmp_path / "installed.hdd"
    source.write_bytes(b"complete-image")
    destination.write_bytes(b"old")
    _publish_atomic(source, destination)
    assert destination.read_bytes() == b"complete-image"
    assert not list(tmp_path.glob(".installed.hdd.*.tmp"))


def test_installer_refuses_overwrite_before_touching_media(tmp_path):
    output = tmp_path / "existing.hdd"
    output.write_bytes(b"keep")
    with pytest.raises(InstallError, match="refusing to overwrite"):
        install_dos622(output, media_dir=tmp_path)
    assert output.read_bytes() == b"keep"


def test_explicit_backend_is_preserved():
    assert choose_backend("python") == "python"
    assert choose_backend("c") == "c"


def test_resolve_media_rejects_unknown_same_size_set(tmp_path):
    _media_set(tmp_path)
    with pytest.raises(InstallError, match="supported retail"):
        resolve_media(tmp_path)


def test_installer_rejects_nonpositive_step_budget(tmp_path):
    output = tmp_path / "new.hdd"
    with pytest.raises(InstallError, match="max_steps must be positive"):
        install_dos622(output, media_dir=tmp_path, max_steps=0)


@pytest.mark.parametrize("prompt", ["C>", "C:\\>", "C:\\DOS>"])
def test_harness_recognizes_dos_prompt_forms(prompt):
    harness = object.__new__(DOSHarness)
    harness.vga_text = lambda: [prompt]
    harness.vga_str = lambda: prompt
    assert harness._at_prompt("previous screen")
