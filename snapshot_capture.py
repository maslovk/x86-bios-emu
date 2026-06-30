#!/usr/bin/env python3
"""Capture the full CPU + 1MB memory state at the moment OPEN CON (INT 21h
AH=3D AL=02 with DS:DX -> "CON") is about to be dispatched to the DOS handler
(at 023E:1460).  Save registers + memory to snapshot.bin for offline replay
in a reference x86 emulator.
"""
import sys, struct, os
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
state = {'captured': False}

def hooked_interrupt(n):
    saved_flags = cpu.flags
    cpu._push(saved_flags); cpu.tf=False; cpu.if_flag=False
    cpu._push(cpu.cs); cpu._push(cpu.ip); cpu.int_no_return=False

    # Capture when the INT 21h OPEN-CON is dispatched. At this moment the
    # CPU has already pushed FLAGS/CS/IP for the INT, but BIOS dispatch will
    # transfer CS:IP to the DOS handler at IVT[0x21] = 023E:1460.  We capture
    # the state JUST BEFORE that transfer (so we have the post-push stack).
    if (not state['captured'] and n == 0x21 and (cpu.ax >> 8) == 0x3D
            and (cpu.ax & 0xFF) == 0x02):
        dsdx = (cpu.ds<<4) + cpu.dx
        fn = bytes(cpu.mem.read_byte(dsdx+i) for i in range(4))
        if fn[:3] == b'CON' and fn[3:4] in (b'\x00', b' '):
            state['captured'] = True
            sys.stderr.write(">>> capturing snapshot at OPEN CON (INT 21h just entered)\n")
            # We want the snapshot to represent state at the START of the DOS
            # INT 21h handler: CS:IP = IVT[0x21] entry, with the 6-byte frame
            # (FLAGS,CS,IP) on the stack that an IRET will consume.
            # Our hook already pushed flags/cs/ip onto SS:SP.  Replicate the
            # real INT dispatch: read IVT[0x21], set CS:IP to it, leave the
            # pushed frame in place.
            ivt_ip = cpu.mem.read_word(0x21*4)
            ivt_cs = cpu.mem.read_word(0x21*4+2)
            # Snapshot registers (BEFORE transfer so we describe the INT entry).
            reg = dict(
                ax=cpu.ax, bx=cpu.bx, cx=cpu.cx, dx=cpu.dx,
                sp=cpu.sp, bp=cpu.bp, si=cpu.si, di=cpu.di,
                cs=ivt_cs, ip=ivt_ip,   # <-- handler entry, post-push-frame
                ds=cpu.ds, es=cpu.es, ss=cpu.ss, flags=saved_flags,
            )
            state['reg'] = reg
            # Save memory (1MB) to file
            ram = bytes(cpu.mem.read_byte(a) for a in range(0x100000))
            with open('snapshot.bin', 'wb') as f:
                f.write(ram)
            with open('snapshot.regs', 'w') as f:
                for k,v in reg.items():
                    f.write(f"{k}=0x{v:04X}\n")
            sys.stderr.write(f"    IVT[21h] -> {ivt_cs:04X}:{ivt_ip:04X}\n")
            sys.stderr.write(f"    AX=0x{reg['ax']:04X} DS={reg['ds']:04X} DX=0x{reg['dx']:04X} "
                             f"(DS:DX -> 'CON')  SS:SP={reg['ss']:04X}:{reg['sp']:04X}  "
                             f"FLAGS=0x{reg['flags']:04X}\n")
            sys.stderr.write(f"    wrote snapshot.bin ({len(ram)} bytes) + snapshot.regs\n")
            # Now we want execution to STOP.  Halt the CPU.
            cpu.halted = True
            cpu.int_no_return = True
            return
    bios_ref.handle_interrupt(cpu, n)
    if not cpu.int_no_return:
        emu._finish_interrupt_return(saved_flags)
emu.cpu._do_interrupt = hooked_interrupt

step=0; pit_acc=0; last_ip=None; stuck=0
while True:
    if not cpu.halted:
        if not cpu.execute(): break
        step += 1
    if step > 2_000_000: break
    if cpu.halted: break
    pit_acc += 1
    if pit_acc>=500 and emu.pit:
        pit_acc=0; emu.io.tick(1.0/18.2)
    if emu.pic: emu._check_and_dispatch_irq()
    if emu.kbd_ctrl and emu.kbd_ctrl.has_data() and not getattr(emu.kbd_ctrl,'irq_pending',False):
        emu.kbd_ctrl.irq_pending=True
        if emu.pic: emu.pic.raise_irq(1)
    cur_ip=(cpu.cs<<4)+cpu.ip
    if cur_ip==last_ip:
        stuck+=1
        if stuck>80000: break
    else: stuck=0
    last_ip=cur_ip
sys.stderr.write(f"[done, {step} steps, captured={state['captured']}]\n")
