"""Unicorn-backed native CPU backend.

This module is intentionally kept behind :mod:`cpu_backend`.  Unicorn does
the instruction execution in its native C engine while the existing Python
BIOS and device models remain authoritative at software interrupts and I/O
ports.  The Python ``CPU`` class is not modified by this adapter and remains
the reference/debugging implementation.
"""

import ctypes

try:
    from unicorn import (Uc, UcError, UC_ARCH_X86, UC_MODE_16,
                         UC_HOOK_BLOCK, UC_HOOK_INSN, UC_HOOK_INTR,
                         UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE,
                         UC_HOOK_MEM_FETCH,
                         UC_PROT_ALL)
    from unicorn.x86_const import (
        UC_X86_INS_IN, UC_X86_INS_OUT,
        UC_X86_REG_AX, UC_X86_REG_BX, UC_X86_REG_CX, UC_X86_REG_DX,
        UC_X86_REG_SP, UC_X86_REG_BP, UC_X86_REG_SI, UC_X86_REG_DI,
        UC_X86_REG_CS, UC_X86_REG_DS, UC_X86_REG_ES, UC_X86_REG_SS,
        UC_X86_REG_EIP, UC_X86_REG_FLAGS,
    )
except ImportError as exc:  # pragma: no cover - exercised by no-dev installs
    raise ImportError('the C backend requires the optional unicorn package') from exc

try:
    from capstone import Cs, CS_ARCH_X86, CS_MODE_16
except ImportError as exc:  # pragma: no cover - installed with Unicorn in dev
    raise ImportError('the C backend requires the optional capstone package') from exc

from cpu import CPU
from native_vga_hook import (install as install_native_vga_hook,
                             install_block_recorder)


# Interrupts whose Python handlers can write arbitrary guest memory (disk
# reads, DOS file/program loading, EMS/XMS services). Console, timer, and
# keyboard interrupts only update modeled devices or ordinary state, so they
# do not need a translation-cache flush.
_MEMORY_MUTATING_INTERRUPTS = frozenset((
    0x15, 0x13, 0x21, 0x25, 0x26, 0x27, 0x2F, 0x31, 0x67,
))


_REGMAP = {
    'ax': UC_X86_REG_AX,
    'bx': UC_X86_REG_BX,
    'cx': UC_X86_REG_CX,
    'dx': UC_X86_REG_DX,
    'sp': UC_X86_REG_SP,
    'bp': UC_X86_REG_BP,
    'si': UC_X86_REG_SI,
    'di': UC_X86_REG_DI,
    'cs': UC_X86_REG_CS,
    'ds': UC_X86_REG_DS,
    'es': UC_X86_REG_ES,
    'ss': UC_X86_REG_SS,
    # Unicorn updates EIP for far control transfers even in 16-bit mode;
    # reading the IP alias leaves the offset stale after RETF/LJMP.
    'ip': UC_X86_REG_EIP,
}
_REG_NAMES = tuple(_REGMAP)
_REG_IDS = tuple(_REGMAP.values()) + (UC_X86_REG_FLAGS,)


