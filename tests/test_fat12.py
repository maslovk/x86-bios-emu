"""Unit tests for fat12.py — FAT12 filesystem."""

import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from fat12 import FAT12, FAT12Error, DirEntry


# ── Helpers: build a minimal FAT12 floppy image ──────────────────

def build_floppy(files=None):
    """Build a minimal 1.44 MB FAT12 floppy image in memory.

    Args:
        files: dict of {name: bytes} files to place in root directory.
    Returns:
        bytearray of 1474560 bytes (2880 sectors × 512 bytes)
    """
    SECTOR = 512
    TOTAL = 2880 * SECTOR  # 1.44 MB
    disk = bytearray(TOTAL)

    # ── Boot sector (sector 0) ────────────────────────────────
    # Jump instruction
    disk[0:3] = b'\xEB\x3C\x90'
    # OEM name
    disk[3:11] = b'SIMPLE12'
    # BPB
    disk[11:13] = (SECTOR).to_bytes(2, 'little')       # BytesPerSector = 512
    disk[13] = 1                                        # SectorsPerCluster = 1
    disk[14:16] = (1).to_bytes(2, 'little')             # ReservedSectors = 1
    disk[16] = 2                                        # NumberOfFATS = 2
    disk[17:19] = (224).to_bytes(2, 'little')           # RootEntries = 224
    disk[19:21] = (2880).to_bytes(2, 'little')          # TotalSectors16 = 2880
    disk[21] = 0xF0                                     # Media = 0xF0 (1.44MB)
    disk[22:24] = (9).to_bytes(2, 'little')             # SectorsPerFAT = 9
    disk[24:26] = (18).to_bytes(2, 'little')            # SectorsPerTrack = 18
    disk[26:28] = (2).to_bytes(2, 'little')             # Heads = 2
    disk[28:32] = (0).to_bytes(4, 'little')             # HiddenSectors = 0
    disk[32:36] = (0).to_bytes(4, 'little')             # TotalSectors32 = 0
    # Boot code (NOPs)
    for i in range(36, 498):
        disk[i] = 0x90
    # Signature
    disk[510:512] = b'\x55\xAA'

    # ── FAT tables (sectors 1-9 = FAT1, sectors 10-18 = FAT2) ─
    fat_start = 1 * SECTOR
    # FAT entry 0: media type
    fat_bytes = bytearray(9 * SECTOR)
    fat_bytes[0] = 0xF0
    fat_bytes[1] = 0xFF
    fat_bytes[2] = 0xFF  # Entry 1 = reserved
    # Entries 2+ = free (0x00) by default

    # ── Root directory (sectors 19-20, 224 entries × 32 bytes = 7168 bytes) ─
    root_start = 19 * SECTOR
    root_bytes = bytearray(224 * 32)

    # ── Data region (sector 33+ = 19 + 14 root dir sectors) ──
    root_sectors = (224 * 32 + SECTOR - 1) // SECTOR  # 14 sectors
    data_start = (19 + root_sectors) * SECTOR  # sector 33

    # Place files
    cluster = 2  # First data cluster
    for name, content in (files or {}).items():
        if len(name) > 12:
            name = name[:12]
        # Split into 8.3 format
        if '.' in name:
            base, ext = name.split('.', 1)
            base = base[:8].ljust(8)
            ext = ext[:3].ljust(3)
        else:
            base = name[:8].ljust(8)
            ext = '   '

        # Write directory entry (32 bytes, standard FAT layout)
        entry = bytearray(32)
        entry[0:8] = base.encode('ascii')
        entry[8:11] = ext.encode('ascii')
        entry[11] = 0x20  # Archive attribute
        entry[14:16] = (12 * 2048 + 30 * 32 + 0).to_bytes(2, 'little')  # Create time: 12:30:00
        entry[16:18] = (0x2000 + 30 * 32 + 15).to_bytes(2, 'little')  # Create date: 2024-01-15
        entry[26:28] = cluster.to_bytes(2, 'little')  # First cluster (low 16 bits)
        entry[28:32] = len(content).to_bytes(4, 'little')  # File size

        # Find next free slot in root directory
        entry_idx = 0
        for i in range(224):
            pos = i * 32
            if root_bytes[pos] == 0x00:
                entry_idx = i
                break
        root_bytes[entry_idx * 32:(entry_idx + 1) * 32] = entry

        # Write file content to data clusters
        file_cluster = cluster
        for block_idx in range(0, len(content), SECTOR):
            block = content[block_idx:block_idx + SECTOR]
            sector = data_start + (file_cluster - 2) * SECTOR
            disk[sector:sector + len(block)] = block
            # FAT entry: point to next cluster or EOC
            fat_offset = file_cluster * 3 // 2
            next_cluster = file_cluster + 1 if block_idx + SECTOR < len(content) else 0xFF8
            if file_cluster % 2 == 0:
                fat_bytes[fat_offset] = next_cluster & 0xFF
                fat_bytes[fat_offset + 1] = (next_cluster >> 8) & 0x0F
            else:
                fat_bytes[fat_offset] |= (next_cluster & 0x0F) << 4
                fat_bytes[fat_offset + 1] = (next_cluster >> 4) & 0xFF
            file_cluster += 1

        cluster = file_cluster + 1

    # Copy FAT to disk
    disk[fat_start:fat_start + len(fat_bytes)] = fat_bytes
    # Mirror FAT2
    fat2_start = 10 * SECTOR
    disk[fat2_start:fat2_start + len(fat_bytes)] = fat_bytes

    # Copy root directory
    disk[root_start:root_start + len(root_bytes)] = root_bytes

    return disk


