#!/usr/bin/env python3
"""Create a private 160 KB DOS 1.x system-disk image.

This intentionally does not touch the source or boot-image files.  It keeps
the original DOS 1.x boot sector, supplies a conventional 160 KB FAT12
layout for host-side tooling, and installs IOSYS.COM, MSDOS.COM, and COMMAND.COM
in that order.
"""

import argparse
from pathlib import Path

from fat12 import FAT12
from video import Disk


GEOMETRIES = {
    320: dict(media=0xFE, sectors_per_cluster=1, root_entries=64,
              sectors_per_track=8, heads=1, cylinders=40),
    640: dict(media=0xFF, sectors_per_cluster=2, root_entries=112,
              sectors_per_track=8, heads=2, cylinders=40),
}
SECTOR_SIZE = 512

def _read_program(path):
    """Read a binary program or SCP Intel-HEX output.

    SCP ASM emits records based at ORG 100h; DOS COM-style files omit that
    origin, so HEX input is rebased by -100h before installation.
    """
    data = Path(path).read_bytes()
    if not data.lstrip().startswith(b':'):
        return data
    image = bytearray(0x10000)
    highest = 0
    for raw in data.splitlines():
        line = raw.strip()
        if not line or not line.startswith(b':'):
            continue
        count = int(line[1:3], 16)
        address = int(line[3:7], 16)
        record_type = int(line[7:9], 16)
        if record_type == 1:
            break
        if record_type != 0:
            continue
        payload = bytes.fromhex(line[9:9 + count * 2].decode('ascii'))
        offset = address - 0x100
        if offset < 0:
            continue
        image[offset:offset + len(payload)] = payload
        highest = max(highest, offset + len(payload))
    if not highest:
        raise ValueError(f'Intel-HEX input contains no loadable data: {path}')
    return bytes(image[:highest])


def build_image(output, boot_image, io_sys, msdos_sys, command_com):
    target = Path(output)
    if target.exists():
        raise FileExistsError(f"refusing to overwrite existing image: {target}")
    boot_path = Path(boot_image)
    boot_data = boot_path.read_bytes()
    if len(boot_data) < SECTOR_SIZE:
        raise ValueError("boot image must contain at least one 512-byte sector")

    boot = bytearray(boot_data[:SECTOR_SIZE])
    sectors = len(boot_data) // SECTOR_SIZE
    geometry = GEOMETRIES.get(sectors)
    if geometry is None:
        raise ValueError('boot image must be a supported 160 KB or 320 KB image')
    original_boot = bytes(boot)
    # DOS 1.x boot code is retained, while these fields let the host FAT12
    # implementation inspect and populate the 160 KB volume.
    boot[11:13] = (512).to_bytes(2, 'little')
    boot[13] = geometry['sectors_per_cluster']
    boot[14:16] = (1).to_bytes(2, 'little')
    boot[16] = 2
    boot[17:19] = geometry['root_entries'].to_bytes(2, 'little')
    boot[19:21] = sectors.to_bytes(2, 'little')
    boot[21] = geometry['media']
    boot[22:24] = (1).to_bytes(2, 'little')
    boot[24:26] = geometry['sectors_per_track'].to_bytes(2, 'little')
    boot[26:28] = geometry['heads'].to_bytes(2, 'little')
    boot[510:512] = b'\x55\xAA'

    disk = Disk(sectors, cylinders=geometry['cylinders'],
                heads=geometry['heads'],
                sectors_per_track=geometry['sectors_per_track'])
    disk.sectors[0][:] = boot
    for sector in (1, 2):
        disk.sectors[sector][:3] = bytes((geometry['media'], 0xFF, 0xFF))
    fat = FAT12(disk).mount()
    # DOS 1.x boot code on the reference disk scans the first root entries
    # for IOSYS.COM and MSDOS.COM in this exact order.
    for name, path in (('IOSYS.COM', io_sys), ('MSDOS.COM', msdos_sys),
                       ('COMMAND.COM', command_com)):
        fat.write_file(name, _read_program(path))
    # DOS 1.x's boot loader requires the two resident files to be marked
    # hidden/system, matching the reference disk's directory entries.
    root = fat.root_start * SECTOR_SIZE
    disk.sectors[fat.root_start][11] = 0x06
    disk.sectors[fat.root_start][43] = 0x06

    # Do not alter executable boot code or its private DOS 1.x fields.
    disk.sectors[0][:] = original_boot

    target.write_bytes(b''.join(bytes(sector) for sector in disk.sectors))
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('--boot-image', default='DOS1_25/DISK01.IMG', type=Path)
    parser.add_argument('--io', required=True, type=Path, metavar='IO.SYS')
    parser.add_argument('--msdos', required=True, type=Path, metavar='MSDOS.SYS')
    parser.add_argument('--command', required=True, type=Path, metavar='COMMAND.COM')
    args = parser.parse_args(argv)
    image = build_image(args.output, args.boot_image, args.io, args.msdos,
                        args.command)
    print(f"created {image} ({image.stat().st_size} bytes)")


if __name__ == '__main__':
    main()
