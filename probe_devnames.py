#!/usr/bin/env python3
"""Search all of low memory for DOS device driver name fields ('NUL     ',
'CON     ', 'AUX     ', 'PRN     ', 'CLOCK$  ') and for each match, decode
the surrounding device-driver header (next ptr/attr/strat/intr/name) so we
can see whether a proper device chain exists at OPEN-CON time, and what
segment it lives in.
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

stub_phys = 0x0500  # free low memory below BDA; written at trigger time
stub_bytes = bytes([0xB4, 0x52, 0xCD, 0x21, 0xF4])  # MOV AH,52; INT 21h; HLT

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
            sys.stderr.write(f">>> OPEN 'CON' caught; redirecting to AH=52 stub at 0x{stub_phys:X}\n")
            # (Re)write the stub now so DOS boot can't have clobbered it.
            for i, b in enumerate(stub_bytes):
                cpu.mem.write_byte(stub_phys + i, b)
            cpu.cs = 0x0000; cpu.ip = stub_phys
            cpu.ss = 0x0000; cpu.sp = 0x7C00
            cpu.ds = 0x0000; cpu.es = 0x0000; cpu.ax = 0x0000
            cpu.int_no_return = True
            state['trace_after'] = 40   # capture the next 40 single-steps
            return
    if state['triggered'] and n == 0x21:
        sp = (cpu.ss<<4)+cpu.sp
        rip = cpu.mem.read_word(sp); rcs = cpu.mem.read_word(sp+2)
        sys.stderr.write(f"\n>>> INT 21h AH={(cpu.ax>>8)&0xFF:02X} AL={cpu.ax&0xFF:02X} "
                         f"from {rcs:04X}:{rip:04X} (frame ret-site); pre-AX={cpu.ax:04X}\n")
    bios_ref.handle_interrupt(cpu, n)
    if not cpu.int_no_return:
        emu._finish_interrupt_return(saved_flags)
emu.cpu._do_interrupt = hooked_interrupt
m = cpu.mem  # also referenced inside the trace loop

step=0; pit_acc=0; last_ip=None; stuck=0
state['trace_after'] = 0
while True:
    if not cpu.halted:
        if state['trace_after'] > 0:
            phys = (cpu.cs<<4)+cpu.ip
            raw = bytes(m.read_byte(phys+i) for i in range(8))
            sys.stderr.write(f"  [post {40-state['trace_after']:3d}] {cpu.cs:04X}:{cpu.ip:04X} "
                             f"p={phys:05X} AX={cpu.ax:04X} SP={cpu.sp:04X} "
                             f"int_no_return={cpu.int_no_return} | {raw.hex(' ')}\n")
            state['trace_after'] -= 1
            if state['trace_after'] == 0:
                sys.stderr.write("  (end of post-trigger trace; continuing to HLT)\n")
                # don't break -- just stop printing; let loop run to HLT
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

m = cpu.mem
# Read whole memory once (file-backed? no, in RAM)
ram = bytes(m.read_byte(p) for p in range(0x00000, 0x100000))

# Also search for any far pointer to the real NUL device (0x023E:0x0048)
# stored anywhere in low memory (should be in SYSVARS if DOS init linked it).
nul_farbytes = bytes([0x48, 0x00, 0x3E, 0x02])  # 0x023E:0x0048 little-endian
sys.stderr.write(f"\n=== Searches for far pointers to NUL device (023E:0048) ===\n")
off = 0
hits = []
while True:
    i = ram.find(nul_farbytes, off)
    if i < 0: break
    hits.append(i)
    seg = i >> 4
    o = i & 0xF
    sys.stderr.write(f"  found at phys {i:05X} = {seg:04X}:{o:04X}  "
                     f"surrounding: {ram[i-4:i+8].hex(' ')}\n")
    off = i + 1
if not hits:
    sys.stderr.write("  (NONE) - DOS has no pointer to the device chain head!\n")

# Same scan, but for pointers into the device chain at 0x0070:0x016E (CON) etc.
for nm, farbytes in [(b'CON', bytes([0x6E,0x01,0x70,0x00])),
                     (b'AUX', bytes([0x80,0x01,0x70,0x00])),
                     (b'PRN', bytes([0x92,0x01,0x70,0x00]))]:
    cnt = 0; o=0
    while True:
        i = ram.find(farbytes, o)
        if i<0: break
        cnt += 1
        o = i+1
    sys.stderr.write(f"  ptrs to {nm.decode()} device ({farbytes.hex(' ')}): {cnt} occurrence(s)\n")

# Search for 8-byte device-name patterns
names = [b'NUL     ', b'CON     ', b'AUX     ', b'PRN     ',
         b'CLOCK$  ', b'COM1    ', b'LPT1    ']
sys.stderr.write("\n=== Device-name field matches across low memory ===\n")
for nm in names:
    off = 0
    while True:
        i = ram.find(nm, off)
        if i < 0: break
        # device header starts 0x0A before the name field
        hdr = i - 0x0A
        nxt_off = ram[hdr] | (ram[hdr+1]<<8)
        nxt_seg = ram[hdr+2] | (ram[hdr+3]<<8)
        attr = ram[hdr+4] | (ram[hdr+5]<<8)
        strat = ram[hdr+6] | (ram[hdr+7]<<8)
        intr  = ram[hdr+8] | (ram[hdr+9]<<8)
        nxt_phys = (nxt_seg<<4)+nxt_off
        sys.stderr.write(f"  name={nm.decode()!r:10} at phys {i:05X}; header @ {hdr:05X}: "
                         f"next={nxt_seg:04X}:{nxt_off:04X} (phys {nxt_phys:05X}) "
                         f"attr={attr:04X} strat={strat:04X} intr={intr:04X}\n")
        off = i + 1

# dump SYSVARS via AH=52 result
sys.stderr.write(f"\n  stub executed: halted={cpu.halted} final CS:IP={cpu.cs:04X}:{cpu.ip:04X} AX={cpu.ax:04X}\n")
lol_phys = (cpu.es<<4)+cpu.bx
sys.stderr.write(f"\n  AH=52 -> ES:BX={cpu.es:04X}:{cpu.bx:04X} (phys {lol_phys:05X})\n")
sys.stderr.write(f"  SYSVARS bytes (LOL-0x10..LOL+0x40):\n")
for r in range(-1, 6):
    base = lol_phys + r*16
    if base < 0: continue
    row = bytes(m.read_byte(base+i) for i in range(16))
    sys.stderr.write(f"    LOL{r*16:+d}  {base:05X}: {row.hex(' ')}\n")

# Dump the table the OPEN CON walks at 023E:0x0C8E (keys 0x38,0x39,...)
sys.stderr.write("\n=== Bytes at 023E:0x0C8E (table the OPEN walks) ===\n")
tbl_base = 0x23E0 + 0xC8E
for r in range(12):
    base = tbl_base + r*16
    row = bytes(m.read_byte(base+i) for i in range(16))
    asc = ''.join(chr(b) if 32<=b<127 else '.' for b in row)
    sys.stderr.write(f"  {base:05X}: {row.hex(' ')}  |{asc}|\n")

# Dump NUL device header to confirm chain head
sys.stderr.write("\n=== SYSVARS+0x22 NUL device header (phys 0x02428) ===\n")
nul_base = 0x2428
row = bytes(m.read_byte(nul_base+i) for i in range(32))
sys.stderr.write(f"  {nul_base:05X}: {row.hex(' ')}\n")
# Walk from NUL via next pointers and list all names
sys.stderr.write("\n=== Walking device chain from NUL ===\n")
p = nul_base; cnt=0
import sys as _s
while p and p != 0xFFFFFFFF and cnt < 32:
    next_off = m.read_word(p); next_seg = m.read_word(p+2)
    attr = m.read_word(p+4); strat = m.read_word(p+6); intr = m.read_word(p+8)
    nm = bytes(m.read_byte(p+0xA+i) for i in range(8)).decode('ascii','replace')
    _s.stderr.write(f"  phys {p:05X}: next_off={next_off:04X} next_seg={next_seg:04X} attr={attr:04X} strat={strat:04X} intr={intr:04X} name={nm!r}\n")
    if next_seg == 0 and next_off == 0:  # some DOS lists use 0:0 as terminator for block devs
        _s.stderr.write(f"    (next=0:0, end)\n"); break
    p = (next_seg<<4)+next_off
    cnt += 1
