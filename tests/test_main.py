"""Unit tests for main.py — boot sector builder and emulator."""

import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from main import build_boot_sector


# ── Boot Sector Builder ────────────────────────────────────────

class TestBootSector:
    def test_length(self):
        assert len(build_boot_sector()) == 512

    def test_boot_signature(self):
        s = build_boot_sector()
        assert s[510] == 0x55 and s[511] == 0xAA

    def test_starts_with_cli(self):
        assert build_boot_sector()[0] == 0xFA

    def test_contains_hello(self):
        assert b'Hello from boot sector!' in build_boot_sector()

    def test_contains_msg2(self):
        assert b'Press any key...' in build_boot_sector()

    def test_contains_msg3(self):
        assert b'Key: ' in build_boot_sector()

    def test_contains_msg4(self):
        assert b' OK!' in build_boot_sector()

    def test_ends_with_hlt_loop(self):
        s = build_boot_sector()
        pos = s.rfind(0xF4)
        assert pos > 100  # HLT is in the code section
        assert s[pos + 1] == 0xEB and s[pos + 2] == 0xFE  # JMP $


# ── Emulator Integration ───────────────────────────────────────

class TestEmulatorIntegration:
    def test_irq_setup_preserves_bios_int1c_stub(self):
        """Timer-hooking guests must be able to chain to the old INT 1Ch."""
        from main import Emulator
        emu = Emulator(enable_hardware=True)
        emu.bios.initialize()
        before = (emu.mem.read_word(0x1C * 4),
                  emu.mem.read_word(0x1C * 4 + 2))

        emu._setup_ivt_irq_handlers()

        after = (emu.mem.read_word(0x1C * 4),
                 emu.mem.read_word(0x1C * 4 + 2))
        assert before == after == tuple(reversed(emu.bios.ivt_stubs[0x1C]))

    def test_boot_sector_prints_hello(self):
        from main import Emulator
        emu = Emulator()
        emu.run()
        row = emu.video.buffer[0]
        text = ''
        for ch, _ in row:
            text += chr(ch) if 0x20 <= ch <= 0x7E else ' '
        assert 'Hello from boot sector!' in text

    def test_boot_sector_final_registers(self):
        from main import Emulator
        emu = Emulator()
        emu.run()
        s = emu.cpu.status()
        assert s['cs'] == 0x0000
        assert s['ip'] == 0x7CB4
        assert emu.cpu.halted is True

    def test_boot_sector_stack_preserved(self):
        from main import Emulator
        emu = Emulator()
        emu.run()
        assert emu.cpu.status()['sp'] == 0x7C00

    def test_boot_sector_instruction_count(self):
        from main import Emulator
        emu = Emulator()
        emu.run()
        assert emu.cpu.insn_count == 48

    def test_custom_boot_sector(self, tmp_path):
        from main import Emulator
        code = bytearray([0xF4])
        code.extend([0] * 509)
        code.append(0x55); code.append(0xAA)
        boot_file = tmp_path / 'boot.bin'
        boot_file.write_bytes(bytes(code))
        emu = Emulator(boot_file=str(boot_file))
        emu.run()
        assert emu.cpu.halted is True
        assert emu.cpu.insn_count == 1

    def test_legacy_dos1_media_descriptor_comes_from_fat(self, tmp_path):
        """DOS 1.x disks use a reserved BPB byte but a valid FAT media byte."""
        from main import Emulator
        image = bytearray(640 * 512)
        image[0x15] = 0xBB  # Compaq DOS 1.x reserved/BPB value
        image[512] = 0xFF   # double-sided 320 KB FAT12 descriptor
        path = tmp_path / 'dos1.img'
        path.write_bytes(image)
        emu = Emulator(floppy_image=str(path))
        assert emu.disk.media_type == 0xFF
        assert (emu.disk.cylinders, emu.disk.heads,
                emu.disk.sectors_per_track) == (40, 2, 8)

    def test_360k_image_uses_nine_sector_geometry(self, tmp_path):
        from main import Emulator
        image = bytearray(720 * 512)
        image[0x15] = 0xFD
        path = tmp_path / '360k.img'
        path.write_bytes(image)
        emu = Emulator(floppy_image=str(path))
        assert emu.disk.media_type == 0xFD
        assert (emu.disk.cylinders, emu.disk.heads,
                emu.disk.sectors_per_track) == (40, 2, 9)

    def test_irq_wakes_halted_cpu(self):
        from main import Emulator
        emu = Emulator()
        emu.bios.initialize()
        emu.pic.initialize()
        emu.cpu.if_flag = True
        emu.cpu.halted = True

        before = emu.mem.read_dword(0x046C)
        emu.pic.raise_irq(0)

        assert emu._check_and_dispatch_irq() is True
        assert emu.cpu.halted is False
        assert emu.mem.read_dword(0x046C) == (before + 1) & 0xFFFFFFFF

    def test_timer_request_does_not_swallow_keyboard_irq(self):
        from main import Emulator
        emu = Emulator()
        emu.bios.initialize()
        emu.pic.initialize()
        emu.cpu.if_flag = True

        emu.pic.raise_irq(0)
        emu.kbd_ctrl.inject_scan_code(0x38)  # Left Alt make

        assert emu._schedule_keyboard_irq() is True
        assert emu.pic.irr & 0x03 == 0x03
        assert emu.pic.ims == 0

        assert emu._check_and_dispatch_irq() is True
        assert emu.pic.is_irq_pending(0) is False
        assert emu.pic.is_irq_pending(1) is True

        assert emu._check_and_dispatch_irq() is True
        assert emu.mem.read_byte(0x00417) & 0x08

        emu.kbd_ctrl.inject_scan_code(0xB8)  # Left Alt break
        assert emu._schedule_keyboard_irq() is True
        assert emu._check_and_dispatch_irq() is True
        assert not (emu.mem.read_byte(0x00417) & 0x08)

    def test_enhanced_key_bytes_receive_separate_keyboard_irqs(self):
        from main import Emulator
        emu = Emulator()
        emu.bios.initialize()
        emu.pic.initialize()
        emu.cpu.if_flag = True

        emu.kbd_ctrl.inject_scan_code(0xE0)
        emu.kbd_ctrl.inject_scan_code(0x4B)

        assert emu._schedule_keyboard_irq() is True
        assert emu._check_and_dispatch_irq() is True
        assert emu.kbd.buffer == []

        assert not emu.kbd_ctrl.has_output_data()

        # No new host key event is needed: after the prefix IRQ/EOI, the next
        # controller service presents the actual Left scan byte and raises a
        # fresh IRQ 1.
        assert emu._schedule_keyboard_irq() is False
        emu.kbd_ctrl._next_output_time = 0.0
        assert emu._schedule_keyboard_irq() is True
        assert emu._check_and_dispatch_irq() is True
        assert emu.kbd.buffer == [(0x4B, 0)]

    def test_irq_dispatch_preserves_handler_updated_flags(self):
        from main import Emulator
        emu = Emulator()
        emu.cpu.cs = 0x1234
        emu.cpu.ip = 0x5678
        emu.cpu.flags = 0x0002
        emu.cpu.if_flag = True

        def fake_handle_interrupt(cpu, vector):
            cpu.cf = True

        emu.bios.handle_interrupt = fake_handle_interrupt
        emu.io.get_pending_irq = lambda: 0
        emu.io.get_irq_vector = lambda irq: 0x08

        assert emu._check_and_dispatch_irq() is True
        assert emu.cpu.cs == 0x1234
        assert emu.cpu.ip == 0x5678
        assert emu.cpu.cf is True
        assert emu.cpu.if_flag is True

    def test_irq_dispatch_honors_dos_replaced_ivt_handler(self):
        from main import Emulator
        emu = Emulator()
        emu.bios.initialize()
        emu.cpu.cs = 0x1111
        emu.cpu.ip = 0x2222
        emu.cpu.sp = 0x9000
        emu.cpu.flags = 0x0202
        emu.cpu.if_flag = True

        emu.mem.write_word(0x08 * 4, 0x3456)
        emu.mem.write_word(0x08 * 4 + 2, 0x789A)
        emu.io.get_pending_irq = lambda: 0
        emu.io.get_irq_vector = lambda irq: 0x08

        assert emu._check_and_dispatch_irq() is True
        assert emu.cpu.cs == 0x789A
        assert emu.cpu.ip == 0x3456
        assert emu.cpu.int_no_return is True
        assert emu.cpu.sp == 0x8FFA

    def test_irq_dispatch_respects_interrupt_shadow(self):
        from main import Emulator
        emu = Emulator()
        emu.cpu.if_flag = True
        emu.cpu._irq_shadow = 1
        emu.io.get_pending_irq = lambda: 0

        assert emu._check_and_dispatch_irq() is False

    def test_bios_interrupt_hook_preserves_handler_updated_flags(self):
        from main import Emulator
        emu = Emulator(enable_hardware=False)
        emu.cpu.cs = 0x1111
        emu.cpu.ip = 0x2222
        emu.cpu.flags = 0x0002
        emu._install_bios_interrupt_hook()

        def fake_handle_interrupt(cpu, vector):
            cpu.cf = True

        emu.bios.handle_interrupt = fake_handle_interrupt
        emu.cpu._do_interrupt(0x13)

        assert emu.cpu.cs == 0x1111
        assert emu.cpu.ip == 0x2222
        assert emu.cpu.cf is True
        assert emu.cpu.if_flag is False

    def test_bios_interrupt_hook_restores_if_from_interrupted_code(self):
        from main import Emulator
        emu = Emulator(enable_hardware=False)
        emu.cpu.flags = 0x0202
        emu._install_bios_interrupt_hook()

        def fake_handle_interrupt(cpu, vector):
            cpu.cf = True

        emu.bios.handle_interrupt = fake_handle_interrupt
        emu.cpu._do_interrupt(0x13)

        assert emu.cpu.cf is True
        assert emu.cpu.if_flag is True

    def test_blocking_keyboard_interrupt_retries_after_device_pump(self):
        from main import Emulator
        emu = Emulator()
        emu.bios.initialize()
        emu.cpu.cs = 0x1234
        emu.cpu.ip = 0x0102  # Unicorn/Python have consumed CD 16 already.
        emu.cpu.sp = 0x9000
        emu.cpu.flags = 0x0202
        emu.cpu.ax = 0x10A5
        emu._install_bios_interrupt_hook()

        emu.cpu._do_interrupt(0x16)

        assert emu.cpu.cs == 0x1234
        assert emu.cpu.ip == 0x0100
        assert emu.cpu.sp == 0x9000
        assert emu.cpu.flags == 0x0202
        assert emu.cpu.ax == 0x10A5
        assert emu.cpu.retry_software_interrupt is True

    def test_blocking_keyboard_interrupt_returns_once_key_is_ready(self):
        from main import Emulator
        emu = Emulator()
        emu.bios.initialize()
        emu.cpu.cs = 0x1234
        emu.cpu.ip = 0x0102
        emu.cpu.sp = 0x9000
        emu.cpu.flags = 0x0202
        emu.cpu.ax = 0x1000
        emu.kbd_ctrl.inject_extended_key(0x50)
        emu._install_bios_interrupt_hook()

        emu.cpu._do_interrupt(0x16)

        assert emu.cpu.cs == 0x1234
        assert emu.cpu.ip == 0x0102
        assert emu.cpu.sp == 0x9000
        assert emu.cpu.flags == 0x0202
        assert emu.cpu.ax == 0x5000
        assert emu.cpu.retry_software_interrupt is False

    def test_blocking_keyboard_retry_enables_irq_then_restores_caller_if(self):
        from main import Emulator
        emu = Emulator()
        emu.bios.initialize()
        emu.cpu.cs = 0x1234
        emu.cpu.ip = 0x0102
        emu.cpu.sp = 0x9000
        emu.cpu.flags = 0x0002  # Caller entered with maskable IRQs disabled.
        emu.cpu.ax = 0x1000
        emu._install_bios_interrupt_hook()

        emu.cpu._do_interrupt(0x16)

        assert emu.cpu.ip == 0x0100
        assert emu.cpu.if_flag is True
        assert emu.cpu._retry_interrupt_state == (
            0x16, 0x1234, 0x0102, 0x0002)

        # Simulate the retried CD 16 instruction advancing to its return IP.
        emu.cpu.ip = 0x0102
        emu.kbd_ctrl.inject_extended_key(0x50)
        emu.cpu._do_interrupt(0x16)

        assert emu.cpu.ip == 0x0102
        assert emu.cpu.sp == 0x9000
        assert emu.cpu.ax == 0x5000
        assert emu.cpu.if_flag is False
        assert emu.cpu._retry_interrupt_state is None

    def test_unrelated_nested_interrupt_preserves_keyboard_retry(self):
        from main import Emulator
        emu = Emulator()
        emu.bios.initialize()
        emu.cpu.cs = 0x1234
        emu.cpu.ip = 0x0102
        emu.cpu.sp = 0x9000
        emu.cpu.flags = 0x0002
        emu.cpu.ax = 0x1000
        emu._install_bios_interrupt_hook()
        emu.cpu._do_interrupt(0x16)
        retry_state = emu.cpu._retry_interrupt_state

        # Model an IRQ handler making an unrelated nonblocking BIOS call.
        emu.cpu.cs = 0x2000
        emu.cpu.ip = 0x3002
        emu.cpu.ax = 0x0200
        emu.cpu._do_interrupt(0x16)

        assert emu.cpu.cs == 0x2000
        assert emu.cpu.ip == 0x3002
        assert emu.cpu._retry_interrupt_state == retry_state
        assert emu.cpu.retry_software_interrupt is True

        emu.cpu.cs = 0x1234
        emu.cpu.ip = 0x0102
        emu.cpu.ax = 0x1000
        emu.kbd_ctrl.inject_extended_key(0x50)
        emu.cpu._do_interrupt(0x16)

        assert emu.cpu.ax == 0x5000
        assert emu.cpu.if_flag is False
        assert emu.cpu._retry_interrupt_state is None

    def test_blocking_read_waits_for_separate_e0_scan_irq(self):
        from main import Emulator
        emu = Emulator()
        emu.bios.initialize()
        emu.pic.initialize()
        emu.cpu.cs = 0x1234
        emu.cpu.ip = 0x0102
        emu.cpu.sp = 0x9000
        emu.cpu.flags = 0x0202
        emu.cpu.ax = 0x1000
        emu._install_bios_interrupt_hook()
        emu.kbd_ctrl.inject_scan_code(0xE0)
        emu.kbd_ctrl.inject_scan_code(0x50)

        assert emu._schedule_keyboard_irq() is True
        assert emu._check_and_dispatch_irq() is True
        assert emu.kbd.buffer == []

        # Model a DOS program owning IRQ1: its physical bytes cannot be
        # short-circuited through the host-only direct-drain path.
        emu.mem.write_word(0x09 * 4, 0x5678)
        emu.mem.write_word(0x09 * 4 + 2, 0x1234)

        # No phantom AX=0000 escapes while only the prefix has arrived.
        emu.cpu._do_interrupt(0x16)
        assert emu.cpu.ax == 0x1000
        assert emu.cpu.ip == 0x0100
        assert emu.cpu.retry_software_interrupt is True

        stub_cs, stub_ip = emu.bios.ivt_stubs[0x09]
        emu.mem.write_word(0x09 * 4, stub_ip)
        emu.mem.write_word(0x09 * 4 + 2, stub_cs)
        emu.kbd_ctrl._next_output_time = 0.0
        assert emu._schedule_keyboard_irq() is True
        assert emu._check_and_dispatch_irq() is True
        assert emu.kbd.buffer == [(0x50, 0)]

        emu.cpu.ip = 0x0102
        emu.cpu._do_interrupt(0x16)
        assert emu.cpu.ax == 0x5000
        assert emu.cpu.ip == 0x0102
        assert emu.cpu.retry_software_interrupt is False

    def test_chained_bios_stub_propagates_zf_empty(self):
        from main import Emulator
        emu = Emulator()
        emu.bios.initialize()
        stub_cs, stub_off = emu.bios.ivt_stubs[0x16]
        # Guest: PUSHF then CALL FAR [INT 16 vec], landing on the BIOS stub.
        # The stub is `INT 16; IRET`; the CPU sits at stub_off+2 (operand read).
        emu.cpu.cs = stub_cs
        emu.cpu.ip = stub_off + 2
        emu.cpu.ss = 0x2000
        emu.cpu.sp = 0x0100
        emu.cpu.flags = 0x0002  # guest PUSHF value
        base = 0x20000 + 0x0100
        # Outer CALL frame: [call-IP][call-CS][guest-FLAGS]
        emu.mem.write_word(base, 0x0400)             # guest return IP
        emu.mem.write_word(base + 2, 0x3000)         # guest return CS
        emu.mem.write_word(base + 4, emu.cpu.flags)  # guest PUSHF word
        emu.cpu.ax = 0x1100  # AH=11h: check key (empty buffer)
        emu._install_bios_interrupt_hook()

        emu.cpu._do_interrupt(0x16)

        # "No key" -> ZF=1 must survive the stub's outer IRET.
        outer_flags = emu.mem.read_word(base + 4)
        assert outer_flags & 0x40
        # Outer frame control flags (IF bit 0x200) are preserved untouched.
        assert outer_flags & 0x0200 == 0x0002 & 0x0200
        assert emu.cpu.zf is True
        assert emu.cpu.sp == 0x0100

    def test_chained_bios_stub_propagates_zf_ready(self):
        from main import Emulator
        emu = Emulator()
        emu.bios.initialize()
        stub_cs, stub_off = emu.bios.ivt_stubs[0x16]
        emu.cpu.cs = stub_cs
        emu.cpu.ip = stub_off + 2
        emu.cpu.ss = 0x2000
        emu.cpu.sp = 0x0100
        emu.cpu.flags = 0x0242  # guest PUSHF word: ZF set, IF set
        base = 0x20000 + 0x0100
        emu.mem.write_word(base, 0x0400)
        emu.mem.write_word(base + 2, 0x3000)
        emu.mem.write_word(base + 4, emu.cpu.flags)
        emu.kbd.buffer.append(0x41)  # 'A' ready
        emu.cpu.ax = 0x1100  # AH=11h: check key (available)
        emu._install_bios_interrupt_hook()

        emu.cpu._do_interrupt(0x16)

        # Key available -> ZF=0 written into the stub's outer FLAGS word.
        outer_flags = emu.mem.read_word(base + 4)
        assert not outer_flags & 0x40
        # The outer frame's IF (0x200) survives the merge.
        assert outer_flags & 0x0200
        assert emu.cpu.zf is False
        assert emu.cpu.sp == 0x0100

    def test_direct_interrupt_leaves_outer_frame_word_untouched(self):
        from main import Emulator
        emu = Emulator()
        emu.bios.initialize()
        # A direct software interrupt (not chained via the stub) must not
        # rewrite whatever word happens to sit at SS:SP+4.
        emu.cpu.cs = 0x1234
        emu.cpu.ip = 0x5678
        emu.cpu.ss = 0x2000
        emu.cpu.sp = 0x0100
        emu.cpu.flags = 0x0002
        base = 0x20000 + 0x0100
        emu.mem.write_word(base + 4, 0x4242)  # unrelated sentinel
        emu.cpu.ax = 0x1100  # AH=11h: check key (empty)
        emu._install_bios_interrupt_hook()

        emu.cpu._do_interrupt(0x16)

        # cpu.flags reflects the handler result (ZF=1)...
        assert emu.cpu.zf is True
        # ...but the outer word at SS:SP+4 is left exactly as it was.
        assert emu.mem.read_word(base + 4) == 0x4242
        assert emu.cpu.sp == 0x0100
