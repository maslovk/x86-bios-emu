#!/usr/bin/env python3
"""Single-step every instruction executed INSIDE the first INT 2F AX=1123
call, from entry until IRET returns to the caller. Prints CS:IP + raw bytes
+ AX/CF so we can see exactly where AX becomes 0001 and CF becomes 1.
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
cpu = emu.cpu

# Step trace state: when set, the run loop single-steps and prints.
trace = {'active': False, 'ret_phys': None, 'count': 0, 'took': False}

def hooked_interrupt(n):
    saved_flags = cpu.flags
    cpu._push(saved_flags); cpu.tf=False; cpu.if_flag=False
    cpu._push(cpu.cs); cpu._push(cpu.ip); cpu.int_no_return=False
    if n == 0x21 and (cpu.ax >> 8) == 0x3D and (cpu.ax & 0xFF) == 0x02 and not trace['took']:
        # OPEN "CON" (AL=02 read/write): capture the caller return site from
        # the 6-byte frame we just pushed (IP at SS:SP, CS at SS:SP+2).
        sp = (cpu.ss<<4)+cpu.sp
        rip = cpu.mem.read_word(sp); rcs = cpu.mem.read_word(sp+2)
        # sanity: confirm filename at DS:DX starts with 'C','O','N'
        dsdx = (cpu.ds<<4)+cpu.dx
        fn = bytes(cpu.mem.read_byte(dsdx+i) for i in range(4))
        if fn[:3] == b'CON' and fn[3:4] in (b'\x00', b' '):
            trace['ret_phys'] = (rcs<<4)+rip
            trace['active'] = True
            trace['took'] = True
            sys.stderr.write(f"\n>>> BEGIN OPEN 'CON' single-step trace; return phys {trace['ret_phys']:05X}\n")
    bios_ref.handle_interrupt(cpu, n)
    if not cpu.int_no_return:
        emu._finish_interrupt_return(saved_flags)

emu.cpu._do_interrupt = hooked_interrupt

step = 0; pit_acc = 0; last_ip=None; stuck=0
while True:
    if trace['active']:
        phys = (cpu.cs<<4)+cpu.ip
        raw = bytes(cpu.mem.read_byte(phys+i) for i in range(8))
        cf = int(bool(cpu.flags & 1)); zf=int(bool(cpu.flags & 0x40))
        sys.stderr.write(f"{trace['count']:5d} {cpu.cs:04X}:{cpu.ip:04X} p={phys:05X} "
                         f"AX={cpu.ax:04X} CX={cpu.cx:04X} DX={cpu.dx:04X} "
                         f"DS={cpu.ds:04X} ES={cpu.es:04X} SI={cpu.si:04X} DI={cpu.di:04X} "
                         f"CF={cf} ZF={zf} | {raw.hex(' ')}\n")
        trace['count'] += 1
        if trace['count'] > 8000:
            sys.stderr.write(">>> trace cap reached\n")
            trace['active']=False
    if not cpu.halted:
        if not cpu.execute(): break
        step += 1
    if trace['active'] and (cpu.cs<<4)+cpu.ip == trace['ret_phys']:
        sys.stderr.write(f">>> END INT 2F AX=1123 trace after {trace['count']} instrs; "
                         f"final AX={cpu.ax:04X} CF={int(bool(cpu.flags&1))}\n")
        trace['active']=False
        break
    if step>2_000_000: break
    pit_acc+=1
    if pit_acc>=500 and emu.pit:
        pit_acc=0; emu.io.tick(1.0/18.2)
    if emu.pic: emu._check_and_dispatch_irq()
    if emu.kbd_ctrl and emu.kbd_ctrl.has_data() and not getattr(emu.kbd_ctrl,'irq_pending',False):
        emu.kbd_ctrl.irq_pending=True
        if emu.pic: emu.pic.raise_irq(1)
    cur_ip=(cpu.cs<<4)+cpu.ip
    if cur_ip==last_ip:
        stuck+=1
        if stuck>100000: break
    else: stuck=0
    last_ip=cur_ip
