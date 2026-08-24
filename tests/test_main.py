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
