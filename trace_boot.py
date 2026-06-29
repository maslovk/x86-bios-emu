#!/usr/bin/env python3
"""Trace boot sector execution with proper INT 13h + INT 10h tracing."""
import sys
sys.path.insert(0, '.')
from main import Emulator

emu = Emulator(boot_file='../dos3.3.img', step_mode=False)
emu.bios.initialize()

# Load boot sector
with open('../dos3.3.img', 'rb') as f:
    boot_code = f.read(512)
if len(boot_code) < 512:
    boot_code = boot_code + bytearray(512 - len(boot_code))
emu.disk.write_boot_sector(bytes(boot_code))
buf = bytearray(512)
emu.disk.read_sector(0, buf)
for i in range(512):
    emu.mem.write_byte(0x7C00 + i, buf[i])

emu.cpu.cs = 0x0000
emu.cpu.ip = 0x7C00
emu.cpu.ds = 0x0000
emu.cpu.es = 0x0000

# Hook interrupts
bios_ref = emu.bios
int13_log = []
int10_log = []

def hooked_interrupt(n):
    emu.cpu._push(emu.cpu.flags)
    emu.cpu.tf = False
    emu.cpu.if_flag = False
    emu.cpu._push(emu.cpu.cs)
    emu.cpu._push(emu.cpu.ip)
    emu.cpu.int_no_return = False
    
    # Trace BEFORE calling handler
    if n == 0x13:
        ah = (emu.cpu.ax >> 8) & 0xFF
        al = emu.cpu.ax & 0xFF
        dl = emu.cpu.dl & 0xFF
        entry = f'INT13h AH={ah:02X} AL={al:02X} DL={dl:02X} CS:IP={emu.cpu.cs:04X}:{emu.cpu.ip:04X}'
        if ah == 0x02:
            sector = emu.cpu.cl & 0x3F
            head = (emu.cpu.dx >> 8) & 0xFF
            cyl = emu.cpu.ch | ((emu.cpu.cl & 0xC0) << 2)
            media = emu.disk.media_type
            spt_map = {0xF9: 18, 0xF8: 8, 0xF0: 15, 0xF1: 9, 0xFD: 9}
            spt = spt_map.get(media, 18)
            lba_correct = (cyl * 2 + head) * spt + sector - 2
            lba_buggy = (cyl * 2 + head) * 2 + sector - 2
            entry += f' CHS={cyl}/{head}/{sector} SPT={spt} LBA_ok={lba_correct} LBA_bug={lba_buggy}'
        elif ah == 0x08:
            entry += f' (disk params)'
        elif ah == 0x00:
            entry += f' (reset)'
        int13_log.append(('IN', entry))
    elif n == 0x10:
        ah = (emu.cpu.ax >> 8) & 0xFF
        al = emu.cpu.ax & 0xFF
        cl = emu.cpu.cl & 0xFF
        entry = f'INT10h AH={ah:02X} AL={al:02X}'
        if ah == 0x0E:
            ch_char = chr(cl) if 32 <= cl < 127 else f'[{cl:02X}]'
            entry += f' TELETYPE "{ch_char}"'
        elif ah == 0x06:
            entry += f' scroll BH={(emu.cpu.bx>>8)&0xFF:02X} CH={emu.cpu.ch:02X} CL={emu.cpu.cl:02X} DH={(emu.cpu.dx>>8)&0xFF:02X} DL={emu.cpu.dl:02X}'
        int10_log.append(('IN', entry))
    
    bios_ref.handle_interrupt(emu.cpu, n)
    
    # Trace AFTER calling handler
    if n == 0x13:
        ah = (emu.cpu.ax >> 8) & 0xFF
        status = 'OK' if (emu.cpu.flags & 1) == 0 else 'FAIL'
        if ah == 0x08:
            entry = f'  -> AX={emu.cpu.ax:04X} BX={emu.cpu.bx:04X} CX={emu.cpu.cx:04X} DX={emu.cpu.dx:04X}'
        else:
            entry = f'  -> {status}'
        int13_log.append(('OUT', entry))
    elif n == 0x10:
        pass  # INT 10h output already logged
    
    if not emu.cpu.int_no_return:
        emu.cpu.ip = emu.cpu._pop()
        emu.cpu.cs = emu.cpu._pop()
        emu.cpu.flags = emu.cpu._pop()

emu.cpu._do_interrupt = hooked_interrupt

# Run CPU
step = 0
stuck_ip = 0
stuck_count = 0

while emu.cpu.execute():
    step += 1
    cs = emu.cpu.cs
    ip = emu.cpu.ip

    if ip == stuck_ip:
        stuck_count += 1
    else:
        stuck_count = 0
        stuck_ip = ip

    if stuck_count >= 30000:
        print(f'\nSTUCK at {step:,}! IP={ip:04X} (repeated {stuck_count}x)', file=sys.stderr)
        phys = (cs << 4) + ip
        print(f'Code: {bytes(emu.mem.ram[phys:phys+16]).hex()}', file=sys.stderr)
        break

    if step % 200000 == 0:
        print(f'[{step:,}] CS:IP={cs:04X}:{ip:04X} AX={emu.cpu.ax:04X}', file=sys.stderr)

    if step >= 2000000:
        print(f'\nReached {step:,} instructions', file=sys.stderr)
        break

print(f'\n=== FINAL STATE ({step:,} instructions) ===', file=sys.stderr)
print(f'CS:IP={emu.cpu.cs:04X}:{emu.cpu.ip:04X}', file=sys.stderr)
print(f'AX={emu.cpu.ax:04X} BX={emu.cpu.bx:04X} CX={emu.cpu.cx:04X} DX={emu.cpu.dx:04X}', file=sys.stderr)
print(f'SI={emu.cpu.si:04X} DI={emu.cpu.di:04X} SP={emu.cpu.sp:04X}', file=sys.stderr)
print(f'DS={emu.cpu.ds:04X} ES={emu.cpu.es:04X} SS={emu.cpu.ss:04X}', file=sys.stderr)
phys = (emu.cpu.cs << 4) + emu.cpu.ip
print(f'Code: {bytes(emu.mem.ram[phys:phys+16]).hex()}', file=sys.stderr)

print(f'\n=== INT 13h CALLS ({len([e for e in int13_log if e[0]=="IN"])} total) ===')
for typ, entry in int13_log:
    indent = '  ' if typ == 'OUT' else '  '
    print(f'{indent}{entry}')

print(f'\n=== INT 10h CALLS ({len(int10_log)} total, first 20 + last 5) ===')
for typ, entry in int10_log[:20]:
    print(f'  {entry}')
if len(int10_log) > 25:
    print(f'  ... ({len(int10_log) - 25} more) ...')
for typ, entry in int10_log[-5:]:
    print(f'  {entry}')

# VGA output
print(f'\n=== VGA TEXT BUFFER ===')
vga = emu.video
for row in range(25):
    line = ''
    for col in range(80):
        ch, attr = vga.buffer[row][col]
        if ch == 0 and col > 0:
            break
        elif 32 <= ch < 127:
            line += chr(ch)
        else:
            line += '.'
    if line.strip():
        print(f'  {line.strip()}')
