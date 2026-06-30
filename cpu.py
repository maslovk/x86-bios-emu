import sys

"""
Simple BIOS Emulator - CPU Core
================================
Minimal x86 real-mode CPU emulator with full instruction decode.
"""

import os


class CPU:
    """Minimal x86 real-mode CPU emulator."""

    def __init__(self, memory, io_ports):
        self.mem = memory
        self.io = io_ports
        self.halted = False
        self.int_no_return = False  # True when INT handler takes over (e.g., boot)
        self.insn_count = 0
        self.max_insns = 50_000_000
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
        return [self.ax, self.cx, self.dx, self.bx,
                self.sp, self.bp, self.si, self.di][r]

    def _set_reg16(self, r, v):
        names = ['ax', 'cx', 'dx', 'bx', 'sp', 'bp', 'si', 'di']
        setattr(self, names[r], v & 0xFFFF)

    def _get_reg16(self, r):
        names = ['ax', 'cx', 'dx', 'bx', 'sp', 'bp', 'si', 'di']
        return getattr(self, names[r]) & 0xFFFF

    def _get_reg8(self, r):
        """Internal 8-bit register: 0=AL,1=AH,2=CL,3=CH,4=DL,5=DH,6=BL,7=BH."""
        base = r // 2
        lo = [self.ax & 0xFF, self.cx & 0xFF, self.dx & 0xFF, self.bx & 0xFF]
        hi = [(self.ax >> 8) & 0xFF, (self.cx >> 8) & 0xFF, (self.dx >> 8) & 0xFF, (self.bx >> 8) & 0xFF]
        return lo[base] if r % 2 == 0 else hi[base]

    def _set_reg8(self, r, v):
        """Internal 8-bit register: 0=AL,1=AH,2=CL,3=CH,4=DL,5=DH,6=BL,7=BH."""
        base = r // 2
        names_lo = ['ax', 'cx', 'dx', 'bx']
        names_hi = ['ax', 'cx', 'dx', 'bx']
        if r % 2 == 0:
            setattr(self, names_lo[base],
                    (getattr(self, names_lo[base]) & 0xFF00) | (v & 0xFF))
        else:
            setattr(self, names_hi[base],
                    (getattr(self, names_hi[base]) & 0x00FF) | ((v & 0xFF) << 8))

    # ModR/M 8-bit register mapping: 0=AL,1=CL,2=DL,3=BL,4=AH,5=CH,6=DH,7=BH
    _modrm8_map = [0, 2, 4, 6, 1, 3, 5, 7]  # ModR/M idx → internal idx

    def _get_reg8_modrm(self, r):
        """ModR/M 8-bit register access."""
        return self._get_reg8(self._modrm8_map[r])

    def _set_reg8_modrm(self, r, v):
        """ModR/M 8-bit register store."""
        self._set_reg8(self._modrm8_map[r], v)

    # ── Memory access ──────────────────────────────────────────────

    def _phys(self, seg, off):
        return ((seg << 4) + off) & 0xFFFFF

    def _readb(self, a): return self.mem.read_byte(a)
    def _readw(self, a): return self.mem.read_word(a)
    def _writeb(self, a, v): self.mem.write_byte(a, v)
    def _writew(self, a, v): self.mem.write_word(a, v)

    def _fetchb(self):
        v = self._readb(self._phys(self.cs, self.ip))
        self.ip = (self.ip + 1) & 0xFFFF
        return v

    def _fetchw(self):
        v = self._readw(self._phys(self.cs, self.ip))
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

    def _ea(self, mod, rm, seg=None):
        """Effective address (physical)."""
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
        base_map = {
            0: self.bx + self.si, 1: self.bx + self.di,
            2: self.bp + self.si, 3: self.bp + self.di,
            4: self.si, 5: self.di, 6: self.bp, 7: self.bx,
        }
        if mod == 0 and rm == 6:
            # In 16-bit addressing, mod=00 rm=110 encodes a direct disp16.
            disp = self._fetchw()
            return self._phys(seg, disp)
        base = base_map.get(rm, self.bp)
        disp = self._read_disp(mod, rm)
        return self._phys(seg, base + disp)

    def _ea_byte(self, mod, rm, seg=None):
        if mod == 3:
            return self._get_reg8_modrm(rm)
        return self._readb(self._ea(mod, rm, seg))

    def _ea_word(self, mod, rm, seg=None):
        if mod == 3:
            return self._get_reg16(rm)
        return self._readw(self._ea(mod, rm, seg))

    def _ea_write_byte(self, mod, rm, val, seg=None):
        if mod == 3:
            self._set_reg8_modrm(rm, val)
        else:
            self._writeb(self._ea(mod, rm, seg), val)

    def _ea_write_word(self, mod, rm, val, seg=None):
        if mod == 3:
            self._set_reg16(rm, val)
        else:
            self._writew(self._ea(mod, rm, seg), val)

    def _default_data_seg(self):
        return self._seg_override if self._seg_override is not None else self.ds

    # ── Stack ──────────────────────────────────────────────────────

    def _push(self, val):
        self.sp = (self.sp - 2) & 0xFFFF
        self._writew(self._phys(self.ss, self.sp), val & 0xFFFF)

    def _pop(self):
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

    def _flags_logic8(self, r):
        r &= 0xFF
        self.zf = r == 0
        self.sf = bool(r & 0x80)
        self.cf = False
        self.of = False
        self.pf = bin(r).count('1') % 2 == 0

    def _flags_logic16(self, r):
        r &= 0xFFFF
        self.zf = r == 0
        self.sf = bool(r & 0x8000)
        self.cf = False
        self.of = False
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

    def _set_es(self, v): self.es = v
    def _set_cs(self, v): self.cs = v
    def _set_ss(self, v): self.ss = v
    def _set_ds(self, v): self.ds = v

    def _arm_irq_shadow(self):
        """Suppress maskable IRQ delivery until after the following instruction."""
        self._irq_shadow = max(self._irq_shadow, 2)

    def _string_repeat_count(self):
        return self.cx if self._rep_prefix else 1

    # ── Main execute loop ──────────────────────────────────────────

    def execute(self):
        """Execute one instruction. Returns False on halt/error."""
        if self.halted or self.insn_count >= self.max_insns:
            return False
        self.insn_count += 1
        save_ip = self.ip
        save_cs = self.cs
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
        if self._irq_shadow:
            self._irq_shadow -= 1
        if self.step_mode:
            self._step_print(opc, save_ip)
        return True

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
        if opc == 0x07: self.es = self._pop(); return
        if opc == 0x0E: self._push(self.cs); return
        if opc == 0x16: self._push(self.ss); return
        if opc == 0x17:
            self.ss = self._pop()
            self._arm_irq_shadow()
            return
        if opc == 0x1E: self._push(self.ds); return
        if opc == 0x1F: self.ds = self._pop(); return

        # Segment prefixes (handled in execute() loop now)

        # Skip unimplemented BCD instructions
        if opc in (0x27, 0x2F, 0x37, 0x3F): return

        # 40-47 INC r16
        if 0x40 <= opc <= 0x47:
            r = opc - 0x40
            v = (self._reg16(r) + 1) & 0xFFFF
            self._set_reg16(r, v)
            self.zf = v == 0; self.sf = bool(v & 0x8000)
            self.of = v == 0x8000; self.pf = bin(v & 0xFF).count('1') % 2 == 0
            return

        # 48-4F DEC r16
        if 0x48 <= opc <= 0x4F:
            r = opc - 0x48
            v = (self._reg16(r) - 1) & 0xFFFF
            self._set_reg16(r, v)
            self.zf = v == 0; self.sf = bool(v & 0x8000)
            self.of = v == 0x7FFF; self.pf = bin(v & 0xFF).count('1') % 2 == 0
            return

        # 50-57 PUSH r16
        if 0x50 <= opc <= 0x57:
            self._push(self._reg16(opc - 0x50)); return

        # 58-5F POP r16
        if 0x58 <= opc <= 0x5F:
            self._set_reg16(opc - 0x58, self._pop()); return

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

        # 84 TEST AL, r/m8
        if opc == 0x84:
            mod, reg, rm = self._decode_modrm()
            self._flags_logic8(self.ax & self._ea_byte(mod, rm))
            return

        # 85 TEST AX, r/m16
        if opc == 0x85:
            mod, reg, rm = self._decode_modrm()
            self._flags_logic16(self.ax & self._ea_word(mod, rm))
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
            self._push(self.cs)
            self._push(self.ip)
            self.cs = seg
            self.ip = off
            return

        # 9C PUSHF
        if opc == 0x9C: self._push(self.flags); return

        # 9D POPF
        if opc == 0x9D: self.flags = self._pop(); return

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
            self.ax = self._readw(self._phys(self._default_data_seg(), addr))
            return

        # A2 MOV [addr], AL
        if opc == 0xA2:
            addr = self._fetchw()
            self._writeb(self._phys(self._default_data_seg(), addr), self.ax & 0xFF)
            return

        # A3 MOV [addr], AX
        if opc == 0xA3:
            addr = self._fetchw()
            self._writew(self._phys(self._default_data_seg(), addr), self.ax)
            return

        # A4 MOVSB
        if opc == 0xA4:
            count = self._string_repeat_count()
            inc = 1 if not self.df else -1
            src_seg = self._default_data_seg()
            for _ in range(count):
                s = self._phys(src_seg, self.si)
                d = self._phys(self.es, self.di)
                self._writeb(d, self._readb(s))
                self.si = (self.si + inc) & 0xFFFF
                self.di = (self.di + inc) & 0xFFFF
                if self._rep_prefix:
                    self.cx = (self.cx - 1) & 0xFFFF
            return

        # A5 MOVSW
        if opc == 0xA5:
            count = self._string_repeat_count()
            inc = 2 if not self.df else -2
            src_seg = self._default_data_seg()
            for _ in range(count):
                s = self._phys(src_seg, self.si)
                d = self._phys(self.es, self.di)
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
            while True:
                a = self._readw(self._phys(src_seg, self.si))
                b = self._readw(self._phys(self.es, self.di))
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
            for _ in range(count):
                self._writew(self._phys(self.es, self.di), self.ax)
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
                self.ax = self._readw(self._phys(src_seg, self.si))
                self.si = (self.si + inc) & 0xFFFF
                if self._rep_prefix:
                    self.cx = (self.cx - 1) & 0xFFFF
            return

        # AE SCASB — Compare AL vs [ES:DI]
        if opc == 0xAE:
            inc = 1 if not self.df else -1
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
            while True:
                b = self._readw(self._phys(self.es, self.di))
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
                self.es = high
            else:
                # LDS r16, m32
                self._set_reg16(reg, low)
                self.ds = high
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
            self.cs = self._pop()
            return

        # CA RETF imm16
        # Fetch the immediate BEFORE popping CS:IP; otherwise _fetchw() would
        # read from the return target (the just-restored CS:IP) instead of the
        # instruction stream, corrupting both SP and the resumed IP.
        if opc == 0xCA:
            extra = self._fetchw()
            self.ip = self._pop()
            self.cs = self._pop()
            self.sp = (self.sp + extra) & 0xFFFF
            return

        # CC INT3
        if opc == 0xCC:
            self._do_interrupt(3)
            return

        # CD INT n
        if opc == 0xCD:
            n = self._fetchb()
            self._do_interrupt(n)
            return

        # CE INTO
        if opc == 0xCE:
            if self.of:
                self._do_interrupt(4)
            return

        # CF IRET
        if opc == 0xCF:
            self.ip = self._pop()
            self.cs = self._pop()
            self.flags = self._pop()
            return

        # D0-D3 SHIFT/ROTATE
        if 0xD0 <= opc <= 0xD3:
            self._do_shift(opc)
            return

        # D4 AAM — ASCII Adjust after Multiply
        if opc == 0xD4:
            factor = self._fetchb()
            if factor == 0:
                self.cf = True; return
            ah_val = (self.ax & 0xFF) // factor
            al_val = (self.ax & 0xFF) % factor
            self.ax = (ah_val << 8) | al_val
            return

        # D5 AAD — ASCII Adjust before Add
        if opc == 0xD5:
            factor = self._fetchb()
            if factor == 0:
                self.cf = True; return
            ax_val = ((self.ax >> 8) & 0xFF) * factor + (self.ax & 0xFF)
            self.ax = ax_val & 0xFF
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
            self.ip = off
            self.cs = seg
            return

        # EB JMP short
        if opc == 0xEB:
            offset = self._fetchb()
            if offset & 0x80: offset |= 0xFF00
            self.ip = (self.ip + offset) & 0xFFFF
            return

        # E4 IN AL, imm8
        if opc == 0xE4:
            port = self._fetchb()
            self.al = self.io.inb(port)
            return

        # E5 IN AX, imm8
        if opc == 0xE5:
            port = self._fetchb()
            self.ax = self.io.inw(port)
            return

        # E6 OUT imm8, AL
        if opc == 0xE6:
            port = self._fetchb()
            self.io.outb(port, self.al)
            return

        # E7 OUT imm8, AX
        if opc == 0xE7:
            port = self._fetchb()
            self.io.outw(port, self.ax)
            return

        # EC IN AL, DX
        if opc == 0xEC:
            port = self.dx
            self.ax = (self.ax & 0xFF00) | self.io.inb(port)
            return

        # ED IN AX, DX
        if opc == 0xED:
            port = self.dx
            self.ax = self.io.inw(port)
            return

        # EE OUT DX, AL
        if opc == 0xEE:
            self.io.outb(self.dx, self.ax & 0xFF)
            return

        # EF OUT DX, AX
        if opc == 0xEF:
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
            elif opc2 == 0x01:
                mod, reg, rm = self._decode_modrm()
                self._skip_disp(mod, rm)
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
                v = self._ea_byte(mod, rm)
                v = (~v) & 0xFF
                self._ea_write_byte(mod, rm, v)
            elif reg == 3:  # NEG r/m8
                v = self._ea_byte(mod, rm)
                r = self._flags_sub8(0, v)
                self._ea_write_byte(mod, rm, r)
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
                if v == 0: self.cf = True; return
                ax_val = self.ax
                self.al = (ax_val // v) & 0xFF
                self.ah = (ax_val % v) & 0xFF
            elif reg == 7:  # IDIV r/m8
                v = self._ea_byte(mod, rm)
                if v & 0x80: v |= 0xFF00
                if v == 0: self.cf = True; return
                ax_val = self.ax
                if ax_val & 0x80: ax_val |= 0xFF00
                q = ax_val // v
                r = ax_val % v
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
                v = self._ea_word(mod, rm)
                v = (~v) & 0xFFFF
                self._ea_write_word(mod, rm, v)
            elif reg == 3:  # NEG r/m16
                v = self._ea_word(mod, rm)
                r = self._flags_sub16(0, v)
                self._ea_write_word(mod, rm, r)
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
                if v == 0: self.cf = True; return
                dxax = (self.dx << 16) | self.ax
                self.ax = (dxax // v) & 0xFFFF
                self.dx = (dxax % v) & 0xFFFF
            elif reg == 7:  # IDIV r/m16
                v = self._ea_word(mod, rm)
                if v & 0x8000: v |= 0xFFFF0000
                if v == 0: self.cf = True; return
                dxax = (self.dx << 16) | self.ax
                if dxax & 0x80000000: dxax |= 0xFFFFFFFF00000000
                q = dxax // v
                r = dxax % v
                self.ax = q & 0xFFFF
                self.dx = r & 0xFFFF
            return

        # F8 CLC
        if opc == 0xF8: self.cf = False; return

        # F9 STC
        if opc == 0xF9: self.cf = True; return

        # FA CLI
        if opc == 0xFA: self.if_flag = False; return

        # FB STI
        if opc == 0xFB:
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
                v = (v - 1) & 0xFF
                self.zf = v == 0; self.sf = bool(v & 0x80)
                self.of = v == 0x7F
            else:  # INC
                v = (v + 1) & 0xFF
                self.zf = v == 0; self.sf = bool(v & 0x80)
                self.of = v == 0x80
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
                if mod == 3: self._set_reg16(rm, v)
                else: self._writew(addr, v)
            elif reg == 1:  # DEC
                v = (target - 1) & 0xFFFF
                self.zf = v == 0; self.sf = bool(v & 0x8000)
                self.of = v == 0x7FFF; self.pf = bin(v & 0xFF).count('1') % 2 == 0
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
                self._push(self.cs)
                self._push(self.ip)
                self.cs = seg
                self.ip = off
            elif reg == 4:  # JMP near
                self.ip = target
            elif reg == 5:  # JMP far
                if mod == 3:
                    off = target
                    seg = self.cs
                else:
                    off = self._readw(addr)
                    seg = self._readw((addr + 2) & 0xFFFFF)
                self.ip = off
                self.cs = seg
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

    def _do_interrupt(self, n):
        """Handle software interrupt."""
        self._push(self.flags)
        self.tf = False
        self.if_flag = False
        self._push(self.cs)
        self._push(self.ip)
        vec = n * 4
        self.ip = self._readw(vec)
        self.cs = self._readw(vec + 2)

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
        if mod == 3:
            val = self._get_reg16(rm) if is_word else self._get_reg8_modrm(rm)
        else:
            val = self._ea_word(mod, rm) if is_word else self._ea_byte(mod, rm)

        size = 16 if is_word else 8
        mask = 0xFFFF if is_word else 0xFF
        sign_bit = 1 << (size - 1)

        for _ in range(count):
            if reg == 0:  # ROL
                cf = bool(val & sign_bit)
                val = ((val << 1) | cf) & mask
                self.cf = cf
            elif reg == 1:  # ROR
                cf = val & 1
                val = ((val >> 1) | (cf << (size - 1))) & mask
                self.cf = bool(cf)
            elif reg == 2:  # RCL
                old_cf = 1 if self.cf else 0
                cf = bool(val & sign_bit)
                val = ((val << 1) | old_cf) & mask
                self.cf = cf
            elif reg == 3:  # RCR
                old_cf = 1 if self.cf else 0
                cf = val & 1
                val = ((val >> 1) | (old_cf << (size - 1))) & mask
                self.cf = bool(cf)
            elif reg == 4:  # SAL/SHL
                self.cf = bool(val & sign_bit)
                val = (val << 1) & mask
                self.of = self.cf ^ bool(val & sign_bit) if count == 1 else False
            elif reg == 5:  # SHR
                self.cf = val & 1
                val = (val >> 1) & mask
                self.of = bool(val & sign_bit) if count == 1 else False
            elif reg == 6:  # SHL (same as SAL)
                self.cf = bool(val & sign_bit)
                val = (val << 1) & mask
            elif reg == 7:  # SAR
                self.cf = val & 1
                val = ((val >> 1) | (val & sign_bit)) & mask

        if is_word:
            if mod == 3:
                self._set_reg16(rm, val)
            else:
                self._ea_write_word(mod, rm, val)
        else:
            if mod == 3:
                self._set_reg8_modrm(rm, val)
            else:
                self._ea_write_byte(mod, rm, val)

        if count != 1:
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
        }
