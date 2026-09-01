import sys

"""
Simple BIOS Emulator - CPU Core
================================
Minimal x86 real-mode CPU emulator with full instruction decode.
"""

import os


class CPU:
    """Minimal x86 real-mode CPU emulator."""

    _REG16_NAMES = ('ax', 'cx', 'dx', 'bx', 'sp', 'bp', 'si', 'di')

    def __init__(self, memory, io_ports):
        self.mem = memory
        # The emulator's normal memory object exposes a flat 1 MiB bytearray.
        # Keep a direct reference so the instruction hot path avoids a Python
        # method call for every fetch/load/store; test doubles still use the
        # generic memory interface below.
        self._ram = getattr(memory, 'ram', None)
        self.io = io_ports
        self.halted = False
        self.int_no_return = False  # True when INT handler takes over (e.g., boot)
        # A synchronous host BIOS handler can ask the emulator loop to pump
        # devices before the guest is allowed to return from a blocking INT.
        self.retry_software_interrupt = False
        self._retry_interrupt_state = None
        self.insn_count = 0
        self.cycle_count = 0.0
        self.last_instruction_cycles = 0.0
        self.cpu_clock_hz = 4_772_727
        self.cycles_per_instruction = 1.0
        self.ram_wait_cycles = 0
        self.prefetch_wait_cycles = 0
        self.vram_wait_cycles = 0
        # The CPU core has no implicit lifetime limit. Embedders may assign a
        # finite value for bounded tests; the emulator applies its separate
        # --max-instructions policy only to noninteractive sessions.
        self.max_insns = float('inf')
        self.debug = False
        self.step_mode = False  # Print mnemonic + regs each instruction

        # General purpose registers (16-bit)
        self.ax = self.cx = self.dx = self.bx = 0
        self.sp = 0xFFFE
        self.bp = 0
        self.si = 0
        self.di = 0

        # Segment registers
        self.cs = 0xF000
        self.ds = 0x0000
        self.es = 0x0000
        self.ss = 0x0000
        self.ip = 0xFFF0

        # Flags (only bits 0-12 used)
        self.flags = 0x0002

        # Segment override (set by prefix, used by next memory instruction)
        self._seg_override = None
        self._rep_prefix = None
        self._irq_shadow = 0

        # ── 80286 protected-mode state ─────────────────────────────────
        # MSW: bit 0 PE, 1 MP, 2 EM, 3 TS; high nibble reads as written
        # (286 reset value 0xFFF0).  ``_pm`` mirrors the PE bit as a plain
        # bool for the hot address-translation paths.
        self.msw = 0xFFF0
        self._pm = False
        # Physical address width: the backing RAM decides how far
        # linear addresses reach (power-of-two sizes give a clean mask).
        # The A20 gate masks address bit 20 when disabled, wrapping the
        # 1 MiB+64 KiB region back to zero as the real gate does.
        self._a20 = True
        ram_size = len(self._ram) if self._ram is not None else 0x100000
        # Only power-of-two backing sizes give a clean address mask; a
        # non-power-of-two buffer (e.g. 1 MiB + 64 KiB with an HMA tail)
        # addresses only its power-of-two prefix.
        largest_pow2 = 1 << (ram_size.bit_length() - 1)
        self._phys_mask = largest_pow2 - 1
        self._ram_size = largest_pow2
        # Descriptor-table registers.  Bases are 24-bit on the 80286 and
        # are masked into the emulator's 1 MiB physical map on use.
        self.gdt_base = 0
        self.gdt_limit = 0
        self.idt_base = 0
        self.idt_limit = 0x03FF
        self.ldtr_selector = 0
        self.tr_selector = 0
        # Hidden descriptor caches, keyed by selector value.  A selector
        # loaded into any segment register (in protected mode) is
        # translated here once; equal selectors therefore share one
        # translation, which matches identical descriptor-cache contents
        # on hardware.  Entries are seeded with real-mode bases when PE is
        # enabled so an un-reloaded segment keeps working, exactly like
        # the 286's physical caches.
        self._desc_cache = {}
        # Fast linear base for instruction fetch (CS descriptor cache).
        self._code_base = (self.cs << 4) & 0xFFFFF
        # Fault-delivery latch: a fault raised while another fault is in
        # flight parks the CPU (emulating the hardware's double/triple
        # fault escalation without a reset path).
        self._exception_active = False
        # Current privilege level: in real mode always 0; in protected
        # mode it is the RPL of the loaded CS selector (conforming code
        # segments inherit the caller's CPL).
        self._cpl = 0

    # ── 8-bit register properties ──────────────────────────────────
    @property
    def al(self): return self.ax & 0xFF
    @al.setter
    def al(self, v): self.ax = (self.ax & 0xFF00) | (v & 0xFF)

    @property
    def ah(self): return (self.ax >> 8) & 0xFF
    @ah.setter
    def ah(self, v): self.ax = (self.ax & 0x00FF) | ((v & 0xFF) << 8)

    @property
    def cl(self): return self.cx & 0xFF
    @cl.setter
    def cl(self, v): self.cx = (self.cx & 0xFF00) | (v & 0xFF)

    @property
    def ch(self): return (self.cx >> 8) & 0xFF
    @ch.setter
    def ch(self, v): self.cx = (self.cx & 0x00FF) | ((v & 0xFF) << 8)

    @property
    def dl(self): return self.dx & 0xFF
    @dl.setter
    def dl(self, v): self.dx = (self.dx & 0xFF00) | (v & 0xFF)

    @property
    def dh(self): return (self.dx >> 8) & 0xFF
    @dh.setter
    def dh(self, v): self.dx = (self.dx & 0x00FF) | ((v & 0xFF) << 8)

    @property
    def bl(self): return self.bx & 0xFF
    @bl.setter
    def bl(self, v): self.bx = (self.bx & 0xFF00) | (v & 0xFF)

    @property
    def bh(self): return (self.bx >> 8) & 0xFF
    @bh.setter
    def bh(self, v): self.bx = (self.bx & 0x00FF) | ((v & 0xFF) << 8)

    # ── Flag properties ────────────────────────────────────────────

    @property
    def zf(self): return bool(self.flags & 0x40)
    @zf.setter
    def zf(self, v): self.flags = (self.flags & ~0x40) | (0x40 if v else 0)

    @property
    def cf(self): return bool(self.flags & 0x01)
    @cf.setter
    def cf(self, v): self.flags = (self.flags & ~0x01) | (0x01 if v else 0)

    @property
    def sf(self): return bool(self.flags & 0x80)
    @sf.setter
    def sf(self, v): self.flags = (self.flags & ~0x80) | (0x80 if v else 0)

    @property
    def of(self): return bool(self.flags & 0x0800)
    @of.setter
    def of(self, v): self.flags = (self.flags & ~0x0800) | (0x0800 if v else 0)

    @property
    def pf(self): return bool(self.flags & 0x04)
    @pf.setter
    def pf(self, v): self.flags = (self.flags & ~0x04) | (0x04 if v else 0)

    @property
    def af(self): return bool(self.flags & 0x10)
    @af.setter
    def af(self, v): self.flags = (self.flags & ~0x10) | (0x10 if v else 0)

    @property
    def tf(self): return bool(self.flags & 0x100)
    @tf.setter
    def tf(self, v): self.flags = (self.flags & ~0x100) | (0x100 if v else 0)

    @property
    def if_flag(self): return bool(self.flags & 0x200)
    @if_flag.setter
    def if_flag(self, v): self.flags = (self.flags & ~0x200) | (0x200 if v else 0)

    @property
    def df(self): return bool(self.flags & 0x400)
    @df.setter
    def df(self, v): self.flags = (self.flags & ~0x400) | (0x400 if v else 0)

    # ── Register helpers ───────────────────────────────────────────

    def _reg16(self, r):
        return (self.ax, self.cx, self.dx, self.bx,
                self.sp, self.bp, self.si, self.di)[r]

    def _set_reg16(self, r, v):
        setattr(self, self._REG16_NAMES[r], v & 0xFFFF)

    def _get_reg16(self, r):
        return getattr(self, self._REG16_NAMES[r]) & 0xFFFF

    def _get_reg8(self, r):
        """Internal 8-bit register: 0=AL,1=AH,2=CL,3=CH,4=DL,5=DH,6=BL,7=BH."""
        value = (self.ax, self.cx, self.dx, self.bx)[r >> 1]
        return (value & 0xFF) if not (r & 1) else (value >> 8)

    def _set_reg8(self, r, v):
        """Internal 8-bit register: 0=AL,1=AH,2=CL,3=CH,4=DL,5=DH,6=BL,7=BH."""
        name = self._REG16_NAMES[r >> 1]
        value = getattr(self, name)
        if not (r & 1):
            setattr(self, name, (value & 0xFF00) | (v & 0xFF))
        else:
            setattr(self, name, (value & 0x00FF) | ((v & 0xFF) << 8))

    # ModR/M 8-bit register mapping: 0=AL,1=CL,2=DL,3=BL,4=AH,5=CH,6=DH,7=BH
    _modrm8_map = [0, 2, 4, 6, 1, 3, 5, 7]  # ModR/M idx → internal idx

    def _get_reg8_modrm(self, r):
        """ModR/M 8-bit register access."""
        return self._get_reg8(self._modrm8_map[r])

    def _set_reg8_modrm(self, r, v):
        """ModR/M 8-bit register store."""
        self._set_reg8(self._modrm8_map[r], v)

    # ── Memory access ──────────────────────────────────────────────

    def _gate(self, address):
        """Apply the A20 gate and the physical address mask."""
        if not self._a20:
            address &= ~0x100000
        return address & self._phys_mask

    def set_a20(self, enabled):
        """Set the A20 address-line gate (keyboard port 0x64/0x92)."""
        self._a20 = bool(enabled)

    def _physw(self, seg, off):
        """Physical address of a word operand at segment:offset.

        Both bytes must lie within the segment limit in protected mode
        (the second byte's check raises ``#GP`` before any access).
        """
        if self._pm:
            self._phys(seg, (off + 1) & 0xFFFF)
        return self._phys(seg, off)

    def _phys(self, seg, off):
        """Linear (physical, 1 MiB map) address for segment:offset.

        Real mode: base = selector * 16.  Protected mode: base comes from
        the hidden descriptor cache populated at segment-register load;
        a segment with no translation (null selector loaded into DS/ES
        and then used) faults with ``#GP``.  Byte-level limit checks
        raise ``#GP(selector)`` for data/code segments and ``#SS`` for
        stack operations (checked in ``_push``/``_pop``); word accesses
        that straddle the limit are a documented milestone-2 gap.
        """
        if self._pm:
            desc = self._desc_cache.get(seg)
            if desc is None:
                self._raise_gp(0)
            off &= 0xFFFF
            ar = desc[2]
            if (ar & 0x18) == 0x10 and (ar & 0x04):
                # Expand-down data: valid offsets are limit+1 .. 0xFFFF.
                if off <= desc[1]:
                    self._raise_gp(seg & 0xFFFC)
            elif off > desc[1]:
                self._raise_gp(seg & 0xFFFC)
            return (desc[0] + off) & 0xFFFFF
        return ((seg << 4) + (off & 0xFFFF)) & 0xFFFFF

    def _stack_check(self, off):
        """``#SS(selector)`` when SP-relative access exceeds the SS limit."""
        desc = self._desc_cache.get(self.ss)
        if desc is None:
            self._raise_ss(self.ss & 0xFFFC)
        ar = desc[2]
        if (ar & 0x18) == 0x10 and (ar & 0x04):
            if off <= desc[1]:
                self._raise_ss(self.ss & 0xFFFC)
        elif off > desc[1]:
            self._raise_ss(self.ss & 0xFFFC)

    def _readb(self, a):
        address = a & 0xFFFFF
        if 0xB8000 <= address < 0xB9000:
            self.cycle_count += self.vram_wait_cycles
        elif not (0xA0000 <= address < 0xB0000
                  and getattr(self.io.video, 'graphics_mode', False)):
            self.cycle_count += self.ram_wait_cycles
        if 0xA0000 <= address < 0xB0000 \
                and getattr(self.io.video, 'graphics_mode', False):
            return self.io.video.graphics_read(a - 0xA0000)
        if self._ram is not None:
            return self._ram[self._gate(a)]
        return self.mem.read_byte(self._gate(a))

    def _readw(self, a):
        address = a & 0xFFFFF
        if not (0xA0000 <= address < 0xB0000
                and getattr(self.io.video, 'graphics_mode', False)):
            self.cycle_count += 2 * self.ram_wait_cycles
        if 0xA0000 <= (a & 0xFFFFF) < 0xB0000 \
                and getattr(self.io.video, 'graphics_mode', False):
            return self._readb(a) | (self._readb(a + 1) << 8)
        if self._ram is not None:
            a = self._gate(a)
            return self._ram[a] | (self._ram[self._gate(a + 1)] << 8)
        return self.mem.read_word(self._gate(a))

    def _writeb(self, a, v):
        address = a & 0xFFFFF
        if 0xB8000 <= address < 0xB9000:
            self.cycle_count += self.vram_wait_cycles
        elif not (0xA0000 <= address < 0xB0000
                  and getattr(self.io.video, 'graphics_mode', False)):
            self.cycle_count += self.ram_wait_cycles
        if 0xA0000 <= address < 0xB0000 \
                and getattr(self.io.video, 'graphics_mode', False):
            self.io.video.graphics_write(a - 0xA0000, v)
            return
        if self._ram is not None:
            self._ram[self._gate(a)] = v & 0xFF
        else:
            self.mem.write_byte(self._gate(a), v)

    def _writew(self, a, v):
        address = a & 0xFFFFF
        if not (0xA0000 <= address < 0xB0000
                and getattr(self.io.video, 'graphics_mode', False)):
            self.cycle_count += 2 * self.ram_wait_cycles
        if 0xA0000 <= (a & 0xFFFFF) < 0xB0000 \
                and getattr(self.io.video, 'graphics_mode', False):
            self._writeb(a, v)
            self._writeb(a + 1, v >> 8)
            return
        if self._ram is not None:
            a = self._gate(a)
            self._ram[a] = v & 0xFF
            self._ram[self._gate(a + 1)] = (v >> 8) & 0xFF
        else:
            self.mem.write_word(self._gate(a), v)

    def _fetchb(self):
        self.cycle_count += self.prefetch_wait_cycles
        base = self._code_base if self._pm else (self.cs << 4)
        if self._ram is not None:
            v = self._ram[self._gate(base + self.ip)]
        else:
            v = self._readb(self._gate(base + self.ip))
        self.ip = (self.ip + 1) & 0xFFFF
        return v

    def _fetchw(self):
        self.cycle_count += 2 * self.prefetch_wait_cycles
        base = self._code_base if self._pm else (self.cs << 4)
        if self._ram is not None:
            a = self._gate(base + self.ip)
            v = self._ram[a] | (self._ram[self._gate(a + 1)] << 8)
        else:
            v = self._readw(self._gate(base + self.ip))
        self.ip = (self.ip + 2) & 0xFFFF
        return v

    # ── ModR/M decoding ────────────────────────────────────────────

    def _decode_modrm(self):
        b = self._fetchb()
        return (b >> 6) & 3, (b >> 3) & 7, b & 7

    def _read_disp(self, mod, rm):
        if mod == 0 and rm == 6:
            return self._fetchw()
        elif mod == 1:
            d = self._fetchb()
            return d | 0xFF00 if d & 0x80 else d
        elif mod == 2:
            return self._fetchw()
        return 0

    def _skip_disp(self, mod, rm):
        if mod == 0 and rm == 6:
            self.ip = (self.ip + 2) & 0xFFFF
        elif mod == 1:
            self.ip = (self.ip + 1) & 0xFFFF
        elif mod == 2:
            self.ip = (self.ip + 2) & 0xFFFF

    def _ea(self, mod, rm, seg=None, wide=False):
        """Effective address (physical; ``wide`` bounds-checks byte 2)."""
        if mod == 3:
            raise RuntimeError("_ea called with mod=3")
        # Determine segment: override > explicit > BP→SS > DS
        if seg is None:
            if self._seg_override is not None:
                seg = self._seg_override
            elif (mod == 0 and rm in (2, 3)) or (mod != 0 and rm in (2, 3, 6)):
                seg = self.ss
            else:
                seg = self.ds
        if mod == 0 and rm == 6:
            # In 16-bit addressing, mod=00 rm=110 encodes a direct disp16.
            disp = self._fetchw()
            return self._phys(seg, disp)
        if rm == 0:
            base = self.bx + self.si
        elif rm == 1:
            base = self.bx + self.di
        elif rm == 2:
            base = self.bp + self.si
        elif rm == 3:
            base = self.bp + self.di
        elif rm == 4:
            base = self.si
        elif rm == 5:
            base = self.di
        elif rm == 6:
            base = self.bp
        else:
            base = self.bx
        disp = self._read_disp(mod, rm)
        if wide and self._pm:
            # A word operand must fit entirely inside the segment: the
            # second byte's limit check raises #GP before the access.
            self._phys(seg, (base + disp + 1) & 0xFFFF)
        return self._phys(seg, base + disp)

    def _ea_byte(self, mod, rm, seg=None):
        if mod == 3:
            return self._get_reg8_modrm(rm)
        return self._readb(self._ea(mod, rm, seg))

    def _ea_word(self, mod, rm, seg=None):
        if mod == 3:
            return self._get_reg16(rm)
        return self._readw(self._ea(mod, rm, seg, wide=True))

    def _ea_write_byte(self, mod, rm, val, seg=None):
        if mod == 3:
            self._set_reg8_modrm(rm, val)
        else:
            self._writeb(self._ea(mod, rm, seg), val)

    def _ea_write_word(self, mod, rm, val, seg=None):
        if mod == 3:
            self._set_reg16(rm, val)
        else:
            self._writew(self._ea(mod, rm, seg, wide=True), val)

    def _default_data_seg(self):
        return self._seg_override if self._seg_override is not None else self.ds

    # ── Stack ──────────────────────────────────────────────────────

    def _push(self, val):
        new_sp = (self.sp - 2) & 0xFFFF
        if self._pm:
            self._stack_check(new_sp)
            self._stack_check((new_sp + 1) & 0xFFFF)
        self.sp = new_sp
        self._writew(self._phys(self.ss, self.sp), val & 0xFFFF)

    def _pop(self):
        if self._pm:
            self._stack_check(self.sp)
            self._stack_check((self.sp + 1) & 0xFFFF)
        val = self._readw(self._phys(self.ss, self.sp))
        self.sp = (self.sp + 2) & 0xFFFF
        return val

    # ── Flag update ────────────────────────────────────────────────

    def _flags_add8(self, a, b):
        r = (a + b) & 0xFF
        self.zf = r == 0
        self.sf = bool(r & 0x80)
        self.cf = (a + b) > 0xFF
        self.af = bool((a ^ b ^ r) & 0x10)
        self.of = bool((~(a ^ b) & (a ^ r)) & 0x80)
        self.pf = bin(r).count('1') % 2 == 0
        return r

    def _flags_add16(self, a, b):
        r = (a + b) & 0xFFFF
        self.zf = r == 0
        self.sf = bool(r & 0x8000)
        self.cf = (a + b) > 0xFFFF
        self.af = bool((a ^ b ^ r) & 0x10)
        self.of = bool((~(a ^ b) & (a ^ r)) & 0x8000)
        self.pf = bin(r & 0xFF).count('1') % 2 == 0
        return r

    def _flags_sub8(self, a, b):
        r = (a - b) & 0xFF
        self.zf = r == 0
        self.sf = bool(r & 0x80)
        self.cf = a < b
        self.af = bool((a ^ b ^ r) & 0x10)
        self.of = bool(((a ^ b) & (a ^ r) & 0x80))
        self.pf = bin(r).count('1') % 2 == 0
        return r

    def _flags_sub16(self, a, b):
        r = (a - b) & 0xFFFF
        self.zf = r == 0
        self.sf = bool(r & 0x8000)
        self.cf = a < b
        self.af = bool((a ^ b ^ r) & 0x10)
        self.of = bool(((a ^ b) & (a ^ r) & 0x8000))
        self.pf = bin(r & 0xFF).count('1') % 2 == 0
        return r

    @staticmethod
    def _idiv_trunc(dividend, divisor):
        """Return an x86-style signed quotient and remainder.

        Python's ``//`` rounds toward negative infinity, while 8086 IDIV
        truncates the quotient toward zero and gives the remainder the same
        sign as the dividend.  Keeping this in one helper avoids subtly
        different behavior between the byte and word instruction groups.
        """
        quotient = abs(dividend) // abs(divisor)
        if (dividend < 0) != (divisor < 0):
            quotient = -quotient
        return quotient, dividend - quotient * divisor

    def _raise_divide_error(self):
        """Raise x86 ``#DE`` (INT 0), halting if no vector is installed."""
        # The standalone CPU has no interrupt dispatcher; the Emulator
        # replaces this bound method with its BIOS-aware hook.  Identify the
        # former so unit-level faults remain deterministic even when the test
        # program occupies the low IVT addresses.
        bare_cpu_interrupt = (getattr(self._do_interrupt, '__func__', None)
                              is CPU._do_interrupt)
        self._do_interrupt(0)
        if bare_cpu_interrupt and not self.int_no_return:
            self.halted = True

    def _flags_logic8(self, r):
        r &= 0xFF
        self.zf = r == 0
        self.sf = bool(r & 0x80)
        self.cf = False
        self.of = False
        self.af = False   # real x86 clears AF on logic ops
        self.pf = bin(r).count('1') % 2 == 0

    def _set_szp8(self, r):
        """Set ZF/SF/PF from an 8-bit result, leaving CF/AF/OF untouched
        (used by DAA/DAS, whose CF/AF are set explicitly and whose OF is
        undefined per the SDM)."""
        r &= 0xFF
        self.zf = r == 0
        self.sf = bool(r & 0x80)
        self.pf = bin(r).count('1') % 2 == 0

    def _flags_logic16(self, r):
        r &= 0xFFFF
        self.zf = r == 0
        self.sf = bool(r & 0x8000)
        self.cf = False
        self.of = False
        self.af = False   # real x86 clears AF on logic ops
        self.pf = bin(r & 0xFF).count('1') % 2 == 0

    # ── Arithmetic helpers for opcode groups ───────────────────────

    def _do_add8(self, a, b): return self._flags_add8(a, b)
    def _do_add16(self, a, b): return self._flags_add16(a, b)
    def _do_sub8(self, a, b): return self._flags_sub8(a, b)
    def _do_sub16(self, a, b): return self._flags_sub16(a, b)
    def _do_and8(self, a, b):
        r = (a & b) & 0xFF
        self._flags_logic8(r)
        return r
    def _do_and16(self, a, b):
        r = (a & b) & 0xFFFF
        self._flags_logic16(r)
        return r
    def _do_or8(self, a, b):
        r = (a | b) & 0xFF
        self._flags_logic8(r)
        return r
    def _do_or16(self, a, b):
        r = (a | b) & 0xFFFF
        self._flags_logic16(r)
        return r
    def _do_xor8(self, a, b):
        r = (a ^ b) & 0xFF
        self._flags_logic8(r)
        return r
    def _do_xor16(self, a, b):
        r = (a ^ b) & 0xFFFF
        self._flags_logic16(r)
        return r

    def _exec_al_arith(self, opc, op_pair):
        """Handle opcodes 00-05, 08-0D, 10-15, 18-1D, 20-25, 28-2D, 30-35."""
        base = opc & 0x38
        idx = opc & 7
        byte_ops = {
            0x00: self._do_add8,
            0x08: self._do_or8,
            0x10: lambda a, b: self._do_add8(a, b + (1 if self.cf else 0)),
            0x18: lambda a, b: self._do_sub8(a, b + (1 if self.cf else 0)),
            0x20: self._do_and8,
            0x28: self._do_sub8,
            0x30: self._do_xor8,
        }
        word_ops = {
            0x00: self._do_add16,
            0x08: self._do_or16,
            0x10: lambda a, b: self._do_add16(a, b + (1 if self.cf else 0)),
            0x18: lambda a, b: self._do_sub16(a, b + (1 if self.cf else 0)),
            0x20: self._do_and16,
            0x28: self._do_sub16,
            0x30: self._do_xor16,
        }

        if idx <= 3:
            mod, reg, rm = self._decode_modrm()
            if idx in (0, 2):
                if idx == 0:
                    src = self._get_reg8_modrm(reg)
                    if mod == 3:
                        dst = self._get_reg8_modrm(rm)
                        result = byte_ops[base](dst, src)
                        self._set_reg8_modrm(rm, result)
                    else:
                        addr = self._ea(mod, rm)
                        dst = self._readb(addr)
                        result = byte_ops[base](dst, src)
                        self._writeb(addr, result)
                else:
                    src = self._get_reg8_modrm(rm) if mod == 3 else self._ea_byte(mod, rm)
                    dst = self._get_reg8_modrm(reg)
                    self._set_reg8_modrm(reg, byte_ops[base](dst, src))
            else:
                if idx == 1:
                    src = self._get_reg16(reg)
                    if mod == 3:
                        dst = self._get_reg16(rm)
                        result = word_ops[base](dst, src)
                        self._set_reg16(rm, result)
                    else:
                        addr = self._ea(mod, rm)
                        dst = self._readw(addr)
                        result = word_ops[base](dst, src)
                        self._writew(addr, result)
                else:
                    src = self._get_reg16(rm) if mod == 3 else self._ea_word(mod, rm)
                    dst = self._get_reg16(reg)
                    self._set_reg16(reg, word_ops[base](dst, src))
        elif idx == 4:
            imm = self._fetchb()
            self.al = byte_ops[base](self.al, imm)
        elif idx == 5:
            imm = self._fetchw()
            self.ax = word_ops[base](self.ax, imm)

    def _exec_al_cmp(self, opc):
        """Handle CMP opcodes 38-3D."""
        idx = opc & 7
        if idx <= 3:
            mod, reg, rm = self._decode_modrm()
            if idx == 0:
                lhs = self._get_reg8_modrm(rm) if mod == 3 else self._ea_byte(mod, rm)
                rhs = self._get_reg8_modrm(reg)
                self._do_sub8(lhs, rhs)
            elif idx == 1:
                lhs = self._get_reg16(rm) if mod == 3 else self._ea_word(mod, rm)
                rhs = self._get_reg16(reg)
                self._do_sub16(lhs, rhs)
            elif idx == 2:
                lhs = self._get_reg8_modrm(reg)
                rhs = self._get_reg8_modrm(rm) if mod == 3 else self._ea_byte(mod, rm)
                self._do_sub8(lhs, rhs)
            else:
                lhs = self._get_reg16(reg)
                rhs = self._get_reg16(rm) if mod == 3 else self._ea_word(mod, rm)
                self._do_sub16(lhs, rhs)
        elif idx == 4:
            imm = self._fetchb()
            self._do_sub8(self.al, imm)
        elif idx == 5:
            imm = self._fetchw()
            self._do_sub16(self.ax, imm)

    def _exec_modrm_arith(self, mod, rm, reg, imm, is_word=True):
        """GROUP 1: ADD(0) OR(1) ADC(2) SBB(3) AND(4) SUB(5) XOR(6) CMP(7)."""
        if is_word:
            read_reg = self._get_reg16
            write_reg = self._set_reg16
            read_mem = self._readw
            write_mem = self._writew
            add_op = self._do_add16
            or_op = self._do_or16
            sub_op = self._do_sub16
            and_op = self._do_and16
            xor_op = self._do_xor16
            mask = 0xFFFF
        else:
            read_reg = self._get_reg8_modrm
            write_reg = self._set_reg8_modrm
            read_mem = self._readb
            write_mem = self._writeb
            add_op = self._do_add8
            or_op = self._do_or8
            sub_op = self._do_sub8
            and_op = self._do_and8
            xor_op = self._do_xor8
            mask = 0xFF

        imm &= mask

        if reg == 7:  # CMP - no store
            if mod == 3:
                value = read_reg(rm)
            else:
                value = read_mem(self._ea(mod, rm))
            sub_op(value, imm)
            return

        if mod == 3:
            value = read_reg(rm)
        else:
            addr = self._ea(mod, rm)
            value = read_mem(addr)

        if reg == 0:
            result = add_op(value, imm)
        elif reg == 1:
            result = or_op(value, imm)
        elif reg == 2:
            carry = 1 if self.cf else 0
            result = add_op(value, (imm + carry) & mask)
        elif reg == 3:
            borrow = 1 if self.cf else 0
            result = sub_op(value, (imm + borrow) & mask)
        elif reg == 4:
            result = and_op(value, imm)
        elif reg == 5:
            result = sub_op(value, imm)
        elif reg == 6:
            result = xor_op(value, imm)
        else:
            return

        if mod == 3:
            write_reg(rm, result)
        else:
            write_mem(addr, result)

    def _exec_group1_mem_arith(self, addr, reg, imm, is_word=True):
        """GROUP 1 helper for memory operands when EA must be resolved before imm."""
        if is_word:
            read_mem = self._readw
            write_mem = self._writew
            add_op = self._do_add16
            or_op = self._do_or16
            sub_op = self._do_sub16
            and_op = self._do_and16
            xor_op = self._do_xor16
            mask = 0xFFFF
        else:
            read_mem = self._readb
            write_mem = self._writeb
            add_op = self._do_add8
            or_op = self._do_or8
            sub_op = self._do_sub8
            and_op = self._do_and8
            xor_op = self._do_xor8
            mask = 0xFF

        imm &= mask
        value = read_mem(addr)

        if reg == 7:
            sub_op(value, imm)
            return
        if reg == 0:
            result = add_op(value, imm)
        elif reg == 1:
            result = or_op(value, imm)
        elif reg == 2:
            carry = 1 if self.cf else 0
            result = add_op(value, (imm + carry) & mask)
        elif reg == 3:
            borrow = 1 if self.cf else 0
            result = sub_op(value, (imm + borrow) & mask)
        elif reg == 4:
            result = and_op(value, imm)
        elif reg == 5:
            result = sub_op(value, imm)
        elif reg == 6:
            result = xor_op(value, imm)
        else:
            return

        write_mem(addr, result)

    # ── LEA address calculation (no memory access) ─────────────────

    def _lea_address(self, mod, rm):
        base_map = {
            0: self.bx + self.si, 1: self.bx + self.di,
            2: self.bp + self.si, 3: self.bp + self.di,
            4: self.si, 5: self.di, 6: self.bp, 7: self.bx,
        }
        if mod == 0 and rm == 6:
            disp = self._fetchw()
            return disp
        base = base_map.get(rm, self.bp)
        disp = self._read_disp(mod, rm)
        return base + disp

    # ── Segment setters ────────────────────────────────────────────

    def _set_es(self, v): self._load_sreg('es', v)
    def _set_cs(self, v): self._load_sreg('cs', v)
    def _set_ss(self, v):
        self._load_sreg('ss', v)
    def _set_ds(self, v): self._load_sreg('ds', v)

    # ── 80286 protected mode: descriptors ───────────────────────────

    # Access-byte type bits (S=1 segments): bit 3 = code, bit 2 =
    # conforming (code) / expand-down (data), bit 1 = readable (code) /
    # writable (data), bit 0 = accessed.  The classic ring-0 descriptors
    # are 0x9A (execute/read code) and 0x92 (read/write data).
    AR_CODE = 0x08
    AR_RDWR = 0x02

    def _load_sreg(self, name, value):
        """Architectural segment-register load.

        In real mode this is a plain assignment (and keeps the CS fetch
        base in sync).  In protected mode the selector is translated
        through the GDT/LDT into a descriptor-cache entry; ``#GP`` faults
        on invalid selectors or descriptor types.
        """
        value &= 0xFFFF
        if not self._pm:
            setattr(self, name, value)
            if name == 'cs':
                self._code_base = (value << 4) & 0xFFFFF
            return
        desc = self._translate_selector(value)
        ar = desc[2]
        is_code = (ar & 0x18) == 0x18
        if name == 'cs':
            if not is_code:
                self._raise_gp(value & 0xFFFC)
        elif name == 'ss':
            # SS is the strict one: writable data with DPL == CPL and
            # selector RPL == CPL (a mismatch is #GP, not merely outer).
            if (ar & 0x18) != 0x10 or not (ar & self.AR_RDWR):
                self._raise_gp(value & 0xFFFC)
            if self._pm and (((ar >> 5) & 3) != self._cpl
                             or (value & 3) != self._cpl):
                self._raise_gp(value & 0xFFFC)
        else:
            # DS/ES accept any data segment or readable code; a null
            # selector is loadable and faults only on use.
            if value & 0xFFFC and is_code and not (ar & self.AR_RDWR):
                self._raise_gp(value & 0xFFFC)
            if (self._pm and value & 0xFFFC
                    and not is_code
                    and ((ar >> 5) & 3) < max(self._cpl, value & 3)):
                self._raise_gp(value & 0xFFFC)
        # Architectural segment loads set the Accessed bit (type bit 0)
        # in the descriptor itself, as the 286 does.
        if not (desc[2] & 0x01):
            desc = (desc[0], desc[1], desc[2] | 0x01, desc[3])
            self._writeb(desc[3] + 5, desc[2])
        setattr(self, name, value)
        # Architectural segment loads set the Accessed bit (type bit 0)
        # in the descriptor itself, as the 286 does.
        if not (desc[2] & 0x01):
            desc = (desc[0], desc[1], desc[2] | 0x01, desc[3])
            self._writeb(desc[3] + 5, desc[2])
        self._desc_cache[value] = desc
        if name == 'cs':
            self._code_base = desc[0] & 0xFFFFF
            if not ((desc[2] & 0x18) == 0x18 and (desc[2] & 0x04)):
                # Non-conforming code: CPL follows the selector's RPL.
                self._cpl = value & 0x0003

    def _translate_selector(self, sel):
        """Return (base, limit, access, descriptor_address) for a selector.

        ``#GP`` for out-of-table indices or bad descriptor types and
        ``#NP`` for a not-present segment.  The 286 descriptor layout is
        limit:16 base:24 with a 386-style extension byte left at zero.
        The descriptor's linear address is returned so architectural
        loads can set the Accessed bit in the table itself.
        """
        index = sel >> 3
        if sel & 0x04:
            table_base, table_limit = self._ldt_base(), self._ldt_limit()
        else:
            table_base, table_limit = self.gdt_base, self.gdt_limit
        addr = table_base + index * 8
        if index == 0 or addr + 7 > table_base + table_limit:
            self._raise_gp(sel & 0xFFFC)
        raw = bytes(self._readb((addr + i) & 0xFFFFF) for i in range(8))
        limit = raw[0] | (raw[1] << 8)
        base = raw[2] | (raw[3] << 8) | (raw[4] << 16)
        access = raw[5]
        if not (access & 0x80):
            self._raise_np(sel & 0xFFFC)
        return (base & 0xFFFFF, limit, access, addr & 0xFFFFF)

    def _ldt_base(self):
        desc = self._desc_cache.get(self.ldtr_selector)
        return desc[0] if desc else self.gdt_base

    def _ldt_limit(self):
        desc = self._desc_cache.get(self.ldtr_selector)
        return desc[1] if desc else 0

    def _seed_real_mode_caches(self):
        """Seed descriptor caches for the current selectors on PE enable.

        The physical 286 keeps its descriptor caches across the switch, so
        segments not yet reloaded still address real-mode style (base =
        selector * 16, limit 64 KiB).  Mirroring that keeps the transition
        seamless until the guest installs proper selectors.
        """
        for name in ('cs', 'ds', 'es', 'ss'):
            sel = getattr(self, name)
            self._desc_cache[sel] = ((sel << 4) & 0xFFFFF, 0xFFFF,
                                     0x93 if name != 'cs' else 0x9B)

    def _tss_word(self, offset):
        """Read a word from the current TSS (286 layout)."""
        desc = self._desc_cache.get(self.tr_selector)
        if desc is None:
            self._raise_gp(self.tr_selector & 0xFFFC)
        return self._readw((desc[0] + offset) & 0xFFFFF)

    def _write_tss_word(self, offset, value):
        """Write a word into the current TSS (286 layout)."""
        desc = self._desc_cache.get(self.tr_selector)
        if desc is None:
            self._raise_gp(self.tr_selector & 0xFFFC)
        self._writew((desc[0] + offset) & 0xFFFFF, value & 0xFFFF)

    # 286 TSS field offsets (dynamic set saved/restored on task switch).
    TSS_BACKLINK = 0x00
    TSS_IP = 0x0E
    TSS_FLAGS = 0x10
    TSS_AX = 0x12
    TSS_CX = 0x14
    TSS_DX = 0x16
    TSS_BX = 0x18
    TSS_SP = 0x1A
    TSS_BP = 0x1C
    TSS_SI = 0x1E
    TSS_DI = 0x20
    TSS_ES = 0x22
    TSS_CS = 0x24
    TSS_SS = 0x26
    TSS_DS = 0x28
    TSS_LDT = 0x2A

    def _save_task_state(self):
        """Write the dynamic register set into the current TSS."""
        self._write_tss_word(self.TSS_IP, self.ip)
        self._write_tss_word(self.TSS_FLAGS, self.flags)
        self._write_tss_word(self.TSS_AX, self.ax)
        self._write_tss_word(self.TSS_CX, self.cx)
        self._write_tss_word(self.TSS_DX, self.dx)
        self._write_tss_word(self.TSS_BX, self.bx)
        self._write_tss_word(self.TSS_SP, self.sp)
        self._write_tss_word(self.TSS_BP, self.bp)
        self._write_tss_word(self.TSS_SI, self.si)
        self._write_tss_word(self.TSS_DI, self.di)
        self._write_tss_word(self.TSS_ES, self.es)
        self._write_tss_word(self.TSS_CS, self.cs)
        self._write_tss_word(self.TSS_SS, self.ss)
        self._write_tss_word(self.TSS_DS, self.ds)

    def _peek_descriptor_for(self, sel):
        """Non-faulting descriptor fetch by selector (None if unusable)."""
        if not (sel & 0xFFFC):
            return None
        index = sel >> 3
        if sel & 0x04:
            table_base, table_limit = self._ldt_base(), self._ldt_limit()
        else:
            table_base, table_limit = self.gdt_base, self.gdt_limit
        addr = table_base + index * 8
        if addr + 7 > table_base + table_limit:
            return None
        raw = bytes(self._readb((addr + i) & 0xFFFFF) for i in range(8))
        if not (raw[5] & 0x80):
            return None
        return (raw[2] | (raw[3] << 8) | (raw[4] << 16),
                raw[0] | (raw[1] << 8), raw[5], addr & 0xFFFFF)

    def _set_tss_busy(self, sel, busy):
        """Flip the busy bit (type 1 <-> 3) of a TSS descriptor."""
        desc = self._peek_descriptor_for(sel)
        if desc is None:
            return
        access = (desc[2] | 0x02) if busy else (desc[2] & ~0x02)
        self._writeb(desc[3] + 5, access)
        self._desc_cache[sel] = (desc[0], desc[1], access, desc[3])

    def _load_task_state(self):
        """Load the dynamic register set from the current TSS."""
        self.ax = self._tss_word(self.TSS_AX)
        self.cx = self._tss_word(self.TSS_CX)
        self.dx = self._tss_word(self.TSS_DX)
        self.bx = self._tss_word(self.TSS_BX)
        self.bp = self._tss_word(self.TSS_BP)
        self.si = self._tss_word(self.TSS_SI)
        self.di = self._tss_word(self.TSS_DI)
        self.es = self._tss_word(self.TSS_ES)
        self.cs = self._tss_word(self.TSS_CS)
        self.ss = self._tss_word(self.TSS_SS)
        self.ds = self._tss_word(self.TSS_DS)
        self.sp = self._tss_word(self.TSS_SP)
        self.ip = self._tss_word(self.TSS_IP)
        self.flags = self._tss_word(self.TSS_FLAGS)
        self._cpl = self.cs & 3
        # Refresh the descriptor caches for the loaded selectors; where a
        # descriptor is missing fall back to real-mode-style bases so the
        # guest can resume even with a minimal TSS image.
        for sel in (self.cs, self.ds, self.es, self.ss):
            if sel not in self._desc_cache:
                desc = self._peek_descriptor_for(sel)
                if desc is None:
                    desc = ((sel << 4) & 0xFFFFF, 0xFFFF, 0x93, 0)
                self._desc_cache[sel] = desc
        self._code_base = self._desc_cache[self.cs][0]

    def _do_task_switch(self, tss_sel, source):
        """Perform a 286 hardware task switch.

        ``source`` is 'jmp', 'call', 'int', or 'iret'.  The outgoing task
        is saved to its TSS; its busy bit clears for jmp/int/iret (a
        'call' leaves it nested); the incoming task's busy bit sets, its
        back-link records the outgoing TSS for call/int (so IRET with NT
        can return), and the register set loads from the incoming TSS.
        """
        if tss_sel & 0x04:
            self._raise_gp(tss_sel & 0xFFFC)   # TSS descriptors live in GDT
        desc = self._peek_descriptor_for(tss_sel)
        if desc is None:
            self._raise_gp(tss_sel & 0xFFFC)
        access = desc[2]
        if (access & 0x1F) not in (0x01, 0x03):
            self._raise_gp(tss_sel & 0xFFFC)   # not a TSS
        if source != 'iret':
            # A busy TSS rejects new entries; IRET may return to the
            # (still busy) task it was called from.
            if (access & 0x1F) == 0x03:
                self._raise_gp(tss_sel & 0xFFFC)   # busy
            if ((access >> 5) & 3) < max(self._cpl, tss_sel & 3):
                self._raise_gp(tss_sel & 0xFFFC)   # DPL too privileged
        old_selector = self.tr_selector
        self._save_task_state()
        if source in ('jmp', 'int', 'iret'):
            self._set_tss_busy(old_selector, False)
        self._set_tss_busy(tss_sel, True)
        self.tr_selector = tss_sel
        self._desc_cache[tss_sel] = self._peek_descriptor_for(tss_sel) or desc
        self._load_task_state()
        if source in ('call', 'int'):
            # Nested: back-link the outgoing TSS and set NT.
            self._write_tss_word(self.TSS_BACKLINK, old_selector)
            self.flags |= 0x4000
        elif source == 'iret':
            self.flags &= ~0x4000

    def _ring_stack_from_tss(self, ring):
        """Read the ring's SS:SP from the TSS (286: SP0@2 SS0@4, ...)."""
        return (self._tss_word(4 + 4 * ring),   # SS
                self._tss_word(2 + 4 * ring))   # SP

    def _enter_ring(self, ring, new_cs):
        """Switch privilege: load CS first, then the TSS ring stack.

        The CS load drops CPL to the target ring so the TSS stack
        selector passes its DPL check at the new privilege level — the
        order hardware uses, avoiding the outer-ring SS DPL fault.
        """
        new_ss, new_sp = self._ring_stack_from_tss(ring)
        self._load_sreg('cs', new_cs)
        self._set_ss(new_ss)
        self.sp = new_sp

    def _far_transfer(self, sel, off, is_call):
        """Far JMP/CALL through a selector, honouring 286 call gates.

        A direct code selector transfers as in milestone 1 (with the
        standard DPL checks).  A call-gate descriptor (system type 4)
        transfers to its target selector/offset; when the target is more
        privileged, the stack switches to the ring's TSS stack, the
        caller's SS:SP is pushed (after the parameter words for CALL),
        then the return address.
        """
        if not self._pm:
            if is_call:
                self._push(self.cs)
                self._push(self.ip)
            self._set_cs(sel)
            self.ip = off
            return
        index = sel >> 3
        if index == 0:
            self._raise_gp(0)
        table_base = (self._ldt_base() if sel & 0x04 else self.gdt_base)
        table_limit = (self._ldt_limit() if sel & 0x04 else self.gdt_limit)
        gate_addr = table_base + index * 8
        if gate_addr + 7 > table_base + table_limit:
            self._raise_gp(sel & 0xFFFC)
        gate = bytes(self._readb((gate_addr + i) & 0xFFFFF)
                     for i in range(8))
        access = gate[5]
        if (access & 0x1F) == 0x05:            # task gate
            if not (access & 0x80):
                self._raise_np(sel & 0xFFFC)
            if ((access >> 5) & 3) < max(self._cpl, sel & 3):
                self._raise_gp(sel & 0xFFFC)
            self._do_task_switch(gate[2] | (gate[3] << 8),
                                 'call' if is_call else 'jmp')
            return
        if (access & 0x1F) in (0x01, 0x03):
            # Direct TSS selector: a hardware task switch (a busy TSS
            # faults inside _do_task_switch).
            self._do_task_switch(sel, 'call' if is_call else 'jmp')
            return
        if (access & 0x1F) == 0x04:            # 286 call gate
            if not (access & 0x80):
                self._raise_np(sel & 0xFFFC)
            if ((access >> 5) & 3) < max(self._cpl, sel & 3):
                self._raise_gp(sel & 0xFFFC)   # gate DPL too inner
            target_sel = gate[2] | (gate[3] << 8)
            target_off = gate[0] | (gate[1] << 8)
            param_words = gate[4] & 0x1F
            desc = self._translate_selector(target_sel)
            if not ((desc[2] & 0x18) == 0x18):
                self._raise_gp(target_sel & 0xFFFC)  # not code
            target_dpl = (desc[2] >> 5) & 3
            conforming = ((desc[2] & 0x1C) == 0x1C)
            if not conforming and target_sel & 3 and (target_sel & 3) != target_dpl:
                self._raise_gp(target_sel & 0xFFFC)
            gate_dpl = (access >> 5) & 3
            if conforming:
                pass                          # any CPL may enter
            elif is_call:
                # CALL through a gate may stay or go inner, but never
                # through a gate that is less privileged than the target.
                if target_dpl > gate_dpl:
                    self._raise_gp(target_sel & 0xFFFC)
            else:
                # JMP through a gate targets the gate's own ring (or a
                # conforming segment handled above).
                if target_dpl != gate_dpl:
                    self._raise_gp(target_sel & 0xFFFC)
            if not conforming:
                # Canonicalise the target selector: the CPU forces CS RPL
                # to the target ring, so CPL lands on the target DPL.
                target_sel = (target_sel & 0xFFFC) | target_dpl
            if is_call:
                old_cs, old_ip = self.cs, self.ip
                if not conforming and target_dpl < self._cpl:
                    # Inner-ring call: privilege change first, then the
                    # TSS stack, then parameters and the return frame.
                    saved_ss, saved_sp = self.ss, self.sp
                    self._enter_ring(target_dpl, target_sel)
                    for i in range(param_words - 1, -1, -1):
                        word = self._readw(self._physw(saved_ss,
                                                      (saved_sp + 2 * i)
                                                      & 0xFFFF))
                        self._push(word)
                    self._push(saved_ss)
                    self._push(saved_sp)
                else:
                    self._load_sreg('cs', target_sel)
                self._push(old_cs)
                self._push(old_ip)
            else:
                self._load_sreg('cs', target_sel)
            self.ip = target_off
            return
        # Plain code-descriptor transfer.
        desc = self._translate_selector(sel)
        if not ((desc[2] & 0x18) == 0x18):
            self._raise_gp(sel & 0xFFFC)
        dpl = (desc[2] >> 5) & 3
        conforming = ((desc[2] & 0x1C) == 0x1C)
        if not conforming and dpl != self._cpl:
            # A direct far transfer can never change rings; use a gate
            # to go inner or RETF/IRET to return outer.
            self._raise_gp(sel & 0xFFFC)
        if conforming and dpl > self._cpl:
            self._raise_gp(sel & 0xFFFC)
        if is_call:
            self._push(self.cs)
            self._push(self.ip)
        self._load_sreg('cs', sel)
        self.ip = off

    def _iopl(self):
        """The IOPL field of the flags word."""
        return (self.flags >> 12) & 3

    def _check_io_privilege(self):
        """``#GP(0)`` when I/O-sensitive instructions are not permitted.

        On the 80286 there is no I/O permission bitmap: CPL must be
        numerically no higher (outer) than IOPL.
        """
        if self._pm and self._cpl > self._iopl():
            self._raise_gp(0)

    def _pop_flags(self, value):
        """POPF/IRET flag loading with 286 privilege gating.

        Only ring 0 may change IOPL; IF may be changed when CPL is no
        higher (outer) than IOPL.  Other flag bits load unconditionally.
        """
        value &= 0xFFFF
        if not self._pm or self._cpl == 0:
            self.flags = value
            return
        old = self.flags
        if self._cpl > self._iopl():
            value = (value & ~0x0200) | (old & 0x0200)   # keep IF
        value = (value & ~0x3000) | (old & 0x3000)       # keep IOPL
        self.flags = value

    def _set_msw(self, value):
        """Write MSW (LMSW / privileged loads).

        The 286 cannot clear PE without a reset; the emulator permits it
        (flushing the descriptor caches) so tests can return to real mode.
        """
        value &= 0xFFFF
        was_pm = self._pm
        self.msw = (self.msw & 0xFFF0) | (value & 0x000F)
        self._pm = bool(self.msw & 0x0001)
        if self._pm and not was_pm:
            self._seed_real_mode_caches()
            # The CS cache now holds the real-mode base; keep the fetch
            # fast-path in sync (external code may have written CS
            # directly while in real mode).
            self._code_base = (self.cs << 4) & 0xFFFFF
        elif not self._pm and was_pm:
            self._desc_cache.clear()
            self._code_base = (self.cs << 4) & 0xFFFFF

    def _raise_gp(self, error_code=0):
        """Raise ``#GP`` (INT 13) with the 286 error code."""
        self._do_exception(13, error_code)

    def _raise_np(self, error_code=0):
        self._do_exception(11, error_code)

    def _raise_ss(self, error_code=0):
        self._do_exception(12, error_code)

    def _do_exception(self, n, error_code):
        """CPU exception delivery: like an interrupt plus error code.

        The error code is pushed above IP/CS/FLAGS, so the handler pops it
        before IRET.  A fault raised while another fault is being
        delivered would double- then triple-fault on hardware; the
        emulator parks deterministically instead of recursing.  With no
        Python-side dispatcher installed (bare CPU in unit tests), the
        CPU halts like ``_raise_divide_error``.
        """
        if self._exception_active:
            self.halted = True
            return
        self._exception_active = True
        try:
            self._do_interrupt(n, error_code=error_code)
        finally:
            self._exception_active = False

    def _peek_selector(self, sel):
        """Non-faulting ``_translate_selector`` for VERR/VERW/LAR/LSL.

        Returns None for any invalid index, table bound, type, or
        not-present condition instead of raising.
        """
        index = sel >> 3
        if index == 0:
            return None
        if sel & 0x04:
            table_base, table_limit = self._ldt_base(), self._ldt_limit()
        else:
            table_base, table_limit = self.gdt_base, self.gdt_limit
        addr = table_base + index * 8
        if addr + 7 > table_base + table_limit:
            return None
        raw = bytes(self._readb((addr + i) & 0xFFFFF) for i in range(8))
        if not (raw[5] & 0x80):
            return None
        base = raw[2] | (raw[3] << 8) | (raw[4] << 16)
        return (base & 0xFFFFF, raw[0] | (raw[1] << 8), raw[5])

    def _verify_selector(self, sel, write=False):
        """VERR/VERW: True when the selector addresses a usable segment."""
        desc = self._peek_selector(sel)
        if desc is None or not (desc[2] & 0x10):
            return False                      # system or invalid
        ar = desc[2]
        if ar & self.AR_CODE:                 # code
            if write:
                return False
            return bool(ar & self.AR_RDWR)
        if write:
            return bool(ar & self.AR_RDWR)
        return True

    def _arm_irq_shadow(self):
        """Suppress maskable IRQ delivery until after the following instruction."""
        self._irq_shadow = max(self._irq_shadow, 2)

    def _string_repeat_count(self):
        return self.cx if self._rep_prefix else 1

    def _fast_vga_mode1_movs(self, width, count):
        """Accelerate forward REP MOVS blits wholly inside planar VRAM."""
        if (not self._rep_prefix or self.df or count <= 0
                or self.io is None
                or not getattr(self.io.video, 'graphics_mode', False)):
            return False
        source = self._phys(self._default_data_seg(), self.si)
        destination = self._phys(self.es, self.di)
        if not (0xA0000 <= source < 0xB0000
                and 0xA0000 <= destination < 0xB0000):
            return False
        length = count * width
        if not self.io.video.graphics_copy_mode1(
                source - 0xA0000, destination - 0xA0000, length):
            return False
        self.si = (self.si + length) & 0xFFFF
        self.di = (self.di + length) & 0xFFFF
        self.cx = 0
        return True

    def _fast_vga_mode1_stos(self, width, count):
        """Accelerate forward REP STOS fills wholly inside planar VRAM."""
        if (not self._rep_prefix or self.df or count <= 0
                or not getattr(self.io.video, 'graphics_mode', False)):
            return False
        destination = self._phys(self.es, self.di)
        if not 0xA0000 <= destination < 0xB0000:
            return False
        length = count * width
        if not self.io.video.graphics_fill_mode1(
                destination - 0xA0000, length):
            return False
        self.di = (self.di + length) & 0xFFFF
        self.cx = 0
        return True

    # ── Main execute loop ──────────────────────────────────────────

    def execute(self):
        """Execute one instruction. Returns False on halt/error."""
        if self.halted or self.insn_count >= self.max_insns:
            return False
        self.insn_count += 1
        save_ip = self.ip
        save_cs = self.cs
        # Latch the Trap Flag as it was *before* this instruction.  The
        # single-step trap (INT 1) fires after the instruction completes iff
        # TF was set coming in.  Latching implements the one-instruction delay
        # for POPF/IRET that set TF (their incoming TF is the old value, so the
        # setting instruction itself doesn't trap; the next one does).
        was_tf = self.tf
        # Consume segment prefixes before main opcode
        self._seg_override = None
        self._rep_prefix = None
        while True:
            opc = self._fetchb()
            if opc == 0x26:
                self._seg_override = self.es
                continue
            elif opc == 0x2E:
                self._seg_override = self.cs
                continue
            elif opc == 0x36:
                self._seg_override = self.ss
                continue
            elif opc == 0x3E:
                self._seg_override = self.ds
                continue
            elif opc == 0x66:
                # Operand-size override (ignore for now - 16-bit mode)
                continue
            elif opc == 0xF0:
                # LOCK prefix (ignore)
                continue
            elif opc == 0xF2:
                self._rep_prefix = 'repne'
                continue
            elif opc == 0xF3:
                self._rep_prefix = 'rep'
                continue
            break
        try:
            self._dispatch(opc)
        except Exception as e:
            import traceback
            print(f"\n[CPU EXCEPTION] CS:IP={self.cs:04X}:{save_ip:04X} "
                  f"Opcode={opc:#04X}: {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            self.halted = True
            return False
        self.last_instruction_cycles = self.cycles_per_instruction
        self.cycle_count += self.last_instruction_cycles
        if self._irq_shadow:
            self._irq_shadow -= 1
        # Single-step trap: fire INT 1 after the instruction if TF was set
        # at the start AND the instruction did not itself clear TF (INT/IRET
        # clear TF and transfer control, so self.tf/__read back as clear and
        # int_no_return guards the handoff).  This is what DEBUG's T needs.
        if was_tf and self.tf and not self.int_no_return:
            self._do_interrupt(1)
        if self.step_mode:
            self._step_print(opc, save_ip)
        return True

    @property
    def emulated_time(self):
        """Virtual seconds elapsed according to the selected CPU profile."""
        return self.cycle_count / self.cpu_clock_hz

    def _step_print(self, opc, ip):
        """Print mnemonic + register state for step debugging."""
        mnemonic = self._decode_mnemonic(opc, ip)
        print(f"[{self.insn_count:5d}] {self.cs:04X}:{ip:04X}  "
              f"{mnemonic:<28s}  "
              f"AX={self.ax:04X} BX={self.bx:04X} CX={self.cx:04X} DX={self.dx:04X}  "
              f"SP={self.sp:04X} BP={self.bp:04X} SI={self.si:04X} DI={self.di:04X}  "
              f"DS={self.ds:04X} ES={self.es:04X} SS={self.ss:04X}  "
              f"FL={self.flags:04X}", file=sys.stderr)

    def _decode_mnemonic(self, opc, ip):
        """Decode a single opcode byte into a readable mnemonic."""
        reg_names8 = ['al', 'cl', 'dl', 'bl', 'ah', 'ch', 'dh', 'bh']
        reg_names16 = ['ax', 'cx', 'dx', 'bx', 'sp', 'bp', 'si', 'di']
        sreg_names = ['es', 'cs', 'ss', 'ds']

        # Helper to read next bytes from CS:IP for operands
        def peek(n):
            return [self._readb(self._phys(self.cs, ip + i)) for i in range(n)]

        def peekw():
            b = peek(2)
            return b[0] | (b[1] << 8)

        def modrm_str():
            b = peek(1)[0]
            mod = (b >> 6) & 3
            reg = (b >> 3) & 7
            rm = b & 7
            if mod == 3:
                return reg_names16[rm]
            bases = ['[bx+si]', '[bx+di]', '[si]', '[di]', '[bp]', '[bp]', '[addr]', '[addr]']
            return bases[rm] if mod == 0 or rm != 6 else '[addr]'

        if 0x00 <= opc <= 0x05: return f"ADD {modrm_str()}, ..." if opc <= 3 else f"ADD AL, {peek(1)[0]:02X}" if opc == 4 else f"ADD AX, {peekw():04X}"
        if 0x08 <= opc <= 0x0D: return f"OR {modrm_str()}, ..." if opc <= 0x0B else f"OR AL, {peek(1)[0]:02X}" if opc == 0x0C else f"OR AX, {peekw():04X}"
        if 0x10 <= opc <= 0x15: return f"ADC {modrm_str()}, ..." if opc <= 0x13 else f"ADC AL, {peek(1)[0]:02X}" if opc == 0x14 else f"ADC AX, {peekw():04X}"
        if 0x18 <= opc <= 0x1D: return f"SBB {modrm_str()}, ..." if opc <= 0x1B else f"SBB AL, {peek(1)[0]:02X}" if opc == 0x1C else f"SBB AX, {peekw():04X}"
        if 0x20 <= opc <= 0x25: return f"AND {modrm_str()}, ..." if opc <= 0x23 else f"AND AL, {peek(1)[0]:02X}" if opc == 0x24 else f"AND AX, {peekw():04X}"
        if 0x28 <= opc <= 0x2D: return f"SUB {modrm_str()}, ..." if opc <= 0x2B else f"SUB AL, {peek(1)[0]:02X}" if opc == 0x2C else f"SUB AX, {peekw():04X}"
        if 0x30 <= opc <= 0x35: return f"XOR {modrm_str()}, ..." if opc <= 0x33 else f"XOR AL, {peek(1)[0]:02X}" if opc == 0x34 else f"XOR AX, {peekw():04X}"
        if 0x38 <= opc <= 0x3D: return f"CMP {modrm_str()}, ..." if opc <= 0x3B else f"CMP AL, {peek(1)[0]:02X}" if opc == 0x3C else f"CMP AX, {peekw():04X}"

        if opc == 0x06: return "PUSH ES"
        if opc == 0x07: return "POP ES"
        if opc == 0x0E: return "PUSH CS"
        if opc == 0x16: return "PUSH SS"
        if opc == 0x17: return "POP SS"
        if opc == 0x1E: return "PUSH DS"
        if opc == 0x1F: return "POP DS"
        if opc == 0x26: return "ES: (prefix)"
        if opc == 0x2E: return "CS: (prefix)"
        if opc == 0x36: return "SS: (prefix)"
        if opc == 0x3E: return "DS: (prefix)"

        if 0x40 <= opc <= 0x47: return f"INC {reg_names16[opc - 0x40]}"
        if 0x48 <= opc <= 0x4F: return f"DEC {reg_names16[opc - 0x48]}"
        if 0x50 <= opc <= 0x57: return f"PUSH {reg_names16[opc - 0x50]}"
        if 0x58 <= opc <= 0x5F: return f"POP {reg_names16[opc - 0x58]}"

        if opc == 0x60: return "PUSHA"
        if opc == 0x61: return "POPA"
        if opc == 0x68: return f"PUSH {peekw():04X}"
        if opc == 0x6A: return f"PUSH {peek(1)[0]:02X}"

        if 0x70 <= opc <= 0x7F:
            idx = opc - 0x70
            names = ['JO','JNO','JB','JNB','JZ','JNZ','BE','JA',
                     'JS','JNS','JPE','JPO',' JL','JGE','JLE','JG']
            return f"{names[idx]} {ip+2}"

        if opc in (0x80, 0x82, 0x83):
            b = peek(1)[0]
            reg = (b >> 3) & 7
            grp = ['ADD','OR','ADC','SBB','AND','SUB','XOR','CMP']
            return f"{grp[reg]} {modrm_str()}, imm"
        if opc == 0x84: return f"TEST AL, {modrm_str()}"
        if opc == 0x85: return f"TEST AX, {modrm_str()}"
        if opc == 0x86: return f"XCHG AL, {modrm_str()}"
        if opc == 0x87: return f"XCHG AX, {modrm_str()}"
        if opc == 0x88:
            b = peek(1)[0]; reg = (b >> 3) & 7; rm = b & 7
            return f"MOV {modrm_str()}, {reg_names8[reg]}"
        if opc == 0x89:
            b = peek(1)[0]; reg = (b >> 3) & 7; rm = b & 7
            return f"MOV {modrm_str()}, {reg_names16[reg]}"
        if opc == 0x8A:
            b = peek(1)[0]; reg = (b >> 3) & 7
            return f"MOV {reg_names8[reg]}, {modrm_str()}"
        if opc == 0x8B:
            b = peek(1)[0]; reg = (b >> 3) & 7
            return f"MOV {reg_names16[reg]}, {modrm_str()}"
        if opc == 0x8C:
            b = peek(1)[0]; reg = (b >> 3) & 7
            return f"MOV {modrm_str()}, {sreg_names[reg]}"
        if opc == 0x8D:
            b = peek(1)[0]; reg = (b >> 3) & 7
            return f"LEA {reg_names16[reg]}, {modrm_str()}"
        if opc == 0x8E:
            b = peek(1)[0]; reg = (b >> 3) & 7
            return f"MOV {sreg_names[reg]}, {modrm_str()}"
        if opc == 0x8F:
            b = peek(1)[0]; rm = b & 7
            return f"POP {modrm_str()}"

        if 0x90 <= opc <= 0x97:
            r = opc - 0x90
            return "NOP" if r == 0 else f"XCHG AX, {reg_names16[r]}"
        if opc == 0x98: return "CBW"
        if opc == 0x99: return "CWD"
        if opc == 0x9A: return f"CALL {peekw():04X}:{peekw()>>16:04X}"
        if opc == 0x9C: return "PUSHF"
        if opc == 0x9D: return "POPF"
        if opc == 0x9E: return "SAHF"
        if opc == 0x9F: return "LAHF"

        if opc == 0xA0: return f"MOV AL, [{peekw():04X}]"
        if opc == 0xA1: return f"MOV AX, [{peekw():04X}]"
        if opc == 0xA2: return f"MOV [{peekw():04X}], AL"
        if opc == 0xA3: return f"MOV [{peekw():04X}], AX"
        if opc == 0xA4: return "MOVSB"
        if opc == 0xA5: return "MOVSW"
        if opc == 0xA6: return "CMPSB"
        if opc == 0xA7: return "CMPSW"
        if opc == 0xAA: return "STOSB"
        if opc == 0xAB: return "STOSW"
        if opc == 0xAC: return "LODSB"
        if opc == 0xAD: return "LODSW"
        if opc == 0xAE: return "SCASB"
        if opc == 0xAF: return "SCASW"
        if opc == 0xA8: return f"TEST AL, {peek(1)[0]:02X}"
        if opc == 0xA9: return f"TEST AX, {peekw():04X}"

        if 0xB0 <= opc <= 0xB7: return f"MOV {reg_names8[opc-0xB0]}, {peek(1)[0]:02X}"
        if 0xB8 <= opc <= 0xBF: return f"MOV {reg_names16[opc-0xB8]}, {peekw():04X}"

        if opc == 0xC0: return "LDS r16, [modrm]"
        if opc == 0xC3: return "RET"
        if opc == 0xC4: return "LES AX, [modrm]"
        if opc == 0xC5: return "LES r16, [modrm]"
        if opc == 0xC6: return "MOV r/m8, imm8"
        if opc == 0xC7: return "MOV r/m16, imm16"
        if opc == 0xC8: return f"ENTER {peekw():04X}, {peek(1)[0]:02X}"
        if opc == 0xC9: return "LEAVE"
        if opc == 0xCB: return "RETF"
        if opc == 0xCA: return f"RETF {peekw():04X}"
        if opc == 0xCC: return "INT3"
        if opc == 0xCD: return f"INT {peek(1)[0]:02X}"
        if opc == 0xCE: return "INTO"
        if opc == 0xCF: return "IRET"

        if 0xD0 <= opc <= 0xD3:
            b = peek(1)[0]; reg = (b >> 3) & 7
            shift = ['ROL','ROR','RCL','RCR','SAL','SHR','SHL','SAR']
            cnt = "1" if opc <= 0xD1 else "CL"
            return f"{shift[reg]} {modrm_str()}, {cnt}"
        if opc == 0xD4: return f"AAM {peek(1)[0]:02X}"
        if opc == 0xD5: return f"AAD {peek(1)[0]:02X}"
        if opc == 0xD6: return "SALC"
        if opc == 0xD7: return "XLAT"

        if opc == 0xE0: return f"LOOPNE {ip+2}"
        if opc == 0xE1: return f"LOOPE {ip+2}"
        if opc == 0xE2: return f"LOOP {ip+2}"
        if opc == 0xE3: return f"JCXZ {ip+2}"
        if opc == 0xE8: return f"CALL {ip+3}"
        if opc == 0xE9: return f"JMP {ip+3}"
        if opc == 0xEA: return f"JMP {peekw():04X}:{peekw()>>16:04X}"
        if opc == 0xEB: return f"JMP {ip+2}"

        if opc == 0xE4: return f"IN AL, {peek(1)[0]:02X}"
        if opc == 0xE5: return f"IN AX, {peek(1)[0]:02X}"
        if opc == 0xE6: return f"OUT {peek(1)[0]:02X}, AL"
        if opc == 0xE7: return f"OUT {peek(1)[0]:02X}, AX"
        if opc == 0xEC: return "IN AL, DX"
        if opc == 0xED: return "IN AX, DX"
        if opc == 0xEE: return "OUT DX, AL"
        if opc == 0xEF: return "OUT DX, AX"

        if opc == 0x0F:
            b = peek(1)[0]
            if 0x90 <= b <= 0x9F:
                names = ['SETO','SETNO','SETB','SETNB','SETZ','SETNZ','SETBE','SETA',
                         'SETS','SETNS','SETPE','SETPO','SETL','SETGE','SETLE','SETG']
                return f"{names[b-0x90]} {modrm_str()}"
            return f"0F {b:02X}"
        if opc == 0xF0: return "LOCK (prefix)"
        if opc == 0xF2: return "REPNE (prefix)"
        if opc == 0xF3: return "REP (prefix)"
        if opc == 0xF4: return "HLT"
        if opc == 0xF5: return "CMC"
        if opc == 0xF8: return "CLC"
        if opc == 0xF9: return "STC"
        if opc == 0xFA: return "CLI"
        if opc == 0xFB: return "STI"
        if opc == 0xFC: return "CLD"
        if opc == 0xFD: return "STD"

        if opc == 0xFE:
            b = peek(1)[0]; reg = (b >> 3) & 7
            return f"DEC {modrm_str()}" if reg & 1 else f"INC {modrm_str()}"
        if opc == 0xFF:
            b = peek(1)[0]; reg = (b >> 3) & 7
            ops = ['INC','DEC','CALL','CALL far','JMP','JMP far','PUSH']
            return f"{ops[reg]} {modrm_str()}"

        if opc == 0xF6:
            b = peek(1)[0]; reg = (b >> 3) & 7
            if reg >= 6: return f"TEST {modrm_str()}, imm8"
            if reg == 0: return f"NOT {modrm_str()}"
            if reg == 1: return f"NEG {modrm_str()}"
            if reg == 2: return f"MUL {modrm_str()}"
            if reg == 3: return f"IMUL {modrm_str()}"
            if reg == 4: return f"DIV {modrm_str()}"
            if reg == 5: return f"IDIV {modrm_str()}"
        if opc == 0xF7:
            b = peek(1)[0]; reg = (b >> 3) & 7
            if reg >= 6: return f"TEST {modrm_str()}, imm16"
            if reg == 0: return f"NOT {modrm_str()}"
            if reg == 1: return f"NEG {modrm_str()}"
            if reg == 2: return f"MUL {modrm_str()}"
            if reg == 3: return f"IMUL {modrm_str()}"
            if reg == 4: return f"DIV {modrm_str()}"
            if reg == 5: return f"IDIV {modrm_str()}"

        return f"??? {opc:02X}"

    def _dispatch(self, opc):
        """Main opcode dispatcher."""

        # 00-05 ADD, 08-0D OR, 10-15 ADC, 18-1D SBB,
        # 20-25 AND, 28-2D SUB, 30-35 XOR, 38-3D CMP
        if opc in (0x00, 0x01, 0x02, 0x03, 0x04, 0x05):
            self._exec_al_arith(opc, None); return
        if 0x08 <= opc <= 0x0D:
            self._exec_al_arith(opc, None); return
        if 0x10 <= opc <= 0x15:
            self._exec_al_arith(opc, None); return
        if 0x18 <= opc <= 0x1D:
            self._exec_al_arith(opc, None); return
        if 0x20 <= opc <= 0x25:
            self._exec_al_arith(opc, None); return
        if 0x28 <= opc <= 0x2D:
            self._exec_al_arith(opc, None); return
        if 0x30 <= opc <= 0x35:
            self._exec_al_arith(opc, None); return
        if 0x38 <= opc <= 0x3D:
            self._exec_al_cmp(opc); return

        # PUSH/POP segment registers
        if opc == 0x06: self._push(self.es); return
        if opc == 0x07: self._set_es(self._pop()); return
        if opc == 0x0E: self._push(self.cs); return
        if opc == 0x16: self._push(self.ss); return
        if opc == 0x17:
            self._set_ss(self._pop())
            self._arm_irq_shadow()
            return
        if opc == 0x1E: self._push(self.ds); return
        if opc == 0x1F: self._set_ds(self._pop()); return

        # Segment prefixes (handled in execute() loop now)

        # 27 DAA — Decimal Adjust AL after Addition
        if opc == 0x27:
            old_al = self.al
            old_cf = self.cf
            if ((self.al & 0x0F) > 9) or self.af:
                self.al = (self.al + 6) & 0xFF
                self.cf = old_cf or (old_al + 6 > 0xFF)
                self.af = True
            else:
                self.af = False
            if (old_al > 0x99) or old_cf:
                self.al = (self.al + 0x60) & 0xFF
                self.cf = True
            else:
                self.cf = False
            self._set_szp8(self.al)
            return
        # 2F DAS — Decimal Adjust AL after Subtraction
        if opc == 0x2F:
            old_al = self.al
            old_cf = self.cf
            if ((self.al & 0x0F) > 9) or self.af:
                self.cf = old_cf or (old_al < 6)
                self.al = (self.al - 6) & 0xFF
                self.af = True
            else:
                self.af = False
            if (old_al > 0x99) or old_cf:
                self.al = (self.al - 0x60) & 0xFF
                self.cf = True
            # else: CF retains the value from the low-nibble adjust above.
            self._set_szp8(self.al)
            return
        # 37 AAA — ASCII Adjust after Addition
        if opc == 0x37:
            if ((self.al & 0x0F) > 9) or self.af:
                self.ax = (self.ax + 0x0106) & 0xFFFF
                self.af = True
                self.cf = True
            else:
                self.af = False
                self.cf = False
            self.al = self.al & 0x0F
            return
        # 3F AAS — ASCII Adjust after Subtraction
        if opc == 0x3F:
            if ((self.al & 0x0F) > 9) or self.af:
                self.ax = (self.ax - 0x0106) & 0xFFFF
                self.af = True
                self.cf = True
            else:
                self.af = False
                self.cf = False
            self.al = self.al & 0x0F
            return

        # 40-47 INC r16
        if 0x40 <= opc <= 0x47:
            r = opc - 0x40
            old = self._reg16(r)
            v = (old + 1) & 0xFFFF
            self._set_reg16(r, v)
            self.zf = v == 0; self.sf = bool(v & 0x8000)
            self.of = v == 0x8000; self.pf = bin(v & 0xFF).count('1') % 2 == 0
            # AF carries out of bit 3 (0x0F -> 0x10 wrap)
            self.af = (old & 0x0F) == 0x0F
            return

        # 48-4F DEC r16
        if 0x48 <= opc <= 0x4F:
            r = opc - 0x48
            old = self._reg16(r)
            v = (old - 1) & 0xFFFF
            self._set_reg16(r, v)
            self.zf = v == 0; self.sf = bool(v & 0x8000)
            self.of = v == 0x7FFF; self.pf = bin(v & 0xFF).count('1') % 2 == 0
            # AF borrows out of bit 3 (0x00 -> 0x0F wrap); EXEPACK-style
            # decompressors (MS-DOS 6 SYSINIT) probe this flag.
            self.af = (old & 0x0F) == 0x00
            return

        # 50-57 PUSH r16
        if 0x50 <= opc <= 0x57:
            self._push(self._reg16(opc - 0x50)); return

        # 58-5F POP r16
        if 0x58 <= opc <= 0x5F:
            self._set_reg16(opc - 0x58, self._pop()); return

        # 63 ARPL r/m16, r16 — adjust requested privilege level
        if opc == 0x63:
            mod, reg, rm = self._decode_modrm()
            if mod == 3:
                dest = self._get_reg16(rm)
            else:
                # Read-modify-write: decode the effective address exactly
                # once; a second _ea call would consume the displacement
                # again and skip into the next instruction.
                addr = self._ea(mod, rm)
                dest = self._readw(addr)
            rpl = self._get_reg16(reg) & 0x0003
            if (dest & 0x0003) < rpl:
                adjusted = (dest & 0xFFFC) | rpl
                if mod == 3:
                    self._set_reg16(rm, adjusted)
                else:
                    self._writew(addr, adjusted)
                self.zf = True
            else:
                self.zf = False
            return

        # 60 PUSHA
        if opc == 0x60:
            orig_sp = self.sp
            for value in (
                self.ax, self.cx, self.dx, self.bx,
                orig_sp, self.bp, self.si, self.di,
            ):
                self._push(value)
            return

        # 61 POPA
        if opc == 0x61:
            self.di = self._pop()
            self.si = self._pop()
            self.bp = self._pop()
            self.sp = (self.sp + 2) & 0xFFFF  # Skip the saved SP word.
            self.bx = self._pop()
            self.dx = self._pop()
            self.cx = self._pop()
            self.ax = self._pop()
            return

        # 62 BOUND, 63 ARPL (skip)
        if opc in (0x62, 0x63):
            mod, reg, rm = self._decode_modrm()
            self._skip_disp(mod, rm)
            return

        # 64-65 TEST r16, imm16 (skip)
        if opc in (0x64, 0x65): return

        # 66 SEG CS prefix (skip)
        if opc == 0x66: return

        # 67 SS: segment override (skip)
        if opc == 0x67: return

        # 68 PUSH imm16
        if opc == 0x68:
            self._push(self._fetchw())
            return

        # 69 IMUL r16, r/m16, imm16 (skip — partial)
        if opc == 0x69:
            mod, reg, rm = self._decode_modrm()
            self._skip_disp(mod, rm)
            self.ip = (self.ip + 2) & 0xFFFF  # skip imm16
            return

        # 6A PUSH imm8 (sign-extended)
        if opc == 0x6A:
            imm = self._fetchb()
            if imm & 0x80: imm |= 0xFF00
            self._push(imm)
            return

        # 6B IMUL r16, r/m8, imm8 (skip)
        if opc == 0x6B:
            mod, reg, rm = self._decode_modrm()
            self._skip_disp(mod, rm)
            self.ip = (self.ip + 1) & 0xFFFF  # skip imm8
            return

        # 6C-6F INS/OUTS (stub - skip ModR/M byte)
        if 0x6C <= opc <= 0x6F:
            self._fetchb()  # consume ModR/M byte
            return

        # 70-7F Conditional jumps
        if 0x70 <= opc <= 0x7F:
            idx = opc - 0x70
            cond_map = {
                0: self.of,           # JO
                1: not self.of,       # JNO
                2: self.cf,           # JB/JC/JNAE
                3: not self.cf,       # JNB/JNC/JAE
                4: self.zf,           # JZ/JE
                5: not self.zf,       # JNZ/JNE
                6: self.zf or self.cf,  # JBE/JNA
                7: not (self.zf or self.cf),  # JA/JNBE
                8: self.sf,           # JS
                9: not self.sf,       # JNS
                10: self.pf,          # JPE
                11: not self.pf,      # JPO
                12: self.sf ^ self.of,  # JL/JNGE
                13: not (self.sf ^ self.of),  # JGE/JNL
                14: self.zf or (self.sf ^ self.of),  # JLE/JNG
                15: not (self.zf or (self.sf ^ self.of)),  # JG/JNLE
            }
            if idx not in cond_map:
                self.ip = (self.ip + 1) & 0xFFFF
                return
            offset = self._fetchb()
            if offset & 0x80: offset |= 0xFF00
            if cond_map[idx]:
                self.ip = (self.ip + offset) & 0xFFFF
            return

        # 80, 82, 83 GROUP 1 (ib)
        if opc in (0x80, 0x82):
            mod, reg, rm = self._decode_modrm()
            if mod == 3:
                imm = self._fetchb()
                self._exec_modrm_arith(mod, rm, reg, imm, is_word=False)
            else:
                addr = self._ea(mod, rm)
                imm = self._fetchb()
                self._exec_group1_mem_arith(addr, reg, imm, is_word=False)
            return
        if opc == 0x83:
            mod, reg, rm = self._decode_modrm()
            if mod == 3:
                imm = self._fetchb()
                if imm & 0x80:
                    imm |= 0xFF00
                self._exec_modrm_arith(mod, rm, reg, imm, is_word=True)
            else:
                addr = self._ea(mod, rm)
                imm = self._fetchb()
                if imm & 0x80:
                    imm |= 0xFF00
                self._exec_group1_mem_arith(addr, reg, imm, is_word=True)
            return

        # 81 GROUP 1 (iw)
        if opc == 0x81:
            mod, reg, rm = self._decode_modrm()
            if mod == 3:
                imm = self._fetchw()
                self._exec_modrm_arith(mod, rm, reg, imm, is_word=True)
            else:
                addr = self._ea(mod, rm)
                imm = self._fetchw()
                self._exec_group1_mem_arith(addr, reg, imm, is_word=True)
            return

        # 84 TEST r/m8, r8
        if opc == 0x84:
            mod, reg, rm = self._decode_modrm()
            self._flags_logic8(self._get_reg8_modrm(reg) & self._ea_byte(mod, rm))
            return

        # 85 TEST r/m16, r16
        if opc == 0x85:
            mod, reg, rm = self._decode_modrm()
            self._flags_logic16(self._reg16(reg) & self._ea_word(mod, rm))
            return

        # 86 XCHG r/m8, r8
        if opc == 0x86:
            mod, reg, rm = self._decode_modrm()
            if mod == 3:  # register-register exchange
                v1 = self._get_reg8_modrm(reg)
                v2 = self._get_reg8_modrm(rm)
                self._set_reg8_modrm(reg, v2)
                self._set_reg8_modrm(rm, v1)
            else:  # register-memory exchange (reg is the register, rm is memory)
                a = self._ea(mod, rm)
                v = self._readb(a)
                self._writeb(a, self._get_reg8_modrm(reg))
                self._set_reg8_modrm(reg, v)
            return

        # 87 XCHG r/m16, r16
        if opc == 0x87:
            mod, reg, rm = self._decode_modrm()
            if mod == 3:  # register-register exchange
                v1 = self._reg16(reg)
                v2 = self._reg16(rm)
                self._set_reg16(reg, v2)
                self._set_reg16(rm, v1)
            else:  # register-memory exchange
                a = self._ea(mod, rm)
                v = self._readw(a)
                self._writew(a, self._reg16(reg))
                self._set_reg16(reg, v)
            return

        # 88 MOV r/m8, r8
        if opc == 0x88:
            mod, reg, rm = self._decode_modrm()
            self._ea_write_byte(mod, rm, self._get_reg8_modrm(reg))
            return

        # 89 MOV r/m16, r16
        if opc == 0x89:
            mod, reg, rm = self._decode_modrm()
            self._ea_write_word(mod, rm, self._reg16(reg))
            return

        # 8A MOV r8, r/m8
        if opc == 0x8A:
            mod, reg, rm = self._decode_modrm()
            self._set_reg8_modrm(reg, self._ea_byte(mod, rm))
            return

        # 8B MOV r16, r/m16
        if opc == 0x8B:
            mod, reg, rm = self._decode_modrm()
            self._set_reg16(reg, self._ea_word(mod, rm))
            return

        # 8C MOV r/m16, Sreg
        if opc == 0x8C:
            mod, reg, rm = self._decode_modrm()
            sregs = [self.es, self.cs, self.ss, self.ds, 0, 0, 0, 0]
            self._ea_write_word(mod, rm, sregs[reg] if reg < 4 else 0)
            return

        # 8D LEA r16, m
        if opc == 0x8D:
            mod, reg, rm = self._decode_modrm()
            self._set_reg16(reg, self._lea_address(mod, rm))
            return

        # 8E MOV Sreg, r/m16
        if opc == 0x8E:
            mod, reg, rm = self._decode_modrm()
            if mod == 3:
                v = self._get_reg16(rm)
            else:
                v = self._ea_word(mod, rm)
            setters = [self._set_es, self._set_cs, self._set_ss, self._set_ds]
            if reg < 4:
                setters[reg](v)
                if reg == 2:
                    self._arm_irq_shadow()
            return

        # 8F POP r/m16
        if opc == 0x8F:
            mod, reg, rm = self._decode_modrm()
            v = self._pop()
            if mod == 3:
                self._set_reg16(rm, v)
            else:
                self._ea_write_word(mod, rm, v)
            return

        # 90-97 XCHG AX, r16 (90 = NOP)
        if 0x90 <= opc <= 0x97:
            r = opc - 0x90
            old_ax = self.ax
            self.ax = self._reg16(r)
            self._set_reg16(r, old_ax)
            return

        # 98 CBW
        if opc == 0x98:
            v = self.ax & 0xFF
            self.ax = v | (0xFF00 if v & 0x80 else 0)
            return

        # 99 CWD
        if opc == 0x99:
            self.dx = 0xFFFF if self.ax & 0x8000 else 0
            return

        # 9A CALL far
        if opc == 0x9A:
            off = self._fetchw()
            seg = self._fetchw()
            self._far_transfer(seg, off, is_call=True)
            return

        # 9C PUSHF
        if opc == 0x9C:
            word = self.flags
            if self._pm and self._cpl > self._iopl():
                word &= ~0x3000        # IOPL reads as 00 from outer rings
            self._push(word)
            return

        # 9D POPF
        if opc == 0x9D: self._pop_flags(self._pop()); return

        # 9E SAHF — load AH into flags low byte
        if opc == 0x9E:
            self.flags = (self.flags & 0xFF00) | ((self.ax >> 8) & 0xFF)
            return

        # 9F LAHF — store flags low byte into AH
        if opc == 0x9F:
            self.ax = ((self.flags & 0xFF) << 8) | (self.ax & 0xFF)
            return

        # A0 MOV AL, [addr]
        if opc == 0xA0:
            addr = self._fetchw()
            self.ax = (self.ax & 0xFF00) | self._readb(self._phys(self._default_data_seg(), addr))
            return

        # A1 MOV AX, [addr]
        if opc == 0xA1:
            addr = self._fetchw()
            self.ax = self._readw(self._physw(self._default_data_seg(), addr))
            return

        # A2 MOV [addr], AL
        if opc == 0xA2:
            addr = self._fetchw()
            self._writeb(self._phys(self._default_data_seg(), addr), self.ax & 0xFF)
            return

        # A3 MOV [addr], AX
        if opc == 0xA3:
            addr = self._fetchw()
            self._writew(self._physw(self._default_data_seg(), addr), self.ax)
            return

        # A4 MOVSB
        if opc == 0xA4:
            count = self._string_repeat_count()
            if self._fast_vga_mode1_movs(1, count):
                return
            inc = 1 if not self.df else -1
            src_seg = self._default_data_seg()
            for _ in range(count):
                s = self._physw(src_seg, self.si)
                d = self._physw(self.es, self.di)
                self._writeb(d, self._readb(s))
                self.si = (self.si + inc) & 0xFFFF
                self.di = (self.di + inc) & 0xFFFF
                if self._rep_prefix:
                    self.cx = (self.cx - 1) & 0xFFFF
            return

        # A5 MOVSW
        if opc == 0xA5:
            count = self._string_repeat_count()
            if self._fast_vga_mode1_movs(2, count):
                return
            inc = 2 if not self.df else -2
            src_seg = self._default_data_seg()
            for _ in range(count):
                s = self._physw(src_seg, self.si)
                d = self._physw(self.es, self.di)
                self._writew(d, self._readw(s))
                self.si = (self.si + inc) & 0xFFFF
                self.di = (self.di + inc) & 0xFFFF
                if self._rep_prefix:
                    self.cx = (self.cx - 1) & 0xFFFF
            return

        # A6 CMPSB
        if opc == 0xA6:
            inc = 1 if not self.df else -1
            src_seg = self._default_data_seg()
            if self._rep_prefix and self.cx == 0:
                return  # REP with CX=0: no-op
            while True:
                a = self._readb(self._phys(src_seg, self.si))
                b = self._readb(self._phys(self.es, self.di))
                self._flags_sub8(a, b)
                self.si = (self.si + inc) & 0xFFFF
                self.di = (self.di + inc) & 0xFFFF
                if not self._rep_prefix:
                    break
                self.cx = (self.cx - 1) & 0xFFFF
                if self.cx == 0:
                    break
                if self._rep_prefix == 'rep' and not self.zf:
                    break
                if self._rep_prefix == 'repne' and self.zf:
                    break
            return

        # A7 CMPSW
        if opc == 0xA7:
            inc = 2 if not self.df else -2
            src_seg = self._default_data_seg()
            if self._rep_prefix and self.cx == 0:
                return  # REP with CX=0: no-op
            while True:
                a = self._readw(self._physw(src_seg, self.si))
                b = self._readw(self._physw(self.es, self.di))
                self._flags_sub16(a, b)
                self.si = (self.si + inc) & 0xFFFF
                self.di = (self.di + inc) & 0xFFFF
                if not self._rep_prefix:
                    break
                self.cx = (self.cx - 1) & 0xFFFF
                if self.cx == 0:
                    break
                if self._rep_prefix == 'rep' and not self.zf:
                    break
                if self._rep_prefix == 'repne' and self.zf:
                    break
            return

        # AA STOSB — Store AL → [ES:DI]
        if opc == 0xAA:
            inc = 1 if not self.df else -1
            count = self._string_repeat_count()
            if self._fast_vga_mode1_stos(1, count):
                return
            for _ in range(count):
                self._writeb(self._phys(self.es, self.di), self.ax & 0xFF)
                self.di = (self.di + inc) & 0xFFFF
                if self._rep_prefix:
                    self.cx = (self.cx - 1) & 0xFFFF
            return

        # AB STOSW — Store AX → [ES:DI]
        if opc == 0xAB:
            inc = 2 if not self.df else -2
            count = self._string_repeat_count()
            if self._fast_vga_mode1_stos(2, count):
                return
            for _ in range(count):
                self._writew(self._physw(self.es, self.di), self.ax)
                self.di = (self.di + inc) & 0xFFFF
                if self._rep_prefix:
                    self.cx = (self.cx - 1) & 0xFFFF
            return

        # AC LODSB — Load AL ← [DS:SI]
        if opc == 0xAC:
            inc = 1 if not self.df else -1
            count = self._string_repeat_count()
            src_seg = self._default_data_seg()
            for _ in range(count):
                self.ax = (self.ax & 0xFF00) | self._readb(self._phys(src_seg, self.si))
                self.si = (self.si + inc) & 0xFFFF
                if self._rep_prefix:
                    self.cx = (self.cx - 1) & 0xFFFF
            return

        # AD LODSW — Load AX ← [DS:SI]
        if opc == 0xAD:
            inc = 2 if not self.df else -2
            count = self._string_repeat_count()
            src_seg = self._default_data_seg()
            for _ in range(count):
                self.ax = self._readw(self._physw(src_seg, self.si))
                self.si = (self.si + inc) & 0xFFFF
                if self._rep_prefix:
                    self.cx = (self.cx - 1) & 0xFFFF
            return

        # AE SCASB — Compare AL vs [ES:DI]
        if opc == 0xAE:
            inc = 1 if not self.df else -1
            if self._rep_prefix and self.cx == 0:
                return  # REP with CX=0: no-op
            while True:
                b = self._readb(self._phys(self.es, self.di))
                self._flags_sub8(self.ax & 0xFF, b)
                self.di = (self.di + inc) & 0xFFFF
                if not self._rep_prefix:
                    break
                self.cx = (self.cx - 1) & 0xFFFF
                if self.cx == 0:
                    break
                if self._rep_prefix == 'rep' and not self.zf:
                    break
                if self._rep_prefix == 'repne' and self.zf:
                    break
            return

        # AF SCASW — Compare AX vs [ES:DI]
        if opc == 0xAF:
            inc = 2 if not self.df else -2
            if self._rep_prefix and self.cx == 0:
                return  # REP with CX=0: no-op
            while True:
                b = self._readw(self._physw(self.es, self.di))
                self._flags_sub16(self.ax, b)
                self.di = (self.di + inc) & 0xFFFF
                if not self._rep_prefix:
                    break
                self.cx = (self.cx - 1) & 0xFFFF
                if self.cx == 0:
                    break
                if self._rep_prefix == 'rep' and not self.zf:
                    break
                if self._rep_prefix == 'repne' and self.zf:
                    break
            return

        # A8 TEST AL, imm8
        if opc == 0xA8:
            self._flags_logic8(self.ax & self._fetchb())
            return

        # A9 TEST AX, imm16
        if opc == 0xA9:
            self._flags_logic16(self.ax & self._fetchw())
            return

        # B0-B7 MOV r8, imm8
        # x86 order: AL,CL,DL,BL,AH,CH,DH,BH → map to internal order via _modrm8_map
        if 0xB0 <= opc <= 0xB7:
            self._set_reg8(self._modrm8_map[opc - 0xB0], self._fetchb())
            return

        # B8-BF MOV r16, imm16
        if 0xB8 <= opc <= 0xBF:
            self._set_reg16(opc - 0xB8, self._fetchw())
            return

        # C2 RET imm16
        if opc == 0xC2:
            extra = self._fetchw()
            self.ip = self._pop()
            self.sp = (self.sp + extra) & 0xFFFF
            return

        # C3 RET
        if opc == 0xC3:
            self.ip = self._pop()
            return

        # C4 LES r16, m32 / C5 LDS r16, m32
        if opc == 0xC4 or opc == 0xC5:
            modrm = self._fetchb()
            mod = (modrm >> 6) & 3
            reg = (modrm >> 3) & 7
            rm = modrm & 7
            # Calculate effective address for the 32-bit far pointer
            # Uses segment override if present, otherwise DS
            if mod == 3:
                ea = 0  # register direct not valid for LDS/LES
            else:
                ea = self._ea(mod, rm)  # physical address
            # Read 32-bit far pointer from memory
            low = self._readw(ea)
            high = self._readw(ea + 2)
            if opc == 0xC4:
                # LES AX, m32
                self._set_reg16(reg, low)
                self._set_es(high)
            else:
                # LDS r16, m32
                self._set_reg16(reg, low)
                self._set_ds(high)
            return

        # C6 MOV r/m8, imm8
        if opc == 0xC6:
            mod, reg, rm = self._decode_modrm()
            if mod == 3:
                imm = self._fetchb()
                self._set_reg8_modrm(rm, imm)
            else:
                ea = self._ea(mod, rm)
                self._writeb(ea, self._fetchb())
            return

        # C7 MOV r/m16, imm16
        if opc == 0xC7:
            mod, reg, rm = self._decode_modrm()
            if mod == 3:
                imm = self._fetchw()
                self._set_reg16(rm, imm)
            else:
                ea = self._ea(mod, rm)
                self._writew(ea, self._fetchw())
            return

        # C8 ENTER imm16, imm8
        if opc == 0xC8:
            frame_size = self._fetchw()
            level = self._fetchb()
            self._push(self.bp)
            self.bp = self.sp
            if level > 0:
                bp = self.bp
                for _ in range(level):
                    bp = self._readw(self._phys(self.ss, bp))
                    self._push(bp)
            self.sp = (self.sp - frame_size) & 0xFFFF
            return

        # C9 LEAVE
        if opc == 0xC9:
            self.sp = self.bp
            self.bp = self._pop()
            return

        # CB RETF
        if opc == 0xCB:
            self.ip = self._pop()
            ret_cs = self._pop()
            if self._pm and (ret_cs & 3) > self._cpl:
                outer_sp = self._pop()
                outer_ss = self._pop()
                self._set_cs(ret_cs)
                self._set_ss(outer_ss)
                self.sp = outer_sp
            else:
                self._set_cs(ret_cs)
            return

        # CA RETF imm16
        # Fetch the immediate BEFORE popping CS:IP; otherwise _fetchw() would
        # read from the return target (the just-restored CS:IP) instead of the
        # instruction stream, corrupting both SP and the resumed IP.
        if opc == 0xCA:
            extra = self._fetchw()
            self.ip = self._pop()
            ret_cs = self._pop()
            if self._pm and (ret_cs & 3) > self._cpl:
                # Outer-ring return: the caller's SS:SP sits below the
                # return address on the current (inner) stack.
                outer_sp = self._pop()
                outer_ss = self._pop()
                self._set_cs(ret_cs)
                self._set_ss(outer_ss)
                self.sp = (outer_sp + extra) & 0xFFFF
            else:
                self._set_cs(ret_cs)
                self.sp = (self.sp + extra) & 0xFFFF
            return

        # CC INT3
        if opc == 0xCC:
            self._do_interrupt(3, software=True)
            return

        # CD INT n
        if opc == 0xCD:
            n = self._fetchb()
            self._do_interrupt(n, software=True)
            return

        # CE INTO
        if opc == 0xCE:
            if self.of:
                self._do_interrupt(4, software=True)
            return

        # CF IRET
        if opc == 0xCF:
            if self._pm and (self.flags & 0x4000):
                # Nested-task return: switch back via the TSS back-link.
                back = self._tss_word(self.TSS_BACKLINK) & 0xFFFC
                self._do_task_switch(back, 'iret')
                return
            self.ip = self._pop()
            ret_cs = self._pop()
            # Returning to an outer ring also restores the outer stack.
            if self._pm and (ret_cs & 3) > self._cpl:
                flags = self._pop()
                outer_sp = self._pop()
                outer_ss = self._pop()
                self._set_cs(ret_cs)
                self._pop_flags(flags)
                self._set_ss(outer_ss)
                self.sp = outer_sp
            else:
                flags = self._pop()
                self._set_cs(ret_cs)
                self._pop_flags(flags)
            return

        # D0-D3 SHIFT/ROTATE
        if 0xD0 <= opc <= 0xD3:
            self._do_shift(opc)
            return

        # D4 AAM — ASCII Adjust after Multiply
        if opc == 0xD4:
            factor = self._fetchb()
            if factor == 0:
                self._raise_divide_error()
                return
            ah_val = (self.ax & 0xFF) // factor
            al_val = (self.ax & 0xFF) % factor
            self.ax = (ah_val << 8) | al_val
            self._set_szp8(self.al)
            return

        # D5 AAD — ASCII Adjust before Add
        if opc == 0xD5:
            factor = self._fetchb()
            # AAD multiplies (not divides), so factor==0 is well-defined:
            # AL = AH*0 + AL = AL, AH = 0.
            ax_val = ((self.ax >> 8) & 0xFF) * factor + (self.ax & 0xFF)
            self.ax = ax_val & 0xFF
            self._set_szp8(self.al)
            return

        # D6 SALC — Set AL on Carry
        if opc == 0xD6:
            self.ax = 0xFF if self.cf else 0x00
            return

        # D7 XLAT — Translate AL via table at [seg:BX+AL], honouring the
        # segment-override prefix (e.g. `CS: XLAT` reads from CS:BX+AL).
        # Default segment for XLAT is DS, matching the Intel SDM.
        if opc == 0xD7:
            seg = self._default_data_seg()
            addr = self._phys(seg, self.bx + (self.ax & 0xFF))
            self.ax = (self.ax & 0xFF00) | self._readb(addr)
            return

        # D8-DF FPU (skip)
        if 0xD8 <= opc <= 0xDF:
            mod, reg, rm = self._decode_modrm()
            self._skip_disp(mod, rm)
            return

        # E0 LOOPNE
        if opc == 0xE0:
            offset = self._fetchb()
            if offset & 0x80: offset |= 0xFF00
            self.cx = (self.cx - 1) & 0xFFFF
            if self.cx and not self.zf:
                self.ip = (self.ip + offset) & 0xFFFF
            return

        # E1 LOOPE
        if opc == 0xE1:
            offset = self._fetchb()
            if offset & 0x80: offset |= 0xFF00
            self.cx = (self.cx - 1) & 0xFFFF
            if self.cx and self.zf:
                self.ip = (self.ip + offset) & 0xFFFF
            return

        # E2 LOOP
        if opc == 0xE2:
            offset = self._fetchb()
            if offset & 0x80: offset |= 0xFF00
            self.cx = (self.cx - 1) & 0xFFFF
            if self.cx:
                self.ip = (self.ip + offset) & 0xFFFF
            return

        # E3 JCXZ
        if opc == 0xE3:
            offset = self._fetchb()
            if offset & 0x80: offset |= 0xFF00
            if self.cx == 0:
                self.ip = (self.ip + offset) & 0xFFFF
            return

        # E8 CALL near
        if opc == 0xE8:
            offset = self._fetchw()
            if offset & 0x8000: offset |= 0xFFFF0000
            self._push(self.ip)
            self.ip = (self.ip + offset) & 0xFFFF
            return

        # E9 JMP near
        if opc == 0xE9:
            offset = self._fetchw()
            if offset & 0x8000: offset |= 0xFFFF0000
            self.ip = (self.ip + offset) & 0xFFFF
            return

        # EA JMP far
        if opc == 0xEA:
            off = self._fetchw()
            seg = self._fetchw()
            self._far_transfer(seg, off, is_call=False)
            return

        # EB JMP short
        if opc == 0xEB:
            offset = self._fetchb()
            if offset & 0x80: offset |= 0xFF00
            self.ip = (self.ip + offset) & 0xFFFF
            return

        # E4 IN AL, imm8
        if opc == 0xE4:
            self._check_io_privilege()
            port = self._fetchb()
            self.al = self.io.inb(port)
            return

        # E5 IN AX, imm8
        if opc == 0xE5:
            self._check_io_privilege()
            port = self._fetchb()
            self.ax = self.io.inw(port)
            return

        # E6 OUT imm8, AL
        if opc == 0xE6:
            self._check_io_privilege()
            port = self._fetchb()
            self.io.outb(port, self.al)
            return

        # E7 OUT imm8, AX
        if opc == 0xE7:
            self._check_io_privilege()
            port = self._fetchb()
            self.io.outw(port, self.ax)
            return

        # EC IN AL, DX
        if opc == 0xEC:
            self._check_io_privilege()
            port = self.dx
            self.ax = (self.ax & 0xFF00) | self.io.inb(port)
            return

        # ED IN AX, DX
        if opc == 0xED:
            self._check_io_privilege()
            port = self.dx
            self.ax = self.io.inw(port)
            return

        # EE OUT DX, AL
        if opc == 0xEE:
            self._check_io_privilege()
            self.io.outb(self.dx, self.ax & 0xFF)
            return

        # EF OUT DX, AX
        if opc == 0xEF:
            self._check_io_privilege()
            self.io.outw(self.dx, self.ax)
            return

        # 0F two-byte escape
        if opc == 0x0F:
            opc2 = self._fetchb()
            if 0x90 <= opc2 <= 0x9F:
                # SETcc r/m8
                mod, reg, rm = self._decode_modrm()
                idx = opc2 - 0x90
                cond_map = {
                    0: self.of, 1: not self.of,
                    2: self.cf, 3: not self.cf,
                    4: self.zf, 5: not self.zf,
                    6: self.zf or self.cf, 7: not (self.zf or self.cf),
                    8: self.sf, 9: not self.sf,
                    10: self.pf, 11: not self.pf,
                    12: self.sf ^ self.of, 13: not (self.sf ^ self.of),
                    14: self.zf or (self.sf ^ self.of),
                    15: not (self.zf or (self.sf ^ self.of)),
                }
                val = 1 if cond_map[idx] else 0
                self._ea_write_byte(mod, rm, val)
                return
            elif opc2 == 0x00:
                # 286 segment/control group: SLDT/STR/LLDT/LTR/VERR/VERW
                mod, reg, rm = self._decode_modrm()
                if reg in (0, 1):          # SLDT / STR
                    self._ea_write_word(
                        mod, rm,
                        self.ldtr_selector if reg == 0 else self.tr_selector)
                elif reg == 2:             # LLDT r/m16
                    sel = self._ea_word(mod, rm)
                    if sel & 0xFFFC:
                        desc = self._translate_selector(sel)
                        if (desc[2] & 0x1F) != 0x02:
                            self._raise_gp(sel & 0xFFFC)  # not an LDT
                        self._desc_cache[sel] = desc
                    self.ldtr_selector = sel
                elif reg == 3:             # LTR r/m16
                    sel = self._ea_word(mod, rm)
                    desc = self._translate_selector(sel)
                    if (desc[2] & 0x1F) not in (0x01, 0x03):
                        self._raise_gp(sel & 0xFFFC)  # not a TSS
                    # LTR marks the task busy without switching to it.
                    if (desc[2] & 0x1F) == 0x01:
                        desc = (desc[0], desc[1], desc[2] | 0x02, desc[3])
                        self._writeb(desc[3] + 5, desc[2])
                    self._desc_cache[sel] = desc
                    self.tr_selector = sel
                elif reg in (4, 5):         # VERR / VERW r/m16
                    sel = self._ea_word(mod, rm)
                    self.zf = self._verify_selector(sel, write=(reg == 5))
                else:
                    if self.debug:
                        print(f"[UNKNOWN 0F 00 /{reg}] at "
                              f"{self.cs:04X}:{self.ip:04X}")
                return
            elif opc2 == 0x01:
                # 286 table/MSW group: SGDT/SIDT/LGDT/LIDT/SMSW/LMSW
                mod, reg, rm = self._decode_modrm()
                if reg in (0, 1):          # SGDT / SIDT m16&24
                    if mod == 3:
                        self._raise_gp(0)
                    base, limit = ((self.gdt_base, self.gdt_limit)
                                   if reg == 0
                                   else (self.idt_base, self.idt_limit))
                    addr = self._ea(mod, rm)
                    self._writew(addr, limit)
                    self._writeb((addr + 2) & 0xFFFFF, base & 0xFF)
                    self._writeb((addr + 3) & 0xFFFFF, (base >> 8) & 0xFF)
                    self._writeb((addr + 4) & 0xFFFFF, (base >> 16) & 0xFF)
                elif reg == 2:             # LGDT m16&24
                    if mod == 3:
                        self._raise_gp(0)
                    addr = self._ea(mod, rm)
                    self.gdt_limit = self._readw(addr)
                    self.gdt_base = self._readb((addr + 2) & 0xFFFFF) \
                        | (self._readb((addr + 3) & 0xFFFFF) << 8) \
                        | (self._readb((addr + 4) & 0xFFFFF) << 16)
                elif reg == 3:             # LIDT m16&24
                    if mod == 3:
                        self._raise_gp(0)
                    addr = self._ea(mod, rm)
                    self.idt_limit = self._readw(addr)
                    self.idt_base = self._readb((addr + 2) & 0xFFFFF) \
                        | (self._readb((addr + 3) & 0xFFFFF) << 8) \
                        | (self._readb((addr + 4) & 0xFFFFF) << 16)
                elif reg == 4:             # SMSW r/m16
                    self._ea_write_word(mod, rm, self.msw)
                elif reg == 6:             # LMSW r/m16
                    self._set_msw(self._ea_word(mod, rm))
                else:
                    if self.debug:
                        print(f"[UNKNOWN 0F 01 /{reg}] at "
                              f"{self.cs:04X}:{self.ip:04X}")
                return
            elif opc2 == 0x02:
                # LAR r16, r/m16 — load access rights
                mod, reg, rm = self._decode_modrm()
                sel = self._ea_word(mod, rm)
                desc = self._peek_selector(sel)
                if desc is not None and (desc[2] & 0x10):
                    self._set_reg16(reg, desc[2] & 0xFF)
                    self.zf = True
                else:
                    self.zf = False
                return
            elif opc2 == 0x03:
                # LSL r16, r/m16 — load segment limit
                mod, reg, rm = self._decode_modrm()
                sel = self._ea_word(mod, rm)
                desc = self._peek_selector(sel)
                if desc is not None and (desc[2] & 0x10):
                    self._set_reg16(reg, desc[1])
                    self.zf = True
                else:
                    self.zf = False
                return
            elif opc2 == 0x06:
                # CLTS — clear the task-switched bit
                self.msw &= ~0x0008
                return
            elif opc2 in (0x05, 0x07, 0x08, 0x30, 0x31, 0x32):
                return
            else:
                if self.debug:
                    print(f"[UNKNOWN 0F OPCODE] 0F {opc2:02X}")
            return

        # F0 LOCK (skip)
        if opc == 0xF0: return

        # F1 INT1 (skip)
        if opc == 0xF1: return

        # F2 REPNE/REPNZ (skip)
        if opc == 0xF2: return

        # F3 REP/REPE/REPZ (skip)
        if opc == 0xF3: return

        # F4 HLT
        if opc == 0xF4:
            if self._pm and self._cpl > 0:
                self._raise_gp(0)      # HLT is privileged
            self.halted = True
            return

        # F5 CMC
        if opc == 0xF5:
            self.cf = not self.cf
            return

        # F6 TEST/NOT/NEG/MUL/IMUL/DIV/IDIV r/m8
        if opc == 0xF6:
            mod, reg, rm = self._decode_modrm()
            if reg == 0:  # TEST r/m8, imm8
                v = self._ea_byte(mod, rm)
                imm = self._fetchb()
                self._flags_logic8(v & imm)
            elif reg == 2:  # NOT r/m8
                if mod == 3:
                    addr = None
                    v = self._get_reg8_modrm(rm)
                else:
                    addr = self._ea(mod, rm)
                    v = self._readb(addr)
                v = (~v) & 0xFF
                if mod == 3:
                    self._set_reg8_modrm(rm, v)
                else:
                    self._writeb(addr, v)
            elif reg == 3:  # NEG r/m8
                if mod == 3:
                    addr = None
                    v = self._get_reg8_modrm(rm)
                else:
                    addr = self._ea(mod, rm)
                    v = self._readb(addr)
                r = self._flags_sub8(0, v)
                if mod == 3:
                    self._set_reg8_modrm(rm, r)
                else:
                    self._writeb(addr, r)
            elif reg == 4:  # MUL r/m8
                v = self._ea_byte(mod, rm)
                prod = (self.ax & 0xFF) * v
                self.ax = prod & 0xFFFF
                self.cf = prod > 0xFF
                self.of = self.cf
            elif reg == 5:  # IMUL r/m8
                v = self._ea_byte(mod, rm)
                if v & 0x80: v |= 0xFF00
                a = self.ax & 0xFF
                if a & 0x80: a |= 0xFF00
                prod = (a * v) & 0xFFFF
                self.ax = prod
                self.cf = prod != ((prod << 8) >> 8) & 0xFF
                self.of = self.cf
            elif reg == 6:  # DIV r/m8
                v = self._ea_byte(mod, rm)
                if v == 0:
                    self._raise_divide_error()
                    return
                ax_val = self.ax
                quotient, remainder = divmod(ax_val, v)
                if quotient > 0xFF:
                    self._raise_divide_error()
                    return
                self.al = quotient
                self.ah = remainder
            elif reg == 7:  # IDIV r/m8
                v = self._ea_byte(mod, rm)
                v = v - 0x100 if v & 0x80 else v
                if v == 0:
                    self._raise_divide_error()
                    return
                ax_val = self.ax - 0x10000 if self.ax & 0x8000 else self.ax
                q, r = self._idiv_trunc(ax_val, v)
                if not -0x80 <= q <= 0x7F:
                    self._raise_divide_error()
                    return
                self.al = q & 0xFF
                self.ah = r & 0xFF
            return

        # F7 TEST/NOT/NEG/MUL/IMUL/DIV/IDIV r/m16
        if opc == 0xF7:
            mod, reg, rm = self._decode_modrm()
            if reg == 0:  # TEST r/m16, imm16
                v = self._ea_word(mod, rm)
                imm = self._fetchw()
                self._flags_logic16(v & imm)
            elif reg == 2:  # NOT r/m16
                if mod == 3:
                    addr = None
                    v = self._get_reg16(rm)
                else:
                    addr = self._ea(mod, rm)
                    v = self._readw(addr)
                v = (~v) & 0xFFFF
                if mod == 3:
                    self._set_reg16(rm, v)
                else:
                    self._writew(addr, v)
            elif reg == 3:  # NEG r/m16
                if mod == 3:
                    addr = None
                    v = self._get_reg16(rm)
                else:
                    addr = self._ea(mod, rm)
                    v = self._readw(addr)
                r = self._flags_sub16(0, v)
                if mod == 3:
                    self._set_reg16(rm, r)
                else:
                    self._writew(addr, r)
            elif reg == 4:  # MUL r/m16
                v = self._ea_word(mod, rm)
                prod = self.ax * v
                self.ax = prod & 0xFFFF
                self.dx = (prod >> 16) & 0xFFFF
                self.cf = prod > 0xFFFF
                self.of = self.cf
            elif reg == 5:  # IMUL r/m16
                v = self._ea_word(mod, rm)
                if v & 0x8000: v |= 0xFFFF0000
                a = self.ax
                if a & 0x8000: a |= 0xFFFF0000
                prod = (a * v) & 0xFFFFFFFF
                self.ax = prod & 0xFFFF
                self.dx = (prod >> 16) & 0xFFFF
                self.cf = prod != ((prod << 16) >> 16) & 0xFFFF
                self.of = self.cf
            elif reg == 6:  # DIV r/m16
                v = self._ea_word(mod, rm)
                if v == 0:
                    self._raise_divide_error()
                    return
                dxax = (self.dx << 16) | self.ax
                quotient, remainder = divmod(dxax, v)
                if quotient > 0xFFFF:
                    self._raise_divide_error()
                    return
                self.ax = quotient
                self.dx = remainder
            elif reg == 7:  # IDIV r/m16
                v = self._ea_word(mod, rm)
                v = v - 0x10000 if v & 0x8000 else v
                if v == 0:
                    self._raise_divide_error()
                    return
                dxax = (self.dx << 16) | self.ax
                if dxax & 0x80000000:
                    dxax -= 0x100000000
                q, r = self._idiv_trunc(dxax, v)
                if not -0x8000 <= q <= 0x7FFF:
                    self._raise_divide_error()
                    return
                self.ax = q & 0xFFFF
                self.dx = r & 0xFFFF
            return

        # F8 CLC
        if opc == 0xF8: self.cf = False; return

        # F9 STC
        if opc == 0xF9: self.cf = True; return

        # 9B WAIT/FWAIT -- no 8087 present, so waiting for the FPU is a no-op
        # (the equipment word reports no coprocessor, so software uses FP).
        if opc == 0x9B:
            return

        # FA CLI
        if opc == 0xFA:
            self._check_io_privilege()
            self.if_flag = False
            return

        # FB STI
        if opc == 0xFB:
            self._check_io_privilege()
            self.if_flag = True
            self._arm_irq_shadow()
            return

        # FC CLD
        if opc == 0xFC: self.df = False; return

        # FD STD
        if opc == 0xFD: self.df = True; return

        # FE INC/DEC r/m8
        if opc == 0xFE:
            mod, reg, rm = self._decode_modrm()
            if mod == 3:
                v = self._get_reg8_modrm(rm)
            else:
                a = self._ea(mod, rm)
                v = self._readb(a)
            if reg & 1:  # DEC
                old = v
                v = (v - 1) & 0xFF
                self.zf = v == 0; self.sf = bool(v & 0x80)
                self.of = v == 0x7F
                self.af = (old & 0x0F) == 0x00
            else:  # INC
                old = v
                v = (v + 1) & 0xFF
                self.zf = v == 0; self.sf = bool(v & 0x80)
                self.of = v == 0x80
                self.af = (old & 0x0F) == 0x0F
            self.pf = bin(v).count('1') % 2 == 0
            if mod == 3:
                self._set_reg8_modrm(rm, v)
            else:
                self._writeb(a, v)
            return

        # FF INC/DEC/CALL/JMP/PUSH r/m16
        if opc == 0xFF:
            mod, reg, rm = self._decode_modrm()
            if mod == 3:
                target = self._reg16(rm)
                addr = None
            else:
                addr = self._ea(mod, rm)
                target = self._readw(addr)
            if reg == 0:  # INC
                v = (target + 1) & 0xFFFF
                self.zf = v == 0; self.sf = bool(v & 0x8000)
                self.of = v == 0x8000; self.pf = bin(v & 0xFF).count('1') % 2 == 0
                self.af = (target & 0x0F) == 0x0F
                if mod == 3: self._set_reg16(rm, v)
                else: self._writew(addr, v)
            elif reg == 1:  # DEC
                v = (target - 1) & 0xFFFF
                self.zf = v == 0; self.sf = bool(v & 0x8000)
                self.of = v == 0x7FFF; self.pf = bin(v & 0xFF).count('1') % 2 == 0
                self.af = (target & 0x0F) == 0x00
                if mod == 3: self._set_reg16(rm, v)
                else: self._writew(addr, v)
            elif reg == 2:  # CALL near
                self._push(self.ip)
                self.ip = target
            elif reg == 3:  # CALL far
                if mod == 3:
                    off = target
                    seg = self.cs
                else:
                    off = self._readw(addr)
                    seg = self._readw((addr + 2) & 0xFFFFF)
                self._far_transfer(seg, off, is_call=True)
            elif reg == 4:  # JMP near
                self.ip = target
            elif reg == 5:  # JMP far
                if mod == 3:
                    off = target
                    seg = self.cs
                else:
                    off = self._readw(addr)
                    seg = self._readw((addr + 2) & 0xFFFFF)
                self._far_transfer(seg, off, is_call=False)
            elif reg == 6:  # PUSH
                if mod == 3:
                    self._push(target)
                else:
                    self._push(self._readw(addr))
            return

        # Unknown opcode
        if self.debug:
            print(f"[UNKNOWN OPCODE] {opc:#04X} at CS:IP={self.cs:04X}:{self.ip-1:04X}")
        self.halted = True

    def _do_interrupt(self, n, error_code=None, software=False):
        """Handle a software interrupt / exception entry.

        Real mode reads the IVT directly.  Protected mode walks the IDT:
        286 interrupt gates (type 6) clear IF, trap gates (type 7) do not;
        both clear TF, and a CPU-supplied ``error_code`` is pushed above
        IP/CS/FLAGS for the handler to pop before IRET.  Task gates and
        privilege stack switches are later milestones; this path assumes
        the interrupt is serviced at the current privilege level.
        """
        if not self._pm:
            self._push(self.flags)
            self.tf = False
            self.if_flag = False
            self._push(self.cs)
            self._push(self.ip)
            vec = n * 4
            self.ip = self._readw(vec)
            self.cs = self._readw(vec + 2)
            return
        gate_addr = self.idt_base + n * 8
        if n * 8 + 7 > self.idt_limit:
            self._raise_gp((n << 3) | 2)
        gate = bytes(self._readb((gate_addr + i) & 0xFFFFF)
                     for i in range(8))
        access = gate[5]
        gate_type = access & 0x0F
        if gate_type == 0x05 and (access & 0x80):
            # Task gate: switch to the referenced TSS (error codes are
            # not delivered across a task switch).
            self._do_task_switch(gate[2] | (gate[3] << 8), 'int')
            return
        if not (access & 0x80) or gate_type not in (0x06, 0x07):
            # Absent/invalid gate.  Hardware would double- then triple-fault
            # into a reset; the deterministic emulator answer is to park
            # the CPU (matching the bare-CPU divide-error behaviour)
            # rather than recurse through #NP.
            if self.debug:
                print(f"[PM] no usable IDT gate for INT {n:02X}; halting",
                      file=sys.stderr)
            self.halted = True
            return
        if software and ((access >> 5) & 3) < self._cpl:
            # ``INT n`` requires a gate DPL no more privileged than CPL;
            # hardware IRQs and CPU exceptions enter regardless of DPL.
            self._raise_gp((n << 3) | 2)
        sel = gate[2] | (gate[3] << 8)
        offset = gate[0] | (gate[1] << 8)
        target = self._translate_selector(sel)
        target_dpl = (target[2] >> 5) & 3
        # Canonicalise: the CPU forces CS RPL to the target ring.
        sel = (sel & 0xFFFC) | target_dpl
        old_cs, old_ip = self.cs, self.ip
        switched = False
        if self._pm and target_dpl < self._cpl:
            # Inner-ring entry: privilege change + TSS stack first, then
            # save the interrupted stack below the return frame.
            saved_ss, saved_sp = self.ss, self.sp
            self._enter_ring(target_dpl, sel)      # loads CS
            switched = True
            self._push(saved_ss)
            self._push(saved_sp)
        self._push(self.flags)
        self.tf = False
        if gate_type == 0x06:
            self.if_flag = False
        self._push(old_cs)
        self._push(old_ip)
        if error_code is not None:
            self._push(error_code)
        if not switched:
            self._load_sreg('cs', sel)
        self.ip = offset

    def _do_shift(self, opc):
        """D0-D3 shift/rotate instructions."""
        mod, reg, rm = self._decode_modrm()
        if opc == 0xD0:
            count = 1
        elif opc == 0xD1:
            count = 1
        elif opc == 0xD2:
            count = self.cl & 0x1F
        elif opc == 0xD3:
            count = self.cl & 0x1F

        is_word = opc in (0xD1, 0xD3)
        # Rotate counts wrap at the operand width.  Through-carry rotates
        # include CF in the ring, so their modulus is width+1.  Without this
        # reduction ROL AL,8 and RCL AL,9 incorrectly perform a full cycle,
        # changing CF even though the architectural count is zero.
        if reg in (0, 1):
            count %= 16 if is_word else 8
        elif reg in (2, 3):
            count %= 17 if is_word else 9
        if mod == 3:
            addr = None
            val = self._get_reg16(rm) if is_word else self._get_reg8_modrm(rm)
        else:
            # Decode the effective address exactly once.  These are
            # read-modify-write instructions, so calling _ea_* again for the
            # write would consume the displacement a second time and advance
            # IP into the following instruction (EDLIN's ``SHR word
            # [13F2],1`` exposed this with a direct disp16 operand).
            addr = self._ea(mod, rm)
            val = self._readw(addr) if is_word else self._readb(addr)

        size = 16 if is_word else 8
        mask = 0xFFFF if is_word else 0xFF
        sign_bit = 1 << (size - 1)

        for _ in range(count):
            if reg == 0:  # ROL
                cf = bool(val & sign_bit)
                val = ((val << 1) | cf) & mask
                self.cf = cf
                if count == 1:
                    self.of = bool((val & sign_bit) ^ self.cf)
            elif reg == 1:  # ROR
                cf = val & 1
                val = ((val >> 1) | (cf << (size - 1))) & mask
                self.cf = bool(cf)
                if count == 1:
                    self.of = bool((val & sign_bit) ^
                                   ((val >> (size - 2)) & 1))
            elif reg == 2:  # RCL
                old_cf = 1 if self.cf else 0
                cf = bool(val & sign_bit)
                val = ((val << 1) | old_cf) & mask
                self.cf = cf
                if count == 1:
                    self.of = bool((val & sign_bit) ^ self.cf)
            elif reg == 3:  # RCR
                old_cf = 1 if self.cf else 0
                cf = val & 1
                val = ((val >> 1) | (old_cf << (size - 1))) & mask
                self.cf = bool(cf)
                if count == 1:
                    self.of = bool((val & sign_bit) ^
                                   ((val >> (size - 2)) & 1))
            elif reg == 4:  # SAL/SHL
                self.cf = bool(val & sign_bit)
                val = (val << 1) & mask
                self.of = self.cf ^ bool(val & sign_bit) if count == 1 else False
            elif reg == 5:  # SHR
                self.cf = val & 1
                # OF (count=1) is the MSB of the ORIGINAL operand
                self.of = bool(val & sign_bit) if count == 1 else False
                val = (val >> 1) & mask
            elif reg == 6:  # SHL (same as SAL)
                self.cf = bool(val & sign_bit)
                val = (val << 1) & mask
                self.of = self.cf ^ bool(val & sign_bit) if count == 1 else False
            elif reg == 7:  # SAR
                self.cf = val & 1
                val = ((val >> 1) | (val & sign_bit)) & mask
                self.of = False

        if is_word:
            if mod == 3:
                self._set_reg16(rm, val)
            else:
                self._writew(addr, val)
        else:
            if mod == 3:
                self._set_reg8_modrm(rm, val)
            else:
                self._writeb(addr, val)

        if count > 1:
            self.of = False

        # Scalar shifts (SHL/SAL, SHR, SAR -- reg 4/5/6/7) update SF, ZF, PF
        # from the result, per the Intel SDM.  AF is officially undefined;
        # we clear it (matches QEMU/unicorn behaviour observed during the
        # DOS-3.3 OPEN-CON differential trace).  Rotates (ROL/ROR/RCL/RCR,
        # reg 0/1/2/3) only touch CF/OF and must NOT modify these.  A count
        # of 0 affects no flags at all.
        if count != 0 and reg in (4, 5, 6, 7):
            self.sf = bool(val & sign_bit)
            self.zf = (val == 0)
            self.pf = bin(val & 0xFF).count('1') % 2 == 0
            self.af = False

    def status(self):
        """Return register state as dict."""
        return {
            'cs': self.cs, 'ip': self.ip,
            'ax': self.ax, 'bx': self.bx, 'cx': self.cx, 'dx': self.dx,
            'sp': self.sp, 'bp': self.bp, 'si': self.si, 'di': self.di,
            'ds': self.ds, 'es': self.es, 'ss': self.ss,
            'flags': self.flags,
            'insn_count': self.insn_count,
            'cycle_count': self.cycle_count,
            'emulated_time': self.emulated_time,
        }
