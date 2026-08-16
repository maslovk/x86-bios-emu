#!/usr/bin/env python3
"""Diagnostic: find where the DOS 4 kernel produces error 3
(path_not_found) when opening a path at boot.

Used to chase the "Bad or missing Command Interpreter" boot failure: the
canonicalizer at 0286:8299 selects CDS[A:] and rejects it because
SYSINIT's TEMPCDS zeroed curdir_flags/curdir_devptr (Fake_Floppy_Drv path
after INT 11h reported no floppy drives — equipment-word bit 0).

Boots the image, hooks interrupts like trace_dos.py, and when the first
INT 21h AH=3D open of the target name is dispatched to the DOS-owned IVT
handler, switches to single-step tracing with a ring buffer of decoded
instructions.  Stops when AX becomes 0003 with CF=1 (error committed) and
dumps the last instructions before it (capstone) plus the kernel CDS/DPB
state.  Kernel data segment 0x0286 matches DOS 4.00's OPERATI3 layout.
"""
import sys
sys.path.insert(0, '.')

from collections import deque
from main import Emulator
from cpu import CPU

IMG = sys.argv[1] if len(sys.argv) > 1 else 'DOS4/OPERATI3.IMG'
TARGET = sys.argv[2].encode() if len(sys.argv) > 2 else b'\\CONFIG.SYS'

emu = Emulator(boot_file=None, step_mode=False, floppy_image=IMG)
emu.bios.initialize()
if emu.pic:
    emu.pic.initialize()
emu._setup_ivt_irq_handlers()
buf = bytearray(512)
emu.disk.read_sector(0, buf)
for i in range(512):
    emu.mem.write_byte(0x7C00 + i, buf[i])
cpu = emu.cpu
cpu.cs = 0; cpu.ip = 0x7C00; cpu.ds = 0; cpu.es = 0; cpu.ss = 0; cpu.sp = 0x7C00
if emu.kbd_ctrl:
    emu.kbd_ctrl.feed_string("\r")

bios_ref = emu.bios

state = {
    'armed': False,      # inside the failing OPEN
    'ret_phys': None,    # caller return address
    'done': False,
}

ring = deque(maxlen=120)


def dump_cds():
    """Dump the DOS kernel CDS array + DOSINFO (SysInitVars) + DPB chain."""
    kd = 0x0286  # kernel data segment
    cds_off, cds_seg = (cpu.mem.read_word((kd << 4) + 0x3e),
                        cpu.mem.read_word((kd << 4) + 0x3c))
    # Find DOSINFO by scanning kernel data for the SYSI_CDS far pointer.
    # SYSVAR.INC: SYSI_DPB@+0, ..., SYSI_CDS@+0x18, SYSI_NUMIO@+0x1E
    target = bytes((cds_off & 0xFF, cds_off >> 8, cds_seg & 0xFF, cds_seg >> 8))
    dosinfo = None
    for off in range(0, 0xFF00, 2):
        b = bytes(emu.mem.read_byte((kd << 4) + off + i) for i in range(4))
        if b == target:
            cand = off - 0x18
            if cand >= 0:
                # sanity: SYSI_DPB seg should be a plausible DOS segment
                dpb_seg = cpu.mem.read_word((kd << 4) + cand + 2)
                if dpb_seg in (0x0286, 0x0070, 0x0457) or dpb_seg == 0xFFFF:
                    dosinfo = (kd << 4) + cand
                    break
    if dosinfo is None:
        sys.stderr.write("[DOSINFO] not found by scan\n")
    else:
        dpb_off = cpu.mem.read_word(dosinfo)
        dpb_seg = cpu.mem.read_word(dosinfo + 2)
        numio = cpu.mem.read_byte(dosinfo + 0x1E)
        ncds = cpu.mem.read_byte(dosinfo + 0x1F)
        bootdrv = cpu.mem.read_byte(dosinfo + 0x28)
        sys.stderr.write(f"[DOSINFO] @{dosinfo:05X} SYSI_DPB={dpb_seg:04X}:{dpb_off:04X} "
                         f"NUMIO={numio} NCDS={ncds} BOOT_DRIVE={bootdrv}\n")
        # Walk DPB chain (dpb_drive@0, dpb_UNIT@1, dpb_next_dpb@DPBSIZ-4?)
        seg, off = dpb_seg, dpb_off
        for i in range(6):
            if off == 0xFFFF or seg == 0:
                sys.stderr.write(f"[DPB] chain end ({seg:04X}:{off:04X})\n")
                break
            a = (seg << 4) + off
            drv = cpu.mem.read_byte(a)
            nxt_off = cpu.mem.read_word(a + 0x19)
            nxt_seg = cpu.mem.read_word(a + 0x1B)
            sys.stderr.write(f"[DPB] [{i}] @{seg:04X}:{off:04X} drive={chr(65+drv)} "
                             f"next={nxt_seg:04X}:{nxt_off:04X}\n")
            seg, off = nxt_seg, nxt_off
    ds_ = kd
    def w(off):
        return cpu.mem.read_word((ds_ << 4) + off)
    cds_off, cds_seg = w(0x3c), w(0x3e)
    count = w(0x47)
    curdrv = cpu.mem.read_byte((ds_ << 4) + 0x336)
    sys.stderr.write(f"[CDS] default_drive={curdrv} (0-based), "
                     f"n_cds={count}, cds@{cds_seg:04X}:{cds_off:04X}\n")
    if not (cds_seg or cds_off):
        return
    base = (cds_seg << 4) + cds_off
    for i in range(min(count, 8)):
        e = base + i * 0x58
        text = bytes(emu.mem.read_byte(e + j) for j in range(67))
        text = text.split(b'\x00')[0].decode('ascii', 'replace')
        flags = cpu.mem.read_word(e + 0x43)
        dpb_off = cpu.mem.read_word(e + 0x45)
        dpb_seg = cpu.mem.read_word(e + 0x47)
        sys.stderr.write(f"[CDS] [{i}] text={text!r:24} flags={flags:04X} "
                         f"dpb={dpb_seg:04X}:{dpb_off:04X}\n")