class CCPU(CPU):
    """CPU-compatible adapter around Unicorn's native 16-bit engine."""

    # Keep the native call boundary small enough that a software interrupt
    # cannot inflate the harness' instruction budget by thousands of steps.
    # Larger batches are faster in isolation, but DOS performs frequent INT
    # calls and Unicorn does not report how many instructions ran before a
    # hook stopped the block.
    native_batch_size = 128
    graphics_native_batch_size = 1024

    def __init__(self, memory, io_ports):
        super().__init__(memory, io_ports)
        self.ram_wait_cycles = getattr(io_ports, 'ram_wait_cycles', 0)
        self.prefetch_wait_cycles = getattr(io_ports, 'prefetch_wait_cycles', 0)
        self.vram_wait_cycles = getattr(io_ports, 'vram_wait_cycles', 0)
        ram_size = len(self._ram) if self._ram is not None else 0
        # Power-of-two backing at least 1 MiB maps cleanly into Unicorn
        # (the default emulator image is 8 MiB: conventional + extended).
        if (self._ram is None or ram_size < 0x100000
                or ram_size & (ram_size - 1)):
            raise RuntimeError('the C backend requires a flat power-of-two '
                               'memory buffer of at least 1 MiB')

        self._uc = Uc(UC_ARCH_X86, UC_MODE_16)
        # Map Unicorn directly onto the emulator's bytearray.  Keeping one
        # backing store removes the former 1 MiB copy in both directions at
        # every batch and means BIOS/DOS/VGA writes are immediately visible to
        # native code and Python-side screen inspection alike.
        self._ram_buffer = (ctypes.c_char * len(self._ram)).from_buffer(
            self._ram)
        self._uc.mem_map_ptr(
            0, len(self._ram), UC_PROT_ALL,
            ctypes.addressof(self._ram_buffer))
        self._pending_interrupt = None
        self._a20_alias_hook = None
        self._a20_low_alias_hook = None
        self._a20_alias_read_hook = None
        self._a20_aliasing = False
        self._native_vram_read_fallback = False
        self._native_vram_stos_fallback = False
        self._last_reg_values = None
        self._last_block = None
        self._decoder = Cs(CS_ARCH_X86, CS_MODE_16)

        self._uc.hook_add(UC_HOOK_INTR, self._on_interrupt)
        self._native_block_recorder = install_block_recorder(self._uc)
        if self._native_block_recorder is None:
            self._uc.hook_add(UC_HOOK_BLOCK, self._on_block)
        self._uc.hook_add(
            UC_HOOK_INSN, self._on_in, aux1=UC_X86_INS_IN)
        self._uc.hook_add(
            UC_HOOK_INSN, self._on_out, aux1=UC_X86_INS_OUT)
        # Native writes can apply the current graphics latches correctly, but
        # a mode-1 A000h read must reload them through Video.graphics_read().
        # Limit this hook to the VGA aperture: ordinary RAM reads stay wholly
        # inside Unicorn.
        self._uc.hook_add(
            UC_HOOK_MEM_READ, self._on_vram_read,
            begin=0xA0000, end=0xAFFFF)
        self._uc.hook_add(
            UC_HOOK_MEM_READ, self._on_text_vram_read,
            begin=0xB8000, end=0xB8FFF)
        if (self.ram_wait_cycles or self.prefetch_wait_cycles):
            self._uc.hook_add(
                UC_HOOK_MEM_READ, self._on_ram_read,
                begin=0x00000, end=0x9FFFF)
            self._uc.hook_add(
                UC_HOOK_MEM_READ, self._on_ram_read,
                begin=0xB9000, end=0xFFFFF)
            self._uc.hook_add(
                UC_HOOK_MEM_FETCH, self._on_fetch,
                begin=0x00000, end=0xFFFFF)
        # Some DOS games bypass INT 1Ah and inspect the BDA tick counter
        # directly. This tiny range is inert unless timing tracing is active.
        self._uc.hook_add(
            UC_HOOK_MEM_READ, self._on_bda_timer_read,
            begin=0x046C, end=0x046F)
        # Pole spends most of its time in write-mode-1 A000h blits.  A Python
        # Unicorn memory callback per byte dominates that workload, so use a
        # compiled hook when available. Trace mode deliberately keeps the
        # instrumented Python path.
        self._native_vga_hook = None
        if (not getattr(self.io.video, 'trace_graphics_writes', None)
                and not getattr(self, 'vram_wait_cycles', 0)):
            self._native_vga_hook = install_native_vga_hook(
                self._uc, self.io.video, self._ram)
        if self._native_vga_hook is None:
            self._uc.hook_add(UC_HOOK_MEM_WRITE, self._on_mem_write)
        # A20 is disabled after reset, including during the first DPMIINST
        # probe; install the alias hook before guest execution begins.
        self.set_a20(False)

    # ── State synchronization ──────────────────────────────────────

    def _sync_regs_to_uc(self):
        values = tuple([getattr(self, name) & 0xFFFF
                        for name in _REG_NAMES] + [self.flags & 0xFFFF])
        previous = self._last_reg_values
        if previous is None:
            for reg, value in zip(_REG_IDS, values):
                self._uc.reg_write(reg, value)
        else:
            for reg, value, old in zip(_REG_IDS, values, previous):
                if value != old:
                    self._uc.reg_write(reg, value)
        self._last_reg_values = values

    def _sync_regs_from_uc(self):
        values = []
        for name, reg in _REGMAP.items():
            value = self._uc.reg_read(reg) & 0xFFFF
            values.append(value)
            setattr(self, name, value)
        flags = self._uc.reg_read(UC_X86_REG_FLAGS) & 0xFFFF
        values.append(flags)
        self.flags = flags
        self._last_reg_values = tuple(values)

    def _sync_to_uc(self):
        self._sync_regs_to_uc()

    def _sync_from_uc(self):
        self._sync_regs_from_uc()

    def sync_from_native(self):
        """Refresh all registers before Python-side IRQ/device handling."""
        self._sync_from_uc()

    def mark_external_state_dirty(self):
        """Flush translated blocks after external Python memory changes."""
        self._uc.ctl_flush_tb()

    # Python BIOS and interrupt glue uses these stack helpers directly.
    def _push(self, value):
        return super()._push(value)

    def _pop(self):
        return super()._pop()

    # ── Native callbacks ────────────────────────────────────────────

    def set_a20(self, enabled):
        """Set the A20 gate and emulate its 1 MiB address alias in Unicorn."""
        super().set_a20(enabled)
        if enabled:
            if self._a20_alias_hook is not None:
                self._uc.hook_del(self._a20_alias_hook)
                self._a20_alias_hook = None
            if self._a20_low_alias_hook is not None:
                self._uc.hook_del(self._a20_low_alias_hook)
                self._a20_low_alias_hook = None
            if self._a20_alias_read_hook is not None:
                self._uc.hook_del(self._a20_alias_read_hook)
                self._a20_alias_read_hook = None
        elif self._a20_alias_hook is None:
            # Unicorn maps RAM linearly, so establish the state that an
            # address-line mask would expose before the first aliased read.
            # The write hook below maintains this pair thereafter.
            if len(self._ram) >= 0x200000:
                self._ram[0x100000:0x200000] = self._ram[:0x100000]
            self._a20_alias_hook = self._uc.hook_add(
                UC_HOOK_MEM_WRITE, self._on_a20_alias_write,
                begin=0x100000, end=min(len(self._ram), 0x200000) - 1)
            self._a20_low_alias_hook = self._uc.hook_add(
                UC_HOOK_MEM_WRITE, self._on_a20_alias_write,
                begin=0, end=0x0FFFF)
            self._a20_alias_read_hook = self._uc.hook_add(
                UC_HOOK_MEM_READ, self._on_a20_alias_read,
                begin=0x100000, end=min(len(self._ram), 0x200000) - 1)

    def _on_a20_alias_write(self, uc, access, address, size, value,
                            user_data=None):
        """Mirror writes across A20's 1 MiB alias while the gate is off.

        Unicorn has one flat mapping and cannot express a runtime address-line
        mask.  Mirroring both directions keeps reads coherent without adding
        a per-instruction Python execution path; the hook exists only while
        A20 is disabled.
        """
        if self._a20_aliasing or self._a20:
            return
        # Hooking only the high alias keeps normal low-memory writes native;
        # the A20 probe writes the high address and observes the low alias.
        alias = address ^ 0x100000
        if alias < 0 or alias + size > len(self._ram):
            return
        self._a20_aliasing = True
        try:
            uc.mem_write(alias, int(value).to_bytes(size, 'little'))
        finally:
            self._a20_aliasing = False

    def _on_a20_alias_read(self, uc, access, address, size, value,
                           user_data=None):
        """Refresh the requested side of the low/high A20 alias before read."""
        if self._a20_aliasing or self._a20:
            return
        alias = address ^ 0x100000
        if alias < 0 or alias + size > len(self._ram):
            return
        self._a20_aliasing = True
        try:
            uc.mem_write(address, bytes(self._ram[alias:alias + size]))
        finally:
            self._a20_aliasing = False

    def _on_interrupt(self, uc, number, user_data=None):
        # Unicorn has already advanced IP past CD imm8, but has not performed
        # the real-mode stack/vector transfer.  Let the existing emulator hook
        # perform that transfer and BIOS/DOS routing in Python.
        self._pending_interrupt = number & 0xFF
        uc.emu_stop()

    def _on_block(self, uc, address, size, user_data=None):
        self._last_block = (address, size)

    def _stopped_on_hlt(self):
        """Return whether the last executed instruction was actually HLT.

        Unicorn advances IP and returns normally for both HLT and an exhausted
        instruction budget. Looking only at the byte before IP is ambiguous:
        an immediate or branch displacement can also be F4h. The last basic
        block provides a trusted decoding boundary, allowing Capstone to
        identify the instruction that ended at the current linear address.
        """
        if self._native_block_recorder is not None:
            native = self._native_block_recorder[1]
            self._last_block = (native.address, native.size)
        if self._last_block is None:
            return False
        current = ((self.cs << 4) + self.ip) & 0xFFFFF
        if self._ram[(current - 1) & 0xFFFFF] != 0xF4:
            return False
        address, size = self._last_block
        if address + size > len(self._ram):
            return False
        code = bytes(self._ram[address:address + size])
        for instruction in self._decoder.disasm(code, address):
            if ((instruction.address + instruction.size) & 0xFFFFF) == current:
                return instruction.mnemonic == 'hlt'
        return False

    def _on_in(self, uc, port, size, value, user_data=None):
        if size == 2:
            return self.io.inw(port) & 0xFFFF
        return self.io.inb(port) & 0xFF

    def _on_out(self, uc, port, size, value, user_data=None):
        if size == 2:
            self.io.outw(port, value & 0xFFFF)
        else:
            self.io.outb(port, value & 0xFF)
        if (getattr(self.io.video, 'graphics_mode', False)
                and not self._graphics_native_safe()):
            # A port write can change the graphics controller halfway through
            # a native batch.  Stop before a latch-dependent A000h access;
            # the next batch uses the reference path.
            uc.emu_stop()
        return 0

    def _on_vram_read(self, uc, access, address, size, value,
                      user_data=None):
        """Fall back only for a latch-loading mode-1 VGA read.

        ``UC_HOOK_MEM_READ`` runs before the memory access. Stopping here
        leaves IP at the read instruction; execute_many then runs precisely
        that instruction through the reference CPU, which reloads all four
        VGA latches. Subsequent mode-1 stores remain native and consume those
        latches through the existing write hook.
        """
        video = self.io.video
        if (video.graphics_mode and video._planar
                and (video.gdc_regs[5] & 3) == 1):
            # The compiled hook completes forward non-overlapping REP MOVS
            # copies in one plane-wise operation. Let it see this source read
            # instead of stopping for the reference path.
            native_cs = uc.reg_read(UC_X86_REG_CS) & 0xFFFF
            native_ip = uc.reg_read(UC_X86_REG_EIP) & 0xFFFF
            ip = ((native_cs << 4) + native_ip) & 0xFFFFF
            if (self._native_vga_hook is not None and size == 1
                    and self._ram[ip:ip + 2] in (b'\xf3\xa4', b'\xf3\xa5')):
                width = 2 if self._ram[ip + 1] == 0xA5 else 1
                flags = uc.reg_read(UC_X86_REG_FLAGS)
                source = ((uc.reg_read(UC_X86_REG_DS) & 0xFFFF) << 4) + \
                    (uc.reg_read(UC_X86_REG_SI) & 0xFFFF)
                destination = ((uc.reg_read(UC_X86_REG_ES) & 0xFFFF) << 4) + \
                    (uc.reg_read(UC_X86_REG_DI) & 0xFFFF)
                length = (uc.reg_read(UC_X86_REG_CX) & 0xFFFF) * width
                safe = (not (flags & 0x0400) and length
                        and 0xA0000 <= source < 0xB0000
                        and 0xA0000 <= destination < 0xB0000
                        and source + length <= 0xB0000
                        and destination + length <= 0xB0000
                        and not (source < destination < source + length))
                if safe:
                    return
            self._native_vram_read_fallback = True
            uc.emu_stop()

    def _on_bda_timer_read(self, uc, access, address, size, value,
                           user_data=None):
        counter = getattr(self.io.video, 'trace_bda_timer_reads', None)
        if counter is not None:
            counter[0] += 1

    def _on_text_vram_read(self, uc, access, address, size, value,
                           user_data=None):
        self.cycle_count += size * getattr(self, 'vram_wait_cycles', 0)

    def _on_ram_read(self, uc, access, address, size, value,
                     user_data=None):
        self.cycle_count += size * getattr(self, 'ram_wait_cycles', 0)

    def _on_fetch(self, uc, access, address, size, value, user_data=None):
        self.cycle_count += size * getattr(self, 'prefetch_wait_cycles', 0)

    def _on_mem_write(self, uc, access, address, size, value, user_data=None):
        if 0xB8000 <= address < 0xB9000:
            self.cycle_count += size * getattr(self, 'vram_wait_cycles', 0)
            return
        if not (0xA0000 <= address < 0xB0000
                and getattr(self.io.video, 'graphics_mode', False)):
            self.cycle_count += size * getattr(self, 'ram_wait_cycles', 0)
        if (0xA0000 <= address < 0xB0000
                and getattr(self.io.video, 'graphics_mode', False)):
            for i in range(size):
                offset = address - 0xA0000 + i
                video = self.io.video
                # Modes 0, 2, and 3 use the destination latches for their
                # raster operation.  Unicorn cannot expose A000h reads to
                # the device model, so load them from the destination here.
                # Mode 1 instead transfers a previously-read source latch
                # and remains on the reference path below.
                if video._planar and (video.gdc_regs[5] & 3) != 1:
                    for plane in range(4):
                        video.graphics_latches[plane] = video.graphics_planes[
                            plane][offset & 0xFFFF]
                video.graphics_write(offset, value >> (8 * i))

    def _graphics_native_safe(self):
        """Whether native A000h writes cannot observe VGA latches.

        Modes 0, 2, and 3 operate on destination latches, which the native
        write callback can load exactly from the target byte. Mode 1 executes
        natively too; its aperture reads stop at the dedicated read hook and
        reload the source latch through one reference instruction.
        """
        video = self.io.video
        if not video.graphics_mode:
            return True
        if not video._planar:
            return True
        return True

    def preferred_batch_size(self):
        """Use stable native chunks for bulk graphics and responsive input."""
        if self._graphics_native_safe() and self.io.video.graphics_mode:
            return self.graphics_native_batch_size
        return self.native_batch_size

    def _rep_string_count(self):
        """Return the native iteration budget needed to finish REP strings."""
        address = ((self.cs << 4) + self.ip) & 0xFFFFF
        index = address
        # REP/REPE/REPNE prefixes may be preceded by segment/lock prefixes;
        # the boot sectors we support use the conventional F2/F3 form, but
        # scan the complete prefix range so this remains safe for DOS code.
        has_rep = False
        while self._ram[index] in (0x26, 0x2E, 0x36, 0x3E, 0x64, 0x65,
                                   0xF0, 0xF2, 0xF3):
            if self._ram[index] in (0xF2, 0xF3):
                has_rep = True
            index = (index + 1) & 0xFFFFF
        if (not has_rep or
                self._ram[(address + ((index - address) & 0xFFFFF)) & 0xFFFFF] \
                not in (0xA4, 0xA5, 0xA6, 0xA7, 0xAA, 0xAB, 0xAC, 0xAD,
                        0xAE, 0xAF)):
            return 0
        # Unicorn consumes one native count per repeated element.  A zero CX
        # REP is still a single guest instruction and needs no expansion.
        return (self.cx + 1) if self.cx else 0

    # ── Execution ───────────────────────────────────────────────────

    def execute_many(self, count):
        """Execute a batch in native code, stopping at Python interrupts."""
        if count <= 0 or self.halted or self.insn_count >= self.max_insns:
            return 0
        count = min(int(count), self.max_insns - self.insn_count)
        self._pending_interrupt = None
        self._native_vram_read_fallback = False
        self._last_block = None
        self._sync_to_uc()
        start = ((self.cs << 4) + self.ip) & 0xFFFFF
        try:
            # A zero end address is not accepted consistently across Unicorn
            # builds; 1 MiB is a harmless upper bound for this flat map.
            native_count = max(count, self._rep_string_count())
            self._uc.emu_start(start, 0x100000, count=native_count)
        except UcError as exc:
            self._sync_from_uc()
            if self.debug:
                print(f'[C CPU fallback] {exc} at '
                      f'{self.cs:04X}:{self.ip:04X}')
            # Unicorn rejects a small number of legacy/reserved encodings
            # which the reference decoder intentionally tolerates.  Recover
            # one instruction through the reference implementation and then
            # resume native execution; this keeps DOS compatibility without
            # forcing graphics-heavy guests onto the Python CPU permanently.
            self._uc.ctl_flush_tb()
            if CPU.execute(self):
                self.insn_count += 1
                return 1
            self.halted = True
            return 0

        if self._native_vram_read_fallback:
            self._sync_from_uc()
            self._native_vram_read_fallback = False
            self._uc.ctl_flush_tb()
            if CPU.execute(self):
                self.insn_count += 1
                return 1
            self.halted = True
            return 0

        if self._native_vga_hook is not None:
            dirty = self._native_vga_hook[3]
            if dirty[0]:
                self.io.video.graphics_dirty = True
                dirty[0] = 0

        self.last_instruction_cycles = self.cycles_per_instruction
        self.cycle_count += count * self.cycles_per_instruction

        if self._pending_interrupt is not None:
            # BIOS/DOS handlers operate on the Python memory/register view.
            self._sync_from_uc()
            number = self._pending_interrupt
            self._pending_interrupt = None
            self._do_interrupt(number)
            # Direct host-memory writes bypass Unicorn's normal mem_write
            # cache invalidation. Flush only handlers that can load or patch
            # guest code; console/timer interrupts stay on the fast path.
            if number in _MEMORY_MUTATING_INTERRUPTS:
                self._uc.ctl_flush_tb()
        else:
            self._sync_from_uc()
            # A basic-block hook is substantially cheaper than a Python
            # callback for every instruction and avoids mistaking operand
            # bytes equal to F4h for HLT.
            if self._stopped_on_hlt():
                self.halted = True

        # Unicorn's count is the requested architectural budget.  Interrupts
        # can stop early, so accounting is bounded to at most one native
        # batch of over-counting.  The Python reference backend retains exact
        # per-instruction accounting for diagnostics and tests.
        self.insn_count += count
        return count

    def execute(self):
        """Execute one instruction through the native engine."""
        return bool(self.execute_many(1))


def create_cpu(memory, io_ports):
    """Factory consumed by :func:`cpu_backend.create_cpu`."""
    return CCPU(memory, io_ports)
