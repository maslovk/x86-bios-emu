#!/usr/bin/env python3
"""Differential single-step trace: my CPU emulator vs unicorn, starting from
identical OPEN-CON state (snapshot.bin + snapshot.regs).  Find the FIRST
instruction where register/flag state diverges -- that is the CPU emulation
bug causing OPEN CON to return file_not_found.

Constraints:
 - The OPEN-CON local-qualify path runs entirely in DOS machine code in
   low memory; its only interrupts are INT 2Fh (handled by real DOS code at
   IVT[2F], which is also in the snapshot memory) and far CALLs to device
   drivers (also in the snapshot).  So unicorn can run unhooked as long as
   we (a) give it the 1MB memory, (b) the same starting registers, and
   (c) make it execute the INT 2Fh instruction by reading IVT and jumping.
   We override unicorn's interrupt hook to do exactly that.
"""
import sys, struct
sys.path.insert(0, '.')

from capstone import Cs, CS_ARCH_X86, CS_MODE_16
from unicorn import Uc, UC_ARCH_X86, UC_MODE_16, UC_HOOK_CODE, UC_HOOK_MEM_INVALID, UC_ERR_EXCEPTION
# --- load snapshot ---
ram = open('snapshot.bin', 'rb').read()
regs = {}
for line in open('snapshot.regs'):
    k, v = line.strip().split('=')
    regs[k] = int(v, 16)
assert len(ram) == 0x100000

# --- Build my CPU at the same state ---
from main import Emulator
# A throwaway Emulator solely for its CPU object (we override its INT path).
emu = Emulator(boot_file=None, step_mode=False, floppy_image=None)
emu.bios.initialize()
# load memory from snapshot
for a in range(0x100000):
    emu.mem.ram[a] = ram[a]
cpu = emu.cpu
def my_cpu_set_state(regs):
    cpu.ax = regs['ax']; cpu.bx = regs['bx']; cpu.cx = regs['cx']; cpu.dx = regs['dx']
    cpu.sp = regs['sp']; cpu.bp = regs['bp']; cpu.si = regs['si']; cpu.di = regs['di']
    cpu.cs = regs['cs']; cpu.ip = regs['ip']
    cpu.ds = regs['ds']; cpu.es = regs['es']; cpu.ss = regs['ss']
    cpu.flags = regs['flags']
def my_cpu_snapshot():
    return dict(ax=cpu.ax, bx=cpu.bx, cx=cpu.cx, dx=cpu.dx,
                sp=cpu.sp, bp=cpu.bp, si=cpu.si, di=cpu.di,
                cs=cpu.cs, ip=cpu.ip, ds=cpu.ds, es=cpu.es, ss=cpu.ss,
                flags=cpu.flags)
my_cpu_set_state(regs)

# Override my CPU's INT dispatch to mimic real CPU: read IVT[n], push flags/cs/ip,
# jump to handler.  This lets my emulator ALSO execute the INT 2Fh path
# instruction-by-instruction rather than dispatching to a Python handler.
def my_real_int(n):
    saved_flags = cpu.flags
    cpu._push(saved_flags)
    cpu.tf = False
    cpu.if_flag = False
    cpu._push(cpu.cs)
    cpu._push(cpu.ip)
    target_ip = cpu.mem.read_word(n*4)
    target_cs = cpu.mem.read_word(n*4+2)
    cpu.cs = target_cs
    cpu.ip = target_ip
cpu._do_interrupt = my_real_int

# --- Build unicorn at the same state ---
uc = Uc(UC_ARCH_X86, UC_MODE_16)
uc.mem_map(0, 0x100000)
uc.mem_write(0, ram)

