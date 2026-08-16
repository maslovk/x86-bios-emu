"""Fast host-side FAT16 filesystem coverage."""

import pytest

from fat12 import FAT12Error, FAT16
from video import Disk, DiskView


SECTOR_SIZE = 512
TOTAL_SECTORS = 5000
SECTORS_PER_FAT = 20
ROOT_ENTRIES = 16
DATA_START = 1 + 2 * SECTORS_PER_FAT + 1


def _fat16_disk():
    disk = Disk(TOTAL_SECTORS)
    boot = disk.sectors[0]
    boot[0:3] = b'\xeb\x3c\x90'
    boot[3:11] = b'TESTF16 '
    boot[11:13] = SECTOR_SIZE.to_bytes(2, 'little')
    boot[13] = 1
    boot[14:16] = (1).to_bytes(2, 'little')
    boot[16] = 2
    boot[17:19] = ROOT_ENTRIES.to_bytes(2, 'little')
    boot[19:21] = TOTAL_SECTORS.to_bytes(2, 'little')
    boot[21] = 0xF8
    boot[22:24] = SECTORS_PER_FAT.to_bytes(2, 'little')
    boot[24:26] = (17).to_bytes(2, 'little')
    boot[26:28] = (4).to_bytes(2, 'little')
    boot[510:512] = b'\x55\xaa'

    fat = bytearray(SECTORS_PER_FAT * SECTOR_SIZE)
    fat[0:2] = (0xFFF8).to_bytes(2, 'little')
    fat[2:4] = (0xFFFF).to_bytes(2, 'little')
    fat[4:6] = (3).to_bytes(2, 'little')
    fat[6:8] = (0xFFFF).to_bytes(2, 'little')
    for copy_start in (1, 1 + SECTORS_PER_FAT):
        for index in range(SECTORS_PER_FAT):
            start = index * SECTOR_SIZE
            disk.sectors[copy_start + index][:] = fat[start:start + SECTOR_SIZE]

    payload = b'A' * 512 + b'fat16-second-cluster'
    entry = disk.sectors[1 + 2 * SECTORS_PER_FAT]
    entry[0:11] = b'CHAIN   TXT'
    entry[11] = 0x20
    entry[26:28] = (2).to_bytes(2, 'little')
    entry[28:32] = len(payload).to_bytes(4, 'little')
    disk.sectors[DATA_START][:] = payload[:512]
    disk.sectors[DATA_START + 1][:len(payload) - 512] = payload[512:]
    return disk, payload


def test_mount_and_read_fat16_chain():
    disk, payload = _fat16_disk()
    fat = FAT16(disk).mount()

    assert fat.info()['geom_label'] == 'FAT16 BPB'
    assert fat.total_clusters == TOTAL_SECTORS - DATA_START
    assert fat.follow_chain(2) == [2, 3]
    assert fat.read_file_by_name('CHAIN.TXT') == payload

    last_cluster = fat.total_clusters + 1
    fat.set_fat_entry(last_cluster, 0xFFFF)
    assert fat.follow_chain(last_cluster) == [last_cluster]


def test_fat16_host_write_mirrors_fats_and_roundtrips_through_view():
    partition, _payload = _fat16_disk()
    parent = Disk(TOTAL_SECTORS + 17)
    parent.sectors[17:] = partition.sectors
    view = DiskView(parent, 17, TOTAL_SECTORS)

    fat = FAT16(view).mount()
    written = b'host-fat16-write' * 80
    first = fat.write_file('HOST.TXT', written)

    fresh = FAT16(view).mount()
    assert fresh.read_file_by_name('HOST.TXT') == written
    offset = first * 2
    fat1 = 1 * SECTOR_SIZE + offset
    fat2 = (1 + SECTORS_PER_FAT) * SECTOR_SIZE + offset
    raw = b''.join(parent.sectors[17:17 + TOTAL_SECTORS])
    assert raw[fat1:fat1 + 2] == raw[fat2:fat2 + 2]
    assert parent.dirty


def test_fat16_rejects_fat12_cluster_count():
    disk, _payload = _fat16_disk()
    disk.sectors[0][19:21] = (1000).to_bytes(2, 'little')
    with pytest.raises(FAT12Error, match='FAT16 requires'):
        FAT16(disk).mount()
