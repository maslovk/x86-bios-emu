"""Fast unit tests for FAT12 host-side write support (Phase B feature 1)."""
import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from fat12 import FAT12, FAT12Error, DirEntry
from fat12helpers import build_floppy, FakeDisk


def _mount(files=None):
    disk = FakeDisk(bytearray(build_floppy(files)))
    fat = FAT12(disk)
    fat.mount()
    return disk, fat


def test_write_read_roundtrip():
    _, fat = _mount()
    fat.write_file('A.TXT', b'hello')
    assert fat.read_file_by_name('A.TXT') == b'hello'


def test_write_two_cluster_chain():
    _, fat = _mount()
    data = bytes(i & 0xFF for i in range(1024))  # 1024 B > one 512 B cluster
    fat.write_file('BIG.BIN', data)
    entry = fat.find_file('BIG.BIN')
    assert entry.size == 1024
    chain = fat.follow_chain(entry.first_cluster)
    assert len(chain) == 2
    assert chain[1] == chain[0] + 1      # contiguous first-fit allocation
    assert fat.read_file(entry.first_cluster, entry.size) == data


def test_fat_mirror_equality_after_write():
    disk, fat = _mount()
    fat.write_file('C.DAT', b'x' * 1200)
    bps = fat.bytes_per_sector
    spf = fat.sectors_per_fat
    fat1 = bytearray(disk.data[fat.fat_start * bps:(fat.fat_start + spf) * bps])
    f2 = fat.fat_start + spf
    fat2 = bytearray(disk.data[f2 * bps:(f2 + spf) * bps])
    assert fat1 == fat2                   # both FAT copies kept in sync


def test_directory_entry_fields():
    _, fat = _mount()
    fat.write_file('HELLO.TXT', b'world!')
    e = fat.find_file('HELLO.TXT')
    assert e.name == 'HELLO'
    assert e.ext == 'TXT'
    assert e.size == 6
    assert e.first_cluster == 2
    assert e.attributes & DirEntry.ATTR_ARCHIVE
    assert not e.is_dir


def test_delete_then_recreate_reuses_clusters():
    _, fat = _mount()
    fat.write_file('A.TXT', b'A' * 1024)        # clusters 2,3
    a_entry = fat.find_file('A.TXT')
    assert fat.delete_file('A.TXT') is True
    # Clusters are free again.
    assert fat.get_fat_entry(a_entry.first_cluster) == 0
    fat.write_file('B.TXT', b'B' * 1024)
    b_entry = fat.find_file('B.TXT')
    assert b_entry.first_cluster == a_entry.first_cluster   # reused
    assert fat.read_file(b_entry.first_cluster, b_entry.size) == b'B' * 1024


def test_replace_existing_file_frees_old_chain():
    _, fat = _mount()
    fat.write_file('R.TXT', b'old-data-that-is-long' * 4)   # multi-cluster
    old = fat.find_file('R.TXT')
    fat.write_file('R.TXT', b'short')                       # 1 cluster
    new = fat.find_file('R.TXT')
    assert new.size == 5
    # The old chain's clusters must be free again (except the reused first).
    nxt = fat.get_fat_entry(new.first_cluster)
    assert nxt == fat.FAT12_EOC                            # short = single cluster
    assert fat.read_file_by_name('R.TXT') == b'short'


def test_disk_full_raises():
    _, fat = _mount()
    free = fat.free_cluster_count()
    # Need one more cluster than exists -> allocation must fail cleanly.
    with pytest.raises(FAT12Error):
        fat.write_file('FULL.BIN', b'\x00' * ((free + 1) * fat.cluster_size))
    # Failure must not have partially allocated (disk still empty of files).
    assert fat.find_file('FULL.BIN') is None
    assert fat.free_cluster_count() == free


def test_read_dir_parses_subdir_chain():
    # A subdirectory's data is a sequence of 32-byte entries across its
    # clusters; build one manually and confirm read_dir parses it.
    disk, fat = _mount()
    # Allocate 2 clusters for a fake subdir at cluster 2, link 2->3->EOC.
    fat.set_fat_entry(2, 3)
    fat.set_fat_entry(3, fat.FAT12_EOC)
    buf = bytearray(fat.cluster_size * 2)
    # Two entries: a file and an end-of-dir marker.
    buf[0:11] = b'CHILD   TXT'
    buf[11] = 0x20
    buf[26:28] = (4).to_bytes(2, 'little')
    buf[28:32] = (5).to_bytes(4, 'little')
    buf[32] = 0x00  # end-of-directory
    sector = fat._cluster_to_sector(2)
    for j in range(fat.sectors_per_cluster * 2):
        disk.write_sector(sector + j, buf[j * fat.bytes_per_sector:(j + 1) * fat.bytes_per_sector])
    entries = fat.read_dir(2)
    names = [e.full_name for e in entries]
    assert 'CHILD.TXT' in names