# Map my reg names to unicorn constants
from unicorn.x86_const import (
    UC_X86_REG_AX, UC_X86_REG_BX, UC_X86_REG_CX, UC_X86_REG_DX,
    UC_X86_REG_SP, UC_X86_REG_BP, UC_X86_REG_SI, UC_X86_REG_DI,
    UC_X86_REG_CS, UC_X86_REG_DS, UC_X86_REG_ES, UC_X86_REG_SS,
    UC_X86_REG_IP, UC_X86_REG_FLAGS,
)
REGMAP = {
    'ax': UC_X86_REG_AX, 'bx': UC_X86_REG_BX, 'cx': UC_X86_REG_CX, 'dx': UC_X86_REG_DX,
    'sp': UC_X86_REG_SP, 'bp': UC_X86_REG_BP, 'si': UC_X86_REG_SI, 'di': UC_X86_REG_DI,
    'cs': UC_X86_REG_CS, 'ds': UC_X86_REG_DS, 'es': UC_X86_REG_ES, 'ss': UC_X86_REG_SS,
    'ip': UC_X86_REG_IP,
}
for k, const in REGMAP.items():
    uc.reg_write(const, regs[k] & 0xFFFF)
uc.reg_write(UC_X86_REG_FLAGS, regs['flags'] & 0xFFFF)

def uc_snapshot():
    s = {}
    for k, const in REGMAP.items():
        s[k] = uc.reg_read(const) & 0xFFFF
    s['flags'] = uc.reg_read(UC_X86_REG_FLAGS) & 0xFFFF
    return s

# Unicorn handles INT n (0xCD imm) by raising UC_ERR_EXCEPTION unless we hook it.
# We use UC_HOOK_INSN_INVALID-like behaviour: actually unicorn emits UC_ERR_EXCEPTION
# for INT.  We catch it and emulate the INT manually, then resume.
def do_int_in_uc(n):
    # mimic my_real_int: push flags, cs, ip; jump to IVT[n]
    cur_flags = uc.reg_read(UC_X86_REG_FLAGS) & 0xFFFF
    sp = uc.reg_read(UC_X86_REG_SP) & 0xFFFF
    ss = uc.reg_read(UC_X86_REG_SS) & 0xFFFF
    ip = uc.reg_read(UC_X86_REG_IP) & 0xFFFF
    cs = uc.reg_read(UC_X86_REG_CS) & 0xFFFF
    # push flags, cs, ip
    sp = (sp - 2) & 0xFFFF; uc.mem_write((ss<<4)+sp, struct.pack('<H', cur_flags))
    sp = (sp - 2) & 0xFFFF; uc.mem_write((ss<<4)+sp, struct.pack('<H', cs))
    sp = (sp - 2) & 0xFFFF; uc.mem_write((ss<<4)+sp, struct.pack('<H', ip))
    uc.reg_write(UC_X86_REG_SP, sp)
    # clear IF, TF
    new_flags = cur_flags & ~0x0300  # clear TF (0x100) and IF (0x200)
    uc.reg_write(UC_X86_REG_FLAGS, new_flags)
    # read IVT[n]
    ivt = uc.mem_read(n*4, 4)
    tip = struct.unpack('<H', ivt[0:2])[0]
    tcs = struct.unpack('<H', ivt[2:4])[0]
    uc.reg_write(UC_X86_REG_CS, tcs)
    uc.reg_write(UC_X86_REG_IP, tip)

# --- Single-step both, log every instruction, compare ---
md = Cs(CS_ARCH_X86, CS_MODE_16)
md.detail = False

LOG = open('diff_trace.log', 'w')
def log(msg):
    print(msg); LOG.write(msg + '\n'); LOG.flush()

# Track undefined-flag state: MUL/IMUL/DIV/IDIV leave SF/ZF/PF undefined, and
# flag-preserving instructions (POP, PUSH, MOV, etc.) inherit that state.
# We keep masking those flags until a real flag-setting instruction runs.
last_undef = [False]

def instr_preserves_flags(ram, cs, ip):
    """Heuristic: does this instruction NOT modify any flags?"""
    b0 = ram[(cs<<4)+ip]
    if 0x50 <= b0 <= 0x5F: return True
    if 0xB0 <= b0 <= 0xBF: return True
    if b0 in (0x89,0x8B,0x8E,0x8D,0x90,0x98,0x68,0x6A,
              0x06,0x07,0x0E,0x16,0x17,0x1E,0x1F,0xA0,0xA1,
              0xA2,0xA3,0xC6,0xC7,0x86,0x87,0x91,0x92,0x93,0x94,
              0x95,0x96,0x97,0xE3):
        return True
    return False