class FakeDisk:
    """Minimal disk object for FAT12 testing."""

    def __init__(self, data: bytearray):
        self.data = data

    def read_sector(self, sector_num, buf):
        if sector_num < 0 or sector_num >= len(self.data) // 512:
            return False
        start = sector_num * 512
        buf[:512] = self.data[start:start + 512]
        return True

    def write_sector(self, sector_num, buf):
        if sector_num < 0 or sector_num >= len(self.data) // 512:
            return False
        start = sector_num * 512
        self.data[start:start + 512] = buf[:512]
        return True


class TestDirEntry:
    def test_parse_regular_file(self):
        raw = bytearray(32)
        raw[0:4] = b'TEST'
        raw[8:10] = b'BI'
        raw[11] = 0x20  # Archive
        raw[26:28] = (2).to_bytes(2, 'little')  # first_cluster
        raw[28:32] = (1024).to_bytes(4, 'little')  # size
        entry = DirEntry(bytes(raw))
        assert entry.name == 'TEST'
        assert entry.ext == 'BI'
        assert entry.full_name == 'TEST.BI'
        assert entry.size == 1024
        assert entry.first_cluster == 2
        assert entry.is_dir is False
        assert entry.deleted is False

    def test_parse_directory(self):
        raw = bytearray(32)
        raw[0:5] = b'MYDIR'
        raw[11] = DirEntry.ATTR_DIRECTORY
        entry = DirEntry(bytes(raw))
        assert entry.is_dir is True
        assert entry.full_name == 'MYDIR'

    def test_deleted_entry(self):
        raw = bytearray(32)
        raw[0] = 0xE5
        raw[1:4] = b'EST'
        entry = DirEntry(raw)
        assert entry.deleted is True

    def test_eof_entry(self):
        raw = bytearray(32)
        raw[0] = 0x00
        entry = DirEntry(raw)
        assert entry.eof is True

    def test_repr(self):
        raw = bytearray(32)
        raw[0:4] = b'FILE'
        raw[8:11] = b'TXT'
        raw[11] = 0x20
        raw[28:32] = (100).to_bytes(4, 'little')
        entry = DirEntry(bytes(raw))
        r = repr(entry)
        assert 'FILE.TXT' in r
        assert '100B' in r
        assert 'A' in r


