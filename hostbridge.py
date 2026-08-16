"""Read-only FAT12 bridge for exposing a host directory to DOS."""

import re
import tempfile
from pathlib import Path

from fat12 import FAT12, FAT12Error, make_blank_image
from video import Disk


_SHORT_NAME = re.compile(r'^[A-Z0-9$%\-_~!#&()@^`{}\' ]{1,8}(\.[A-Z0-9$%\-_~!#&()@^`{}\' ]{1,3})?$')


def build_host_directory_disk(directory):
    """Build a read-only 1.44MB FAT12 disk containing regular root files."""
    root = Path(directory).resolve()
    if not root.is_dir():
        raise ValueError(f'host directory is not a directory: {directory}')

    entries = []
    names = set()
    for path in sorted(root.iterdir(), key=lambda item: item.name.upper()):
        if path.is_symlink():
            raise ValueError(f'host directory contains unsupported symlink: {path.name}')
        if path.is_dir():
            raise ValueError(f'host directory contains unsupported subdirectory: {path.name}')
        if not path.is_file():
            raise ValueError(f'host directory contains unsupported entry: {path.name}')
        name = path.name.upper()
        if not _SHORT_NAME.fullmatch(name) or name in names:
            raise ValueError(f'host file is not a unique DOS 8.3 name: {path.name}')
        names.add(name)
        entries.append((name, path.read_bytes()))

    with tempfile.NamedTemporaryFile(suffix='.img') as blank:
        make_blank_image(blank.name, size=1440 * 1024)
        raw = Path(blank.name).read_bytes()

    disk = Disk(2880)
    for index in range(2880):
        disk.sectors[index][:] = raw[index * 512:(index + 1) * 512]
    fat = FAT12(disk)
    fat.mount()
    try:
        for name, data in entries:
            fat.write_file(name, data)
    except FAT12Error as exc:
        raise ValueError(f'host directory does not fit in FAT12 image: {exc}') from exc
    disk.dirty = False
    disk.read_only = True
    disk.host_directory = str(root)
    return disk
