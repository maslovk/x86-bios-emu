#!/usr/bin/env python3
"""Decode an ImageDisk (.IMD) floppy image to a raw byte image.

IMD 1.17 track record (5 bytes per spec; some writers emit a
constant 0x05 record marker first — handled below):
    byte 0: cylinder
    byte 1: head (low bits) | 0x40 head-map present | 0x80 cylinder-map present
    byte 2: sector count
    byte 3: sector size code (0=128,1=256,2=512,3=1024,...)
    byte 4: numbering mode (0=1..n, 1=0..n-1)
    n bytes: sector number map (physical order)
    [n bytes cylinder map] if 0x80
    [n bytes head map] if 0x40
    then per sector: type byte
        0 = unavailable (no data)
        1,3,5,7 = normal data (sector_size bytes follow)
        2,4,6,8 = compressed (one byte repeats)

Usage: python3 imd_decode.py IN.IMD OUT.IMG [--info]
"""
import sys


SECTOR_SIZE = {0: 128, 1: 256, 2: 512, 3: 1024, 4: 2048, 5: 4096, 6: 8192}


def decode(data):
    # ASCII header terminated by 0x1A
    end = data.index(b'\x1a')
    header = data[:end].decode('ascii', 'replace')
    pos = end + 1

    tracks = {}          # (cyl, head) -> {sector_number: bytes}
    geo = {}             # observed per-track geometry

    while pos < len(data):
        has_marker = (data[pos] == 0x05 and 0 < data[pos + 3] <= 0x24
                      and data[pos + 4] <= 6 and data[pos + 1] < 128)
        if has_marker:
            pos += 1  # constant per-track record marker used by some writers
        cyl = data[pos]
        head_flags = data[pos + 1]
        head = head_flags & 0x3F
        nsec = data[pos + 2]
        size_code = data[pos + 3]
        if has_marker:
            pos += 4  # this variant omits the numbering-mode byte
        else:
            pos += 5
        sec_map = data[pos:pos + nsec]
        pos += nsec
        if head_flags & 0x80:      # cylinder map
            pos += nsec
        if head_flags & 0x40:      # head map
            pos += nsec
        ssize = SECTOR_SIZE[size_code]
        track = tracks.setdefault((cyl, head), {})
        geo[(cyl, head)] = (ssize, list(sec_map))
        for i in range(nsec):
            stype = data[pos]
            pos += 1
            if stype == 0:
                continue           # unavailable
            if stype in (1, 3, 5, 7):
                sector = data[pos:pos + ssize]
                pos += ssize
            else:                  # 2,4,6,8 compressed
                sector = bytes([data[pos]]) * ssize
                pos += 1
            track[sec_map[i]] = sector
    return header, tracks, geo


def to_raw(tracks):
    cyls = sorted({c for c, _ in tracks})
    heads = sorted({h for _, h in tracks})
    out = bytearray()
    for c in cyls:
        for h in heads:
            track = tracks.get((c, h))
            if track is None:
                out += b'\xE5' * 9 * 512
                continue
            nsec = max(track)
            for s in range(1, nsec + 1):
                out += track.get(s, b'\xE5' * 512)
    return bytes(out), (len(cyls), len(heads), nsec)


def main():
    src, dst = sys.argv[1], sys.argv[2]
    info = '--info' in sys.argv
    with open(src, 'rb') as f:
        data = f.read()
    header, tracks, geo = decode(data)
    if info:
        print('header:', header.strip())
        sizes = {(geo[k][0], tuple(sorted(geo[k][1]))) for k in geo}
        print(f'{len(tracks)} tracks; geometries: {sizes}')
        return
    raw, (c, h, s) = to_raw(tracks)
    with open(dst, 'wb') as f:
        f.write(raw)
    print(f'{dst}: {c} cylinders x {h} heads x {s} sectors '
          f'= {len(raw)} bytes')


if __name__ == '__main__':
    main()