class TestFAT12Mount:
    def test_mount_empty(self):
        disk = FakeDisk(build_floppy())
        fat = FAT12(disk)
        fat.mount()
        assert fat.bytes_per_sector == 512
        assert fat.sectors_per_cluster == 1
        assert fat.reserved_sectors == 1
        assert fat.num_fats == 2
        assert fat.root_entries == 224
        assert fat.total_sectors == 2880
        assert fat.cluster_size == 512

    def test_mount_info(self):
        disk = FakeDisk(build_floppy())
        fat = FAT12(disk)
        fat.mount()
        info = fat.info()
        assert info['sector_size'] == 512
        assert info['cluster_size'] == 512
        assert info['capacity_kb'] == 1440
        assert info['media'] == 0xF0

    def test_mount_invalid_signature(self):
        data = bytearray(1474560)
        # No 0xAA55 signature
        disk = FakeDisk(data)
        fat = FAT12(disk)
        with pytest.raises(FAT12Error):
            fat.mount()

    def test_mount_small_sector(self):
        data = bytearray(100)
        disk = FakeDisk(data)
        fat = FAT12(disk)
        with pytest.raises(FAT12Error):
            fat.mount()


class TestFAT12FAT:
    def test_fat_entry(self):
        disk = FakeDisk(build_floppy({'TEST.TXT': b'hello'}))
        fat = FAT12(disk)
        fat.mount()
        # Entry 0 should be media type
        assert fat.get_fat_entry(0) == 0x000 or fat.get_fat_entry(0) == 0xF0
        # Entry 1 should be reserved
        assert fat.get_fat_entry(1) == 0xFFF

    def test_follow_chain_single(self):
        disk = FakeDisk(build_floppy({'SMALL.TXT': b'hi'}))
        fat = FAT12(disk)
        fat.mount()
        entry = fat.find_file('SMALL.TXT')
        assert entry is not None
        chain = fat.follow_chain(entry.first_cluster)
        assert len(chain) == 1
        assert chain[0] == entry.first_cluster

    def test_follow_chain_multi(self):
        # File > 512 bytes spans multiple clusters
        content = b'A' * 1500
        disk = FakeDisk(build_floppy({'BIG.BIN': content}))
        fat = FAT12(disk)
        fat.mount()
        entry = fat.find_file('BIG.BIN')
        assert entry is not None
        chain = fat.follow_chain(entry.first_cluster)
        assert len(chain) == 3  # 1500 bytes / 512 = 3 clusters

    def test_cluster_to_sector(self):
        disk = FakeDisk(build_floppy())
        fat = FAT12(disk)
        fat.mount()
        # Cluster 2 → first data sector
        assert fat._cluster_to_sector(2) == fat.data_start


class TestFAT12ReadFile:
    def test_read_single_cluster(self):
        content = b'Hello, FAT12 world!'
        disk = FakeDisk(build_floppy({'HELLO.TXT': content}))
        fat = FAT12(disk)
        fat.mount()
        entry = fat.find_file('HELLO.TXT')
        assert entry is not None
        data = fat.read_file(entry.first_cluster, entry.size)
        assert data == content

    def test_read_multi_cluster(self):
        content = b'X' * 2000
        disk = FakeDisk(build_floppy({'MULTI.BIN': content}))
        fat = FAT12(disk)
        fat.mount()
        entry = fat.find_file('MULTI.BIN')
        assert entry is not None
        data = fat.read_file(entry.first_cluster, entry.size)
        assert data == content

    def test_read_file_clusters(self):
        content = b'Z' * 750  # Spans 2 clusters, only 750 bytes
        disk = FakeDisk(build_floppy({'PARTIAL.BIN': content}))
        fat = FAT12(disk)
        fat.mount()
        entry = fat.find_file('PARTIAL.BIN')
        data = fat.read_file_clusters(entry.first_cluster, entry.size)
        assert data == content

    def test_read_cluster(self):
        content = b'\xAB' * 512
        disk = FakeDisk(build_floppy({'FULL.CLU': content}))
        fat = FAT12(disk)
        fat.mount()
        entry = fat.find_file('FULL.CLU')
        cluster_data = fat.read_cluster(entry.first_cluster)
        assert cluster_data == content


