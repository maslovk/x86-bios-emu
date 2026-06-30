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


# ── DOS 1.x media-descriptor geometry table ───────────────────────────
#
# The original IBM-PC BPB (DOS 2.0+, 1983) sits at boot-sector offsets
# 11-35.  DOS 1.x boot sectors predate the BPB: the early fields at
# offsets 11-18 (bytes_per_sector, reserved_sectors, num_fats,
# root_entries) are absent or hold garbage, while the later fields at
# offsets 19-27 (total_sectors, media descriptor, sectors_per_fat,
# sectors_per_track, heads) were sometimes present and valid.  The DOS 1.x
# FORMAT command derived geometry from a *media-descriptor byte*
# (0xF0, 0xF9, 0xFC, 0xFD, 0xFE, 0xFF) via a built-in drive-type
# lookup table.  We mirror that here, keyed on total_sectors to avoid
# the 0xF0-ambiguous (1.2MB vs 1.44MB) case.
# Each trim: (bytes_per_sector, sectors_per_cluster, reserved_sectors,
#            num_fats, root_entries, sectors_per_fat, media,
#            sectors_per_track, heads, label).
_DOS_MEDIA_GEOMETRIES = {
    2880: dict(bytes_per_sector=512, sectors_per_cluster=1,
               reserved_sectors=1, num_fats=2, root_entries=224,
               sectors_per_fat=9,  media=0xF0, sectors_per_track=18,
               heads=2, label='1.44MB 3.5"'),
    2400: dict(bytes_per_sector=512, sectors_per_cluster=1,
               reserved_sectors=1, num_fats=2, root_entries=224,
               sectors_per_fat=7,  media=0xF9, sectors_per_track=15,
               heads=2, label='1.2MB 5.25"'),
    1440: dict(bytes_per_sector=512, sectors_per_cluster=2,
               reserved_sectors=1, num_fats=2, root_entries=112,
               sectors_per_fat=3,  media=0xF9, sectors_per_track=9,
               heads=2, label='720KB 3.5"'),
    1232: dict(bytes_per_sector=512, sectors_per_cluster=2,
               reserved_sectors=1, num_fats=2, root_entries=192,
               sectors_per_fat=2,  media=0xFD, sectors_per_track=8,
               heads=2, label='615KB 5.25"'),
     720: dict(bytes_per_sector=512, sectors_per_cluster=2,
               reserved_sectors=1, num_fats=2, root_entries=112,
               sectors_per_fat=2,  media=0xFD, sectors_per_track=9,
               heads=2, label='360KB 5.25"'),
     640: dict(bytes_per_sector=512, sectors_per_cluster=2,
               reserved_sectors=1, num_fats=2, root_entries=112,
               sectors_per_fat=1,  media=0xFF, sectors_per_track=8,
               heads=2, label='320KB 5.25"'),
     360: dict(bytes_per_sector=512, sectors_per_cluster=1,
               reserved_sectors=1, num_fats=2, root_entries=64,
               sectors_per_fat=2,  media=0xFC, sectors_per_track=9,
               heads=1, label='180KB 5.25"'),
     320: dict(bytes_per_sector=512, sectors_per_cluster=1,
               reserved_sectors=1, num_fats=2, root_entries=64,
               sectors_per_fat=1,  media=0xFE, sectors_per_track=8,
               heads=1, label='160KB 5.25"'),
}

