"""Read-only FAT12 bridge for exposing a host directory to DOS."""

import re
import tempfile
import hashlib
from pathlib import Path

from fat12 import FAT12, FAT12Error, make_blank_image
from video import Disk


_SHORT_NAME = re.compile(r'^[A-Z0-9$%\-_~!#&()@^`{}\' ]{1,8}(\.[A-Z0-9$%\-_~!#&()@^`{}\' ]{1,3})?$')


def build_host_directory_disk(directory):
    """Build a read-only 1.44MB FAT12 disk containing regular root files."""
    root = Path(directory).resolve()
    if not root.is_dir():
        raise ValueError(f'host directory is not a directory: {directory}')

    def collect(path):
        children = []
        names = set()
        for child in sorted(path.iterdir(), key=lambda item: item.name.upper()):
            if child.is_symlink():
                raise ValueError(f'host directory contains unsupported symlink: {child.name}')
            name = child.name.upper()
            if not _SHORT_NAME.fullmatch(name) or name in names:
                raise ValueError(f'host entry is not a unique DOS 8.3 name: {child.name}')
            names.add(name)
            if child.is_dir():
                children.append((name, True, collect(child)))
            elif child.is_file():
                children.append((name, False, child.read_bytes()))
            else:
                raise ValueError(f'host directory contains unsupported entry: {child.name}')
        return children

    entries = collect(root)
    """Build the blank image, then populate files/directories."""

    with tempfile.NamedTemporaryFile(suffix='.img') as blank:
        make_blank_image(blank.name, size=1440 * 1024)
        raw = Path(blank.name).read_bytes()

    disk = Disk(2880)
    for index in range(2880):
        disk.sectors[index][:] = raw[index * 512:(index + 1) * 512]
    fat = FAT12(disk)
    fat.mount()
    def write_data(data):
        count = max(1, (len(data) + fat.cluster_size - 1) // fat.cluster_size)
        chain = fat._alloc_clusters(count)
        for index, cluster in enumerate(chain):
            chunk = data[index * fat.cluster_size:(index + 1) * fat.cluster_size]
            sector = fat._cluster_to_sector(cluster)
            for offset in range(fat.sectors_per_cluster):
                buf = bytearray(fat.bytes_per_sector)
                start = offset * fat.bytes_per_sector
                buf[:len(chunk[start:start + fat.bytes_per_sector])] = chunk[start:start + fat.bytes_per_sector]
                disk.write_sector(sector + offset, buf)
        return chain[0]

    def make_entry(name, is_dir, first, size):
        base, ext = fat._split_83(name)
        return fat._make_dir_entry(base, ext, first if is_dir or size else 0,
                                   size, 0x10 if is_dir else 0x20)

    def write_directory(items, parent_cluster=0):
        count = 2 + len(items)
        data = bytearray(max(1, (count * 32 + fat.cluster_size - 1) // fat.cluster_size) * fat.cluster_size)
        dot = fat._make_dir_entry('.', '', 0, 0, 0x10)
        dotdot = fat._make_dir_entry('..', '', parent_cluster, 0, 0x10)
        data[:32] = dot
        data[32:64] = dotdot
        cluster = write_data(data)
        for index, (name, is_dir, payload) in enumerate(items):
            if is_dir:
                child_cluster = write_directory(payload, cluster)
                entry = make_entry(name, True, child_cluster, 0)
            else:
                child_cluster = write_data(payload) if payload else 0
                entry = make_entry(name, False, child_cluster, len(payload))
            data[64 + index * 32:96 + index * 32] = entry
        for offset in range(0, len(data), fat.bytes_per_sector):
            disk.write_sector(fat._cluster_to_sector(cluster) + offset // fat.bytes_per_sector,
                              data[offset:offset + fat.bytes_per_sector])
        return cluster

    try:
        root_bytes = bytearray(fat.root_sectors * fat.bytes_per_sector)
        for index, (name, is_dir, payload) in enumerate(entries):
            if is_dir:
                first = write_directory(payload)
                entry = make_entry(name, True, first, 0)
            else:
                first = write_data(payload) if payload else 0
                entry = make_entry(name, False, first, len(payload))
            root_bytes[index * 32:(index + 1) * 32] = entry
        for offset in range(0, len(root_bytes), fat.bytes_per_sector):
            disk.write_sector(fat.root_start + offset // fat.bytes_per_sector,
                              root_bytes[offset:offset + fat.bytes_per_sector])
    except FAT12Error as exc:
        raise ValueError(f'host directory does not fit in FAT12 image: {exc}') from exc
    disk.dirty = False
    disk.read_only = True
    disk.host_directory = str(root)
    return disk


def sync_host_directory_disk(disk, directory, baseline=None):
    """Write guest-visible regular files back into an existing host folder."""
    root = Path(directory).resolve()
    fat = FAT12(disk)
    fat.mount()
    changed = []
    conflicts = []

    def sync_dir(cluster, target):
        target.mkdir(parents=True, exist_ok=True)
        for entry in fat.read_dir(cluster):
            if entry.name in ('.', '..') or entry.deleted:
                continue
            path = target / entry.full_name
            if entry.is_dir:
                sync_dir(entry.first_cluster, path)
            else:
                data = fat.read_file(entry.first_cluster, entry.size)
                relative = str(path.relative_to(root))
                if (baseline is not None and relative in baseline and path.exists()
                        and hashlib.sha256(path.read_bytes()).hexdigest() != baseline[relative]
                        and path.read_bytes() != data):
                    conflicts.append(relative)
                    continue
                if not path.exists() or path.read_bytes() != data:
                    path.write_bytes(data)
                    changed.append(str(path))

    for entry in fat.read_root_directory():
        if entry.deleted:
            continue
        path = root / entry.full_name
        if entry.is_dir:
            sync_dir(entry.first_cluster, path)
        else:
            data = fat.read_file(entry.first_cluster, entry.size)
            relative = str(path.relative_to(root))
            if (baseline is not None and relative in baseline and path.exists()
                    and hashlib.sha256(path.read_bytes()).hexdigest() != baseline[relative]
                    and path.read_bytes() != data):
                conflicts.append(relative)
                continue
            if not path.exists() or path.read_bytes() != data:
                path.write_bytes(data)
                changed.append(str(path))
    return (changed, conflicts) if baseline is not None else changed


def snapshot_host_directory(directory):
    """Return relative-file SHA-256 hashes for write-back conflict checks."""
    root = Path(directory).resolve()
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob('*')
        if path.is_file() and not path.is_symlink()
    }


def audit_host_directory_deletions(disk, directory):
    """Return host files that guest FAT no longer references (never delete)."""
    root = Path(directory).resolve()
    fat = FAT12(disk)
    fat.mount()
    guest_files = set()

    def collect(cluster, relative):
        for entry in fat.read_dir(cluster):
            if entry.name in ('.', '..') or entry.deleted:
                continue
            child = relative / entry.full_name
            if entry.is_dir:
                collect(entry.first_cluster, child)
            else:
                guest_files.add(child)

    for entry in fat.read_root_directory():
        if entry.deleted:
            continue
        child = Path(entry.full_name)
        if entry.is_dir:
            collect(entry.first_cluster, child)
        else:
            guest_files.add(child)

    host_files = {path.relative_to(root) for path in root.rglob('*')
                  if path.is_file() and not path.is_symlink()}
    return sorted(str(path) for path in host_files - guest_files)


def delete_missing_host_files(disk, directory):
    """Delete host files absent from guest FAT; caller must explicitly opt in."""
    root = Path(directory).resolve()
    removed = audit_host_directory_deletions(disk, root)
    for relative in removed:
        (root / relative).unlink()
    return removed
