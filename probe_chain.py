#!/usr/bin/env python3
"""Dump the multiplex-chain pointer the INT 2Fh dispatcher uses (CS:0248),
and the code it points at.  Triggered once the DOS kernel is up (after enough
steps that IVT[2F] is installed).
"""
import sys
sys.path.insert(0, '.')
from main import Emulator

IMG = 'DOS3_3_525/DISK01.IMG'
emu = Emulator(boot_file=None, step_mode=False, floppy_image=IMG)
emu.bios.initialize()
if emu.pic: emu.pic.initialize()
emu._setup_ivt_irq_handlers()
buf = bytearray(512); emu.disk.read_sector(0, buf)
for i in range(512): emu.mem.write_byte(0x7C00+i, buf[i])
emu.cpu.cs=0; emu.cpu.ip=0x7C00; emu.cpu.ds=0; emu.cpu.es=0; emu.cpu.ss=0; emu.cpu.sp=0x7C00
if emu.kbd_ctrl: emu.kbd_ctrl.feed_string("\r")

bios_ref = emu.bios
fired = {'once': False}

def hooked_interrupt(n):
    cpu = emu.cpu
    saved_flags = cpu.flags
    cpu._push(saved_flags); cpu.tf=False; cpu.if_flag=False
    cpu._push(cpu.cs); cpu._push(cpu.ip); cpu.int_no_return=False
    if n == 0x2F and not fired['once']:
        fired['once'] = True
        # The dispatcher is at IVT[2F] = 0070:1848 (per probe). CS=0x70, so
        # [CS:0248] reads the physical dword at (0x70<<4)+0x248 = 0x948.
        m = cpu.mem
        chain_ip = m.read_word(0x948)
        chain_cs = m.read_word(0x94A)
        phys = (chain_cs<<4)+chain_ip
        sys.stderr.write(f"\n[CS:0248] dword = {chain_cs:04X}:{chain_ip:04X}  (phys {phys:05X})\n")
        if phys != 0:
            code = bytes(m.read_byte(phys+i) for i in range(32))
            sys.stderr.write(f"  code at chain target: {code.hex(' ')}\n")
        # Also: what is the full first paragraph of the dispatcher again?
        dip = m.read_word(0x2F*4); dcs = m.read_word(0x2F*4+2)
        sys.stderr.write(f"  dispatcher IVT[2F] = {dcs:04X}:{dip:04X}\n")
        # Walk the multiplex-chain the way DOS would: each handler entry is
        # expected to be:  (nextptr:dword at its own internal offset) ; 'IRET'
        # stub at the head. The chain head pointer [70:0248] is one hop.
        sys.stderr.write(f"  bytes around 70:0240-0250: " + ' '.join(f'{m.read_byte(0x940+i):02X}' for i in range(16)) + "\n")
        sys.stderr.write(f"                              " + ' '.join(f'{m.read_byte(0x950+i):02X}' for i in range(16)) + "\n")
    bios_ref.handle_interrupt(cpu, n)
    if not cpu.int_no_return:
        emu._finish_interrupt_return(saved_flags)

emu.cpu._do_interrupt = hooked_interrupt
step=0; pit_acc=0; last_ip=None; stuck=0
while True:
    if not emu.cpu.halted:
        if not emu.cpu.execute(): break
        step+=1
    if step>600000: break
    pit_acc+=1
    if pit_acc>=500 and emu.pit:
        pit_acc=0; emu.io.tick(1.0/18.2)
    if emu.pic: emu._check_and_dispatch_irq()
    if emu.kbd_ctrl and emu.kbd_ctrl.has_data() and not getattr(emu.kbd_ctrl,'irq_pending',False):
        emu.kbd_ctrl.irq_pending=True
        if emu.pic: emu.pic.raise_irq(1)
    cur_ip=(emu.cpu.cs<<4)+emu.cpu.ip
    if cur_ip==last_ip:
        stuck+=1
        if stuck>100000: break
    else: stuck=0
    last_ip=cur_ip
