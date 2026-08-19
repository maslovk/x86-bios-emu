"""Differential test: our CPU vs Unicorn, stepping from a live DOS 6.22 state.

Snapshots the machine at the moment SYSINIT's EXEPACK decoder starts, then
single-steps both engines, stopping at the first divergence in registers.
"""
import sys
import time

sys.path.insert(0, '.')
from dosharness import DOSHarness
from unicorn import *
from unicorn.x86_const import *

SNAP_STEP = 1_000_000       # run_steps before snapshotting
TRACE_N = 200_000           # instructions to compare


def snapshot():
    h = DOSHarness(image_path='DOS6_22/disk01.img')
    h.wait_for('Starting MS-DOS', max_steps=6_000_000)
    h.run_steps(SNAP_STEP)
    return h


def main():
    h = snapshot()
    cpu, emu = h.cpu, h.emu

    # Locate the decoder run: step until CS == SYSINIT (3864) executing the
    # bit-stream decoder around 0060..01CB.
    for _ in range(400_000):
        if cpu.cs == 0x3864 and 0x0060 <= cpu.ip < 0x01CB:
            break
        cpu.execute()
        h._pump()

    # Snapshot registers + RAM
    regs = dict(cs=cpu.cs, ip=cpu.ip, ax=cpu.ax, bx=cpu.bx, cx=cpu.cx,
                dx=cpu.dx, si=cpu.si, di=cpu.di, bp=cpu.bp, sp=cpu.sp,
                ss=cpu.ss, ds=cpu.ds, es=cpu.es, flags=cpu.flags)
    ram = bytes(emu.mem.ram)

    # ── Unicorn side ──────────────────────────────────────────────
    mu = Uc(UC_ARCH_X86, UC_MODE_16)
    mu.mem_map(0, 0x100000)
    mu.mem_write(0, ram)

    def set_mu():
        mu.reg_write(UC_X86_REG_CS, regs['cs'])
        mu.reg_write(UC_X86_REG_IP, regs['ip'])
        mu.reg_write(UC_X86_REG_AX, regs['ax'])
        mu.reg_write(UC_X86_REG_BX, regs['bx'])
        mu.reg_write(UC_X86_REG_CX, regs['cx'])
        mu.reg_write(UC_X86_REG_DX, regs['dx'])
        mu.reg_write(UC_X86_REG_SI, regs['si'])
        mu.reg_write(UC_X86_REG_DI, regs['di'])
        mu.reg_write(UC_X86_REG_BP, regs['bp'])
        mu.reg_write(UC_X86_REG_SP, regs['sp'])
        mu.reg_write(UC_X86_REG_SS, regs['ss'])
        mu.reg_write(UC_X86_REG_DS, regs['ds'])
        mu.reg_write(UC_X86_REG_ES, regs['es'])
        mu.reg_write(UC_X86_REG_FLAGS, regs['flags'])
    set_mu()

    map16 = [
        ('cs', UC_X86_REG_CS, 'ip', UC_X86_REG_IP),
        ('ax', UC_X86_REG_AX, 'bx', UC_X86_REG_BX),
        ('cx', UC_X86_REG_CX, 'dx', UC_X86_REG_DX),
        ('si', UC_X86_REG_SI, 'di', UC_X86_REG_DI),
        ('bp', UC_X86_REG_BP, 'sp', UC_X86_REG_SP),
        ('ss', UC_X86_REG_SS, 'ds', UC_X86_REG_DS),
        ('es', UC_X86_REG_ES, None, None),
    ]

    start = (regs['cs'] << 4) + regs['ip']
    print('snapshot at %04X:%04X (phys %05X), tracing %d instrs'
          % (regs['cs'], regs['ip'], start, TRACE_N))

    cur = dict(regs)
    for n in range(TRACE_N):
        # one instruction in Unicorn; a REP completes when IP advances past it
        ip0 = mu.reg_read(UC_X86_REG_IP)
        cs0 = mu.reg_read(UC_X86_REG_CS)
        base0 = (cs0 << 4) + ip0
        try:
            for _ in range(65536):
                mu.emu_start(base0, base0 + 16, count=1)
                if mu.reg_read(UC_X86_REG_IP) != ip0 or mu.reg_read(UC_X86_REG_CS) != cs0:
                    break
            else:
                print('unicorn rep did not terminate at %04X:%04X' % (cs0, ip0))
                break
        except UcError as e:
            print('unicorn error at %04X:%04X: %s' % (cs0, ip0, e))
            break
        # one instruction in ours (snapshot the RAM region it may touch)
        phys0 = (cpu.cs << 4) + cpu.ip
        before = ram  # keep original for final diff; per-instr RAM diff via small window
        if not cpu.execute():
            print('ours halted at instr %d' % n)
            break
        # compare registers
        div = None
        for a, ua, b, ub in map16:
            va, vu = getattr(cpu, a), mu.reg_read(ua)
            if va != vu:
                div = '%s: ours=%04X uni=%04X' % (a, va, vu)
                break
            if b is not None:
                vb, vbu = getattr(cpu, b), mu.reg_read(ub)
                if vb != vbu:
                    div = '%s: ours=%04X uni=%04X' % (b, vb, vbu)
                    break
        if div is None:
            fo, fu = cpu.flags & 0xFFFF, mu.reg_read(UC_X86_REG_FLAGS) & 0xFFFF
            # mask bits unicorn always keeps set/reserved (1) and high bits
            if (fo & 0x0FD5) != (fu & 0x0FD5):
                div = 'flags: ours=%04X uni=%04X' % (fo, fu)
        if div:
            print('DIVERGENCE at instr %d after %04X:%04X: %s'
                  % (n, cs0, ip0, div))
            code = ram[(cs0 << 4) + ip0:(cs0 << 4) + ip0 + 8]
            print('  instruction bytes:', code.hex(' '))
            import capstone
            md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_16)
            for ins in md.disasm(code, ip0):
                print('  %04X  %-8s %s' % (ins.address, ins.mnemonic, ins.op_str))
                break
            return
    print('no divergence in %d instructions' % TRACE_N)


if __name__ == '__main__':
    main()