# Pre-disassemble a few instruction bytes from a physical address.
def disasm_at(ram, phys, count=1):
    out = []
    for insn in md.disasm(ram[phys:phys+16], phys, count=count):
        out.append(f"{insn.mnemonic} {insn.op_str}")
    return ' ; '.join(out) if out else '?'

# Step my CPU one instruction (it may execute prefixes + INT in 1 logical step).
def step_mine():
    """Returns (cs, ip_before, opc) for the executed instruction, or None on halt."""
    if cpu.halted:
        return None
    cs_b = cpu.cs; ip_b = cpu.ip
    # execute() consumes prefixes and one opcode; for INT it routes through
    # _do_interrupt which we overrode to my_real_int (also a single execute() step).
    ok = cpu.execute()
    return (cs_b, ip_b, ok)

def is_rep_prefixed_string_op(ram, cs, ip):
    """True if the bytes at cs:ip are a REP/REPNE-prefixed string instruction.
    unicorn's count=N counts each REP iteration as one 'instruction'; to
    match my CPU (which runs the whole REP loop in one execute()), we step
    unicorn until IP advances past the REP instruction."""
    phys = (cs << 4) + ip
    b0 = ram[phys]
    if b0 not in (0xF3, 0xF2):
        return False
    b1 = ram[phys + 1]
    # MOVS/CMPS/STOS/LODS/SCAS/INS/OUTS opcodes
    return b1 in (0xA4, 0xA5, 0xA6, 0xA7, 0xAA, 0xAB, 0xAC, 0xAD, 0xAE, 0xAF,
                 0x6C, 0x6D, 0x6E, 0x6F)

def step_uc():
    """Single-step unicorn by one instruction.  Returns (cs, ip_before, ok, int_n)."""
    cs_b = uc.reg_read(UC_X86_REG_CS) & 0xFFFF
    ip_b = uc.reg_read(UC_X86_REG_IP) & 0xFFFF
    addr = (cs_b << 4) + ip_b
    # For REP-prefixed string ops, run the WHOLE loop as one logical step
    # (matching my CPU's execute()). Stop when IP differs from start.
    if is_rep_prefixed_string_op(ram, cs_b, ip_b):
        try:
            # Step with a high count; the code hook stops us at first IP change.
            stopped = {'done': False}
            def _hook_stop(uc_, address, size, user_data):
                cur_ip = uc.reg_read(UC_X86_REG_IP) & 0xFFFF
                if cur_ip != ip_b or stopped['done']:
                    stopped['done'] = True
                    uc.emu_stop()
            hook = uc.hook_add(UC_HOOK_CODE, _hook_stop)
            uc.emu_start(addr, addr + 0x10, count=0x10000)
            uc.hook_del(hook)
            return (cs_b, ip_b, True, None)
        except UcError as e:
            log(f"[uc] REP step error {e} at {cs_b:04X}:{ip_b:04X}")
            return None
    try:
        uc.emu_start(addr, addr + 16, count=1)
    except UcError as e:
        if e.errno == UC_ERR_EXCEPTION:
            opcode = uc.mem_read(addr, 1)[0]
            if opcode == 0xCD:
                n = uc.mem_read(addr+1, 1)[0]
                do_int_in_uc(n)
                return (cs_b, ip_b, True, n)
            else:
                log(f"[uc] UC_ERR_EXCEPTION at {cs_b:04X}:{ip_b:04X} opc=0x{opcode:02X}")
                return None
        else:
            log(f"[uc] error {e} at {cs_b:04X}:{ip_b:04X}")
            return None
    return (cs_b, ip_b, True, None)

from unicorn import Uc, UC_ARCH_X86, UC_MODE_16, UC_HOOK_CODE, UC_HOOK_MEM_INVALID, UC_ERR_EXCEPTION
from unicorn import UcError

