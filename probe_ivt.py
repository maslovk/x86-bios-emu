#!/usr/bin/env python3
"""Probe: dump IVT entries for the DOS-installed vectors right before the
first INT 2F AX=1123 call is issued, and dump the code the vector points to.
"""
import sys
sys.path.insert(0, '.')
from main import Emulator

IMG = 'DOS3_3_525/DISK01.IMG'
emu = Emulator(boot_file=None, step_mode=False, floppy_image=IMG)
emu.bios.initialize()
if emu.pic:
    emu.pic.initialize()
emu._setup_ivt_irq_handlers()
buf = bytearray(512)
emu.disk.read_sector(0, buf)
for i in range(512):
    emu.mem.write_byte(0x7C00 + i, buf[i])
emu.cpu.cs = 0x0000
emu.cpu.ip = 0x7C00
emu.cpu.ds = 0x0000
emu.cpu.es = 0x0000
emu.cpu.ss = 0x0000
emu.cpu.sp = 0x7C00
if emu.kbd_ctrl:
    emu.kbd_ctrl.feed_string("\r")

bios_ref = emu.bios
fired = {'once': False}

def hooked_interrupt(n):
    cpu = emu.cpu
    saved_flags = cpu.flags
    cpu._push(saved_flags)
    cpu.tf = False
    cpu.if_flag = False
    cpu._push(cpu.cs)
    cpu._push(cpu.ip)
    cpu.int_no_return = False
    # Snapshot IVT[d] for key DOS vectors before the first INT 2F 1123 fires.
    if n == 0x2F and not fired['once']:
        fired['once'] = True
        sys.stderr.write("\n==== IVT snapshot at first INT 2Fh call ====\n")
        for vec in (0x20, 0x21, 0x25, 0x2F, 0x10, 0x13, 0x1B):
            ip = cpu.mem.read_word(vec*4)
            cs = cpu.mem.read_word(vec*4+2)
            phys = (cs<<4)+ip
            code = bytes(cpu.mem.read_byte(phys+i) for i in range(16))
            sys.stderr.write(f"  INT {vec:02X}h -> {cs:04X}:{ip:04X} (phys {phys:05X})  code: {code.hex(' ')}\n")
        # Who called INT 2F? The return site (top of stack frame).
        sp = (cpu.ss<<4)+cpu.sp
        rip = cpu.mem.read_word(sp)
        rcs = cpu.mem.read_word(sp+2)
        sys.stderr.write(f"  INT 2F caller return site: {rcs:04X}:{rip:04X} (phys {(rcs<<4)+rip:05X})\n")
        sys.stderr.write(f"  AX={cpu.ax:04X} (multiplex AH={cpu.ax>>8:02X} subfn AL={cpu.ax&0xFF:02X})\n")
        sys.stderr.write(f"  caller instr region: " + bytes(cpu.mem.read_byte(((rcs<<4)+rip)-2+i) for i in range(12)).hex(' ') + "\n")
        # Dump a wider region of the int 2f handler so we can eyeball it.
        ip2 = cpu.mem.read_word(0x2F*4)
        cs2 = cpu.mem.read_word(0x2F*4+2)
        hphys = (cs2<<4)+ip2
        sys.stderr.write(f"\n  First 64 bytes of INT 2F handler at {cs2:04X}:{ip2:04X}:\n")
        for r in range(4):
            base = hphys + r*16
            row = bytes(cpu.mem.read_byte(base+i) for i in range(16))
            sys.stderr.write(f"    {base:05X}: {row.hex(' ')}\n")
    bios_ref.handle_interrupt(cpu, n)
    if not cpu.int_no_return:
        emu._finish_interrupt_return(saved_flags)

emu.cpu._do_interrupt = hooked_interrupt

step = 0
pit_acc = 0
last_ip = None
stuck = 0
while True:
    if not emu.cpu.halted:
        if not emu.cpu.execute():
            break
        step += 1
    if step > 600000:
        break
    if emu.pit:
        pit_acc += 1
        if pit_acc >= 500 or emu.cpu.halted:
            pit_acc = 0
            emu.io.tick(1.0/18.2)
    if emu.pic:
        emu._check_and_dispatch_irq()
    if emu.kbd_ctrl and emu.kbd_ctrl.has_data() and not getattr(emu.kbd_ctrl,'irq_pending',False):
        emu.kbd_ctrl.irq_pending = True
        if emu.pic: emu.pic.raise_irq(1)
    cur_ip = (emu.cpu.cs<<4)+emu.cpu.ip
    if cur_ip == last_ip:
        stuck += 1
        if stuck > 100000:
            break
    else:
        stuck = 0
    last_ip = cur_ip
sys.stderr.write(f"\n[probe done after {step} steps, IVT snapshot {'' if fired['once'] else 'NOT '}taken]\n")
