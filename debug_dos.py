#!/usr/bin/env python3
"""Debug DOS 3.3 boot - trace INT 13h calls and find stall point."""
import sys
sys.path.insert(0, '.')

from main import Emulator

emu = Emulator(boot_file='../dos3.3.img', step_mode=False)

# Patch INT 13h handler to trace calls
orig_int13 = emu.bios._int13h
int13_calls = []

def traced_int13(cpu):
    ah = (cpu.ax >> 8) & 0xFF
    al = cpu.ax & 0xFF
    if ah == 0x02:
        sector = cpu.cl & 0x3F
        head = (cpu.dx >> 8) & 0xFF
        cyl = cpu.ch | ((cpu.cl & 0xC0) << 2)
        lba_calc = cyl * 18 * 2 + head * 18 + (sector - 1)
        int13_calls.append(f'  INT13h AH=02: read {al} sectors, CHS({cyl},{head},{sector}) -> LBA={lba_calc}, ES:BX={cpu.es:04X}:{cpu.bx:04X}')
        if len(int13_calls) <= 30:
            print(int13_calls[-1], file=sys.stderr)
    elif ah == 0x42:
        si = cpu.si
        ds = cpu.ds
        dap_base = (ds << 4) + si
        count = emu.mem.read_word(dap_base + 6)
        buf_seg = emu.mem.read_word(dap_base + 8)
        buf_off = emu.mem.read_word(dap_base + 10)
        lba = emu.mem.read_dword(dap_base + 12)
        msg = f'  INT13h AH=42: ext read {count} sectors, LBA={lba}, buf={buf_seg:04X}:{buf_off:04X}'
        int13_calls.append(msg)
        if len(int13_calls) <= 30:
            print(msg, file=sys.stderr)
    orig_int13(cpu)

emu.bios._int13 = traced_int13

# Run
step = 0
ip_counts = {}

while emu.cpu.execute():
    step += 1
    if emu.cpu.halted:
        break
    
    if step % 500 == 0 and emu.pit:
        emu.io.tick(1.0 / 18.2)
    
    if emu.pic:
        emu._check_and_dispatch_irq()
    
    cur_ip = (emu.cpu.cs << 4) + emu.cpu.ip
    ip_counts[cur_ip] = ip_counts.get(cur_ip, 0) + 1
    
    if ip_counts[cur_ip] > 100000:
        print(f'\n[TIGHT LOOP at phys={cur_ip:#06X} CS:IP={emu.cpu.cs:04X}:{emu.cpu.ip:04X} after {step:,} instructions]', file=sys.stderr)
        break
    
    if step % 500000 == 0:
        s = emu.cpu.status()
        print(f'[{step:,}] CS:IP={s["cs"]:04X}:{s["ip"]:04X} AX={s["ax"]:04X} BX={s["bx"]:04X} CX={s["cx"]:04X} DX={s["dx"]:04X}', file=sys.stderr)

s = emu.cpu.status()
print(f'\n=== FINAL STATE ===', file=sys.stderr)
print(f'CS:IP={s["cs"]:04X}:{s["ip"]:04X} insns={step:,}', file=sys.stderr)
phys = (s['cs'] << 4) + s['ip']
code = bytes(emu.mem.ram[phys:phys+24])
print(f'Code: {code.hex()}', file=sys.stderr)
print(f'AX={s["ax"]:04X} BX={s["bx"]:04X} CX={s["cx"]:04X} DX={s["dx"]:04X}', file=sys.stderr)
print(f'SP={s["sp"]:04X} BP={s["bp"]:04X} SI={s["si"]:04X} DI={s["di"]:04X}', file=sys.stderr)
print(f'DS={s["ds"]:04X} ES={s["es"]:04X} SS={s["ss"]:04X} FL={s["flags"]:04X}', file=sys.stderr)

# Dump BDA
print(f'\n=== BDA (0x00400-0x004FF) ===', file=sys.stderr)
for off in range(0, 256, 16):
    addr = 0x400 + off
    row = emu.mem.ram[addr:addr+16]
    hex_str = ' '.join(f'{b:02X}' for b in row)
    print(f'  {addr:#06X}: {hex_str}', file=sys.stderr)

print(f'\n=== Total INT 13h calls: {len(int13_calls)} ===', file=sys.stderr)