def snapshots_equal(a, b, undef_flags=False):
    """Compare two register snapshots. AF (bit 4) is excluded from every
    comparison because DOS never branches on AF (only the unused BCD ops
    DAA/DAS/AAA/AAS consume it), and QEMU vs real-hardware compute it
    subtly differently. If undef_flags is True (the just-executed
    instruction's SF/ZF/PF/AF are officially undefined -- MUL/IMUL/DIV/IDIV),
    only compare CF and OF among flags; otherwise compare all non-AF flags."""
    for k in a:
        if k == 'flags':
            if undef_flags:
                # Only CF (bit 0) and OF (bit 11) are defined after MUL/IMUL.
                if (a['flags'] & 0x0801) != (b.get('flags', 0) & 0x0801):
                    return False, 'flags(CF/OF)'
            else:
                # All flags except AF (bit 0x10).
                if (a['flags'] & ~0x10) != (b.get('flags', 0) & ~0x10):
                    return False, 'flags(no-AF)'
        else:
            if a[k] != b.get(k, 0):
                return False, k
    return True, None

def just_executed_has_undefined_flags(ram, cs, ip_before):
    """Return True if the instruction that WAS at cs:ip_before (before stepping)
    is MUL/IMUL/DIV/IDIV r/m8 (F6 /4 /5 /6 /7) or r/m16 (F7 /4 /5 /6 /7).
    Those ops have undefined SF/ZF/PF/AF per Intel SDM."""
    phys = (cs << 4) + ip_before
    b0 = ram[phys]
    if b0 in (0xF6, 0xF7):
        # Need ModR/M byte to extract /reg.
        modrm = ram[phys + 1]
        reg = (modrm >> 3) & 7
        return reg in (4, 5, 6, 7)   # MUL, IMUL, DIV, IDIV
    return False

# Run up to N steps, comparing after each.
N = 20000
log(f"=== Differential trace: mine vs unicorn from OPEN-CON entry {regs['cs']:04X}:{regs['ip']:04X} ===")
log(f"initial regs: {regs}")
log(f"{'step':>5}  {'mine CS:IP':<14} {'unicorn CS:IP':<14}  mine_opcode  diverge?")

