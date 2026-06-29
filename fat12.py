"""
Simple BIOS Emulator — FAT12 Filesystem
========================================
Parses 1.44 MB floppy images with FAT12 filesystem.
Reads BPB, FAT table, root directory, and cluster chains.
"""


class FAT12Error(Exception):
    pass


class DirEntry:
    """32-byte FAT directory entry."""

    ATTR_READ_ONLY = 0x01
    ATTR_HIDDEN = 0x02
    ATTR_SYSTEM = 0x04
    ATTR_VOLUME_ID = 0x08
    ATTR_DIRECTORY = 0x10
    ATTR_ARCHIVE = 0x20

    def __init__(self, raw: bytes):
        if len(raw) != 32:
            raise FAT12Error("Dir entry must be 32 bytes")
        self.raw = raw
        self.name = raw[0:8].rstrip(b' \x00').decode('ascii', errors='replace')
        self.ext = raw[8:11].rstrip(b' \x00').decode('ascii', errors='replace')
        self.attributes = raw[11]
        # [12] NT reserved, [13] create time ms, [14:16] create time
        # [16:18] create date, [18:20] last access date
        # [20:22] high cluster (FAT32), [22:24] last mod time
        # [24:26] last mod date, [26:28] first cluster (low 16 bits)
        # [28:32] file size
        self.create_time_ms = raw[13]
        self.create_time = int.from_bytes(raw[14:16], 'little')
        self.create_date = int.from_bytes(raw[16:18], 'little')
        self.first_cluster = int.from_bytes(raw[26:28], 'little')
        self.size = int.from_bytes(raw[28:32], 'little')

        # Clean up name
        if self.ext:
            self.full_name = f"{self.name}.{self.ext}"
        else:
            self.full_name = self.name

        # Deleted entry marker
        self.deleted = (raw[0] == 0xE5)
        # End of directory marker
        self.eof = (raw[0] == 0x00)

        # Is directory?
        self.is_dir = bool(self.attributes & self.ATTR_DIRECTORY)

    def __repr__(self):
        flags = []
        if self.attributes & self.ATTR_READ_ONLY:
            flags.append('R')
        if self.attributes & self.ATTR_HIDDEN:
            flags.append('H')
        if self.attributes & self.ATTR_SYSTEM:
            flags.append('S')
        if self.attributes & self.ATTR_DIRECTORY:
            flags.append('D')
        if self.attributes & self.ATTR_ARCHIVE:
            flags.append('A')
        flag_str = ','.join(flags) if flags else '-'
        return f"<DirEntry {self.full_name:>12s} {self.size:>8d}B [{flag_str}]>"