# Recognised IBM-PC media-descriptor bytes.
_VALID_MEDIA_BYTES = frozenset({0xF0, 0xF9, 0xFC, 0xFD, 0xFE, 0xFF})


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
        # Variants set by _parse_bpb(): 'dos2plus' (IBM PC BPB parse ok)
        # or 'dos1x-fallback' (early BPB fields invalid; geometry derived
        # from the DOS 1.x media-descriptor lookup table below).  Caller
        # code may inspect these via info().
        self.dos_variant = 'dos2plus'
        self.geom_label = 'FAT12 BPB'

    def mount(self):
        """Read and parse the boot sector BPB."""
        buf = bytearray(self.sector_size)
        self.disk.read_sector(0, buf)
        self._parse_bpb(bytes(buf))
        return self

    def _parse_bpb(self, boot_sector: bytes):
        """Parse the BPB; fall back to a DOS 1.x media-descriptor layout
        when the standard DOS 2+ BPB looks corrupt or absent.

        Many real DOS 1.x boot sectors (e.g. the SCP-86-DOS-derived
        "DOS 1.25" reference disk) leave the early BPB fields at
        offsets 11-18 (bytes_per_sector, reserved_sectors, num_fats,
        root_entries) zero-filled or filled with garbage, while the
        later fields at offsets 19-27 (total_sectors, media,
        sectors_per_fat, sectors_per_track, heads) read correctly.
        Falling back through a known PC-floppy geometry table keyed on
        total_sectors lets us mount and read those images.
        """
        if len(boot_sector) < 36:
            raise FAT12Error("Boot sector too small for BPB")

        # Check for valid boot sector signature
        if len(boot_sector) >= 512:
            sig = int.from_bytes(boot_sector[510:512], 'little')
            if sig != 0xAA55:
                raise FAT12Error(f"Invalid boot sector signature: 0x{sig:04X}")

        self._bpb = boot_sector[:36]
        self.dos_variant = 'dos2plus'
        self.geom_label = 'FAT12 BPB'

        # BPB fields (offset from start of boot sector)
        # Jump(3) + OEM(8) = BPB starts at offset 11
        bytes_per_sector    = int.from_bytes(boot_sector[11:13], 'little')
        sectors_per_cluster = boot_sector[13]
        reserved_sectors    = int.from_bytes(boot_sector[14:16], 'little')
        num_fats            = boot_sector[16]
        root_entries        = int.from_bytes(boot_sector[17:19], 'little')
        total_sectors_16    = int.from_bytes(boot_sector[19:21], 'little')
        media               = boot_sector[21]
        sectors_per_fat_16  = int.from_bytes(boot_sector[22:24], 'little')
        sectors_per_track   = int.from_bytes(boot_sector[24:26], 'little')
        heads               = int.from_bytes(boot_sector[26:28], 'little')
        hidden_sectors      = int.from_bytes(boot_sector[28:32], 'little')
        total_sectors_32    = int.from_bytes(boot_sector[32:36], 'little')

        if not self._bpb_valid(bytes_per_sector, sectors_per_cluster,
                               reserved_sectors, num_fats, root_entries,
                               total_sectors_16, total_sectors_32,
                               sectors_per_fat_16):
            # DOS 1.x: derive missing fields from the disk-geometry table.
            gv = self._dos1x_geometry(total_sectors_16, total_sectors_32,
                                      media, sectors_per_fat_16,
                                      sectors_per_track, heads,
                                      sectors_per_cluster)
            bytes_per_sector    = gv['bytes_per_sector']
            sectors_per_cluster = gv['sectors_per_cluster']
            reserved_sectors    = gv['reserved_sectors']
            num_fats            = gv['num_fats']
            root_entries        = gv['root_entries']
            total_sectors_16    = gv['total_sectors']
            media               = gv['media']
            sectors_per_fat_16  = gv['sectors_per_fat']
            sectors_per_track   = gv['sectors_per_track']
            heads               = gv['heads']
            self.dos_variant    = gv.get('variant', 'dos1x-fallback')
            self.geom_label     = gv.get('label', 'DOS 1.x (recovered)')

        self.bytes_per_sector    = bytes_per_sector
        self.sectors_per_cluster = sectors_per_cluster
        self.reserved_sectors    = reserved_sectors
        self.num_fats            = num_fats
        self.root_entries        = root_entries
        self.total_sectors_16    = total_sectors_16
        self.media               = media
        self.sectors_per_fat_16  = sectors_per_fat_16
        self.sectors_per_track   = sectors_per_track
        self.heads               = heads
        self.hidden_sectors      = hidden_sectors
        self.total_sectors_32    = total_sectors_32

        # Derived values
        self.sectors_per_fat = self.sectors_per_fat_16 if self.sectors_per_fat_16 else self.total_sectors_32
        self.total_sectors = self.total_sectors_16 if self.total_sectors_16 else self.total_sectors_32
        self.cluster_size = self.bytes_per_sector * self.sectors_per_cluster

        # Region boundaries (in sectors).  Skip over *every* FAT copy
        # (not just FAT2) so 1-FAT layouts also work.
        self.fat_start = self.reserved_sectors
        self.fat_end = self.fat_start + self.sectors_per_fat
        self.root_start = self.fat_end + self.sectors_per_fat * (self.num_fats - 1)
        self.root_sectors = (self.root_entries * 32 + self.bytes_per_sector - 1) // self.bytes_per_sector
        self.data_start = self.root_start + self.root_sectors

        # Total clusters
        if self.total_sectors > self.data_start:
            data_sectors = self.total_sectors - self.data_start
            self.total_clusters = data_sectors // self.sectors_per_cluster
        else:
            self.total_clusters = 0

        # FAT12 end markers
        self.FAT12_EOC = 0xFF8  # End of chain
        self.FAT12_BAD = 0xFF7  # Bad sector
        self.FAT12_FREE = 0x000

        if self.sectors_per_fat == 0:
            raise FAT12Error("DOS 1.x BPB fallback failed: sectors_per_fat unresolved")
        if self.total_sectors == 0:
            raise FAT12Error("DOS 1.x BPB fallback failed: total_sectors unresolved")

    @staticmethod
    def _bpb_valid(bps, spc, reserved, num_fats, root_entries,
                    total_sectors_16, total_sectors_32, sectors_per_fat_16):
        """Sanity-check the DOS 2+ BPB fields.

        Return True iff the parsed BPB plausibly describes a real FAT12
        filesystem.  Otherwise the caller switches to the DOS 1.x recovery
        path.  Tests treat this as the 'switch' between code paths.
        """
        if bps not in (128, 256, 512, 1024, 2048, 4096):
            return False
        if spc not in (1, 2, 4, 8, 16, 32, 64, 128):
            return False
        if num_fats not in (1, 2):
            return False
        total = total_sectors_16 if total_sectors_16 else total_sectors_32
        if total <= 0:
            return False
        if reserved >= total:
            return False
        if root_entries <= 0 or root_entries > 65535:
            return False
        if sectors_per_fat_16 == 0 or sectors_per_fat_16 >= total:
            return False
        return True

    def _disk_sector_count(self):
        """Best-effort total sector count from the underlying Disk object.

        The Disk object (video.py) stores its image as ``self.sectors``
        (a list of 512-byte bytearrays).  Test fakes use ``self.data``.
        Returns 0 if neither attribute is available.
        """
        disk = self.disk
        if hasattr(disk, 'sectors'):
            try:
                return len(disk.sectors)
            except Exception:
                pass
        if hasattr(disk, 'data'):
            try:
                return len(disk.data) // 512
            except Exception:
                pass
        return 0

    def _dos1x_geometry(self, total_sectors_16, total_sectors_32, media,
                         sectors_per_fat_16, sectors_per_track, heads,
                         sectors_per_cluster_std):
        """Recover missing BPB fields for a DOS 1.x boot sector.

        Strategy:
          1. Pick total_sectors from offset 19-20 (or 32-35, or from
             the disk-image size on disk).
          2. Look up a verified geometry in ``_DOS_MEDIA_GEOMETRIES``
             keyed on total_sectors.  This avoids the well-known
             0xF0 ambiguity (used for both 1.2MB and 1.44MB floppies)
             by trusting total_sectors over the media byte.
          3. Override with any sane 'late' BPB field actually read
             from the boot sector (offsets 19-27).
          4. Estimate sectors_per_fat for unknown geometries so we
             can still walk FAT chains.
        """
        total_sectors = total_sectors_16 if total_sectors_16 else total_sectors_32
        if not total_sectors:
            total_sectors = self._disk_sector_count()
        if not total_sectors:
            raise FAT12Error(
                "DOS 1.x BPB fallback failed: total_sectors unknown "
                f"(t16={total_sectors_16}, t32={total_sectors_32})")

        geom = _DOS_MEDIA_GEOMETRIES.get(total_sectors)
        if geom is None:
            # Heuristic defaults for non-standard sizes.
            if total_sectors >= 1440:
                root_entries,  spc_default = 224,  1
            elif total_sectors >= 640:
                root_entries,  spc_default = 112,  2
            else:
                root_entries,  spc_default = 64,   1
            geom = dict(
                bytes_per_sector=512,
                sectors_per_cluster=spc_default,
                reserved_sectors=1,
                num_fats=2,
                root_entries=root_entries,
                sectors_per_fat=0,           # estimate below
                media=(media if media in _VALID_MEDIA_BYTES else 0xF0),
                sectors_per_track=(sectors_per_track
                                   if 0 < sectors_per_track <= total_sectors else 9),
                heads=(heads if 0 < heads <= 2 else 2),
                label='DOS 1.x (recovered, heuristic)')
        else:
            geom = dict(geom)               # copy so we can mutate

        # Override with any sane late-BPB field from the boot sector.
        if 0 < sectors_per_fat_16 < total_sectors:
            geom['sectors_per_fat'] = sectors_per_fat_16
        if 0 < sectors_per_track <= total_sectors:
            geom['sectors_per_track'] = sectors_per_track
        if 0 < heads <= 2:
            geom['heads'] = heads
        if sectors_per_cluster_std in (1, 2, 4, 8, 16, 32, 64, 128):
            # The SPC byte at offset 13 is preserved on most DOS 1.x disks.
            geom['sectors_per_cluster'] = sectors_per_cluster_std
        if media in _VALID_MEDIA_BYTES:
            geom['media'] = media
        if not geom.get('sectors_per_cluster'):
            geom['sectors_per_cluster'] = 1

        if not geom.get('sectors_per_fat') or geom['sectors_per_fat'] >= total_sectors:
            # Estimate sectors-per-FAT: 1.5 bytes per FAT12 entry, bps=512.
            root_sectors = (geom['root_entries'] * 32 + 511) // 512
            reserved     = geom['reserved_sectors']
            data_sectors = max(0, total_sectors - reserved - root_sectors)
            # FAT12: each cluster occupies 1 FAT entry (12 bits) + 1 cluster
            # of data; that gives 2/3 of the data region as clusters.
            data_clusters = data_sectors * 2 // 3
            spf = max(1, (data_clusters * 3 // 2 + 511) // 512)
            geom['sectors_per_fat'] = spf

        geom['total_sectors'] = total_sectors
        geom['variant']        = 'dos1x-fallback'
        return geom

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
            'dos_variant': self.dos_variant,
            'geom_label': self.geom_label,
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
