"""PIC initialization and mask readback used by DOS protected-mode hosts."""

import pytest

from hardware import PIC


def test_interleaved_pc_pic_initialization_remaps_vectors_not_masks():
    pic = PIC()
    pic.write_master(0x20, 0x11)
    pic.write_slave(0xA0, 0x11)
    pic.write_master(0x21, 0x50)
    pic.write_slave(0xA1, 0x58)
    pic.write_master(0x21, 4)
    pic.write_slave(0xA1, 2)
    pic.write_master(0x21, 1)
    pic.write_slave(0xA1, 1)
    pic.write_master(0x21, 0xFC)
    pic.write_slave(0xA1, 0xFF)
    assert (pic.master_base, pic.slave_base) == (0x50, 0x58)
    assert (pic.mask, pic.slave_mask) == (0xFC, 0xFF)
    pic.raise_irq(0)
    assert pic.get_vector(pic.get_highest_irq()) == 0x50


@pytest.mark.parametrize('icw1,tail', [(0x10, [4]), (0x12, []), (0x13, [1])])
def test_optional_icw3_icw4_do_not_consume_mask(icw1, tail):
    pic = PIC()
    pic.write_master(0x20, icw1)
    pic.write_master(0x21, 0x53)
    for value in tail:
        pic.write_master(0x21, value)
    pic.write_master(0x21, 0xFE)
    assert pic.master_base == 0x50
    assert pic.mask == 0xFE


def test_initialize_finishes_sequence_and_preserves_board_defaults():
    pic = PIC()
    pic.mask, pic.slave_mask = 0xFC, 0xFF
    pic.initialize()
    assert (pic.master_base, pic.slave_base) == (8, 0x70)
    assert (pic.mask, pic.slave_mask) == (0xFC, 0xFF)
    pic.write_master(0x21, 0xFD)
    assert pic.mask == 0xFD


def test_guest_can_read_back_saved_pic_masks():
    from main import Emulator
    emu = Emulator()
    emu.io.outb(0x21, 0xFC)
    emu.io.outb(0xA1, 0xEF)
    assert emu.io.inb(0x21) == 0xFC
    assert emu.io.inb(0xA1) == 0xEF