class FAT12:
    """FAT12 filesystem reader for 1.44 MB floppy images.

    Layout:
        Sector 0:    Boot sector (BPB)
        Reserved:    BPB.ReservedSectors (usually 1)
        FAT1:        BPB.SectorsPerFAT (usually 9)
        FAT2:        Same size as FAT1 (mirror)
        Root dir:    BPB.RootEntries * 32 bytes (usually 14 entries, 2240 bytes)
        Data region: Rest of disk
    """

    def __init__(self, disk):
        """
        Args:
            disk: Disk object with read_sector(sector_num, buf) method.
        """
        self.disk = disk
        self.sector_size = 512
        self._bpb = None
        self._fat_cache = None
        self._root_entries = None

    def mount(self):
        """Read and parse the boot sector BPB."""
        buf = bytearray(self.sector_size)
        self.disk.read_sector(0, buf)
        self._parse_bpb(bytes(buf))
        return self

    def _parse_bpb(self, boot_sector: bytes):
        """Parse BIOS Parameter Block from boot sector."""
        if len(boot_sector) < 36:
            raise FAT12Error("Boot sector too small for BPB")

        # Check for valid boot sector signature
        if len(boot_sector) >= 512:
            sig = int.from_bytes(boot_sector[510:512], 'little')
            if sig != 0xAA55:
                raise FAT12Error(f"Invalid boot sector signature: 0x{sig:04X}")

        # BPB fields (offset from start of boot sector)
        # Jump(3) + OEM(8) = BPB starts at offset 11
        self.bytes_per_sector = int.from_bytes(boot_sector[11:13], 'little')
        self.sectors_per_cluster = boot_sector[13]
        self.reserved_sectors = int.from_bytes(boot_sector[14:16], 'little')
        self.num_fats = boot_sector[16]
        self.root_entries = int.from_bytes(boot_sector[17:19], 'little')
        self.total_sectors_16 = int.from_bytes(boot_sector[19:21], 'little')
        self.media = boot_sector[21]
        self.sectors_per_fat_16 = int.from_bytes(boot_sector[22:24], 'little')
        self.sectors_per_track = int.from_bytes(boot_sector[24:26], 'little')
        self.heads = int.from_bytes(boot_sector[26:28], 'little')
        self.hidden_sectors = int.from_bytes(boot_sector[28:32], 'little')
        self.total_sectors_32 = int.from_bytes(boot_sector[32:36], 'little')

        # Derived values
        self.sectors_per_fat = self.sectors_per_fat_16 if self.sectors_per_fat_16 else self.total_sectors_32
        self.total_sectors = self.total_sectors_16 if self.total_sectors_16 else self.total_sectors_32
        self.cluster_size = self.bytes_per_sector * self.sectors_per_cluster

        # Region boundaries (in sectors)
        self.fat_start = self.reserved_sectors
        self.fat_end = self.fat_start + self.sectors_per_fat
        self.root_start = self.fat_end + self.sectors_per_fat  # FAT2 is mirror
        self.root_sectors = (self.root_entries * 32 + self.bytes_per_sector - 1) // self.bytes_per_sector
        self.data_start = self.root_start + self.root_sectors

        # Total clusters
        data_sectors = self.total_sectors - self.data_start
        self.total_clusters = data_sectors // self.sectors_per_cluster

        # FAT12 end markers
        self.FAT12_EOC = 0xFF8  # End of chain
        self.FAT12_BAD = 0xFF7  # Bad sector
        self.FAT12_FREE = 0x000

        self._bpb = boot_sector[:36]

    def info(self) -> dict:
        """Return filesystem info dict."""
        return {
            'sector_size': self.bytes_per_sector,
            'cluster_size': self.cluster_size,
            'sectors_per_cluster': self.sectors_per_cluster,
            'reserved_sectors': self.reserved_sectors,
            'num_fats': self.num_fats,
            'sectors_per_fat': self.sectors_per_fat,
            'root_entries': self.root_entries,
            'total_sectors': self.total_sectors,
            'total_clusters': self.total_clusters,
            'data_start_sector': self.data_start,
            'media': self.media,
            'capacity_kb': self.total_sectors * self.bytes_per_sector // 1024,
        }

    def _cluster_to_sector(self, cluster: int) -> int:
        """Convert cluster number to first sector of that cluster."""
        return self.data_start + (cluster - 2) * self.sectors_per_cluster

    def _read_fat(self) -> bytearray:
        """Read FAT1 table into memory (cached)."""
        if self._fat_cache is not None:
            return self._fat_cache

        fat_bytes = bytearray(self.sectors_per_fat * self.bytes_per_sector)
        for i in range(self.sectors_per_fat):
            buf = bytearray(self.bytes_per_sector)
            self.disk.read_sector(self.fat_start + i, buf)
            fat_bytes[i * self.bytes_per_sector:(i + 1) * self.bytes_per_sector] = buf

        self._fat_cache = fat_bytes
        return fat_bytes

    def get_fat_entry(self, cluster: int) -> int:
        """Read a 12-bit FAT entry for given cluster.

        FAT12 packing: each entry is 1.5 bytes.
        Entry 0: media type (8 bits only)
        Entry 1: reserved (12 bits, usually 0xFFF)
        Even cluster N: bytes [N*3/2 .. N*3/2+1], low 12 bits
        Odd cluster N:  bytes [N*3/2 .. N*3/2+1], high 12 bits
        """
        fat = self._read_fat()
        if cluster == 0:
            return fat[0]  # Media type (8 bits)
        offset = cluster * 3 // 2
        raw = fat[offset] | (fat[offset + 1] << 8)
        if cluster % 2 == 0:
            return raw & 0xFFF
        else:
            return (raw >> 4) & 0xFFF

    def follow_chain(self, first_cluster: int) -> list:
        """Follow a FAT cluster chain. Returns list of cluster numbers."""
        chain = []
        cluster = first_cluster
        while cluster < self.FAT12_BAD:
            if cluster > self.total_clusters:
                raise FAT12Error(f"Cluster {cluster} exceeds total {self.total_clusters}")
            chain.append(cluster)
            cluster = self.get_fat_entry(cluster)
        return chain

    def read_cluster(self, cluster: int) -> bytes:
        """Read one cluster's worth of data."""
        sector = self._cluster_to_sector(cluster)
        data = bytearray(self.cluster_size)
        for i in range(self.sectors_per_cluster):
            buf = bytearray(self.bytes_per_sector)
            self.disk.read_sector(sector + i, buf)
            data[i * self.bytes_per_sector:(i + 1) * self.bytes_per_sector] = buf
        return bytes(data)

    def read_file(self, first_cluster: int, size: int) -> bytes:
        """Read entire file from cluster chain."""
        chain = self.follow_chain(first_cluster)
        data = bytearray()
        for cl in chain:
            data.extend(self.read_cluster(cl))
        return bytes(data[:size])

    def read_file_clusters(self, first_cluster: int, size: int) -> bytes:
        """Read file reading only needed bytes from last cluster."""
        chain = self.follow_chain(first_cluster)
        data = bytearray()
        bytes_remaining = size
        for i, cl in enumerate(chain):
            if i == len(chain) - 1:
                # Last cluster: only read remaining bytes
                sector = self._cluster_to_sector(cl)
                for j in range(self.sectors_per_cluster):
                    buf = bytearray(self.bytes_per_sector)
                    self.disk.read_sector(sector + j, buf)
                    chunk = min(len(buf), bytes_remaining)
                    data.extend(buf[:chunk])
                    bytes_remaining -= chunk
                    if bytes_remaining <= 0:
                        break
            else:
                data.extend(self.read_cluster(cl))
                bytes_remaining -= self.cluster_size
            if bytes_remaining <= 0:
                break
        return bytes(data[:size])

    def read_root_directory(self) -> list:
        """Read all root directory entries."""
        if self._root_entries is not None:
            return self._root_entries

        entries = []
        total_bytes = self.root_entries * 32
        offset = 0

        while offset < total_bytes:
            sector = self.root_start + offset // self.bytes_per_sector
            buf = bytearray(self.bytes_per_sector)
            self.disk.read_sector(sector, buf)

            for i in range(0, self.bytes_per_sector, 32):
                if offset + i >= total_bytes:
                    break
                raw = buf[i:i + 32]
                entry = DirEntry(raw)
                if entry.eof:
                    break
                entries.append(entry)
            offset += self.bytes_per_sector

            if entries and entries[-1].eof:
                break

        self._root_entries = entries
        return entries

    def find_file(self, name: str) -> DirEntry:
        """Find a file by name in root directory (case-insensitive).

        Args:
            name: Filename (e.g., 'KERNEL.BIN' or 'kernel.bin')
        Returns:
            DirEntry if found, None otherwise
        """
        name_upper = name.upper()
        if '.' in name_upper:
            base, ext = name_upper.split('.', 1)
            target_base = base[:8]
            target_ext = ext[:3]
        else:
            target_base = name_upper[:8]
            target_ext = ''

        entries = self.read_root_directory()
        for entry in entries:
            if entry.deleted or entry.eof:
                continue
            if entry.name.upper() == target_base and entry.ext.upper() == target_ext:
                return entry
        return None

    def list_root(self) -> list:
        """List all non-deleted root directory entries."""
        entries = self.read_root_directory()
        return [e for e in entries if not e.deleted and not e.eof]

    def load_to_memory(self, first_cluster: int, size: int, dest_addr: int, mem):
        """Read a file and write it to emulated memory.

        Args:
            first_cluster: First cluster of the file
            size: File size in bytes
            dest_addr: Destination physical address in memory
            mem: Memory object with write_byte(addr, val)
        """
        data = self.read_file_clusters(first_cluster, size)
        for i, b in enumerate(data):
            mem.write_byte(dest_addr + i, b)
        return data
