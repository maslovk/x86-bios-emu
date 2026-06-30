#!/usr/bin/env python3
"""At the first OPEN 'CON' during DOS boot, snapshot the CPU and inject a
tiny stub that does `MOV AH,52h; INT 21h; HLT` at 0:7E00. Let the DOS handler
run, then read ES:BX (the List-of-Lists). Walk the device driver chain from
the NUL device pointer (at SYSVARS+0x22 for DOS 3.x) and print each driver
name to verify 'CON' is present and reachable.
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

# Stub bytes for AH=52 invocation
# 0x7E00: b4 52 cd 21 f4  = MOV AH,52; INT 21h; HLT
stub_phys = 0x7E00
for i, b in enumerate(bytes([0xB4, 0x52, 0xCD, 0x21, 0xF4])):
    cpu.mem.write_byte(stub_phys + i, b)

state = {'triggered': False}

def hooked_interrupt(n):
    saved_flags = cpu.flags
    cpu._push(saved_flags); cpu.tf=False; cpu.if_flag=False
    cpu._push(cpu.cs); cpu._push(cpu.ip); cpu.int_no_return=False

    if (not state['triggered'] and n == 0x21 and (cpu.ax >> 8) == 0x3D
            and (cpu.ax & 0xFF) == 0x02):
        dsdx = (cpu.ds<<4) + cpu.dx
        fn = bytes(cpu.mem.read_byte(dsdx+i) for i in range(4))
        if fn[:3] == b'CON' and fn[3:4] in (b'\x00', b' '):
            state['triggered'] = True
            sys.stderr.write("\n>>> OPEN 'CON' caught; snapshotting + injecting AH=52 stub\n")
            # snapshot regs
            state['snap'] = dict(ax=cpu.ax, bx=cpu.bx, cx=cpu.cx, dx=cpu.dx,
                                 si=cpu.si, di=cpu.di, bp=cpu.bp, sp=cpu.sp,
                                 cs=cpu.cs, ds=cpu.ds, es=cpu.es, ss=cpu.ss,
                                 flags=cpu.flags, ip=cpu.ip)
            # We hijack: redirect control to the AH=52 stub.  Push our own
            # return frame pointing to HLT at 0x7E05, then set CS:IP to stub.
            # The stub does INT 21h (which re-enters handle_interrupt w/ AH=52).
            # terminate afterwards.
            cpu.cs = 0x0000
            cpu.ip = stub_phys        # 0x7E00
            cpu.ss = 0x0000
            cpu.sp = 0x7C00           # fresh stack in boot area
            cpu.ds = 0x0000
            cpu.es = 0x0000
            cpu.ax = 0x0000
            cpu.int_no_return = True  # don't finish-interrupt-return (we replaced CS:IP)
            return                    # SKIP the OPEN handler -- run our stub instead
    bios_ref.handle_interrupt(cpu, n)
    if not cpu.int_no_return:
        emu._finish_interrupt_return(saved_flags)

emu.cpu._do_interrupt = hooked_interrupt

# Run until halted or step cap
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
        if stuck>50000: break
    else: stuck=0
    last_ip=cur_ip

# After halt: ES:BX should hold List-of-Lists from AH=52
lol_seg = cpu.es
lol_off = cpu.bx
lol_phys = (lol_seg<<4)+lol_off
sys.stderr.write(f"\n=== INT 21h AH=52h result ===\n")
sys.stderr.write(f"  ES:BX = {lol_seg:04X}:{lol_off:04X}  (phys {lol_phys:05X})\n")
sys.stderr.write(f"  CPU halted={cpu.halted}  final AX={cpu.ax:04X} CS:IP={cpu.cs:04X}:{cpu.ip:04X}\n")

# Dump 64 bytes of the LOL structure
sys.stderr.write(f"\n=== List-of-Lists at {lol_phys:05X} (first 64 bytes) ===\n")
m = cpu.mem
for r in range(4):
    base = lol_phys + r*16
    row = bytes(m.read_byte(base+i) for i in range(16))
    sys.stderr.write(f"  {base:05X}: {row.hex(' ')}\n")

# Try NUL device driver pointer at LOL+0x22 (DOS 3.x) and also LOL+0x26 (v5)
def try_walk(label, off):
    nptr_off = m.read_word(lol_phys+off)
    nptr_seg = m.read_word(lol_phys+off+2)
    phys = (nptr_seg<<4)+nptr_off
    sys.stderr.write(f"\n=== Device chain head via LOL+0x{off:02X} -> {nptr_seg:04X}:{nptr_off:04X} (phys {phys:05X}) [{label}] ===\n")
    seen = set()
    p = phys
    count = 0
    while p != 0 and p != 0xFFFFFFFF and p < 0x100000 and count < 64:
        if p in seen:
            sys.stderr.write(f"  LOOP detected at {p:05X}\n"); break
        seen.add(p)
        nxt_off = m.read_word(p)
        nxt_seg = m.read_word(p+2)
        attr = m.read_word(p+4)
        strat = m.read_word(p+6)
        intr = m.read_word(p+8)
        # device name or unit count
        namefield = bytes(m.read_byte(p+0xA+i) for i in range(8))
        is_char = bool(attr & 0x8000)
        name_str = namefield.decode('ascii','replace') if is_char else f"(block, unit0={namefield[0]})"
        sys.stderr.write(f"  {p:05X}: next={nptr_seg:04X}:{nptr_off:04X} attr={attr:04X} strat={strat:04X} intr={intr:04X} name={name_str!r}\n")
        p = (nptr_seg<<4)+nptr_off
        count += 1

try_walk("DOS 3.x NULDEV @LOL+0x22", 0x22)
try_walk("alt @LOL+0x26", 0x26)
try_walk("alt @LOL+0x2A", 0x2A)
# Also try LOL+0x1C and 0x32 area for completeness
try_walk("alt @LOL+0x1C", 0x1C)
