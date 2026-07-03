"""Shared FAT12 test helpers: a minimal in-memory 1.44MB floppy builder."""

import os, sys

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