for step_no in range(1, N+1):
    # Snapshot BEFORE stepping (already equal at step 0 by construction).
    pre_mine = my_cpu_snapshot()
    # Step both
    mine_res = step_mine()
    uc_res = step_uc()
    if uc_res is None:
        log(f"[uc stopped] step {step_no}: uc could not step")
        break
    if mine_res is None or not mine_res[2]:
        log(f"[mine stopped] step {step_no}: mine halted")
        break
    m_cs, m_ip_before = mine_res[0], mine_res[1]
    u_cs, u_ip_before = uc_res[0], uc_res[1]
    m_after = my_cpu_snapshot()
    u_after = uc_snapshot()
    # Disassemble the instruction mine executed
    m_phys = (m_cs << 4) + m_ip_before
    u_phys = (u_cs << 4) + u_ip_before
    m_disasm = disasm_at(ram, m_phys)
    # Did this instruction have undefined flag semantics (MUL/IMUL/DIV/IDIV)?
    is_undef = just_executed_has_undefined_flags(ram, m_cs, m_ip_before)
    # Propagate undefined-flag state through flag-preserving instructions
    # (POP, MOV, etc.) so we don't false-positive on stale MUL flags.
    if is_undef:
        last_undef[0] = True
    elif not instr_preserves_flags(ram, m_cs, m_ip_before):
        last_undef[0] = False
    undef_flags = last_undef[0]
    # Compare
    same, diff_key = snapshots_equal(m_after, u_after, undef_flags=undef_flags)    # If INT happened in unicorn, also do it in mine? mine's execute() for 0xCD
    # calls _do_interrupt = my_real_int, so the post-state should match IF the
    # IVT read + push + jump match. Compare.
    marker = '' if same else f"  *** DIVERGE: {diff_key} mine=0x{m_after.get(diff_key,0) & 0xFFFF if isinstance(m_after.get(diff_key,0), int) else m_after.get(diff_key,0):04X} uc=0x{u_after.get(diff_key,0) & 0xFFFF if isinstance(u_after.get(diff_key,0), int) else u_after.get(diff_key,0):04X}"
    int_note = f" (INT {uc_res[3]:02X})" if uc_res[3] is not None else ''
    uf_note = ' [undef-flags]' if undef_flags else ''
    log(f"{step_no:5d}  {m_cs:04X}:{m_ip_before:04X}   {u_cs:04X}:{u_ip_before:04X}   {m_disasm:<30}{int_note}{uf_note}{marker}")
    if not same:
        log(f"")
        log(f"=== FIRST DIVERGENCE at step {step_no} ===")
        log(f"instruction @ mine phys 0x{m_phys:05X}: {disasm_at(ram, m_phys, 4)}")
        log(f"instruction @ uc   phys 0x{u_phys:05X}: {disasm_at(ram, u_phys, 4)}")
        log(f"")
        # Pre-step state (for reconstructing address computations)
        log(f"   PRE-STEP (mine): AX={pre_mine['ax']:04X} BX={pre_mine['bx']:04X} "
             f"CX={pre_mine['cx']:04X} DX={pre_mine['dx']:04X} SI={pre_mine['si']:04X} "
             f"DI={pre_mine['di']:04X} DS={pre_mine['ds']:04X} ES={pre_mine['es']:04X}")
        log(f"")
        # If the instruction was XLAT (0xD7), dump bytes around the lookup address
        # from BOTH emulators' memory so we can see where memory diverged.
        if ram[m_phys] == 0xD7:
            xlat_addr = ((pre_mine['ds'] << 4) + pre_mine['bx'] + (pre_mine['ax'] & 0xFF)) & 0xFFFFF
            log(f"   XLAT addr = 0x{xlat_addr:05X} (DS<<4 + BX + AL_pre = "
                 f"{pre_mine['ds']:04X}<<4 + {pre_mine['bx']:04X} + {pre_mine['ax']&0xFF:02X})")
            mine_bytes = bytes(cpu.mem.read_byte(xlat_addr + i) for i in range(-8, 16))
            uc_bytes  = bytes(uc.mem_read(xlat_addr - 8, 24))
            log(f"   mine bytes around addr: {mine_bytes.hex(' ')}")
            log(f"   uc   bytes around addr: {uc_bytes.hex(' ')}")
            # walk backward to find the FIRST differing physical address
            for back in range(0, 24):
                a = (xlat_addr - 8 + back) & 0xFFFFF
                if cpu.mem.read_byte(a) != uc.mem_read(a)[0] if isinstance(uc.mem_read(a), (bytes,bytearray)) else cpu.mem.read_byte(a) != uc.mem_read(a):
                    log(f"   first mem divergence in window at phys 0x{a:05X}: "
                         f"mine=0x{cpu.mem.read_byte(a):02X} uc=0x{uc.mem_read(a)[0]:02X}")
                    break
        log(f"")
        log(f"   register      mine        unicorn")
        for k in ['ax','bx','cx','dx','si','di','bp','sp','cs','ip','ds','es','ss','flags']:
            mark = ' <--' if m_after[k] != u_after[k] else ''
            log(f"   {k:>6}    {m_after[k]:04X}      {u_after[k]:04X}{mark}")
        break
    # Detect IRET back to the original OPEN caller (9DFD:0A21) -- stop there.
    if (m_after['cs'], m_after['ip']) == (0x9DFD, 0x0A21):
        log(f"")
        log(f"=== INT 21h OPEN-CON returned to caller 9DFD:0A21 after {step_no} instructions ===")
        log(f"   final AX=0x{m_after['ax']:04X} CF={int(bool(m_after['flags'] & 1))} (mine)   "
             f"AX=0x{u_after['ax']:04X} CF={int(bool(u_after['flags'] & 1))} (unicorn)")
        break

log(f"\n[trace ended at step {step_no}]")
LOG.close()