import capstone
md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_16)
md.detail = False

def snap_instr():
    """Disassemble the current instruction (before execute)."""
    phys = (cpu.cs << 4) + cpu.ip
    code = bytes(emu.mem.read_byte(phys + i) for i in range(16))
    ins = next(md.disasm(code, cpu.ip), None)
    if ins is None:
        return (cpu.cs, cpu.ip, '???', '')
    txt = f"{ins.mnemonic} {ins.op_str}"
    return (cpu.cs, cpu.ip, txt, code[:ins.size].hex())

def hooked_interrupt(n):
    saved_flags = cpu.flags
    cpu._push(saved_flags); cpu.tf = False; cpu.if_flag = False
    cpu._push(cpu.cs); cpu._push(cpu.ip)
    cpu.int_no_return = False

    if (n == 0x21 and (cpu.ax >> 8) == 0x3D and not state['armed']
            and not state['done']):
        dsdx = (cpu.ds << 4) + cpu.dx
        fn = bytes(emu.mem.read_byte(dsdx + i) for i in range(20))
        name = fn.split(b'\x00')[0]
        if name == TARGET:
            sp = (cpu.ss << 4) + cpu.sp
            ret_ip = cpu.mem.read_word(sp)
            ret_cs = cpu.mem.read_word(sp + 2)
            state['armed'] = True
            state['ret_phys'] = (ret_cs << 4) + ret_ip
            sys.stderr.write(f">>> arming at OPEN {name.decode()} "
                             f"ret={ret_cs:04X}:{ret_ip:04X}\n")
            dump_cds()

    bios_ref.handle_interrupt(cpu, n)

    if state['armed'] and cpu.int_no_return:
        # DOS-owned vector: control transferred to the handler. The return
        # frame we care about is already recorded.
        pass
    if not cpu.int_no_return:
        emu._finish_interrupt_return(saved_flags)

cpu._do_interrupt = hooked_interrupt

step = 0
pit_acc = 0
last_phys = None
stuck = 0
while True:
    if not cpu.halted:
        if state['armed']:
            ring.append(snap_instr())
            ok = cpu.execute()
            step += 1
            if ((cpu.ax & 0xFFFF) == 0x0003 and cpu.cf) or not ok:
                sys.stderr.write(f">>> error 3 committed at "
                                 f"CS:IP={cpu.cs:04X}:{cpu.ip:04X} AX={cpu.ax:04X}\n")
                break
            phys = (cpu.cs << 4) + cpu.ip
            if phys == state['ret_phys']:
                sys.stderr.write(f">>> returned to caller CF={int(cpu.cf)} AX={cpu.ax:04X}\n")
                state['done'] = True
                state['armed'] = False
                ring.clear()
        else:
            if not cpu.execute():
                break
            step += 1
    if state['done'] or step > 6_000_000:
        break
    pit_acc += 1
    if pit_acc >= 500 and emu.pit:
        pit_acc = 0
        emu.io.tick(1.0 / 18.2)
    if emu.pic:
        emu._check_and_dispatch_irq()
    if emu.kbd_ctrl and emu.kbd_ctrl.has_data() and not getattr(emu.kbd_ctrl, 'irq_pending', False):
        emu.kbd_ctrl.irq_pending = True
        if emu.pic:
            emu.pic.raise_irq(1)
    cur = (cpu.cs << 4) + cpu.ip
    if cur == last_phys:
        stuck += 1
        if stuck > 300000:
            sys.stderr.write(f"stuck at {cpu.cs:04X}:{cpu.ip:04X}\n")
            break
    else:
        stuck = 0
    last_phys = cur

sys.stderr.write(f"--- ring dump (last {len(ring)} instructions) ---\n")
for cs, ip, txt, code in ring:
    sys.stderr.write(f"{cs:04X}:{ip:04X}  {code:<12} {txt}\n")