class TestFAT12Directory:
    def test_list_root_empty(self):
        disk = FakeDisk(build_floppy())
        fat = FAT12(disk)
        fat.mount()
        entries = fat.list_root()
        assert len(entries) == 0

    def test_list_root_with_files(self):
        files = {'FILE1.TXT': b'a', 'FILE2.BIN': b'bc', 'README.TXT': b'def'}
        disk = FakeDisk(build_floppy(files))
        fat = FAT12(disk)
        fat.mount()
        entries = fat.list_root()
        names = {e.full_name for e in entries}
        assert 'FILE1.TXT' in names
        assert 'FILE2.BIN' in names
        assert 'README.TXT' in names

    def test_find_file(self):
        disk = FakeDisk(build_floppy({'KERNEL.BIN': b'\x00' * 512}))
        fat = FAT12(disk)
        fat.mount()
        entry = fat.find_file('KERNEL.BIN')
        assert entry is not None
        assert entry.full_name == 'KERNEL.BIN'
        assert entry.size == 512

    def test_find_file_case_insensitive(self):
        disk = FakeDisk(build_floppy({'test.txt': b'hi'}))
        fat = FAT12(disk)
        fat.mount()
        assert fat.find_file('TEST.TXT') is not None
        assert fat.find_file('test.txt') is not None
        assert fat.find_file('Test.Txt') is not None

    def test_find_file_not_found(self):
        disk = FakeDisk(build_floppy({'OTHER.TXT': b'x'}))
        fat = FAT12(disk)
        fat.mount()
        assert fat.find_file('MISSING.TXT') is None

    def test_read_root_directory(self):
        files = {'A.TXT': b'1', 'B.TXT': b'22'}
        disk = FakeDisk(build_floppy(files))
        fat = FAT12(disk)
        fat.mount()
        entries = fat.read_root_directory()
        assert len(entries) >= 2


class TestFAT12LoadToMemory:
    def test_load_to_memory(self):
        content = b'\xDE\xAD\xBE\xEF' * 128  # 512 bytes
        disk = FakeDisk(build_floppy({'CODE.BIN': content}))
        fat = FAT12(disk)
        fat.mount()
        entry = fat.find_file('CODE.BIN')

        # Fake memory
        class FakeMem:
            def __init__(self):
                self.ram = bytearray(0x10000)
            def write_byte(self, addr, val):
                self.ram[addr] = val
            def read_byte(self, addr):
                return self.ram[addr]

        mem = FakeMem()
        fat.load_to_memory(entry.first_cluster, entry.size, 0x7C00, mem)

        for i, b in enumerate(content):
            assert mem.read_byte(0x7C00 + i) == b


class TestFAT12EdgeCases:
    def test_zero_byte_file(self):
        disk = FakeDisk(build_floppy({'EMPTY.TXT': b''}))
        fat = FAT12(disk)
        fat.mount()
        entry = fat.find_file('EMPTY.TXT')
        assert entry is not None
        assert entry.size == 0

    def test_exact_cluster_size(self):
        content = b'\xFF' * 512
        disk = FakeDisk(build_floppy({'EXACT.BIN': content}))
        fat = FAT12(disk)
        fat.mount()
        entry = fat.find_file('EXACT.BIN')
        data = fat.read_file(entry.first_cluster, entry.size)
        assert data == content
        chain = fat.follow_chain(entry.first_cluster)
        assert len(chain) == 1

    def test_multiple_files(self):
        files = {f'FILE{i:02d}.TXT': f'content{i}'.encode() for i in range(10)}
        disk = FakeDisk(build_floppy(files))
        fat = FAT12(disk)
        fat.mount()
        for i in range(10):
            name = f'FILE{i:02d}.TXT'
            entry = fat.find_file(name)
            assert entry is not None, f"Could not find {name}"
            data = fat.read_file(entry.first_cluster, entry.size)
            assert data == f'content{i}'.encode()
